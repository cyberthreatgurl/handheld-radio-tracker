"""
View logic for the radio tracker application.

Contains dashboard, CRUD views, FCC sync, brand detail, and
maintenance pages.
"""
# pylint: disable=no-member, broad-exception-caught, too-many-ancestors
# pylint: disable=too-few-public-methods, attribute-defined-outside-init
# pylint: disable=too-many-locals, too-many-branches, too-many-statements
# pylint: disable=too-many-lines, import-outside-toplevel
# no-member: Django ORM / CBV metaclass false positives
# broad-exception-caught: intentionally broad at network/service boundaries
# too-many-ancestors, too-few-public-methods: normal for Django class-based views
# attribute-defined-outside-init: Django CBV sets self.object in post()
# too-many-*, too-many-lines: complex views justified by varied page requirements
# import-outside-toplevel: lazy imports avoid circular deps

import ipaddress
import json
import logging
import re
import socket
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Count, Max, Prefetch, Subquery
from django.db.models.functions import ExtractYear
from django.http import Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView

from .fcc_id_utils import normalize_fcc_id_for_lookup, split_fcc_id
from .fcc_utils import fetch_and_sync_fcc_id
from .forms import (
    RadioForm, RadioSearchForm, BrandForm, ManufacturerForm, RadioImageFormSet,
    RadioCertificationFormSet,
)
from .forms_accounts import RadioCommentForm
from .accounts_decorators import StaffRequiredMixin, is_admin_user, staff_required
from .image_utils import ingest_radio_image
from .models import (
    Radio, Brand, RadioManual, RadioFirmware, Manufacturer, FCCSyncState,
    IgnoredGrantee, SyncSkippedGrantee, RadioFCCTestReport, RadioOETDocument,
    RadioImage, delete_brand_and_related,
)
from .nodal_graph import build_nodal_graph_data

logger = logging.getLogger(__name__)


