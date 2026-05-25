from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib import messages
from django.db.models import Q, Count, Max
from .models import Radio, Brand
from .forms import RadioForm, RadioSearchForm, BrandForm
from .fcc_utils import fetch_and_sync_fcc_id

def sync_fcc_view(request):
    """View to handle fetching and syncing an FCC ID from the dashboard."""
    if request.method == 'POST':
        fcc_id = request.POST.get('fcc_id', '').strip()
        if fcc_id:
            try:
                added, updated, processing_msgs = fetch_and_sync_fcc_id(fcc_id)
                if added > 0 or updated > 0:
                    messages.success(request, f"Success! Added {added} and updated {updated} records for FCC ID '{fcc_id}'.")
                else:
                    messages.warning(request, f"No new records or updates found for '{fcc_id}'.")
            except Exception as e:
                messages.error(request, f"Error processing FCC ID: {e}")
        else:
            messages.error(request, "Please enter a valid FCC ID.")
            
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
        'intro_year': 'intro_year',
        'freq_bands_tx': 'freq_bands_tx',
        'power_watts': 'power_watts',
        'cost_approx': 'cost_approx',
        'aprs': 'aprs',
        'updated_at': 'updated_at',
    }
    
    def get_queryset(self):
        queryset = Radio.objects.all()
        
        # Search functionality
        query = self.request.GET.get('query')
        if query:
            queryset = queryset.filter(
                Q(brand__icontains=query) |
                Q(model__icontains=query) |
                Q(fcc_id__icontains=query) |
                Q(rebadges_clones__icontains=query) |
                Q(white_label_vendors__icontains=query)
            )
        
        # Brand filter
        brand = self.request.GET.get('brand')
        if brand:
            queryset = queryset.filter(brand__iexact=brand)
        
        # Sorting
        sort = self.request.GET.get('sort', 'brand')
        order = self.request.GET.get('order', 'asc')
        
        if sort in self.SORT_FIELDS:
            sort_field = self.SORT_FIELDS[sort]
            if order == 'desc':
                sort_field = f'-{sort_field}'
            queryset = queryset.order_by(sort_field)
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        radio = context.get('radio')
        fcc_id = getattr(radio, 'fcc_id', None)
        fcc_grantee = ''
        fcc_product = ''
        if fcc_id and '-' in fcc_id:
            fcc_grantee, fcc_product = fcc_id.split('-', 1)
        context['fcc_grantee'] = fcc_grantee
        context['fcc_product'] = fcc_product
        
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
    success_url = reverse_lazy('radio_list')
    
    def form_valid(self, form):
        messages.success(self.request, f'Radio {form.instance} has been created successfully!')
        return super().form_valid(form)


class RadioUpdateView(UpdateView):
    """View for updating an existing radio entry"""
    model = Radio
    form_class = RadioForm
    template_name = 'radios/radio_form.html'
    success_url = reverse_lazy('radio_list')
    
    def form_valid(self, form):
        messages.success(self.request, f'Radio {form.instance} has been updated successfully!')
        return super().form_valid(form)


class RadioDeleteView(DeleteView):
    """View for deleting a radio entry"""
    model = Radio
    template_name = 'radios/radio_confirm_delete.html'
    success_url = reverse_lazy('radio_list')
    
    def delete(self, request, *args, **kwargs):
        radio = self.get_object()
        messages.success(request, f'Radio {radio} has been deleted successfully!')
        return super().delete(request, *args, **kwargs)


class BrandListView(ListView):
    """View for listing all brands"""
    model = Brand
    template_name = 'radios/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 50
    ordering = ['name']

    def get_queryset(self):
        queryset = super().get_queryset()
        query = self.request.GET.get('query')
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query) |
                Q(alias__icontains=query) |
                Q(full_name__icontains=query) |
                Q(grantee_code__icontains=query) |
                Q(white_label_vendors__icontains=query)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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
    success_url = reverse_lazy('brand_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['all_brands'] = Brand.objects.all().order_by('name')
        return context
    
    def form_valid(self, form):
        messages.success(self.request, f'Brand {form.instance} has been created successfully!')
        return super().form_valid(form)


class BrandUpdateView(UpdateView):
    """View for updating an existing brand entry"""
    model = Brand
    form_class = BrandForm
    template_name = 'radios/brand_form.html'
    success_url = reverse_lazy('brand_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['all_brands'] = Brand.objects.all().order_by('name')
        return context
    
    def form_valid(self, form):
        messages.success(self.request, f'Brand {form.instance} has been updated successfully!')
        return super().form_valid(form)


def dashboard_view(request):
    """Dashboard view with statistics"""
    context = {
        'total_radios': Radio.objects.count(),
        'total_brands': Radio.objects.values('brand').distinct().count(),
        'recent_radios': Radio.objects.order_by('-created_at')[:10],
        'top_brands': Radio.objects.values('brand').annotate(
            count=Count('id'),
            latest_update=Max('updated_at')
        ).order_by('-latest_update', '-count')[:10],
    }
    return render(request, 'radios/dashboard.html', context)
