from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.db.models import Q, Count, Max, Prefetch, Subquery
from django.conf import settings
from django.views.decorators.cache import cache_page
import logging
import re
import json
from collections import Counter
from pathlib import Path
from django.utils import timezone
from .models import Radio, Brand, RadioManual, RadioFirmware, Manufacturer, FCCSyncState, IgnoredGrantee, RadioFCCTestReport, RadioOETDocument, RadioImage, delete_brand_and_related
from .forms import RadioForm, RadioSearchForm, BrandForm, ManufacturerForm, RadioImageFormSet
from .image_utils import ingest_radio_image
from .fcc_utils import fetch_and_sync_fcc_id
from .fcc_id_utils import normalize_fcc_id_for_lookup, split_fcc_id
from .nodal_graph import build_nodal_graph_data

logger = logging.getLogger(__name__)


def _normalize_brand_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _normalize_fcc_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _normalize_model_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _build_brand_display_alias_map():
    alias_map = {}
    for brand in Brand.objects.exclude(alias__isnull=True).exclude(alias__exact='').only('name', 'full_name', 'alias'):
        alias = (brand.alias or '').strip()
        if not alias:
            continue
        for candidate in (brand.name, brand.full_name):
            key = _normalize_brand_key(candidate)
            if key and key not in alias_map:
                alias_map[key] = alias
    return alias_map


def _preferred_brand_display_name(radio, alias_map):
    # Prefer the radio's own brand field (the market/retail brand) over manufacturer alias
    # (which is the OEM). For white-label radios the brand IS the correct display name.
    brand_value = (getattr(radio, 'brand', '') or '').strip()
    if brand_value:
        brand_key = _normalize_brand_key(brand_value)
        if brand_key and brand_key in alias_map:
            return alias_map[brand_key]
        return brand_value

    # Fall back to manufacturer alias only when brand is absent
    manufacturer = getattr(radio, 'manufacturer', None)
    if manufacturer:
        manufacturer_alias = (manufacturer.alias or '').strip()
        if manufacturer_alias:
            return manufacturer_alias

    return brand_value


def _normalized_query_match_ids(query, radios_qs=None):
    query_key = _normalize_model_key(query)
    if not query_key:
        return []

    source_qs = radios_qs if radios_qs is not None else Radio.objects.all()
    return [
        radio['id']
        for radio in source_qs.values('id', 'brand', 'model', 'fcc_id', 'rebadges_clones', 'white_label_vendors')
        if (
            query_key in _normalize_model_key(radio.get('brand'))
            or query_key in _normalize_model_key(radio.get('model'))
            or query_key in _normalize_model_key(radio.get('fcc_id'))
            or query_key in _normalize_model_key(radio.get('rebadges_clones'))
            or query_key in _normalize_model_key(radio.get('white_label_vendors'))
        )
    ]