def _host_is_public(hostname):
    """Return True when every resolved address is a public internet IP."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        ):
            return False
    return True


@staff_required
@require_POST
def probe_embeddable_view(request):
    """Check whether a URL can be shown inside the edit-page split viewer.

    Many sites send ``X-Frame-Options`` or a CSP ``frame-ancestors``
    directive that blocks iframe embedding.  We probe only the response
    headers (never the body) so the client can show a graceful fallback
    instead of the browser's error page.
    """
    url = (request.POST.get('url') or '').strip()
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return JsonResponse({'embeddable': False, 'reason': 'invalid'})
    if not _host_is_public(parsed.hostname):
        return JsonResponse({'embeddable': False, 'reason': 'blocked_host'})

    try:
        resp = curl_requests.get(
            url, impersonate='chrome124', timeout=5,
            allow_redirects=False, stream=True,
        )
    except Exception:
        return JsonResponse({'embeddable': False, 'reason': 'unreachable'})

    status = resp.status_code
    xfo = (resp.headers.get('X-Frame-Options') or '').strip().lower()
    csp = (resp.headers.get('Content-Security-Policy') or '').lower()
    try:
        resp.close()
    except Exception:
        pass

    # Redirects are deferred to the browser (which enforces the final
    # page's frame headers); we only probe the exact URL provided.
    if 300 <= status < 400:
        return JsonResponse({'embeddable': True, 'reason': 'redirect'})
    if xfo in ('deny', 'sameorigin'):
        return JsonResponse({'embeddable': False, 'reason': 'x_frame_options'})
    if 'frame-ancestors' in csp:
        return JsonResponse({'embeddable': False, 'reason': 'csp'})
    return JsonResponse({'embeddable': True, 'reason': 'ok'})


def _normalize_brand_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _normalize_fcc_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _normalize_model_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _build_brand_display_alias_map():
    alias_map = {}
    for brand in (
        Brand.objects.exclude(alias__isnull=True)
        .exclude(alias__exact='')
        .only('name', 'full_name', 'alias')
    ):
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
        for radio in source_qs.values(
            'id', 'brand', 'model', 'fcc_id', 'rebadges_clones',
            'white_label_vendors',
        )
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


def _run_sync_fcc(fcc_id):
    """Run a single FCC ID sync in a background thread."""
    import os
    from django.db import close_old_connections
    # Allow sync-only DB operations in this background thread even when the
    # dev server's parent request runs under ASGI (Django 4.2+ async safety).
    os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
    close_old_connections()
    try:
        added, updated, _processing_msgs = fetch_and_sync_fcc_id(
            fcc_id, honor_skip_lists=False,
        )
        ignore_msg = next((msg for msg in _processing_msgs if 'ignore list' in msg.lower()), '')
        result = ignore_msg or (
            f"Success! Added {added} and updated {updated} records for FCC ID '{fcc_id}'."
            if added > 0 or updated > 0
            else f"No new records or updates found for '{fcc_id}'."
        )
        cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
            'in_progress': False,
            'total': 1,
            'completed': 1,
            'current': '',
            'message': 'Sync complete.',
            'added': added,
            'updated': updated,
            'success': True,
            'result': result,
        }, timeout=300)
        logger.info(
            "Background sync_fcc result fcc_id=%s added=%s updated=%s",
            fcc_id, added, updated,
        )
    except Exception as e:
        cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
            'in_progress': False,
            'total': 0,
            'completed': 0,
            'current': '',
            'message': 'Sync error.',
            'added': 0,
            'updated': 0,
            'success': False,
            'result': f"Error processing FCC ID '{fcc_id}': {e}",
        }, timeout=300)
        logger.exception("Background sync_fcc error fcc_id=%s", fcc_id)
    finally:
        close_old_connections()


@staff_required
def sync_fcc_view(request):
    """Start a background sync for an FCC ID and return immediately."""
    redirect_to = request.POST.get('redirect_to', 'dashboard')

    if request.method == 'POST':
        fcc_id = request.POST.get('fcc_id', '').strip()
        logger.info("User action sync_fcc submit actor=%s fcc_id=%s", _actor_label(request), fcc_id)
        if fcc_id:
            cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
                'in_progress': True,
                'total': 1,
                'completed': 0,
                'current': fcc_id,
                'message': f'Syncing {fcc_id} — this may take a while...',
                'added': 0,
                'updated': 0,
            }, timeout=3600)
            thread = threading.Thread(
                target=_run_sync_fcc, args=(fcc_id,), daemon=True,
            )
            thread.start()
        else:
            messages.error(request, "Please enter a valid FCC ID.")

    return redirect(redirect_to)


@staff_required
def sync_radio_fcc_view(request, pk):
    """Fetch and sync FCC data for a specific radio.

    Always performs a full refresh of OET documents and test reports
    but preserves existing radio field values (freq_bands_tx, power,
    grant_date, etc.) — only fills in fields that are currently blank.
    """
    import os
    radio = get_object_or_404(Radio, pk=pk)

    if request.method == 'POST':
        if not radio.fcc_id:
            messages.error(request, "This radio does not have an FCC ID assigned.")
            return redirect('radio_detail', pk=pk)

        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
        force_reload = request.POST.get('force_reload') == '1'
        logger.info(
            "User action sync_radio_fcc submit actor=%s radio_pk=%s "
            "fcc_id=%s force_reload=%s",
            _actor_label(request), pk, radio.fcc_id, force_reload,
        )
        try:
            added, updated, _processing_msgs = fetch_and_sync_fcc_id(
                radio.fcc_id,
                force_reload=force_reload,
                honor_skip_lists=False,
                preserve_existing=True,
            )
            logger.info(
                "User action sync_radio_fcc result actor=%s radio_pk=%s "
                "fcc_id=%s added=%s updated=%s force_reload=%s",
                _actor_label(request), pk, radio.fcc_id, added, updated,
                force_reload,
            )

            ignore_message = next(
                (msg for msg in _processing_msgs
                 if 'ignore list' in msg.lower()),
                '',
            )
            if ignore_message:
                messages.warning(request, ignore_message)
            elif added > 0 or updated > 0:
                messages.success(
                    request,
                    f"Success! Updated FCC data for '{radio.fcc_id}'. "
                    f"Added {added} and updated {updated} records.",
                )
            else:
                messages.info(
                    request,
                    f"FCC sync completed for '{radio.fcc_id}'. "
                    "No new records or updates found.",
                )
        except Exception as e:
            logger.exception(
                "User action sync_radio_fcc error actor=%s radio_pk=%s "
                "fcc_id=%s",
                _actor_label(request), pk, radio.fcc_id,
            )
            messages.error(request, f"Error syncing FCC data: {e}")

    return redirect('radio_detail', pk=pk)



def _discover_unknown_grantees(start_date, end_date):
    """Scan radio FCC IDs for grantee codes not yet in the Brand table
    and sync them. This catches grantees that were imported via CSV/XML
    but never added to the Brand table for FCC API discovery.
    """
    from django.db import close_old_connections
    close_old_connections()

    known_codes = set(
        Brand.objects.exclude(grantee_code__isnull=True)
        .exclude(grantee_code='')
        .values_list('grantee_code', flat=True)
    )
    known_codes = {c.upper() for c in known_codes}
    ignored_codes = set(IgnoredGrantee.ignored_codes())
    skipped_codes = set(SyncSkippedGrantee.skipped_codes())
    excluded = known_codes | ignored_codes | skipped_codes

    # Collect unknown grantee codes from radio FCC IDs
    candidates = Counter()
    for radio in Radio.objects.exclude(fcc_id='').exclude(
        fcc_id__isnull=True
    ).iterator():
        try:
            grantee, _ = split_fcc_id(radio.fcc_id.strip().upper())
            if grantee and grantee not in excluded:
                candidates[grantee] += 1
        except (ValueError, IndexError):
            continue

    if not candidates:
        logger.info("Grantee discovery — no unknown grantees found.")
        return 0, 0

    logger.info(
        "Grantee discovery — found %d unknown grantee codes: %s",
        len(candidates),
        dict(candidates.most_common(20)),
    )

    discovered = 0
    for code, _count in candidates.most_common():
        # Re-check exclusion (may have been added by a prior iteration)
        if Brand.objects.filter(grantee_code__iexact=code).exists():
            continue
        if code in ignored_codes or code in skipped_codes:
            continue

        logger.info("Grantee discovery — querying unknown grantee=%s", code)
        close_old_connections()
        added, updated, _msgs = fetch_and_sync_fcc_id(
            code, start_date=start_date, end_date=end_date,
        )
        if added or updated:
            discovered += 1
            logger.info(
                "Grantee discovery — synced new grantee=%s added=%s "
                "updated=%s",
                code, added, updated,
            )

    return discovered, len(candidates)


def _sync_single_grantee(code, start_date, end_date):
    """Wrapper for parallel grantee sync — ensures fresh DB connection per thread."""
    import os
    from django.db import close_old_connections
    os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
    close_old_connections()
    return fetch_and_sync_fcc_id(code, start_date=start_date, end_date=end_date)


def _run_sync_all_grantees(start_date, end_date, grantee_codes):
    """Run the full grantee sync in a background thread."""
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from django.db import close_old_connections
    from .fcc_utils import (
        reset_sync_metadata_cache, _close_playwright_instance,
    )

    os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
    close_old_connections()
    reset_sync_metadata_cache()
    try:
        total_count = len(grantee_codes)
        total_added = 0
        total_updated = 0
        completed = 0
        errors = []

        max_workers = min(4, max(1, total_count))
        logger.info(
            "Background sync_all_grantees starting parallel=%s grantees=%s",
            max_workers, total_count,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _sync_single_grantee,
                    code, start_date, end_date,
                ): code
                for code in grantee_codes
            }

            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    added, updated, _msgs = future.result()
                    total_added += added
                    total_updated += updated
                except Exception as exc:
                    logger.exception(
                        "Background sync_all_grantees grantee failed "
                        "grantee=%s",
                        code,
                    )
                    errors.append(str(exc))

                completed += 1
                cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
                    'in_progress': True,
                    'total': total_count,
                    'completed': completed,
                    'current': code,
                    'message': (
                        f'[{completed}/{total_count}] Processed {code}...'
                    ),
                    'added': total_added,
                    'updated': total_updated,
                }, timeout=3600)

        # Phase 2: discover grantee codes from radio FCC IDs that are not
        # yet in the Brand table (e.g. imported via CSV before the Brand
        # was added).
        discovered, total_unknown = _discover_unknown_grantees(
            start_date, end_date,
        )
        if discovered:
            logger.info(
                "Grantee discovery complete discovered=%s total_unknown=%s",
                discovered, total_unknown,
            )

        # Phase 3: HTTP-based FCC GenericSearch for brand-new grantees.
        # Uses curl_cffi (Chrome impersonation) to POST date range to the
        # FCC GenericSearch form and fetch the XML export.  Falls back to
        # HTML table parsing if XML is not available.
        from .fcc_utils import discover_new_grantees_from_fcc  # pylint: disable=import-outside-toplevel
        new_grantees = discover_new_grantees_from_fcc(start_date, end_date)
        if new_grantees:
            logger.info(
                "Grantee FCC discovery found=%d codes=%s",
                len(new_grantees), sorted(new_grantees)[:20],
            )
            for code in sorted(new_grantees):
                try:
                    added, updated, _msgs = fetch_and_sync_fcc_id(
                        code,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if added or updated:
                        logger.info(
                            "Grantee FCC discovery synced grantee=%s "
                            "added=%s updated=%s",
                            code, added, updated,
                        )
                except Exception:
                    logger.exception(
                        "Grantee FCC discovery sync failed grantee=%s", code,
                    )

        sync_state = FCCSyncState.get_instance()
        sync_state.last_grantee_sync_at = end_date
        sync_state.save(update_fields=['last_grantee_sync_at'])

        cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
            'in_progress': False,
            'total': total_count,
            'completed': total_count,
            'current': '',
            'message': 'Sync complete.',
            'added': total_added,
            'updated': total_updated,
            'success': True,
            'result': (
                f"Updated grantees for grants since {start_date.strftime('%b %d, %Y')}. "
                f"Added {total_added}, updated {total_updated} records."
                if start_date else
                f"Full history sync complete. Added {total_added}, updated {total_updated} records."
            ),
        }, timeout=300)

        logger.info(
            "Background sync_all_grantees result added=%s updated=%s last_sync=%s",
            total_added, total_updated, end_date.isoformat(),
        )
    except Exception as e:
        cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
            'in_progress': False,
            'total': 0,
            'completed': 0,
            'current': '',
            'message': 'Sync error.',
            'added': 0,
            'updated': 0,
            'success': False,
            'result': f'Error processing grantees: {e}',
        }, timeout=300)
        logger.exception("Background sync_all_grantees error")
    finally:
        _close_playwright_instance()
        close_old_connections()


@staff_required
def sync_all_grantees_view(request):
    """Start a background sync of all grantees and return immediately."""
    if request.method == 'POST':
        logger.info("User action sync_all_grantees submit actor=%s", _actor_label(request))

        sync_state = FCCSyncState.get_instance()
        start_date = sync_state.last_grantee_sync_at
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
        skipped_codes = SyncSkippedGrantee.skipped_codes()
        grantees = Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code='')
        if ignored_codes:
            grantees = grantees.exclude(grantee_code__in=ignored_codes)
        if skipped_codes:
            grantees = grantees.exclude(grantee_code__in=skipped_codes)
        grantee_codes = list(grantees.values_list('grantee_code', flat=True))

        # Set initial progress and start background thread
        cache.set(_GRANTEE_SYNC_PROGRESS_KEY, {
            'in_progress': True,
            'total': len(grantee_codes),
            'completed': 0,
            'current': '',
            'message': f'Starting sync of {len(grantee_codes)} grantees...',
            'added': 0,
            'updated': 0,
        }, timeout=3600)

        thread = threading.Thread(
            target=_run_sync_all_grantees,
            args=(start_date, end_date, grantee_codes),
            daemon=True,
        )
        thread.start()

    return redirect(request.POST.get('redirect_to', 'dashboard'))


_GRANTEE_SYNC_PROGRESS_KEY = 'grantee_sync_progress'


def sync_progress_view(_request):
    """Return current grantee sync progress as JSON for the frontend progress bar."""
    progress = cache.get(_GRANTEE_SYNC_PROGRESS_KEY)
    if progress is None:
        progress = {'in_progress': False, 'total': 0, 'completed': 0,
                     'current': '', 'message': '', 'added': 0, 'updated': 0}
    return JsonResponse(progress)


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
            queryset=RadioManual.objects.exclude(manual_pdf='')
            .only('id', 'radio_id', 'manual_pdf', 'updated_at')
            .order_by('-updated_at'),
            to_attr='available_manuals',
        )
        firmware_prefetch = Prefetch(
            'firmware_versions',
            queryset=RadioFirmware.objects
            .only('id', 'radio_id', 'label', 'version', 'download_url')
            .order_by('label'),
            to_attr='prefetched_firmware',
        )
        queryset = (
            Radio.objects.all()
            .select_related('manufacturer')
            .prefetch_related(manual_prefetch, firmware_prefetch)
        )

        # Search functionality
        query = self.request.GET.get('query')
        if query:
            logger.info(
                "User action radio_search actor=%s query=%s",
                _actor_label(self.request), query,
            )
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
            logger.info(
                "User action radio_filter_brand actor=%s brand=%s",
                _actor_label(self.request), brand,
            )
            queryset = queryset.filter(brand__icontains=brand)

        # Sorting
        sort = self.request.GET.get('sort', 'brand')
        order = self.request.GET.get('order', 'asc')
        logger.info(
            "User action radio_list_sort actor=%s sort=%s order=%s",
            _actor_label(self.request), sort, order,
        )

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
        context['query_string'] = (
            f"&{query_params.urlencode()}" if query_params else ""
        )

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
            messages.error(
                request,
                f'Radio #{kwargs.get("pk")} no longer exists '
                '— it may have been deleted.',
            )
            return redirect('radio_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        radio = context.get('radio')
        fcc_id = (getattr(radio, 'fcc_id', None) or '').strip().upper()
        brand_match = (
            Brand.objects.filter(name__iexact=radio.brand)
            .only('id', 'grantee_code').first()
        )
        preferred_grantee_code = (
            (brand_match.grantee_code or '').strip().upper()
            if brand_match else ''
        )
        fcc_lookup_id = normalize_fcc_id_for_lookup(
            fcc_id,
            preferred_grantee_code=preferred_grantee_code,
        )

        context['fcc_lookup_id'] = fcc_lookup_id
        context['brand_pk'] = brand_match.pk if brand_match else None

        # Use stored OET page URL; for legacy records without one, derive it on the fly
        # from the application_id embedded in any RadioOETDocument document_url.
        oet_url = radio.oet_page_url or ''
        if not oet_url and fcc_id:
            _app_id_re = re.compile(r'application_id=([A-Za-z0-9%+=/]+)', re.IGNORECASE)
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

        # Certifications and service types
        context['certifications'] = radio.certifications.all()
        context['service_types'] = radio.service_types.all()

        # Aggregate frequency ranges and rule parts from certifications
        freq_ranges = set()
        rule_parts_set = set()
        for cert in radio.certifications.all():
            lower = cert.freq_range_lower_mhz
            upper = cert.freq_range_upper_mhz
            if lower is not None and upper is not None:
                freq_ranges.add((float(lower), float(upper)))
            for part in (cert.rule_parts or '').replace(';', ',').split(','):
                part = part.strip()
                if part:
                    rule_parts_set.add(part)
        context['cert_freq_ranges'] = sorted(freq_ranges)
        context['cert_rule_parts'] = sorted(rule_parts_set)
        context['comment_form'] = RadioCommentForm()

        return context


class RadioCreateView(StaffRequiredMixin, CreateView):
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
        logger.info(
            "User action radio_create submit actor=%s brand=%s model=%s",
            _actor_label(self.request), form.instance.brand,
            form.instance.model,
        )
        messages.success(
            self.request,
            f'Radio {form.instance} has been created successfully!',
        )
        return super().form_valid(form)


class RadioUpdateView(StaffRequiredMixin, UpdateView):
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
            context['certification_formset'] = RadioCertificationFormSet(
                self.request.POST,
                instance=self.object,
                prefix='certifications',
            )
        else:
            context['image_formset'] = RadioImageFormSet(
                instance=self.object,
                prefix='images',
            )
            context['certification_formset'] = RadioCertificationFormSet(
                instance=self.object,
                prefix='certifications',
            )
        return context

    def post(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except Http404:
            messages.error(
                request,
                f'Radio #{kwargs.get("pk")} no longer exists '
                '— it may have been deleted.',
            )
            return redirect('radio_list')
        form = self.get_form()
        image_formset = RadioImageFormSet(
            request.POST,
            request.FILES,
            instance=self.object,
            prefix='images',
        )
        certification_formset = RadioCertificationFormSet(
            request.POST,
            instance=self.object,
            prefix='certifications',
        )
        if form.is_valid() and image_formset.is_valid() and certification_formset.is_valid():
            return self.form_valid(form, image_formset, certification_formset)
        context = self.get_context_data(form=form)
        context['image_formset'] = image_formset
        context['certification_formset'] = certification_formset
        return self.render_to_response(context)

    def form_valid(self, form, image_formset=None, certification_formset=None):
        logger.info(
            "User action radio_update submit actor=%s radio_id=%s "
            "brand=%s model=%s",
            _actor_label(self.request), form.instance.pk,
            form.instance.brand, form.instance.model,
        )
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

        if certification_formset is not None:
            certification_formset.save()
            self.object.recompute_certification_summary(save=True)

        messages.success(self.request, f'Radio {form.instance} has been updated successfully!')
        return redirect(self.request.path)


@staff_required
def scrape_radio_website_view(request, pk):
    """POST-only: scrape a radio's website using the site-import pipeline."""
    radio = get_object_or_404(Radio, pk=pk)
    if request.method == 'POST':
        logger.info(
            "User action scrape_website actor=%s radio_pk=%s",
            _actor_label(request), pk,
        )
        website = (radio.website or '').strip()
        if not website:
            messages.warning(request, "This radio has no website URL to scrape.")
            return redirect('radio_edit', pk=pk)

        try:
            from .site_import import apply_website_to_radio
            report = apply_website_to_radio(radio, website, apply=True)
        except Exception:
            logger.exception(
                "User action scrape_website error radio_pk=%s", pk,
            )
            messages.error(request, "Error scraping the website.")
            return redirect('radio_edit', pk=pk)

        if report.get('errors'):
            messages.warning(
                request,
                "Could not extract specs from the radio's website.",
            )
        elif report['updated_fields']:
            messages.success(
                request,
                f"Scraped {len(report['updated_fields'])} field(s): "
                f"{', '.join(report['updated_fields'])}",
            )
        else:
            messages.info(
                request,
                "Website scraped, but no new data to apply "
                "(all target fields already populated).",
            )
    return redirect('radio_edit', pk=pk)