def _actor_label(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        return str(user)
    return 'anonymous'


def sync_fcc_view(request):
    """View to handle fetching and syncing an FCC ID from the dashboard."""
    if request.method == 'POST':
        fcc_id = request.POST.get('fcc_id', '').strip()
        logger.info("User action sync_fcc submit actor=%s fcc_id=%s", _actor_label(request), fcc_id)
        if fcc_id:
            try:
                added, updated, _processing_msgs = fetch_and_sync_fcc_id(fcc_id)
                logger.info("User action sync_fcc result actor=%s fcc_id=%s added=%s updated=%s", _actor_label(request), fcc_id, added, updated)
                ignore_message = next((msg for msg in _processing_msgs if 'ignore list' in msg.lower()), '')
                if ignore_message:
                    messages.warning(request, ignore_message)
                elif added > 0 or updated > 0:
                    messages.success(request, f"Success! Added {added} and updated {updated} records for FCC ID '{fcc_id}'.")
                else:
                    messages.warning(request, f"No new records or updates found for '{fcc_id}'.")
            except Exception as e:
                logger.exception("User action sync_fcc error actor=%s fcc_id=%s", _actor_label(request), fcc_id)
                messages.error(request, f"Error processing FCC ID: {e}")
        else:
            messages.error(request, "Please enter a valid FCC ID.")
            
    return redirect('dashboard')


def sync_radio_fcc_view(request, pk):
    """View to handle fetching and syncing FCC data for a specific radio."""
    radio = get_object_or_404(Radio, pk=pk)
    
    if request.method == 'POST':
        if not radio.fcc_id:
            messages.error(request, "This radio does not have an FCC ID assigned.")
            return redirect('radio_detail', pk=pk)
        
        force_reload = request.POST.get('force_reload') == '1'
        logger.info("User action sync_radio_fcc submit actor=%s radio_pk=%s fcc_id=%s force_reload=%s", _actor_label(request), pk, radio.fcc_id, force_reload)
        try:
            added, updated, _processing_msgs = fetch_and_sync_fcc_id(radio.fcc_id, force_reload=force_reload)
            logger.info("User action sync_radio_fcc result actor=%s radio_pk=%s fcc_id=%s added=%s updated=%s force_reload=%s", 
                       _actor_label(request), pk, radio.fcc_id, added, updated, force_reload)
            
            ignore_message = next((msg for msg in _processing_msgs if 'ignore list' in msg.lower()), '')
            if ignore_message:
                messages.warning(request, ignore_message)
            elif added > 0 or updated > 0:
                messages.success(request, f"Success! Updated FCC data for '{radio.fcc_id}'. Added {added} and updated {updated} records.")
            else:
                messages.info(request, f"FCC sync completed for '{radio.fcc_id}'. No new records or updates found.")
        except Exception as e:
            logger.exception("User action sync_radio_fcc error actor=%s radio_pk=%s fcc_id=%s", _actor_label(request), pk, radio.fcc_id)
            messages.error(request, f"Error syncing FCC data: {e}")
    
    return redirect('radio_detail', pk=pk)


def sync_all_grantees_view(request):
    """View to update all existing grantees by iterating through Brand records.

    By default, only queries the FCC API for grants issued since the last
    successful run (stored in FCCSyncState).  On the very first run, no date
    filter is applied and the full history is fetched.
    """
    if request.method == 'POST':
        logger.info("User action sync_all_grantees submit actor=%s", _actor_label(request))
        try:
            sync_state = FCCSyncState.get_instance()
            start_date = sync_state.last_grantee_sync_at  # None on first run → no filter
            end_date = timezone.now()

            if start_date:
                logger.info(
                    "User action sync_all_grantees date_filter actor=%s start_date=%s end_date=%s",
                    _actor_label(request), start_date.isoformat(), end_date.isoformat(),
                )
            else:
                logger.info(
                    "User action sync_all_grantees date_filter actor=%s start_date=none (full history)",
                    _actor_label(request),
                )

            ignored_codes = IgnoredGrantee.ignored_codes()
            grantees = Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code='')
            if ignored_codes:
                grantees = grantees.exclude(grantee_code__in=ignored_codes)
            total_added = 0
            total_updated = 0

            for brand in grantees:
                added, updated, _ = fetch_and_sync_fcc_id(
                    brand.grantee_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                total_added += added
                total_updated += updated

            # Persist the end_date as the new baseline for future incremental syncs.
            sync_state.last_grantee_sync_at = end_date
            sync_state.save(update_fields=['last_grantee_sync_at'])

            logger.info(
                "User action sync_all_grantees result actor=%s added=%s updated=%s last_sync=%s",
                _actor_label(request), total_added, total_updated, end_date.isoformat(),
            )
            if start_date:
                messages.success(
                    request,
                    f"Updated grantees for grants since {start_date.strftime('%b %d, %Y')}. "
                    f"Added {total_added}, updated {total_updated} records.",
                )
            else:
                messages.success(
                    request,
                    f"Full history sync complete. Added {total_added}, updated {total_updated} records.",
                )
        except Exception as e:
            logger.exception("User action sync_all_grantees error actor=%s", _actor_label(request))
            messages.error(request, f"Error processing grantees: {e}")

    return redirect('dashboard')


class RadioListView(ListView):
    """View for listing all radios with search and filter"""
    model = Radio
    template_name = 'radios/radio_list.html'
    context_object_name = 'radios'
    paginate_by = 50
    
    # Define allowed sort fields
    SORT_FIELDS = {
        'brand': 'brand',
        'model': 'model',
        'grant_date': 'grant_date',
        'freq_bands_tx': 'freq_bands_tx',
        'power_watts': 'power_watts',
        'cost_approx': 'cost_approx',
        'aprs': 'aprs',
        'updated_at': 'updated_at',
    }
    
    def get_queryset(self):
        manual_prefetch = Prefetch(
            'manuals',
            queryset=RadioManual.objects.exclude(manual_pdf='').only('id', 'radio_id', 'manual_pdf', 'updated_at').order_by('-updated_at'),
            to_attr='available_manuals',
        )
        firmware_prefetch = Prefetch(
            'firmware_versions',
            queryset=RadioFirmware.objects.only('id', 'radio_id', 'label', 'version', 'download_url').order_by('label'),
            to_attr='prefetched_firmware',
        )
        queryset = Radio.objects.all().select_related('manufacturer').prefetch_related(manual_prefetch, firmware_prefetch)
        
        # Search functionality
        query = self.request.GET.get('query')
        if query:
            logger.info("User action radio_search actor=%s query=%s", _actor_label(self.request), query)
            normalized_match_ids = _normalized_query_match_ids(query, radios_qs=queryset)
            queryset = queryset.filter(
                Q(brand__icontains=query) |
                Q(model__icontains=query) |
                Q(fcc_id__icontains=query) |
                Q(rebadges_clones__icontains=query) |
                Q(white_label_vendors__icontains=query) |
                Q(id__in=normalized_match_ids)
            )
        
        # Brand filter
        brand = self.request.GET.get('brand')
        if brand:
            logger.info("User action radio_filter_brand actor=%s brand=%s", _actor_label(self.request), brand)
            queryset = queryset.filter(brand__icontains=brand)
        
        # Sorting
        sort = self.request.GET.get('sort', 'brand')
        order = self.request.GET.get('order', 'asc')
        logger.info("User action radio_list_sort actor=%s sort=%s order=%s", _actor_label(self.request), sort, order)
        
        if sort in self.SORT_FIELDS:
            sort_field = self.SORT_FIELDS[sort]
            if order == 'desc':
                sort_field = f'-{sort_field}'
            queryset = queryset.order_by(sort_field)
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        alias_map = _build_brand_display_alias_map()
        for radio in context.get('radios', []):
            radio.display_brand = _preferred_brand_display_name(radio, alias_map)

        context['search_form'] = RadioSearchForm(self.request.GET)
        context['total_count'] = Radio.objects.count()
        context['brands'] = Radio.objects.values('brand').annotate(
            count=Count('id')
        ).order_by('brand')
        # Pass current sort parameters to template
        context['current_sort'] = self.request.GET.get('sort', 'brand')
        context['current_order'] = self.request.GET.get('order', 'asc')
        
        # Build query string for pagination
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']
        context['query_string'] = f"&{query_params.urlencode()}" if query_params else ""
        
        return context



class RadioDetailView(DetailView):
    """View for displaying a single radio's details"""
    model = Radio
    template_name = 'radios/radio_detail.html'
    context_object_name = 'radio'

    def get(self, request, *args, **kwargs):
        try:
            return super().get(request, *args, **kwargs)
        except Http404:
            messages.error(request, f'Radio #{kwargs.get("pk")} no longer exists — it may have been deleted.')
            return redirect('radio_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        radio = context.get('radio')
        fcc_id = (getattr(radio, 'fcc_id', None) or '').strip().upper()
        brand_match = Brand.objects.filter(name__iexact=radio.brand).only('id', 'grantee_code').first()
        preferred_grantee_code = (brand_match.grantee_code or '').strip().upper() if brand_match else ''
        fcc_lookup_id = normalize_fcc_id_for_lookup(fcc_id, preferred_grantee_code=preferred_grantee_code)

        context['fcc_lookup_id'] = fcc_lookup_id
        context['brand_pk'] = brand_match.pk if brand_match else None

        # Use stored OET page URL; for legacy records without one, derive it on the fly
        # from the application_id embedded in any RadioOETDocument document_url.
        oet_url = radio.oet_page_url or ''
        if not oet_url and fcc_id:
            _app_id_re = re.compile(r'application_id=([A-Za-z0-9]+)', re.IGNORECASE)
            doc_with_url = RadioOETDocument.objects.filter(
                radio=radio, fcc_id__iexact=fcc_id
            ).exclude(document_url='').first()
            if doc_with_url:
                m = _app_id_re.search(doc_with_url.document_url)
                if m:
                    oet_url = (
                        f"https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
                        f"?mode=Exhibits&RequestTimeout=500&application_id={m.group(1)}"
                    )
                    # Persist so future page loads don't need to re-derive it
                    Radio.objects.filter(pk=radio.pk).update(oet_page_url=oet_url)
        context['oet_url'] = oet_url

        # Gather lineage and relationship data
        manufacturer = radio.manufacturer
        primary_models = []
        white_label_models = []
        
        if fcc_id:
            # Group by FCC ID
            related_radios = Radio.objects.filter(fcc_id__iexact=fcc_id)
            primary_models = related_radios.filter(is_a_whitelabel=False)
            white_label_models = related_radios.filter(is_a_whitelabel=True)
        
        context['manufacturer'] = manufacturer
        context['primary_models'] = primary_models
        context['white_label_models'] = white_label_models
        
        return context


class RadioCreateView(CreateView):
    """View for creating a new radio entry"""
    model = Radio
    form_class = RadioForm
    template_name = 'radios/radio_form.html'

    def get_success_url(self):
        return reverse_lazy('radio_edit', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['brand_name_to_pk'] = {b.name: b.pk for b in Brand.objects.only('id', 'name').all()}
        return context

    def post(self, request, *args, **kwargs):
        self.object = None
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form)
        return self.render_to_response(self.get_context_data(form=form))

    def form_valid(self, form):
        logger.info("User action radio_create submit actor=%s brand=%s model=%s", _actor_label(self.request), form.instance.brand, form.instance.model)
        messages.success(self.request, f'Radio {form.instance} has been created successfully!')
        return super().form_valid(form)


class RadioUpdateView(UpdateView):
    """View for updating an existing radio entry"""
    model = Radio
    form_class = RadioForm
    template_name = 'radios/radio_form.html'

    def get_success_url(self):
        return reverse_lazy('radio_edit', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['brand_name_to_pk'] = {b.name: b.pk for b in Brand.objects.only('id', 'name').all()}
        if self.request.method == 'POST':
            context['image_formset'] = RadioImageFormSet(
                self.request.POST,
                self.request.FILES,
                instance=self.object,
                prefix='images',
            )
        else:
            context['image_formset'] = RadioImageFormSet(
                instance=self.object,
                prefix='images',
            )
        return context

    def post(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except Http404:
            messages.error(request, f'Radio #{kwargs.get("pk")} no longer exists — it may have been deleted.')
            return redirect('radio_list')
        form = self.get_form()
        image_formset = RadioImageFormSet(
            request.POST,
            request.FILES,
            instance=self.object,
            prefix='images',
        )
        if form.is_valid() and image_formset.is_valid():
            return self.form_valid(form, image_formset)
        context = self.get_context_data(form=form)
        context['image_formset'] = image_formset
        return self.render_to_response(context)

    def form_valid(self, form, image_formset=None):
        logger.info("User action radio_update submit actor=%s radio_id=%s brand=%s model=%s", _actor_label(self.request), form.instance.pk, form.instance.brand, form.instance.model)
        self.object = form.save()

        if image_formset is not None:
            # Save formset rows that have a file OR a URL
            for img_form in image_formset:
                if img_form.cleaned_data.get('DELETE') and img_form.instance.pk:
                    # Handled by formset.save() below
                    continue
                has_file = bool(img_form.cleaned_data.get('image_file'))
                has_url = bool((img_form.cleaned_data.get('image_url') or '').strip())
                if not has_file and not has_url:
                    continue
                if has_url and not has_file:
                    # URL import — do NOT save via formset; use ingest_radio_image instead
                    ingest_radio_image(
                        img_form.cleaned_data['image_url'],
                        self.object,
                        caption=img_form.cleaned_data.get('caption', ''),
                    )
                # file uploads are handled by image_formset.save() below
            image_formset.save()

        messages.success(self.request, f'Radio {form.instance} has been updated successfully!')
        return redirect(self.request.path)


def radio_image_delete(request, radio_pk, pk):
    """POST-only view: delete a single RadioImage and its stored file."""
    image = get_object_or_404(RadioImage, pk=pk, radio_id=radio_pk)
    if request.method == 'POST':
        logger.info(
            "User action radio_image_delete actor=%s radio_pk=%s image_pk=%s",
            _actor_label(request), radio_pk, pk,
        )
        image.image_file.delete(save=False)
        image.delete()
        messages.success(request, 'Image deleted.')
    return redirect('radio_edit', pk=radio_pk)


class RadioDeleteView(DeleteView):
    model = Radio
    success_url = reverse_lazy('radio_list')
    
    def delete(self, request, *args, **kwargs):
        radio = self.get_object()
        logger.info("User action radio_delete submit actor=%s radio_id=%s brand=%s model=%s", _actor_label(request), radio.pk, radio.brand, radio.model)
        messages.success(request, f'Radio {radio} has been deleted successfully!')
        return super().delete(request, *args, **kwargs)


class BrandListView(ListView):
    """View for listing all brands"""
    model = Brand
    template_name = 'radios/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 50
    ordering = ['name']
    SORT_FIELDS = {
        'name': 'name',
        'grantee_code': 'grantee_code',
        'country': 'country',
        'parent_brand': 'parent_brand__name',
        'last_modified_date': 'last_modified_date',
    }

    def get_queryset(self):
        queryset = super().get_queryset().select_related('parent_brand')
        query = self.request.GET.get('query')
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query) |
                Q(alias__icontains=query) |
                Q(full_name__icontains=query) |
                Q(grantee_code__icontains=query) |
                Q(white_label_vendors__icontains=query)
            )

        sort = self.request.GET.get('sort', 'name')
        order = self.request.GET.get('order', 'asc')
        if sort in self.SORT_FIELDS:
            sort_field = self.SORT_FIELDS[sort]
            if order == 'desc':
                sort_field = f'-{sort_field}'
            queryset = queryset.order_by(sort_field)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['total_count'] = Brand.objects.count()
        context['filtered_count'] = context['paginator'].count if context.get('paginator') else 0
        context['current_sort'] = self.request.GET.get('sort', 'name')
        context['current_order'] = self.request.GET.get('order', 'asc')
        # Build query string for pagination
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']
        context['query_string'] = f"&{query_params.urlencode()}" if query_params else ""
        return context

class BrandCreateView(CreateView):
    """View for creating a new brand entry"""
    model = Brand
    form_class = BrandForm
    template_name = 'radios/brand_form.html'

    def get_success_url(self):
        return reverse_lazy('brand_edit', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brands_qs = Brand.objects.only('id', 'name').order_by('name')
        context['all_brands'] = brands_qs
        context['brand_name_to_pk'] = {b.name: b.pk for b in brands_qs}
        return context

    def form_valid(self, form):
        logger.info("User action brand_create submit actor=%s brand=%s", _actor_label(self.request), form.instance.name)
        messages.success(self.request, f'Brand {form.instance} has been created successfully!')
        return super().form_valid(form)


class BrandUpdateView(UpdateView):
    """View for updating an existing brand entry"""
    model = Brand
    form_class = BrandForm
    template_name = 'radios/brand_form.html'

    def get_success_url(self):
        return reverse_lazy('brand_edit', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brands_qs = Brand.objects.only('id', 'name').order_by('name')
        context['all_brands'] = brands_qs
        context['brand_name_to_pk'] = {b.name: b.pk for b in brands_qs}
        brand = self.object
        context['brand_radios'] = (
            Radio.objects.filter(brand__iexact=brand.name)
            .only('id', 'brand', 'model')
            .order_by('model')
        )
        return context

    def form_valid(self, form):
        old_name = Brand.objects.values_list('name', flat=True).get(pk=form.instance.pk)
        new_name = form.cleaned_data['name']
        logger.info("User action brand_update submit actor=%s brand_id=%s brand=%s", _actor_label(self.request), form.instance.pk, new_name)
        response = super().form_valid(form)
        if old_name != new_name:
            updated = Radio.objects.filter(brand__iexact=old_name).update(brand=new_name)
            logger.info(
                "Brand rename cascade old=%s new=%s radios_updated=%s",
                old_name, new_name, updated,
            )
            if updated:
                messages.info(
                    self.request,
                    f"Updated {updated} radio record(s) from brand \"{old_name}\" to \"{new_name}\".",
                )
        messages.success(self.request, f'Brand {form.instance} has been saved successfully!')
        return response


class BrandDeleteView(DeleteView):
    """View for deleting a brand and associated records."""
    model = Brand
    template_name = 'radios/brand_confirm_delete.html'
    success_url = reverse_lazy('brand_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brand = context['object']
        associated_radios = Radio.objects.filter(brand__iexact=brand.name).distinct()
        context['associated_radio_count'] = associated_radios.count()
        context['associated_manual_count'] = RadioManual.objects.filter(radio__in=associated_radios).count()
        context['associated_test_report_count'] = RadioFCCTestReport.objects.filter(radio__in=associated_radios).count()
        context['associated_oet_document_count'] = RadioOETDocument.objects.filter(radio__in=associated_radios).count()
        context['associated_firmware_count'] = RadioFirmware.objects.filter(radio__in=associated_radios).count()
        linked_manufacturer_ids = Manufacturer.objects.filter(brands=brand).values_list('id', flat=True)
        context['associated_manufacturer_count'] = (
            Manufacturer.objects.annotate(brand_count=Count('brands', distinct=True))
            .filter(id__in=linked_manufacturer_ids)
            .annotate(brand_count=Count('brands', distinct=True))
            .filter(brand_count=1)
            .count()
        )
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        brand = self.object

        if request.POST.get('confirm_delete') != 'yes':
            messages.error(request, 'Please confirm that you understand this deletion is permanent.')
            logger.info(
                "User action brand_delete blocked_missing_confirmation actor=%s brand_id=%s brand=%s",
                _actor_label(request),
                brand.pk,
                brand.name,
            )
            context = self.get_context_data(object=brand)
            return render(request, self.template_name, context)

        associated_radios = Radio.objects.filter(brand__iexact=brand.name).distinct()
        radio_count = associated_radios.count()
        manual_count = RadioManual.objects.filter(radio__in=associated_radios).count()

        logger.warning(
            "User action brand_delete submit actor=%s brand_id=%s brand=%s associated_radios=%s associated_manuals=%s",
            _actor_label(request),
            brand.pk,
            brand.name,
            radio_count,
            manual_count,
        )

        brand_name = brand.name
        delete_summary = delete_brand_and_related(brand)

        messages.success(
            request,
            f'Deleted brand {brand_name} and associated records '
            f'({delete_summary["radios_deleted"]} radios, '
            f'{delete_summary["manuals_deleted"]} manuals, '
            f'{delete_summary["test_reports_deleted"]} test reports, '
            f'{delete_summary["oet_documents_deleted"]} OET documents, '
            f'{delete_summary["firmware_deleted"]} firmware entries, '
            f'{delete_summary["manufacturers_deleted"]} manufacturers).',
        )
        return redirect(self.success_url)


def brand_merge_view(request, pk):
    """Merge a source brand into a chosen target brand.

    All radio records, manufacturer FK references, child brand parent_brand
    pointers, and Manufacturer M2M links are re-pointed from source → target,
    then the source brand is deleted.
    """
    from django.db import transaction
    from .models import Manufacturer

    source = get_object_or_404(Brand, pk=pk)

    if request.method == 'POST':
        target_pk = request.POST.get('target_brand')
        if not target_pk:
            messages.error(request, 'Please select a target brand to merge into.')
            return redirect('brand_merge', pk=pk)

        try:
            target = Brand.objects.exclude(pk=pk).get(pk=target_pk)
        except Brand.DoesNotExist:
            messages.error(request, 'Invalid target brand selected.')
            return redirect('brand_merge', pk=pk)

        if request.POST.get('confirm') != 'yes':
            messages.error(request, 'Please check the confirmation box before merging.')
            return redirect('brand_merge', pk=pk)

        logger.warning(
            "User action brand_merge submit actor=%s source_id=%s source=%s target_id=%s target=%s",
            _actor_label(request), source.pk, source.name, target.pk, target.name,
        )

        with transaction.atomic():
            # 1. Update Radio.brand string
            r1 = Radio.objects.filter(brand__iexact=source.name).update(brand=target.name)
            # 2. Update child Brand.parent_brand FK
            r2 = Brand.objects.filter(parent_brand=source).update(parent_brand=target)
            # 3. Manufacturer M2M (Manufacturer.brands)
            r3 = 0
            for mfr in Manufacturer.objects.filter(brands=source):
                mfr.brands.add(target)
                mfr.brands.remove(source)
                r3 += 1
            # 4. Delete source
            source_name = source.name
            source.delete()

        logger.info(
            "Brand merge complete source=%s target=%s radios_brand=%s child_brands=%s manufacturers=%s",
            source_name, target.name, r1, r2, r3,
        )
        messages.success(
            request,
            f'Merged "{source_name}" into "{target.name}": '
            f'{r1} radio brand(s), {r2} child brand(s) updated.',
        )
        return redirect('brand_edit', pk=target.pk)

    # GET — show merge form
    radio_count = Radio.objects.filter(brand__iexact=source.name).count()
    child_brands = Brand.objects.filter(parent_brand=source)
    other_brands = Brand.objects.exclude(pk=pk).order_by('name')

    return render(request, 'radios/brand_merge.html', {
        'source': source,
        'radio_count': radio_count,
        'child_brands': child_brands,
        'other_brands': other_brands,
    })


# ---------------------------------------------------------------------------
# Manufacturer views
# ---------------------------------------------------------------------------

class ManufacturerListView(ListView):
    """List all manufacturers with optional search."""
    model = Manufacturer
    template_name = 'radios/manufacturer_list.html'
    context_object_name = 'manufacturers'
    paginate_by = 50
    ordering = ['full_name']

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related('brands')
        query = self.request.GET.get('query', '').strip()
        if query:
            qs = qs.filter(
                Q(full_name__icontains=query) |
                Q(alias__icontains=query) |
                Q(country__icontains=query) |
                Q(address__icontains=query) |
                Q(brands__name__icontains=query)
            ).distinct()
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['total_count'] = Manufacturer.objects.count()
        context['filtered_count'] = context['paginator'].count if context.get('paginator') else 0
        query_params = self.request.GET.copy()
        query_params.pop('page', None)
        context['query_string'] = f"&{query_params.urlencode()}" if query_params else ""
        return context


class ManufacturerCreateView(CreateView):
    """Create a new manufacturer record."""
    model = Manufacturer
    form_class = ManufacturerForm
    template_name = 'radios/manufacturer_form.html'

    def get_success_url(self):
        return reverse_lazy('manufacturer_edit', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        logger.info("User action manufacturer_create actor=%s name=%s", _actor_label(self.request), form.instance.full_name)
        messages.success(self.request, f'Manufacturer "{form.instance.full_name}" created.')
        return super().form_valid(form)


class ManufacturerUpdateView(UpdateView):
    """Edit an existing manufacturer record."""
    model = Manufacturer
    form_class = ManufacturerForm
    template_name = 'radios/manufacturer_form.html'

    def get_success_url(self):
        return reverse_lazy('manufacturer_edit', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['linked_brands'] = self.object.brands.all().order_by('name')
        return context

    def form_valid(self, form):
        logger.info("User action manufacturer_update actor=%s pk=%s name=%s", _actor_label(self.request), self.object.pk, form.instance.full_name)
        messages.success(self.request, f'Manufacturer "{form.instance.full_name}" updated.')
        return super().form_valid(form)


class ManufacturerDeleteView(DeleteView):
    """Delete a manufacturer record (does not delete linked brands)."""
    model = Manufacturer
    template_name = 'radios/manufacturer_confirm_delete.html'
    success_url = reverse_lazy('manufacturer_list')

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        logger.warning("User action manufacturer_delete actor=%s pk=%s name=%s", _actor_label(request), self.object.pk, self.object.full_name)
        messages.success(request, f'Manufacturer "{self.object.full_name}" deleted.')
        self.object.delete()
        return redirect(self.success_url)


def manufacturer_map_view(request):
    """Page view for the interactive manufacturer geo-map."""
    logger.info("User action manufacturer_map_view actor=%s", _actor_label(request))
    countries = (
        Manufacturer.objects
        .exclude(country='')
        .exclude(latitude__isnull=True)
        .values_list('country', flat=True)
        .distinct()
        .order_by('country')
    )
    total_geocoded = Manufacturer.objects.exclude(latitude__isnull=True).count()
    total_ungeocode = Manufacturer.objects.filter(latitude__isnull=True).exclude(address='').count()
    return render(request, 'radios/manufacturer_geomap.html', {
        'countries': list(countries),
        'total_geocoded': total_geocoded,
        'total_ungeocoded': total_ungeocode,
    })


def manufacturer_map_data_view(request):
    """
    JSON API — returns geocoded manufacturers for the map.

    Optional GET params:
      country    — exact country match (case-insensitive)
      q          — text search across name, alias, address
      sw_lat, sw_lon, ne_lat, ne_lon — bounding box pre-filter
    """
    qs = Manufacturer.objects.exclude(latitude__isnull=True).exclude(longitude__isnull=True)

    country = request.GET.get('country', '').strip()
    if country:
        qs = qs.filter(country__iexact=country)

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(alias__icontains=q) |
            Q(address__icontains=q)
        )

    try:
        sw_lat = float(request.GET['sw_lat'])
        sw_lon = float(request.GET['sw_lon'])
        ne_lat = float(request.GET['ne_lat'])
        ne_lon = float(request.GET['ne_lon'])
        qs = qs.filter(
            latitude__gte=sw_lat, latitude__lte=ne_lat,
            longitude__gte=sw_lon, longitude__lte=ne_lon,
        )
    except (KeyError, ValueError):
        pass

    qs = qs.prefetch_related('brands').order_by('full_name')

    results = []
    for mfr in qs:
        display_name = mfr.alias or mfr.full_name
        brand_names = [b.alias or b.name for b in mfr.brands.all()]
        results.append({
            'id': mfr.pk,
            'display_name': display_name,
            'full_name': mfr.full_name,
            'lat': mfr.latitude,
            'lon': mfr.longitude,
            'address': mfr.address,
            'country': mfr.country,
            'geocode_precision': mfr.geocode_precision,
            'website': mfr.website,
            'brand_names': brand_names,
            'edit_url': reverse_lazy('manufacturer_edit', kwargs={'pk': mfr.pk}),
        })

    return JsonResponse({'manufacturers': results})


@cache_page(60 * 5)  # Cache for 5 minutes
def dashboard_view(request):
    """Dashboard view with statistics"""
    logger.info("User action dashboard_view actor=%s", _actor_label(request))

    # Parse adjustable recent_days parameter (default 30, range 7-365)
    recent_days = request.GET.get('recent_days', '30')
    try:
        recent_days = max(7, min(365, int(recent_days)))
    except (ValueError, TypeError):
        recent_days = 30

    from django.utils import timezone as tz_utils
    from datetime import timedelta
    cutoff_date = tz_utils.now().date() - timedelta(days=recent_days)

    raw_brand_rows = list(
        Radio.objects.values('brand').annotate(
            count=Count('id'),
            latest_update=Max('updated_at')
        )
    )

    # Merge equivalent brand names that differ only by punctuation/casing/spacing.
    merged_brands = {}
    for row in raw_brand_rows:
        brand_name = (row.get('brand') or '').strip()
        brand_key = _normalize_brand_key(brand_name) or brand_name.lower()
        if not brand_key:
            continue

        entry = merged_brands.get(brand_key)
        if not entry:
            entry = {
                'brand': brand_name,
                'count': 0,
                'latest_update': row.get('latest_update'),
                'source_brands': set(),
                'brand_key': brand_key,
            }
            merged_brands[brand_key] = entry

        entry['count'] += row.get('count') or 0
        if brand_name:
            entry['source_brands'].add(brand_name)

        row_latest = row.get('latest_update')
        entry_latest = entry.get('latest_update')
        if row_latest and (not entry_latest or row_latest > entry_latest):
            entry['latest_update'] = row_latest
            entry['brand'] = brand_name

    top_brands = sorted(
        merged_brands.values(),
        key=lambda item: ((item.get('latest_update') is not None), item.get('latest_update'), item.get('count') or 0),
        reverse=True,
    )[:10]

    # Attach brand grantee codes for display in the dashboard list.
    grantee_by_brand = {}
    for b in Brand.objects.only('name', 'alias', 'full_name', 'grantee_code'):
        code = (b.grantee_code or '').strip().upper()
        if not code:
            continue
        for candidate in (b.name, b.alias, b.full_name):
            key = _normalize_brand_key(candidate)
            if key and key not in grantee_by_brand:
                grantee_by_brand[key] = code

    for row in top_brands:
        brand_name = row.get('brand') or ''
        row['grantee_code'] = ''
        for source_brand in row.get('source_brands', []):
            match = grantee_by_brand.get(_normalize_brand_key(source_brand), '')
            if match:
                row['grantee_code'] = match
                break

        row['source_brands'] = sorted(row.get('source_brands', []))

    # Fallback: batch a single query for all top_brands still missing a grantee code.
    needs_grantee = [row for row in top_brands if not row['grantee_code']]
    if needs_grantee:
        batch_filter = Q()
        for row in needs_grantee:
            for sb in row.get('source_brands', []):
                batch_filter |= Q(brand__iexact=sb)
        brand_fcc_pairs = list(
            Radio.objects.filter(batch_filter)
            .exclude(fcc_id__exact='')
            .values_list('brand', 'fcc_id')
        )
        # Build a grantee_counts map keyed by normalised brand.
        brand_grantee_counts = {}
        for brand_val, fcc_id in brand_fcc_pairs:
            grantee_code, _ = split_fcc_id(fcc_id)
            if not grantee_code:
                continue
            bkey = _normalize_brand_key(brand_val)
            brand_grantee_counts.setdefault(bkey, Counter())[grantee_code] += 1

        for row in needs_grantee:
            counts = Counter()
            for sb in row.get('source_brands', []):
                bkey = _normalize_brand_key(sb)
                counts += brand_grantee_counts.get(bkey, Counter())
            if counts:
                row['grantee_code'] = counts.most_common(1)[0][0]

    alias_map = _build_brand_display_alias_map()

    # Resolve display alias for each top_brands row so the template can use it.
    for row in top_brands:
        row['display_brand'] = alias_map.get(row.get('brand_key', '')) or row.get('brand', '')

    recent_radios = list(
        Radio.objects.select_related('manufacturer')
        .filter(grant_date__isnull=False, grant_date__gte=cutoff_date)
        .order_by('-grant_date')[:10]
    )
    for radio in recent_radios:
        radio.display_brand = _preferred_brand_display_name(radio, alias_map)

    # Deduplicate by PDF filename in the database: keep the most-recent manual per unique PDF.
    latest_manual_ids = (
        RadioManual.objects
        .filter(doc_type=RadioManual.DocType.MANUAL)
        .exclude(manual_pdf='')
        .values('manual_pdf')
        .annotate(latest_id=Max('id'))
        .values('latest_id')
        .order_by()
    )
    recent_manual_uploads = list(
        RadioManual.objects
        .select_related('radio', 'radio__manufacturer')
        .filter(id__in=Subquery(latest_manual_ids))
        .order_by('-created_at')[:25]
    )
    for manual in recent_manual_uploads:
        if manual.radio:
            manual.radio.display_brand = _preferred_brand_display_name(manual.radio, alias_map)

    context = {
        'total_radios': sum(r.get('count', 0) for r in raw_brand_rows),
        'total_brands': len(merged_brands),
        'recent_radios': recent_radios,
        'recent_manual_uploads': recent_manual_uploads,
        'top_brands': top_brands,
        'recent_days': recent_days,
        'last_grantee_sync_at': FCCSyncState.get_instance().last_grantee_sync_at,
    }
    return render(request, 'radios/dashboard.html', context)


def processing_logs_view(request):
    """Show recent processing events and a scrollable application log."""
    logger.info("User action processing_logs_view actor=%s", _actor_label(request))

    log_file = Path(settings.LOG_DIR) / 'radio_tracker.log'
    lines = []
    if log_file.exists():
        try:
            with log_file.open('r', encoding='utf-8', errors='replace') as fp:
                lines = fp.readlines()
        except OSError:
            lines = []

    # Keep this page fast even with larger log files.
    recent_log_lines = lines[-600:]

    event_markers = (
        'User action',
        'FCC sync',
        'Manual upload',
        'Manual parse',
        'Web enrichment',
        'XML import',
    )
    recent_processing_events = [
        line.strip() for line in reversed(recent_log_lines) if any(marker in line for marker in event_markers)
    ][:80]

    recent_manual_events = RadioManual.objects.select_related('radio').order_by('-updated_at')[:30]

    context = {
        'recent_processing_events': recent_processing_events,
        'recent_manual_events': recent_manual_events,
        'log_lines': ''.join(recent_log_lines),
        'log_file_path': str(log_file),
        'log_file_exists': log_file.exists(),
    }
    return render(request, 'radios/processing_logs.html', context)


def nodal_visualization_view(request):
    """Interactive node graph for brand, model, FCC, and OEM relationships."""
    brand_query = (request.GET.get('brand') or '').strip()
    model_query = (request.GET.get('model') or '').strip()
    fcc_query = (request.GET.get('fcc_id') or '').strip()

    logger.info(
        "User action nodal_visualization_view actor=%s brand=%s model=%s fcc_id=%s",
        _actor_label(request),
        brand_query,
        model_query,
        fcc_query,
    )

    radios = Radio.objects.all()
    if brand_query:
        radios = radios.filter(
            Q(brand__icontains=brand_query)
            | Q(manufacturer__full_name__icontains=brand_query)
            | Q(manufacturer__alias__icontains=brand_query)
        )
    if model_query:
        # Normalize punctuation so UV5R and UV-5R match the same model family.
        requested_model_key = _normalize_model_key(model_query)
        if requested_model_key:
            candidate_ids = [
                radio.id
                for radio in radios.only('id', 'model')
                if requested_model_key in _normalize_model_key(radio.model)
            ]
            radios = radios.filter(id__in=candidate_ids)
    if fcc_query:
        # Exact normalized match to avoid broad captures like all 2AJGM-* records.
        requested_fcc_key = _normalize_fcc_key(fcc_query)
        grantee_code, product_code = split_fcc_id(fcc_query)

        candidate_qs = radios.exclude(fcc_id='')
        if grantee_code:
            candidate_qs = candidate_qs.filter(fcc_id__istartswith=grantee_code)
        if product_code:
            candidate_qs = candidate_qs.filter(fcc_id__icontains=product_code)

        candidate_ids = [
            radio.id
            for radio in candidate_qs.only('id', 'fcc_id')
            if _normalize_fcc_key(radio.fcc_id) == requested_fcc_key
        ]
        radios = radios.filter(id__in=candidate_ids)

    is_filtered = bool(brand_query or model_query or fcc_query)
    if not is_filtered:
        radios = radios.filter(
            Q(is_a_whitelabel=True)
            | Q(manufacturer__isnull=False)
            | ~Q(fcc_id='')
        )

    graph_data = build_nodal_graph_data(radios_queryset=radios, max_radios=500)
    graph_stats = graph_data.get('stats', {})

    context = {
        'graph_nodes': graph_data.get('nodes', []),
        'graph_edges': graph_data.get('edges', []),
        'graph_stats': graph_stats,
        'brand_query': brand_query,
        'model_query': model_query,
        'fcc_query': fcc_query,
        'is_filtered': is_filtered,
        'node_total': len(graph_data.get('nodes', [])),
        'edge_total': len(graph_data.get('edges', [])),
        'graph_nodes_json': json.dumps(graph_data.get('nodes', [])),
        'graph_edges_json': json.dumps(graph_data.get('edges', [])),
    }
    return render(request, 'radios/nodal_visualization.html', context)