@staff_required
def import_radio_from_url_view(request):
    """POST: import (check-and-create/update) a radio from a pasted URL."""
    if request.method == 'POST':
        url = (request.POST.get('url') or '').strip()
        logger.info(
            "User action import_from_url submit actor=%s url=%s",
            _actor_label(request), url,
        )
        if not url:
            messages.error(request, "Please enter a product page URL.")
            return redirect('radio_list')

        try:
            from .site_import import upsert_radio_from_url
            report = upsert_radio_from_url(url, apply=True)
        except Exception:
            logger.exception("User action import_from_url error url=%s", url)
            messages.error(request, "Error importing the URL.")
            return redirect('radio_list')

        if report.get('errors'):
            messages.warning(
                request,
                "Could not extract a brand and model from that URL.",
            )
        else:
            action = "Created" if report['radio_created'] else "Updated"
            message = f"{action} {report['brand']} {report['model']}."
            if report['updated_fields']:
                message += (
                    f" Updated: {', '.join(report['updated_fields'])}."
                )
            if report['manuals']:
                message += f" Manuals: {len(report['manuals'])}."
            messages.success(request, message)

            radio_id = report.get('radio_id')
            if radio_id:
                return redirect('radio_edit', pk=radio_id)
    return redirect('radio_list')


@staff_required
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


class RadioDeleteView(StaffRequiredMixin, DeleteView):
    """Confirm and delete a radio record."""
    model = Radio
    success_url = reverse_lazy('radio_list')

    def delete(self, request, *args, **kwargs):
        radio = self.get_object()
        logger.info(
            "User action radio_delete submit actor=%s radio_id=%s "
            "brand=%s model=%s",
            _actor_label(request), radio.pk, radio.brand, radio.model,
        )
        messages.success(
            request,
            f'Radio {radio} has been deleted successfully!',
        )
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
        'radio_count': 'radio_count',
    }

    def get_queryset(self):
        queryset = super().get_queryset().select_related('parent_brand')

        # ── Annotate with radio count ──
        # Radio.brand is a CharField, not a FK, so we use a correlated
        # subquery via RawSQL.  A Subquery with .values('brand') would
        # group by distinct brand values and return multiple rows when
        # the OR filter matches via name, alias, and full_name.
        from django.db.models.expressions import RawSQL
        queryset = queryset.annotate(
            radio_count=RawSQL(
                """
                (SELECT COUNT(*) FROM radios_radio
                 WHERE UPPER(radios_radio.brand) IN (
                     UPPER(radios_brand.name),
                     UPPER(radios_brand.alias),
                     UPPER(radios_brand.full_name)
                 ))
                """,
                (),
            ),
        )

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
        context['query_string'] = (
            f"&{query_params.urlencode()}" if query_params else ""
        )
        return context

class BrandCreateView(StaffRequiredMixin, CreateView):
    """View for creating a new brand entry"""
    model = Brand
    form_class = BrandForm
    template_name = 'radios/brand_form.html'

    def get_success_url(self):
        return reverse_lazy('brand_detail', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brands_qs = Brand.objects.only('id', 'name').order_by('name')
        context['all_brands'] = brands_qs
        context['brand_name_to_pk'] = {b.name: b.pk for b in brands_qs}
        return context

    def form_valid(self, form):
        logger.info(
            "User action brand_create submit actor=%s brand=%s",
            _actor_label(self.request), form.instance.name,
        )
        messages.success(
            self.request,
            f'Brand {form.instance} has been created successfully!',
        )
        return super().form_valid(form)


class BrandDeleteView(StaffRequiredMixin, DeleteView):
    """View for deleting a brand and associated records."""
    model = Brand
    template_name = 'radios/brand_confirm_delete.html'
    success_url = reverse_lazy('brand_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brand = context['object']
        associated_radios = (
            Radio.objects.filter(brand__iexact=brand.name).distinct()
        )
        context['associated_radio_count'] = associated_radios.count()
        context['associated_manual_count'] = (
            RadioManual.objects.filter(radio__in=associated_radios).count()
        )
        context['associated_test_report_count'] = (
            RadioFCCTestReport.objects
            .filter(radio__in=associated_radios).count()
        )
        context['associated_oet_document_count'] = (
            RadioOETDocument.objects
            .filter(radio__in=associated_radios).count()
        )
        context['associated_firmware_count'] = (
            RadioFirmware.objects
            .filter(radio__in=associated_radios).count()
        )
        linked_manufacturer_ids = (
            Manufacturer.objects.filter(brands=brand)
            .values_list('id', flat=True)
        )
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
            messages.error(
                request,
                'Please confirm that you understand this deletion is permanent.',
            )
            logger.info(
                "User action brand_delete blocked_missing_confirmation "
                "actor=%s brand_id=%s brand=%s",
                _actor_label(request),
                brand.pk,
                brand.name,
            )
            context = self.get_context_data(object=brand)
            return render(request, self.template_name, context)

        associated_radios = (
            Radio.objects.filter(brand__iexact=brand.name).distinct()
        )
        radio_count = associated_radios.count()
        manual_count = (
            RadioManual.objects.filter(radio__in=associated_radios).count()
        )

        logger.warning(
            "User action brand_delete submit actor=%s brand_id=%s "
            "brand=%s associated_radios=%s associated_manuals=%s",
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
        if delete_summary.get('grantee_code'):
            if delete_summary.get('grantee_ignored'):
                messages.info(
                    request,
                    f'Added FCC grantee ID {delete_summary["grantee_code"]} '
                    'to the ignored grantees list.',
                )
            else:
                messages.info(
                    request,
                    f'FCC grantee ID {delete_summary["grantee_code"]} was '
                    'already in the ignored grantees list.',
                )
        return redirect(self.success_url)


@staff_required
def brand_bulk_delete_view(request):
    """POST-only: delete multiple brands, their radios, and ignore grantee IDs."""
    if request.method != 'POST':
        return redirect('brand_list')

    raw_ids = request.POST.getlist('brand_ids')
    brand_ids = []
    for raw in raw_ids:
        try:
            brand_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    if not brand_ids:
        messages.error(request, 'No brands were selected for deletion.')
        return redirect('brand_list')

    if request.POST.get('confirm_delete') != 'yes':
        messages.error(
            request,
            'Please confirm that you understand this deletion is permanent.',
        )
        return redirect('brand_list')

    brands = list(Brand.objects.filter(pk__in=brand_ids))
    deleted_brands = 0
    deleted_radios = 0
    ignored_grantees = []
    for brand in brands:
        brand_pk = brand.pk
        brand_name = brand.name
        summary = delete_brand_and_related(brand)
        deleted_brands += 1
        deleted_radios += summary.get('radios_deleted', 0)
        grantee_code = summary.get('grantee_code')
        if grantee_code:
            ignored_grantees.append(grantee_code)
        logger.warning(
            "User action brand_bulk_delete submit actor=%s brand_id=%s "
            "brand=%s radios_deleted=%s",
            _actor_label(request), brand_pk, brand_name,
            summary.get('radios_deleted', 0),
        )

    messages.success(
        request,
        f'Deleted {deleted_brands} brand(s) and {deleted_radios} radio model(s).',
    )
    if ignored_grantees:
        messages.info(
            request,
            'Added FCC grantee ID(s) to the ignored grantees list: '
            f'{', '.join(ignored_grantees)}.',
        )
    return redirect('brand_list')


@staff_required
def brand_merge_view(request, pk):
    """Merge a source brand into a chosen target brand.

    All radio records, manufacturer FK references, child brand parent_brand
    pointers, and Manufacturer M2M links are re-pointed from source → target,
    then the source brand is deleted.
    """
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
            "Brand merge complete source=%s target=%s radios_brand=%s "
            "child_brands=%s manufacturers=%s",
            source_name, target.name, r1, r2, r3,
        )
        messages.success(
            request,
            f'Merged "{source_name}" into "{target.name}": '
            f'{r1} radio brand(s), {r2} child brand(s) updated.',
        )
        return redirect('brand_detail', pk=target.pk)

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
        context['query_string'] = (
            f"&{query_params.urlencode()}" if query_params else ""
        )
        return context


class ManufacturerCreateView(StaffRequiredMixin, CreateView):
    """Create a new manufacturer record."""
    model = Manufacturer
    form_class = ManufacturerForm
    template_name = 'radios/manufacturer_form.html'

    def get_success_url(self):
        return reverse_lazy('manufacturer_edit', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        logger.info(
            "User action manufacturer_create actor=%s name=%s",
            _actor_label(self.request), form.instance.full_name,
        )
        messages.success(
            self.request,
            f'Manufacturer "{form.instance.full_name}" created.',
        )
        return super().form_valid(form)


class ManufacturerUpdateView(StaffRequiredMixin, UpdateView):
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
        logger.info(
            "User action manufacturer_update actor=%s pk=%s name=%s",
            _actor_label(self.request), self.object.pk,
            form.instance.full_name,
        )
        messages.success(
            self.request,
            f'Manufacturer "{form.instance.full_name}" updated.',
        )
        return super().form_valid(form)


class ManufacturerDeleteView(StaffRequiredMixin, DeleteView):
    """Delete a manufacturer record (does not delete linked brands)."""
    model = Manufacturer
    template_name = 'radios/manufacturer_confirm_delete.html'
    success_url = reverse_lazy('manufacturer_list')

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        logger.warning(
            "User action manufacturer_delete actor=%s pk=%s name=%s",
            _actor_label(request), self.object.pk,
            self.object.full_name,
        )
        messages.success(
            request,
            f'Manufacturer "{self.object.full_name}" deleted.',
        )
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


def _annotate_brand_grant_dates(top_brands):
    """Annotate each brand row with its newest FCC grant date and re-sort.

    Queries all radios for the given brands to find the maximum grant_date,
    updates each row with 'latest_grant_date', and re-sorts top_brands
    in-place descending by that date.
    """
    brand_keys = {row.get('brand_key', '') for row in top_brands}
    if not brand_keys:
        return

    # Collect all brand names across the merged groups
    all_names = set()
    for row in top_brands:
        all_names.update(row.get('source_brands', set()))

    grant_dates = dict(
        Radio.objects.filter(brand__in=all_names, grant_date__isnull=False)
        .values('brand')
        .annotate(max_date=Max('grant_date'))
        .values_list('brand', 'max_date')
    )

    # Map normalized keys to the max grant date for their group
    key_dates = {}
    for row in top_brands:
        latest = None
        for sb in row.get('source_brands', []):
            d = grant_dates.get(sb)
            if d and (latest is None or d > latest):
                latest = d
        key_dates[row.get('brand_key', '')] = latest
        row['latest_grant_date'] = latest

    top_brands.sort(
        key=lambda item: (
            key_dates.get(item.get('brand_key', '')) is not None,
            key_dates.get(item.get('brand_key', '')) or '',
        ),
        reverse=True,
    )


def dashboard_view(request):
    """Dashboard view with statistics"""
    logger.info("User action dashboard_view actor=%s", _actor_label(request))

    # Parse adjustable recent_days parameter (default 30, range 7-365)
    recent_days = request.GET.get('recent_days', '30')
    try:
        recent_days = max(7, min(365, int(recent_days)))
    except (ValueError, TypeError):
        recent_days = 30

    from datetime import timedelta as _timedelta
    now = timezone.now()
    cutoff_datetime = now - _timedelta(days=recent_days)

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

    # ── Exclude brands that have been deleted from the Brand table ──
    # Build a set of normalized keys for every active Brand record so we
    # can filter out dashboard entries whose Brand was deleted (radios
    # may still reference the old brand name via the CharField).
    active_brand_keys: set[str] = set()
    for b in Brand.objects.only('name', 'alias', 'full_name'):
        for val in (b.name, b.alias, b.full_name):
            key = _normalize_brand_key(val)
            if key:
                active_brand_keys.add(key)

    filtered_brands: dict[str, dict] = {}
    excluded_count = 0
    for key, entry in merged_brands.items():
        if key in active_brand_keys:
            filtered_brands[key] = entry
        else:
            excluded_count += 1
            logger.info(
                "Dashboard brand filter excluded deleted brand key=%s "
                "display=%s radio_count=%s",
                key, entry.get('brand', ''), entry.get('count', 0),
            )

    if excluded_count:
        logger.info(
            "Dashboard brand filter excluded %d deleted brands",
            excluded_count,
        )
    merged_brands = filtered_brands

    top_brands = sorted(
        merged_brands.values(),
        key=lambda item: (
            (item.get('latest_update') is not None),
            item.get('latest_update'),
            item.get('count') or 0,
        ),
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

    sort_by = request.GET.get('sort', 'created')
    if sort_by not in ('created', 'grant_date'):
        sort_by = 'created'

    if sort_by == 'grant_date':
        _annotate_brand_grant_dates(top_brands)

    if sort_by == 'grant_date':
        recent_radios = list(
            Radio.objects.select_related('manufacturer')
            .filter(
                grant_date__isnull=False,
                grant_date__gte=now.date() - _timedelta(days=recent_days),
            )
            .order_by('-grant_date')[:50]
        )
    else:
        recent_radios = list(
            Radio.objects.select_related('manufacturer')
            .filter(created_at__gte=cutoff_datetime)
            .order_by('-created_at')[:50]
        )
    # ── Exclude radios whose brand has been deleted ──
    recent_radios = [
        r for r in recent_radios
        if _normalize_brand_key(r.brand) in active_brand_keys
    ]
    for radio in recent_radios:
        radio.display_brand = _preferred_brand_display_name(radio, alias_map)

    # Deduplicate by PDF filename in the database: keep the most-recent manual per unique PDF.
    # Exclude orphaned manuals (SET_NULL on Radio FK leaves zombie records after deletion).
    latest_manual_ids = (
        RadioManual.objects
        .filter(doc_type=RadioManual.DocType.MANUAL, radio__isnull=False)
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
    # Exclude manuals whose radio references a brand that no longer exists.
    recent_manual_uploads = [
        manual for manual in recent_manual_uploads
        if manual.radio is not None
        and _normalize_brand_key(manual.radio.brand) in active_brand_keys
    ]
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
        'sort_by': sort_by,
        'last_grantee_sync_at': FCCSyncState.get_instance().last_grantee_sync_at,
    }
    return render(request, 'radios/dashboard.html', context)


def fcc_lookup_view(request):
    """AJAX view: look up an FCC ID or grantee code via the FCC API (read-only)."""
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'error': 'No query provided'}, status=400)

    try:
        resp = curl_requests.get(
            f'https://apps.fcc.gov/OETLabServices/getFCCIDList?fccId={query}',
            impersonate='chrome124',
            timeout=15,
        )
    except Exception as e:
        return JsonResponse({'error': f'FCC API error: {e}'}, status=502)

    if resp.status_code != 200:
        return JsonResponse(
            {'error': f'FCC API returned status {resp.status_code}'},
            status=502,
        )

    try:
        import xmltodict as _x
        data = _x.parse(resp.text)
        wrapper = data.get('fCCIDInfoes') or {}
        records = wrapper.get('fccidInfo') or []
        if isinstance(records, dict):
            records = [records]
    except Exception as e:
        return JsonResponse({'error': f'Parse error: {e}'}, status=502)

    if not records:
        return JsonResponse({'error': 'No records found.'}, status=404)

    is_grantee = '-' not in query
    summary = {
        'type': 'grantee' if is_grantee else 'fcc_id',
        'query': query,
        'record_count': len(records),
    }

    if is_grantee:
        summary['grantee_names'] = sorted(
            set(r.get('grantee', '') for r in records)
        )

    summary['records'] = [
        {
            'FCCId': r.get('FCCId', ''),
            'applicationPurpose': r.get('applicationPurpose', ''),
            'grantDate': r.get('grantDate', ''),
        }
        for r in records
    ]
    if not is_grantee:
        for rec, src in zip(summary['records'], records):
            rec['grantee'] = src.get('grantee', '')

    return JsonResponse(summary)


@staff_required
def fcc_validate_fccids_view(request):
    """Validate unique FCC IDs against the live FCC API and local database.

    For each unique FCC ID in the database:
    1. First checks if its stripped (no-hyphen) form matches a radio that
       already has OET documents downloaded — these are hyphen-placement
       duplicates and are marked VALID locally.
    2. Falls back to querying the live FCC API with both the original and
       compact (no-hyphen) forms.
    """
    # Collect unique FCC IDs and their sample radios
    unique_fcc_ids = {}
    pk_limit = int(request.GET.get('limit', '0'))
    count = 0
    for radio in Radio.objects.exclude(fcc_id='').exclude(fcc_id__isnull=True).iterator():
        fcc_id = radio.fcc_id.strip().upper()
        if fcc_id and fcc_id not in unique_fcc_ids:
            unique_fcc_ids[fcc_id] = {
                'brand': radio.brand,
                'model': radio.model,
                'radio_id': radio.pk,
            }
            count += 1
            if pk_limit and count >= pk_limit:
                break

    # Build local valid-FCC-ID lookup: {stripped: original} for radios
    # that have OET documents downloaded.
    local_valid = _build_local_fcc_id_map()

    logger.info(
        "FCC validation start total_unique=%d local_valid_count=%d",
        len(unique_fcc_ids), len(local_valid),
    )

    results = []
    checked = 0
    auto_deleted = 0
    api_checked = 0
    api_errors = 0
    not_found_count = 0
    local_match_count = 0

    for fcc_id, info in sorted(unique_fcc_ids.items()):
        checked += 1
        stripped = fcc_id.replace('-', '')

        # ── Step 1: check local DB for hyphen-placement duplicate ──
        if stripped in local_valid:
            correct = local_valid[stripped]
            deleted_count, _details = Radio.objects.filter(
                fcc_id__iexact=fcc_id,
            ).delete()
            auto_deleted += deleted_count
            local_match_count += 1
            logger.info(
                "FCC validation local_match fcc_id=%s correct=%s deleted=%d",
                fcc_id, correct, deleted_count,
            )
            results.append({
                'fcc_id': fcc_id, 'status': 'VALID',
                'brand': info['brand'], 'model': info['model'],
                'source': 'local',
                'correct_fcc_id': correct,
                'deleted': deleted_count,
            })
            continue

        # ── Step 2: query the live FCC API ──
        api_checked += 1
        if api_checked % 50 == 0:
            logger.info(
                "FCC validation api_progress checked=%d/%d local=%d "
                "not_found=%d errors=%d",
                api_checked, len(unique_fcc_ids) - local_match_count,
                local_match_count, not_found_count, api_errors,
            )
        compact_id = fcc_id.replace('-', '')
        candidates = list(set([fcc_id, compact_id]))

        records = []
        for candidate in candidates:
            try:
                resp = curl_requests.get(
                    f'https://apps.fcc.gov/OETLabServices/getFCCIDList?fccId={candidate}',
                    impersonate='chrome124',
                    timeout=15,
                )
            except Exception:
                logger.warning(
                    "FCC validation api_error fcc_id=%s candidate=%s",
                    fcc_id, candidate, exc_info=True,
                )
                continue

            if resp.status_code != 200:
                logger.warning(
                    "FCC validation api_status=%d fcc_id=%s candidate=%s",
                    resp.status_code, fcc_id, candidate,
                )
                continue

            try:
                import xmltodict as _x
                data = _x.parse(resp.text)
                wrapper = data.get('fCCIDInfoes') or {}
                records = wrapper.get('fccidInfo') or []
                if isinstance(records, dict):
                    records = [records]
            except Exception:
                logger.warning(
                    "FCC validation xml_parse_error fcc_id=%s candidate=%s",
                    fcc_id, candidate, exc_info=True,
                )
                continue

            if records:
                break

        if not records:
            not_found_count += 1
            logger.debug(
                "FCC validation not_found fcc_id=%s candidates=%s",
                fcc_id, candidates,
            )
            results.append({
                'fcc_id': fcc_id, 'status': 'NOT_FOUND',
                'brand': info['brand'], 'model': info['model'],
            })
        else:
            has_grant_date = any(r.get('grantDate') for r in records)
            results.append({
                'fcc_id': fcc_id, 'status': 'VALID',
                'brand': info['brand'], 'model': info['model'],
                'source': 'fcc_api',
                'has_grant_date': has_grant_date,
            })

    invalid_count = sum(1 for r in results if r['status'] != 'VALID')
    logger.info(
        "FCC validation complete total=%d local=%d api=%d not_found=%d "
        "api_errors=%d auto_deleted=%d",
        checked, local_match_count, api_checked,
        not_found_count, api_errors, auto_deleted,
    )
    return JsonResponse({
        'total_unique': len(unique_fcc_ids),
        'checked': checked,
        'invalid_count': invalid_count,
        'auto_deleted': auto_deleted,
        'results': results,
    })


def _build_local_fcc_id_map():
    """Return {stripped_fcc_id: original_fcc_id} for radios with OET docs."""
    valid_ids = (
        RadioOETDocument.objects
        .exclude(radio__fcc_id='')
        .values_list('radio__fcc_id', flat=True)
        .distinct()
    )
    mapping = {}
    for fcc_id in valid_ids:
        if fcc_id:
            stripped = fcc_id.replace('-', '').strip().upper()
            mapping.setdefault(stripped, fcc_id)
    return mapping


@staff_required
def maintenance_view(request):
    """Maintenance page — DB stats, country counts, grant year chart."""
    logger.info("User action maintenance_view actor=%s", _actor_label(request))

    total_radios = Radio.objects.count()
    total_manufacturers = Manufacturer.objects.count()
    total_brands = Brand.objects.count()

    # Count radios by country of manufacturer — list each country found
    country_counts = {}
    for row in Manufacturer.objects.exclude(
        country__isnull=True
    ).exclude(country__exact='').values('country').distinct():
        cname = row['country'].strip()
        if not cname:
            continue
        cnt = Radio.objects.filter(
            manufacturer__country__iexact=cname
        ).count()
        if cnt:
            country_counts[cname] = cnt
    country_counts['Unknown'] = Radio.objects.filter(
        Q(manufacturer__isnull=True)
        | Q(manufacturer__country__isnull=True)
        | Q(manufacturer__country__exact='')
    ).count()
    # Sort by count descending for display
    country_counts = dict(
        sorted(country_counts.items(), key=lambda x: -x[1])
    )

    # Grant_date chart data: count of radios by year of first FCC grant
    year_counts = Counter()
    for row in Radio.objects.exclude(grant_date__isnull=True).values('grant_date'):
        year_counts[row['grant_date'].year] += 1
    grant_chart = sorted(year_counts.items())

    total_oet_docs = RadioOETDocument.objects.count()
    context = {
        'total_radios': total_radios,
        'total_manufacturers': total_manufacturers,
        'total_brands': total_brands,
        'total_oet_docs': total_oet_docs,
        'country_counts': country_counts,
        'grant_chart': grant_chart,
        'grant_chart_json': json.dumps([[int(y), c] for y, c in grant_chart]),
        'max_grant_year_count': max((c for _, c in grant_chart), default=1),
        'last_grantee_sync_at': FCCSyncState.get_instance().last_grantee_sync_at,
    }
    return render(request, 'radios/maintenance.html', context)


@staff_required
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
        line.strip()
        for line in reversed(recent_log_lines)
        if any(marker in line for marker in event_markers)
    ][:80]

    recent_manual_events = (
        RadioManual.objects.select_related('radio')
        .order_by('-updated_at')[:30]
    )

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


# ---------------------------------------------------------------------------
# Brand detail page — stats, chart, feature filtering
# ---------------------------------------------------------------------------


def _band_filter(band_keywords):
    """Return a Q filter for ``freq_bands_tx`` matching any keyword."""
    q = Q()
    for kw in band_keywords:
        q |= Q(freq_bands_tx__icontains=kw)
    return q


def _freq_range_filter(lower_mhz, upper_mhz, text_keywords):
    """Return a Q filter for radios that transmit in the given frequency
    range, using numeric certification ranges when available and falling
    back to text matching on ``freq_bands_tx``."""
    overlap = Q(
        certifications__freq_range_lower_mhz__isnull=False,
        certifications__freq_range_upper_mhz__isnull=False,
        certifications__freq_range_lower_mhz__lte=upper_mhz,
        certifications__freq_range_upper_mhz__gte=lower_mhz,
    )
    return overlap | _band_filter(text_keywords)


# Feature definitions used on the brand detail page.
FEATURE_DEFS = {
    'aprs': {
        'label': 'APRS',
        'description': 'Automatic Packet Reporting System',
        'filter': ~Q(aprs='') & ~Q(aprs__iexact='No')
                  & ~Q(aprs__iexact='Not specified'),
    },
    'gps': {
        'label': 'GPS',
        'description': 'Global Positioning System',
        'filter': Q(gps__iexact='Yes') | Q(gps__iexact='Optional'),
    },
    'dmr': {
        'label': 'DMR',
        'description': 'Digital Mobile Radio',
        'filter': Q(dmr__iexact='Yes'),
    },
    'vhf': {
        'label': 'VHF',
        'description': '144–148 MHz (2 meter)',
        'filter': _freq_range_filter(144.0, 148.0, ['vhf', '144', '2m', '2 meter']),
    },
    'uhf': {
        'label': 'UHF',
        'description': '420–450 MHz (70 cm)',
        'filter': _freq_range_filter(420.0, 450.0, ['uhf', '430', '440', '70cm', '70 cm']),
    },
    '5m': {
        'label': '6 meters',
        'description': '50–54 MHz (6 meter)',
        'filter': _freq_range_filter(50.0, 54.0, ['6m', '6 meter', '50-54', '50 mhz']),
    },
    '10m': {
        'label': '10 meters',
        'description': '28–29.7 MHz',
        'filter': _freq_range_filter(28.0, 29.7, ['10m', '10 meter', '28-30', '28 mhz', '29 mhz']),
    },
    '11m': {
        'label': '11 meters (CB)',
        'description': '26–27 MHz Citizens Band',
        'filter': _freq_range_filter(
            26.0, 27.5,
            ['11m', '11 meter', 'cb band', 'citizens band', '26-28', '27 mhz'],
        ),
    },
    'frs': {
        'label': 'FRS',
        'description': 'Family Radio Service (462–467 MHz)',
        'filter': Q(service_types__name__iexact='FRS')
                  | _band_filter(['frs']),
    },
    'gmrs': {
        'label': 'GMRS',
        'description': 'General Mobile Radio Service (462–467 MHz)',
        'filter': Q(service_types__name__iexact='GMRS')
                  | _band_filter(['gmrs']),
    },
    'cb': {
        'label': 'CB',
        'description': 'Citizens Band Radio Service (Part 95D)',
        'filter': Q(service_types__name__iexact='CB'),
    },
    'murs': {
        'label': 'MURS',
        'description': 'Multi-Use Radio Service (Part 95J)',
        'filter': Q(service_types__name__iexact='MURS'),
    },
    'amateur': {
        'label': 'Amateur',
        'description': 'Amateur Radio Service (Part 97)',
        'filter': Q(service_types__name__iexact='Amateur'),
    },
    'commercial': {
        'label': 'Commercial',
        'description': 'Land Mobile Radio Service (Part 90)',
        'filter': Q(service_types__name__iexact='Commercial'),
    },
    'marine': {
        'label': 'Marine',
        'description': 'Maritime Mobile Service (Part 80)',
        'filter': Q(service_types__name__iexact='Marine'),
    },
    'aviation': {
        'label': 'Aviation',
        'description': 'Aviation Services (Part 87)',
        'filter': Q(service_types__name__iexact='Aviation'),
    },
    'poc': {
        'label': 'PoC',
        'description': 'Push-to-Talk over Cellular (Parts 22/24/27)',
        'filter': Q(service_types__name__iexact='PoC'),
    },
    'part15b': {
        'label': 'Part 15B',
        'description': 'Part 15 Subpart B (unintentional radiators)',
        'filter': Q(service_types__name__iexact='Part 15 Subpart B'),
    },
    'part15c': {
        'label': 'Part 15C',
        'description': 'Part 15 Subpart C (intentional radiators)',
        'filter': Q(service_types__name__iexact='Part 15 Subpart C'),
    },
}


def brand_detail_view(request, pk, edit=False):
    """Detail page for a single brand with stats, chart, and feature
    filtering. Shows total model count, newest model, subsidiary/white-label
    info, a year-over-year bar chart of new models, and clickable feature
    count tiles that filter the model list below.

    The same page hosts inline editing: POSTs are validated and saved here,
    and the ``/edit/`` URL (or ``?edit=1``) opens the edit form by default.
    """
    brand = get_object_or_404(Brand, pk=pk)

    # Editing (via /edit/, ?edit=1, or a POST) is staff-only; the read-only
    # detail page stays public.
    if (
        edit or request.GET.get('edit') == '1' or request.method == 'POST'
    ) and not is_admin_user(request.user):
        return redirect('login')

    edit_mode = edit or request.GET.get('edit') == '1'
    form = BrandForm(instance=brand)

    if request.method == 'POST':
        form = BrandForm(request.POST, instance=brand)
        if form.is_valid():
            old_name = Brand.objects.values_list('name', flat=True).get(pk=brand.pk)
            new_name = form.cleaned_data['name']
            form.save()
            logger.info(
                "User action brand_update submit actor=%s brand_id=%s brand=%s",
                _actor_label(request), brand.pk, new_name,
            )
            if old_name != new_name:
                updated = Radio.objects.filter(brand__iexact=old_name).update(brand=new_name)
                logger.info(
                    "Brand rename cascade old=%s new=%s radios_updated=%s",
                    old_name, new_name, updated,
                )
                if updated:
                    messages.info(
                        request,
                        f'Updated {updated} radio record(s) from brand '
                        f'"{old_name}" to "{new_name}".',
                    )
            messages.success(request, f'Brand {new_name} has been saved successfully!')
            return redirect('brand_detail', pk=brand.pk)
        edit_mode = True

    # Build a broad query for radios associated with this brand.
    # Include: direct brand name match + grantee code match (own or parent).
    brand_radio_query = Q(brand__iexact=brand.name)

    # Resolve the effective grantee code: check own code first, then parent.
    effective_grantee = brand.grantee_code
    if not effective_grantee and brand.parent_brand:
        effective_grantee = brand.parent_brand.grantee_code

    if effective_grantee:
        # Include radios whose FCC ID starts with this grantee code, covering
        # white-label brands sold under different names but from the same OEM.
        brand_radio_query |= Q(fcc_id__istartswith=effective_grantee)

    radios_qs = Radio.objects.filter(brand_radio_query)
    total_models = radios_qs.count()

    # Newest model (by FCC grant date)
    newest_radio = (
        radios_qs.filter(grant_date__isnull=False)
        .order_by('-grant_date')
        .first()
    )

    # White-label / subsidiary brands
    subsidiary_brands = Brand.objects.filter(parent_brand=brand).order_by('name')

    # Parse comma-separated white_label_vendors
    white_label_names = []
    if brand.white_label_vendors:
        white_label_names = [
            v.strip() for v in brand.white_label_vendors.split(',')
            if v.strip()
        ]

    # Manufacturer linked via the Brand-M2M
    manufacturer = brand.manufacturers.first()

    # Year-over-year chart data
    yearly_counts = list(
        radios_qs.filter(grant_date__isnull=False)
        .annotate(year=ExtractYear('grant_date'))
        .values('year')
        .annotate(count=Count('id'))
        .order_by('year')
    )
    max_year_count = max(
        (r['count'] for r in yearly_counts if r['year']), default=1
    )

    # Feature counts
    feature_counts = {}
    for key, defn in FEATURE_DEFS.items():
        feature_counts[key] = radios_qs.filter(defn['filter']).distinct().count()

    # Interactive feature filter
    active_feature = request.GET.get('feature', '')
    filtered_radios = None
    if active_feature in FEATURE_DEFS:
        filtered_radios = list(
            radios_qs.filter(FEATURE_DEFS[active_feature]['filter'])
            .distinct()
            .order_by('model')
        )

    # All brand radios — use filtered list when a feature filter is active.
    # Keep filtered_radios as a list even when empty so the template knows
    # the user applied a filter (vs no filter at all).
    if filtered_radios is not None:
        brand_radios = filtered_radios
    else:
        brand_radios = list(
            radios_qs.values(
                'id', 'model', 'fcc_id', 'grant_date', 'freq_bands_tx',
                'power_watts', 'radio_type',
            ).order_by('model')
        )

    logger.info(
        "User action brand_detail_view actor=%s brand_id=%s brand=%s "
        "feature=%s",
        _actor_label(request), brand.pk, brand.name, active_feature,
    )

    context = {
        'brand': brand,
        'manufacturer': manufacturer,
        'form': form,
        'edit_mode': edit_mode,
        'total_models': total_models,
        'newest_radio': newest_radio,
        'subsidiary_brands': subsidiary_brands,
        'white_label_names': white_label_names,
        'yearly_counts': yearly_counts,
        'yearly_counts_json': json.dumps([
            [int(r['year']), r['count']]
            for r in yearly_counts if r['year']
        ]),
        'max_year_count': max_year_count,
        'feature_counts': feature_counts,
        'feature_defs': FEATURE_DEFS,
        'active_feature': active_feature,
        'filtered_radios': filtered_radios,
        'brand_radios': brand_radios,
    }
    return render(request, 'radios/brand_detail.html', context)
