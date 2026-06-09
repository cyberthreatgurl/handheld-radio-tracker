import xmltodict
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from html import unescape
from datetime import datetime, time as datetime_time, timezone as datetime_timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup
from django.db.models import Q
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from radios.models import Brand, IgnoredGrantee, Manufacturer, Radio, RadioFCCTestReport, RadioManual, RadioOETDocument, normalize_grantee_code
from radios.fcc_id_utils import normalize_fcc_id_for_lookup, split_fcc_id
from radios.manual_extraction import extract_specs_from_text, extract_text_from_pdf_with_metadata
from radios.fcc_validation import validate_fcc_brand_assignment

URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"
GENERIC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm"
GENERIC_SEARCH_FORM_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
OET_EXHIBITS_URL = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
logger = logging.getLogger(__name__)

# Set to True within a process when the FCC site is completely unreachable
# (curl error 28 — 0 bytes received). Avoids burning retries for every
# subsequent FCC ID in the same sync run; resets to False on any success.
_fcc_connection_down = False

# Set to True when Playwright fails to reach the FCC site at all (initial
# page.goto times out). All subsequent Playwright calls in the same sync
# are skipped immediately — no browser is launched, no 30s waits burned.
# Resets to False at the start of each new sync.
_fcc_playwright_down = False


class _FCCConnectionDownError(Exception):
    """Raised instead of TimeoutError when the connection-down fast-fail fires.
    Lets callers distinguish an expected skip from a genuine unexpected error
    and suppress the noisy full-traceback ERROR log.
    """

DEFAULT_RADIO_ALLOWLIST_TERMS = "TRANSCEIVER,TRANSMITTER,RECEIVER,MURS,ORIGINAL EQUIPMENT"
RADIO_ALLOWLIST_ENV_NAME = "FCC_RADIO_ALLOWLIST_TERMS"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_OET_APP_ID_RE = re.compile(r'application_id=([A-Za-z0-9]+)', re.IGNORECASE)
FCC_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def _normalize_brand_identity(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _find_existing_grantee_brand(normalized_grantee_code, normalized_grantee_name):
    brand = Brand.objects.filter(grantee_code__iexact=normalized_grantee_code).first()
    if brand is not None:
        return brand

    for field_name in ('name', 'alias', 'full_name'):
        brand = Brand.objects.filter(**{f'{field_name}__iexact': normalized_grantee_name}).first()
        if brand is not None:
            return brand

    normalized_name_key = _normalize_brand_identity(normalized_grantee_name)
    if not normalized_name_key:
        return None

    blank_code_brands = Brand.objects.filter(Q(grantee_code__isnull=True) | Q(grantee_code__exact='')).only(
        'id', 'name', 'alias', 'full_name', 'grantee_code'
    )
    for candidate in blank_code_brands:
        for value in (candidate.name, candidate.alias, candidate.full_name):
            if _normalize_brand_identity(value) == normalized_name_key:
                return candidate

    return None


def _find_matching_blank_code_brand(normalized_grantee_name, exclude_brand_id=None):
    normalized_name_key = _normalize_brand_identity(normalized_grantee_name)
    if not normalized_name_key:
        return None

    blank_code_brands = Brand.objects.filter(Q(grantee_code__isnull=True) | Q(grantee_code__exact='')).only(
        'id', 'name', 'alias', 'full_name', 'grantee_code'
    )
    if exclude_brand_id is not None:
        blank_code_brands = blank_code_brands.exclude(pk=exclude_brand_id)

    for candidate in blank_code_brands:
        for value in (candidate.name, candidate.alias, candidate.full_name):
            if _normalize_brand_identity(value) == normalized_name_key:
                return candidate

    return None


def _resolve_authoritative_radio_brand_name(authoritative_brand, grantee_code, grantee_name):
    normalized_grantee_name = (grantee_name or '').strip()
    if authoritative_brand is None or not normalized_grantee_name:
        return normalized_grantee_name

    normalized_brand_name = _normalize_brand_identity(authoritative_brand.name)
    normalized_grantee_key = _normalize_brand_identity(normalized_grantee_name)
    if normalized_brand_name == normalized_grantee_key:
        return authoritative_brand.name

    authoritative_code = normalize_grantee_code(getattr(authoritative_brand, 'grantee_code', ''))
    if authoritative_code != normalize_grantee_code(grantee_code):
        return normalized_grantee_name

    matching_blank_brand = _find_matching_blank_code_brand(
        normalized_grantee_name,
        exclude_brand_id=authoritative_brand.id,
    )
    if matching_blank_brand is None:
        return normalized_grantee_name

    return authoritative_brand.name


def _is_connection_timeout_error(exc):
    """Return True when the exception is a network-level connection timeout
    (curl error 28: 0 bytes received).  These should NOT be retried — the
    host is unreachable and retrying burns time with no benefit."""
    msg = str(exc).lower()
    return 'curl: (28)' in msg or '0 bytes received' in msg


def _is_playwright_timeout_error(exc):
    """Return True for any Playwright or network timeout.

    Playwright raises two distinct error types:
    - ``playwright.TimeoutError`` (class name contains 'TimeoutError') for
      explicit wait/timeout calls.
    - ``playwright.Error`` (class name is just 'Error') for network-level
      failures such as ``net::ERR_TIMED_OUT``, whose message contains
      'timed_out' but NOT 'timeout'.
    """
    type_name = type(exc).__name__
    msg = str(exc).lower()
    return (
        'timeouterror' in type_name.lower()
        or 'timeout' in msg
        or 'timed_out' in msg
        or 'err_timed_out' in msg
    )


def _fcc_request_with_retry(method, url, *, session=None, retries=2, retry_delay=0.6, **kwargs):
    global _fcc_connection_down
    requester = session if session is not None else requests
    last_response = None

    # If a prior request in this process already hit a connection timeout,
    # skip the HTTP call entirely and raise immediately so callers fall
    # through to the Playwright path without wasting additional retries.
    if _fcc_connection_down:
        raise _FCCConnectionDownError('FCC site unreachable (connection-down flag set); skipping HTTP attempt')

    for attempt in range(retries + 1):
        try:
            response = getattr(requester, method)(url, **kwargs)
        except Exception as exc:
            if _is_connection_timeout_error(exc):
                # Network-level failure — don't retry, mark the site as down.
                _fcc_connection_down = True
                logger.warning(
                    'FCC request connection timeout (no bytes received) method=%s url=%s '
                    '— skipping retries and marking FCC site as unreachable',
                    method.upper(), url,
                )
                raise
            if attempt >= retries:
                raise
            logger.warning(
                "FCC request retry after exception method=%s url=%s attempt=%s retries=%s",
                method.upper(),
                url,
                attempt + 1,
                retries,
            )
            time.sleep(retry_delay * (attempt + 1))
            continue

        last_response = response
        # A successful response means the site is reachable — clear the flag.
        _fcc_connection_down = False
        if response.status_code not in FCC_RETRY_STATUS_CODES or attempt >= retries:
            return response

        logger.info(
            "FCC request retrying method=%s url=%s status=%s attempt=%s retries=%s",
            method.upper(),
            url,
            response.status_code,
            attempt + 1,
            retries,
        )
        time.sleep(retry_delay * (attempt + 1))

    return last_response


def _parse_allowlist_terms(value):
    terms = []
    for raw in (value or '').split(','):
        term = raw.strip().upper()
        if term and term not in terms:
            terms.append(term)
    return terms


def _radio_allowlist_terms():
    raw = os.environ.get(RADIO_ALLOWLIST_ENV_NAME, DEFAULT_RADIO_ALLOWLIST_TERMS)
    terms = _parse_allowlist_terms(raw)
    for required in _parse_allowlist_terms(DEFAULT_RADIO_ALLOWLIST_TERMS):
        if required not in terms:
            terms.append(required)
    return terms


def _iter_dict_nodes(payload):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_dict_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_dict_nodes(item)


def _extract_fcc_key(value):
    return _clean_query((value or '').replace('-', ''))


def _fcc_lookup_variants(fcc_id):
    variants = []
    for candidate in (
        (fcc_id or '').strip(),
        normalize_fcc_id_for_lookup(fcc_id),
        _extract_fcc_key(fcc_id),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _dict_text_blob(payload):
    values = []
    for value in payload.values():
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                values.append(text)
    return ' | '.join(values).upper()


def _extract_product_designation(payload):
    if not isinstance(payload, dict):
        return ''

    for key in ('product_designation', 'productDesignation', 'product_description', 'productDescription', 'equipment_description'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    blob = _dict_text_blob(payload)
    match = re.search(r'PRODUCT\s+DESIGNATION\s*[:\-]\s*([^|]{2,120})', blob, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ''


def _extract_urls_from_payload(payload):
    urls = []
    if not isinstance(payload, dict):
        return urls

    for key, value in payload.items():
        if isinstance(value, str):
            lower_key = str(key).lower()
            if lower_key.endswith('url') or lower_key.endswith('link') or 'href' in lower_key:
                trimmed = value.strip()
                if trimmed.lower().startswith('http'):
                    urls.append(trimmed)
            for found in URL_PATTERN.findall(value):
                urls.append(found)
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _is_original_equipment_purpose(value):
    return (value or '').strip().lower() == 'original equipment'


def _parse_year_from_grant_date(value):
    text = (value or '').strip()
    if not text:
        return None

    parsed = _parse_datetime_value(text)
    if parsed:
        return parsed.year

    match = re.search(r'(19|20)\d{2}', text)
    if match:
        return int(match.group(0))
    return None


def _parse_decimal(value):
    text = (value or '').strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, TypeError):
        return None


def _format_decimal_8(value):
    return f"{value.quantize(Decimal('0.00000000'))}"


def _extract_original_equipment_summary(primary_record, secondary_metadata):
    years = []
    frequency_ranges = []

    # Primary API record can contribute grant date + purpose.
    primary_purpose = primary_record.get('applicationPurpose', '') if isinstance(primary_record, dict) else ''
    if _is_original_equipment_purpose(primary_purpose):
        year = _parse_year_from_grant_date(primary_record.get('grantDate', ''))
        if year:
            years.append(year)

    # Secondary metadata can include grant date + purpose + frequency rows.
    for node in (secondary_metadata or {}).get('original_equipment_rows', []):
        year = _parse_year_from_grant_date(node.get('grant_date', ''))
        if year:
            years.append(year)

        lower = _parse_decimal(node.get('lower_freq_mhz', ''))
        upper = _parse_decimal(node.get('upper_freq_mhz', ''))
        if lower is None or upper is None:
            continue
        if lower > upper:
            lower, upper = upper, lower
        frequency_ranges.append((lower, upper))

    intro_year = min(years) if years else None

    # Choose the narrowest original-equipment frequency range as the representative TX band.
    # This favors the most specific emission band when FCC metadata contains broad + narrow entries.
    freq_bands_tx = ''
    if frequency_ranges:
        lower, upper = min(frequency_ranges, key=lambda pair: (pair[1] - pair[0], pair[0]))
        freq_bands_tx = f"{_format_decimal_8(lower)}-{_format_decimal_8(upper)} MHz"

    return {
        'intro_year': intro_year,
        'freq_bands_tx': freq_bands_tx,
    }


def _is_test_report_candidate(payload):
    blob = _dict_text_blob(payload)
    return any(
        marker in blob
        for marker in (
            'TEST REPORT',
            'TEST-REPORT',
            'EXHIBIT TYPE: TEST REPORT',
            'EXHIBIT DESCRIPTION: TEST REPORT',
        )
    )


def _extract_test_report_candidates(data, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    candidates = []
    seen = set()

    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )

        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        if not _is_test_report_candidate(node):
            continue

        urls = _extract_urls_from_payload(node)
        designation = _extract_product_designation(node)
        title = (
            node.get('exhibit_description')
            or node.get('exhibitDescription')
            or node.get('document_description')
            or node.get('documentDescription')
            or node.get('description')
            or 'FCC Test Report'
        )

        for url in urls:
            key = (url, title, designation)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    'url': url,
                    'title': str(title).strip(),
                    'product_designation': designation,
                }
            )

    return candidates


def _strip_html_tags(value):
    text = re.sub(r'<[^>]+>', ' ', value or '', flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', unescape(text)).strip()


def _parse_date_only(value):
    text = (value or '').strip()
    if not text:
        return None

    parsed = parse_date(text)
    if parsed:
        return parsed

    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_oet_documents_from_xml(data, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    documents = []
    seen = set()

    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )
        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        view_attachment = (
            node.get('view_attachment')
            or node.get('viewAttachment')
            or node.get('exhibit_description')
            or node.get('exhibitDescription')
            or node.get('description')
            or ''
        ).strip()
        exhibit_type = (
            node.get('exhibit_type')
            or node.get('exhibitType')
            or node.get('doc_type')
            or node.get('document_type')
            or ''
        ).strip()
        date_submitted = (
            node.get('date_submitted_to_fcc')
            or node.get('dateSubmittedToFcc')
            or node.get('date_submitted')
            or node.get('dateSubmitted')
            or ''
        ).strip()
        display_type = (
            node.get('display_type')
            or node.get('displayType')
            or node.get('file_type')
            or ''
        ).strip()
        date_available = (
            node.get('date_available')
            or node.get('dateAvailable')
            or ''
        ).strip()

        document_url = ''
        for url in _extract_urls_from_payload(node):
            lower_url = url.lower()
            if 'viewexhibitreport' in lower_url or 'report=' in lower_url or lower_url.endswith('.pdf'):
                document_url = url
                break

        if not any((view_attachment, exhibit_type, date_submitted, display_type, date_available, document_url)):
            continue

        key = (document_url, view_attachment, exhibit_type, date_submitted, display_type, date_available)
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': exhibit_type,
                'date_submitted_to_fcc': date_submitted,
                'display_type': display_type,
                'date_available': date_available,
                'document_url': document_url,
            }
        )

    return documents


def _extract_oet_documents_from_html(html_text, base_url):
    documents = []
    seen = set()

    soup = BeautifulSoup(html_text or '', 'html.parser')
    for row in soup.find_all('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) < 5:
            continue

        attachment_index = None
        document_url = ''
        for index, cell in enumerate(cells):
            link = cell.find(
                'a',
                href=lambda href: isinstance(href, str) and _is_fcc_attachment_document_url(href),
            )
            if link:
                attachment_index = index
                document_url = unescape(urljoin(base_url, link.get('href').strip()))
                break

            cell_html = str(cell)
            link_match = re.search(
                r'(?:["\'])(/oetcf/eas/reports/(?:GenericExhibit\.cfm|GetAttachment\.cfm|ViewAttachment\.cfm)[^"\'\s)]*|/eas/GetApplicationAttachment\.html[^"\'\s)]*)(?:["\'])',
                cell_html,
                flags=re.IGNORECASE,
            )
            if link_match:
                attachment_index = index
                document_url = unescape(urljoin(base_url, link_match.group(1).strip()))
                break

        if attachment_index is None or not document_url:
            continue

        row_cells = cells[attachment_index:attachment_index + 5]
        if len(row_cells) < 5:
            continue

        view_attachment = _strip_html_tags(row_cells[0].get_text(' ', strip=True) or '')
        exhibit_type = _strip_html_tags(row_cells[1].get_text(' ', strip=True) or '')
        date_submitted = _strip_html_tags(row_cells[2].get_text(' ', strip=True) or '')
        display_type = _strip_html_tags(row_cells[3].get_text(' ', strip=True) or '')
        date_available = _strip_html_tags(row_cells[4].get_text(' ', strip=True) or '')

        if not any((view_attachment, exhibit_type, date_submitted, display_type, date_available, document_url)):
            continue

        if not _is_fcc_attachment_document_url(document_url):
            continue

        if view_attachment.lower() == 'view attachment' and exhibit_type.lower() == 'exhibit type':
            continue

        key = (document_url, view_attachment, exhibit_type, date_submitted, display_type, date_available)
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': exhibit_type,
                'date_submitted_to_fcc': date_submitted,
                'display_type': display_type,
                'date_available': date_available,
                'document_url': document_url,
                'referer_url': base_url,
            }
        )

    return documents


def _extract_oet_documents_from_attachment_html(html_text, base_url):
    documents = []
    seen = set()

    soup = BeautifulSoup(html_text or '', 'html.parser')
    for link in soup.find_all('a', href=True):
        href = str(link.get('href') or '').strip()
        if not href:
            continue

        lower_href = href.lower()
        if (
            'getattachment.cfm' not in lower_href
            and 'genericexhibit.cfm' not in lower_href
            and 'getapplicationattachment.html' not in lower_href
        ):
            continue

        document_url = unescape(urljoin(base_url, href))
        view_attachment = _strip_html_tags(link.get_text(' ', strip=True) or '') or 'FCC Attachment'
        key = (document_url, view_attachment)
        if key in seen:
            continue
        seen.add(key)

        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': '',
                'date_submitted_to_fcc': '',
                'display_type': 'pdf',
                'date_available': '',
                'document_url': document_url,
                'referer_url': base_url,
            }
        )

    return documents


def _is_fcc_attachment_document_url(url):
    lower_url = (url or '').strip().lower()
    return any(
        token in lower_url
        for token in (
            'getattachment.cfm',
            'genericexhibit.cfm',
            'getapplicationattachment.html',
            'viewattachment.cfm',
        )
    )


def _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    matched_records = []
    matched_keys = set()
    original_equipment_rows = []
    candidate_exhibit_urls = []

    body_match = re.search(
        r'<tbody[^>]*id=["\']offTblBdy["\'][^>]*>(.*?)</tbody>',
        html_text or '',
        flags=re.IGNORECASE | re.DOTALL,
    )
    row_source = body_match.group(1) if body_match else (html_text or '')

    for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', row_source, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 16:
            continue

        row_fcc_id = _strip_html_tags(cells[11])
        if not row_fcc_id:
            continue
        if target_key and _extract_fcc_key(row_fcc_id) != target_key:
            continue

        application_purpose = _strip_html_tags(cells[12])
        grant_date = _strip_html_tags(cells[13])
        lower_freq = _strip_html_tags(cells[14])
        upper_freq = _strip_html_tags(cells[15])

        matched_records.append(
            ' | '.join(
                part for part in (
                    row_fcc_id,
                    application_purpose,
                    grant_date,
                    lower_freq,
                    upper_freq,
                )
                if part
            )
        )
        matched_keys.update(
            {
                'fcc_id',
                'application_purpose',
                'grant_date',
                'lower_freq_mhz',
                'upper_freq_mhz',
            }
        )

        for href in re.findall(r'href=["\']([^"\']+)["\']', cells[2], flags=re.IGNORECASE):
            url = unescape(urljoin(base_url, href.strip()))
            if 'ViewExhibitReport.cfm' in url:
                candidate_exhibit_urls.append(url)

        if _is_original_equipment_purpose(application_purpose):
            original_equipment_rows.append(
                {
                    'grant_date': grant_date,
                    'application_purpose': application_purpose,
                    'lower_freq_mhz': lower_freq,
                    'upper_freq_mhz': upper_freq,
                }
            )

    # NOTE: Intentionally no unconstrained application_id regex fallback here.
    # Grabbing the first application_id found anywhere in the HTML when no row
    # matched the target FCC ID caused cross-contamination (e.g. a grantee-wide
    # search showing 2AZSA-RT490's row alongside 2AZSA-RT950 would hand RT490's
    # application_id to the RT950 document fetch, attaching the wrong exhibits).
    # Callers receive an empty candidate_exhibit_urls list and should treat that
    # as "no OET exhibits found" rather than falling back to an unverified ID.

    return {
        'record_count': len(matched_records),
        'text_blob': ' || '.join(matched_records),
        'matched_keys': sorted(matched_keys),
        'original_equipment_rows': original_equipment_rows,
        'candidate_exhibit_urls': candidate_exhibit_urls,
    }


def _build_generic_search_payload(fcc_id):
    grantee_code, product_code = split_fcc_id(fcc_id)
    if not grantee_code or not product_code:
        return None

    return {
        'grantee_code': grantee_code,
        'product_code': product_code,
        'product_exact_match': '',
        'applicant_name': '',
        'grant_date_from': '',
        'grant_date_to': '',
        'comments': '',
        'application_purpose_description': '',
        'sdr_filings_only': '',
        'eas_apps_only': 'Y',
        'tcb_apps_only': '',
        'composite_apps_only': '',
        'test_firm': '',
        'application_status_description': '',
        'equipment_class_description': '',
        'lower_frequency': '',
        'upper_frequency': '',
        'freq_exact_match': '',
        'bandwidth_from': '',
        'emission_designator': '',
        'tolerance_from': '',
        'tolerance_to': '',
        'tolerance_exact_match': '',
        'power_output_from': '',
        'power_output_to': '',
        'power_exact_match': '',
        'rule_part_exact_match': '',
        'product_description': '',
        'modular_type_description': '',
        'modular_type_two': 'A',
    }


def _generic_search_headers():
    return {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': GENERIC_SEARCH_FORM_URL,
    }


def _submit_generic_search_form_via_playwright(fcc_id):
    global _fcc_playwright_down
    if _fcc_playwright_down:
        logger.info(
            'FCC browser fallback skipped fcc_id=%s reason=playwright_down',
            fcc_id,
        )
        return '', GENERIC_SEARCH_FORM_URL

    payload = _build_generic_search_payload(fcc_id)
    if payload is None:
        return '', GENERIC_SEARCH_FORM_URL

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info('FCC browser fallback unavailable fcc_id=%s reason=playwright_missing', fcc_id)
        return '', GENERIC_SEARCH_FORM_URL

    try:
        with sync_playwright() as playwright:
            browser = _launch_fcc_playwright_browser(playwright)
            page = _new_fcc_page(browser)
            page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
            page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
            page.locator('input[name="product_code"]').fill(payload['product_code'])
            exact_match = page.locator('input[name="product_exact_match"]')
            if exact_match.is_checked():
                exact_match.uncheck()
            page.locator('input[type="submit"][value="Start Search"]').click()
            page.wait_for_load_state('networkidle', timeout=30000)
            
            # Wait for results table to render
            try:
                page.wait_for_selector('tbody#offTblBdy', timeout=15000)
            except Exception:
                logger.info('FCC browser fallback search table wait timeout fcc_id=%s', fcc_id)
            
            html_text = page.content()
            current_url = page.url
            
            # Strategy 1: Look for links in the DOM — use evaluate() for a single
            # RPC round-trip instead of N get_attribute() calls (one per link).
            exhibit_links = []
            try:
                raw_hrefs = page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                        'a[href*="ViewExhibitReport.cfm"]'
                    )).filter(a => a.href.includes('application_id='))
                      .map(a => a.href).slice(0, 20)"""
                )
                for href in (raw_hrefs or []):
                    if href:
                        absolute_url = urljoin(current_url, href)
                        if absolute_url not in exhibit_links:
                            exhibit_links.append(absolute_url)
                            logger.info('FCC browser found exhibit link in DOM fcc_id=%s url=%s', fcc_id, absolute_url)
            except Exception:
                logger.exception('FCC browser fallback link extraction failed fcc_id=%s', fcc_id)
            
            # Strategy 2: Extract application_id from page HTML using regex
            if not exhibit_links:
                app_id_pattern = r'application_id=([A-Za-z0-9%+/=]+)'
                matches = re.findall(app_id_pattern, html_text)
                if matches:
                    # Use the first application_id found
                    app_id = matches[0]
                    constructed_url = f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500&calledFromFrame=N&application_id={app_id}&fcc_id={fcc_id}"
                    exhibit_links.append(constructed_url)
                    logger.info('FCC browser extracted application_id from HTML fcc_id=%s app_id=%s', fcc_id, app_id[:20])
            
            # Strategy 3: Try clicking on the first row to trigger navigation
            if not exhibit_links:
                try:
                    first_link = page.query_selector('tbody#offTblBdy tr td a')
                    if first_link:
                        href = first_link.get_attribute('href')
                        if href:
                            absolute_url = urljoin(current_url, href)
                            if 'ViewExhibitReport.cfm' in absolute_url:
                                exhibit_links.append(absolute_url)
                                logger.info('FCC browser found exhibit link via first row fcc_id=%s', fcc_id)
                except Exception:
                    logger.info('FCC browser first row click attempt failed fcc_id=%s', fcc_id)

            # --- Background: FCC grantee/product code rules -------------------------
            # FCC IDs beginning with a digit have a 5-character grantee code;
            # those beginning with a letter have a 3-character grantee code.
            # split_fcc_id() applies these rules and always returns the product code
            # WITHOUT the separator dash (e.g. grantee='2AJGM', product='UV5RPRO').
            # However the FCC's own search database sometimes stores the product code
            # WITH a leading dash (e.g. product_code='-UV5RPRO').  Searching without
            # the dash returns "no applications on file" while searching with the dash
            # returns the correct record.  Strategy 4a and Strategy 5 both cover this
            # by retrying with f"-{product_code}" whenever the target FCC ID is not
            # found among the collected exhibit links.
            # -----------------------------------------------------------------------

            # Strategy 4a: Hyphen-prefixed product code — fast targeted retry.
            # When the initial search found no exhibit links, try the same grantee
            # with the product code prefixed by '-' before falling back to the much
            # more expensive grantee-wide search.  This covers radios like
            # 2AJGM-UV5RPRO where the FCC stores the product code as '-UV5RPRO'.
            if not exhibit_links and not payload['product_code'].startswith('-'):
                hyphen_product_code_4a = f"-{payload['product_code']}"
                logger.info(
                    'FCC browser fallback hyphen-product early retry fcc_id=%s product_code=%s',
                    fcc_id, hyphen_product_code_4a,
                )
                try:
                    page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                    page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                    page.locator('input[name="product_code"]').fill(hyphen_product_code_4a)
                    hp_exact_4a = page.locator('input[name="product_exact_match"]')
                    if hp_exact_4a.is_checked():
                        hp_exact_4a.uncheck()
                    page.locator('input[type="submit"][value="Start Search"]').click()
                    page.wait_for_load_state('networkidle', timeout=30000)
                    try:
                        page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                    except Exception:
                        pass
                    hp_html_4a = page.content()
                    hp_url_4a = page.url
                    fcc_id_upper_4a = (fcc_id or '').upper()
                    if fcc_id_upper_4a in (hp_html_4a or '').upper():
                        logger.info(
                            'FCC browser hyphen-product early retry found target fcc_id=%s',
                            fcc_id,
                        )
                        html_text = hp_html_4a
                        current_url = hp_url_4a
                        raw_hrefs = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'a[href*="ViewExhibitReport.cfm"]'
                            )).filter(a => a.href.includes('application_id='))
                              .map(a => a.href).slice(0, 20)"""
                        )
                        for href in (raw_hrefs or []):
                            if href:
                                absolute_url = urljoin(hp_url_4a, href)
                                if absolute_url not in exhibit_links:
                                    exhibit_links.append(absolute_url)
                    else:
                        logger.info(
                            'FCC browser hyphen-product early retry no match fcc_id=%s — falling back to grantee-only',
                            fcc_id,
                        )
                except Exception:
                    logger.exception('FCC browser hyphen-product early retry failed fcc_id=%s', fcc_id)

            # Strategy 4b: Grantee-only retry — when the product_code search (both
            # normal and hyphen-prefix) returns no matching exhibit links, search by
            # grantee code alone so the FCC returns ALL records for this grantee.
            # The HTML parser upstream filters for the matching FCC ID row and
            # extracts the application_id link.  Only run this when a "no applications
            # on file" message was in the original response, confirming that the
            # per-product search truly returned nothing rather than timing out.
            if not exhibit_links and 'no applications on file' in html_text.lower():
                logger.info(
                    'FCC browser fallback grantee-only retry fcc_id=%s grantee=%s',
                    fcc_id, payload['grantee_code'],
                )
                try:
                    page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                    page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                    # Leave product_code empty so the FCC returns all records for this grantee
                    grantee_exact = page.locator('input[name="product_exact_match"]')
                    if grantee_exact.is_checked():
                        grantee_exact.uncheck()
                    page.locator('input[type="submit"][value="Start Search"]').click()
                    page.wait_for_load_state('networkidle', timeout=30000)
                    try:
                        page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                    except Exception:
                        logger.info('FCC browser grantee-only table wait timeout fcc_id=%s', fcc_id)
                    html_text = page.content()
                    current_url = page.url
                    logger.info(
                        'FCC browser grantee-only results fcc_id=%s url=%s has_table=%s',
                        fcc_id, current_url, 'offTblBdy' in html_text,
                    )
                    # Extract exhibit links from the grantee-wide results — single
                    # evaluate() RPC instead of 50 individual get_attribute() calls.
                    try:
                        raw_hrefs = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'a[href*="ViewExhibitReport.cfm"]'
                            )).filter(a => a.href.includes('application_id='))
                              .map(a => a.href).slice(0, 50)"""
                        )
                        for href in (raw_hrefs or []):
                            if href:
                                absolute_url = urljoin(current_url, href)
                                if absolute_url not in exhibit_links:
                                    exhibit_links.append(absolute_url)
                    except Exception:
                        logger.exception('FCC browser grantee-only link extraction failed fcc_id=%s', fcc_id)
                except Exception:
                    logger.exception('FCC browser grantee-only retry failed fcc_id=%s', fcc_id)

            # Strategy 5: Hyphen-prefixed product code retry after grantee-only — for
            # radios whose FCC product code is stored with a leading hyphen but whose
            # grantee-only search returned links for OTHER radios of the same grantee.
            # Only runs when the target FCC ID is not already in any collected exhibit
            # link.  Not needed if Strategy 4a already found the target.
            fcc_id_upper = (fcc_id or '').upper()
            target_link_found = any(fcc_id_upper in (lnk or '').upper() for lnk in exhibit_links)
            if not target_link_found and not payload['product_code'].startswith('-'):
                hyphen_product_code = f"-{payload['product_code']}"
                logger.info(
                    'FCC browser fallback hyphen-product retry fcc_id=%s product_code=%s',
                    fcc_id, hyphen_product_code,
                )
                try:
                    page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                    page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                    page.locator('input[name="product_code"]').fill(hyphen_product_code)
                    hyphen_exact = page.locator('input[name="product_exact_match"]')
                    if hyphen_exact.is_checked():
                        hyphen_exact.uncheck()
                    page.locator('input[type="submit"][value="Start Search"]').click()
                    page.wait_for_load_state('networkidle', timeout=30000)
                    try:
                        page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                    except Exception:
                        logger.info('FCC browser hyphen-product table wait timeout fcc_id=%s', fcc_id)
                    hyphen_html = page.content()
                    hyphen_url = page.url
                    fcc_id_upper = (fcc_id or '').upper()
                    logger.info(
                        'FCC browser hyphen-product results fcc_id=%s has_target=%s',
                        fcc_id, fcc_id_upper in (hyphen_html or '').upper(),
                    )
                    if fcc_id_upper in (hyphen_html or '').upper():
                        # This search found the target — capture its exhibit links and
                        # replace the current html_text so the caller's HTML-level parse
                        # also sees the correct search-result page.
                        html_text = hyphen_html
                        current_url = hyphen_url
                        try:
                            raw_hrefs = page.evaluate(
                                """() => Array.from(document.querySelectorAll(
                                    'a[href*="ViewExhibitReport.cfm"]'
                                )).filter(a => a.href.includes('application_id='))
                                  .map(a => a.href).slice(0, 20)"""
                            )
                            for href in (raw_hrefs or []):
                                if href:
                                    absolute_url = urljoin(hyphen_url, href)
                                    if absolute_url not in exhibit_links:
                                        exhibit_links.insert(0, absolute_url)
                                        logger.info(
                                            'FCC browser hyphen-product exhibit link fcc_id=%s url=%s',
                                            fcc_id, absolute_url,
                                        )
                        except Exception:
                            logger.exception('FCC browser hyphen-product link extraction failed fcc_id=%s', fcc_id)
                except Exception:
                    logger.exception('FCC browser hyphen-product retry failed fcc_id=%s', fcc_id)

            browser.close()
            logger.info('FCC browser fallback search success fcc_id=%s url=%s has_detail=%s exhibit_links=%s', 
                       fcc_id, current_url, 'ViewExhibitReport.cfm' in html_text, len(exhibit_links))
            return html_text, current_url
    except Exception as exc:
        if _is_playwright_timeout_error(exc):
            _fcc_playwright_down = True
            logger.warning(
                'FCC browser fallback search timeout fcc_id=%s — setting playwright_down flag',
                fcc_id,
            )
        else:
            logger.exception('FCC browser fallback search failed fcc_id=%s', fcc_id)
        return '', GENERIC_SEARCH_FORM_URL


def _launch_fcc_playwright_browser(playwright):
    headless_env = os.environ.get('FCC_PLAYWRIGHT_HEADLESS')
    # Default to headless so browser windows never steal focus during background syncs.
    # Set FCC_PLAYWRIGHT_HEADLESS=0 in the environment to show the browser for debugging.
    preferred_headless = True
    if headless_env is not None:
        preferred_headless = headless_env.strip().lower() not in {'0', 'false', 'no', 'off'}

    # Anti-bot-detection args: remove the AutomationControlled flag that FCC's server
    # uses to detect and stall headless browsers, causing networkidle timeouts.
    stealth_args = [
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
    ]
    launch_attempts = [
        {'channel': 'chrome', 'headless': preferred_headless, 'args': stealth_args + ['--disable-http2']},
        {'channel': 'chrome', 'headless': preferred_headless, 'args': stealth_args},
        {'headless': preferred_headless, 'args': stealth_args + ['--disable-http2']},
        {'headless': preferred_headless, 'args': stealth_args},
        {'headless': preferred_headless},
    ]
    last_error = None
    for launch_kwargs in launch_attempts:
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            last_error = exc
            logger.info('FCC browser launch attempt failed kwargs=%s error=%s', launch_kwargs, exc)
    if last_error is not None:
        raise last_error
    raise RuntimeError('Unable to launch FCC browser fallback')


def _new_fcc_page(browser):
    """Create a new browser page with anti-bot-detection context settings.

    FCC's CFML server detects headless Chrome via navigator.webdriver and stalls
    the connection, causing networkidle timeouts.  Overriding the property in every
    new document (add_init_script) and using a realistic viewport/user-agent reduces
    the chance of being fingerprinted as an automated browser.
    """
    context = browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        viewport={'width': 1280, 'height': 800},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context.new_page()


def _submit_generic_search_form(fcc_id):
    payload = _build_generic_search_payload(fcc_id)
    if payload is None:
        return '', GENERIC_SEARCH_FORM_URL

    headers = _generic_search_headers()
    session = requests.Session()

    try:
        _fcc_request_with_retry(
            'get',
            GENERIC_SEARCH_FORM_URL,
            session=session,
            headers=headers,
            impersonate='chrome124',
            timeout=20,
        )
    except _FCCConnectionDownError:
        logger.info('FCC generic search form seed skipped fcc_id=%s reason=connection_down', fcc_id)
    except Exception:
        logger.exception('FCC generic search form seed failed fcc_id=%s', fcc_id)

    try:
        response = _fcc_request_with_retry(
            'post',
            GENERIC_SEARCH_URL,
            session=session,
            data=payload,
            headers=headers,
            impersonate='chrome124',
            timeout=20,
        )
    except _FCCConnectionDownError:
        logger.info('FCC generic search form submit skipped fcc_id=%s reason=connection_down — falling back to Playwright', fcc_id)
        return _submit_generic_search_form_via_playwright(fcc_id)
    except Exception:
        logger.exception('FCC generic search form submit failed fcc_id=%s', fcc_id)
        return _submit_generic_search_form_via_playwright(fcc_id)

    if response.status_code != 200:
        logger.info(
            'FCC generic search form non-200 fcc_id=%s status=%s',
            fcc_id,
            response.status_code,
        )
        return _submit_generic_search_form_via_playwright(fcc_id)

    if 'ViewExhibitReport.cfm' not in (response.text or ''):
        body_text = ' '.join((response.text or '').split())[:220]
        logger.info(
            'FCC generic search form returned no detail link fcc_id=%s body_snippet=%s',
            fcc_id,
            body_text,
        )
        # Before falling back to the browser, try the hyphen-prefixed product code.
        # The FCC database stores some product codes with a leading '-' (e.g.
        # '-UV5RPRO'), so a search with 'UV5RPRO' returns nothing while '-UV5RPRO'
        # returns the correct record.  split_fcc_id() always strips the separator
        # dash from the product, so we must explicitly retry with the dash here.
        if not payload['product_code'].startswith('-'):
            hyphen_payload = dict(payload, product_code=f"-{payload['product_code']}")
            try:
                hyphen_response = _fcc_request_with_retry(
                    'post',
                    GENERIC_SEARCH_URL,
                    session=session,
                    data=hyphen_payload,
                    headers=headers,
                    impersonate='chrome124',
                    timeout=20,
                )
                if hyphen_response.status_code == 200 and 'ViewExhibitReport.cfm' in (hyphen_response.text or ''):
                    logger.info(
                        'FCC generic search form hyphen-product found detail link fcc_id=%s',
                        fcc_id,
                    )
                    return hyphen_response.text, GENERIC_SEARCH_FORM_URL
            except Exception:
                pass  # fall through to browser
        browser_html, browser_url = _submit_generic_search_form_via_playwright(fcc_id)
        if browser_html:
            return browser_html, browser_url

    return response.text or '', GENERIC_SEARCH_FORM_URL


def _fetch_oet_documents_via_playwright(fcc_id, candidate_urls=None, _allow_grantee_fallback=True):
    if _fcc_playwright_down:
        logger.info(
            'FCC browser OET skipped fcc_id=%s reason=playwright_down',
            fcc_id,
        )
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info('FCC browser fallback unavailable for OET docs fcc_id=%s reason=playwright_missing', fcc_id)
        return []

    detail_urls = []
    seen = set()
    for url in candidate_urls or []:
        if isinstance(url, str) and url.strip() and url not in seen:
            seen.add(url)
            detail_urls.append(url)

    if not detail_urls:
        html_text, base_url = _submit_generic_search_form_via_playwright(fcc_id)
        if html_text:
            parsed = _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id)
            for url in parsed.get('candidate_exhibit_urls', []):
                if url not in seen:
                    seen.add(url)
                    detail_urls.append(url)

        if not detail_urls:
            # Generic search form could not produce exhibit URLs.
            # Fall back to navigating the browser directly to the ViewExhibitReport
            # URL variants — the browser bypasses the 503 that blocks curl_cffi.
            for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
                direct_url = (
                    f"{OET_EXHIBITS_URL}?mode=Exhibits"
                    f"&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
                )
                if direct_url not in seen:
                    seen.add(direct_url)
                    detail_urls.append(direct_url)
            logger.info(
                'FCC browser OET falling back to direct ViewExhibitReport navigation fcc_id=%s url_count=%s',
                fcc_id, len(detail_urls),
            )
        else:
            logger.info('FCC browser fallback extracted exhibit URLs fcc_id=%s url_count=%s', fcc_id, len(detail_urls))

    if not detail_urls:
        return []

    try:
        with sync_playwright() as playwright:
            browser = _launch_fcc_playwright_browser(playwright)
            page = _new_fcc_page(browser)
            consecutive_timeouts = 0
            for url in detail_urls[:10]:
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    consecutive_timeouts = 0
                    html_text = page.content()
                    current_url = page.url

                    # If the page redirected to a ViewExhibitReport with application_id, capture it
                    if 'application_id=' in current_url and current_url not in seen:
                        logger.info(
                            'FCC browser OET redirect captured fcc_id=%s redirect_url=%s',
                            fcc_id, current_url,
                        )

                    documents = _extract_oet_documents_from_html(html_text, base_url=current_url)
                    documents += _extract_oet_documents_from_attachment_html(html_text, base_url=current_url)

                    # If no documents found yet, give JS a short window to finish rendering
                    # before reading content a second time (avoids full 10s networkidle wait).
                    if not documents:
                        try:
                            page.wait_for_load_state('networkidle', timeout=3000)
                        except Exception:
                            pass
                        html_text = page.content()
                        current_url = page.url
                        documents = _extract_oet_documents_from_html(html_text, base_url=current_url)
                        documents += _extract_oet_documents_from_attachment_html(html_text, base_url=current_url)

                    if documents:
                        # Verify the exhibit page actually references the target FCC ID.
                        # FCC sometimes redirects fcc_id=X queries to a different
                        # application (e.g. a Change-in-ID's original grant), and the
                        # documents on that page belong to the *original* FCC ID, not X.
                        # Check both the hyphenated form ("2AZSA-RT950", as FCC renders
                        # it in page text) and the stripped key ("2AZSART950") since
                        # some pages omit the hyphen.  If neither appears, reject the
                        # documents to prevent cross-contamination (RT950 ← RT490).
                        html_upper = (html_text or '').upper()
                        fcc_id_upper = (fcc_id or '').upper()
                        target_fcc_key = _extract_fcc_key(fcc_id)
                        fcc_id_in_page = (
                            fcc_id_upper in html_upper or
                            (target_fcc_key and target_fcc_key in html_upper)
                        )
                        if not fcc_id_in_page:
                            logger.warning(
                                'FCC exhibit page fcc_id_mismatch: target=%s not found in page HTML '
                                'page_url=%s — rejecting %s docs to prevent cross-contamination',
                                fcc_id, current_url, len(documents),
                            )
                            documents = []

                    if documents:
                        # Prefer docs with full metadata (exhibit_type filled in, from the
                        # structured exhibit table) over docs that came purely from bare
                        # attachment links (no exhibit_type — from _extract_oet_documents_from_attachment_html).
                        # If we have at least one doc with exhibit_type, return immediately.
                        # If ALL docs lack exhibit_type, fall through to the app_links
                        # step so we can try navigating to the application_id exhibit
                        # page and get the full table there.
                        has_metadata = any(d.get('exhibit_type') for d in documents)
                        if has_metadata:
                            browser.close()
                            logger.info('FCC browser fallback OET success fcc_id=%s url=%s document_count=%s', fcc_id, url, len(documents))
                            return documents
                        # Attachment-only docs found — keep them as a fallback but
                        # continue to the app_links step for better data.
                        attachment_only_fallback = list(documents)
                        logger.info(
                            'FCC browser OET attachment-only docs found fcc_id=%s url=%s count=%s — '
                            'trying application_id link for full metadata',
                            fcc_id, url, len(attachment_only_fallback),
                        )
                    else:
                        attachment_only_fallback = []

                    # If page itself contains an application_id link but no documents table yet,
                    # try following that link within the same browser session
                    app_links = page.query_selector_all('a[href*="ViewExhibitReport.cfm"][href*="application_id="]')
                    for link in app_links[:5]:
                        href = link.get_attribute('href')
                        if not href:
                            continue
                        abs_href = urljoin(current_url, href)
                        if abs_href in seen:
                            continue
                        seen.add(abs_href)
                        logger.info('FCC browser OET following application_id link fcc_id=%s url=%s', fcc_id, abs_href)
                        page.goto(abs_href, wait_until='domcontentloaded', timeout=30000)
                        inner_html = page.content()
                        inner_url = page.url
                        docs = _extract_oet_documents_from_html(inner_html, base_url=inner_url)
                        docs += _extract_oet_documents_from_attachment_html(inner_html, base_url=inner_url)
                        if not docs:
                            try:
                                page.wait_for_load_state('networkidle', timeout=3000)
                            except Exception:
                                pass
                            inner_html = page.content()
                            inner_url = page.url
                            docs = _extract_oet_documents_from_html(inner_html, base_url=inner_url)
                            docs += _extract_oet_documents_from_attachment_html(inner_html, base_url=inner_url)
                        if docs:
                            inner_upper = (inner_html or '').upper()
                            fcc_id_upper = (fcc_id or '').upper()
                            target_fcc_key = _extract_fcc_key(fcc_id)
                            fcc_id_in_inner = (
                                fcc_id_upper in inner_upper or
                                (target_fcc_key and target_fcc_key in inner_upper)
                            )
                            if not fcc_id_in_inner:
                                logger.warning(
                                    'FCC OET inner link fcc_id_mismatch: target=%s not in page HTML '
                                    'inner_url=%s — rejecting %s docs to prevent cross-contamination',
                                    fcc_id, inner_url, len(docs),
                                )
                                docs = []
                        if docs:
                            browser.close()
                            logger.info('FCC browser OET application_id link success fcc_id=%s url=%s document_count=%s', fcc_id, abs_href, len(docs))
                            return docs

                    # app_links exhausted — if we had attachment-only docs (no exhibit metadata),
                    # use them as last resort rather than moving on to the next candidate URL.
                    if attachment_only_fallback:
                        browser.close()
                        logger.info(
                            'FCC browser OET using attachment-only fallback fcc_id=%s url=%s count=%s',
                            fcc_id, url, len(attachment_only_fallback),
                        )
                        return attachment_only_fallback

                except Exception as exc:
                    if _is_playwright_timeout_error(exc):
                        consecutive_timeouts += 1
                        logger.warning(
                            'FCC browser OET page load timeout fcc_id=%s url=%s consecutive=%s',
                            fcc_id, url, consecutive_timeouts,
                        )
                    else:
                        consecutive_timeouts = 0
                        logger.exception('FCC browser OET page load failed fcc_id=%s url=%s', fcc_id, url)
                    if consecutive_timeouts >= 2:
                        logger.warning(
                            'FCC browser OET circuit breaker triggered fcc_id=%s '
                            'consecutive_timeouts=%s — skipping remaining direct URLs',
                            fcc_id, consecutive_timeouts,
                        )
                        break
                    continue

            browser.close()
    except Exception:
        logger.exception('FCC browser fallback OET failed fcc_id=%s', fcc_id)

    # Nothing found via direct URL navigation.  Fall back to the grantee-only
    # browser search so we can discover the application_id through the FCC's
    # search results table and then navigate to the correct exhibit page.
    if not _allow_grantee_fallback:
        # Already inside a grantee-fallback attempt; don't recurse again.
        return []

    logger.info('FCC browser OET direct URLs exhausted, trying grantee search fcc_id=%s', fcc_id)
    grantee_html, grantee_base = _submit_generic_search_form_via_playwright(fcc_id)
    if grantee_html:
        parsed = _extract_secondary_metadata_from_generic_search_html(grantee_html, grantee_base, fcc_id)
        # Row-filtered candidates (highest confidence — from the target FCC ID row)
        filtered_urls = parsed.get('candidate_exhibit_urls', [])

        # Broaden to ALL ViewExhibitReport.cfm links on the grantee page.
        # For Change-in-ID grants the target FCC ID row in the search table may
        # point to a page with no parseable documents; the correct exhibit page
        # (with the real application_id) exists elsewhere on the same page.
        # FCC ID verification at navigation time (below) guards against
        # cross-contamination from other grantees' exhibit links.
        all_exhibit_urls = list(filtered_urls)
        for href_raw in re.findall(
            r'href=["\']([^"\']*ViewExhibitReport\.cfm[^"\']*application_id=[^"\']+)["\']',
            grantee_html, re.IGNORECASE,
        ):
            abs_url = unescape(urljoin(grantee_base, href_raw.strip()))
            if abs_url not in all_exhibit_urls:
                all_exhibit_urls.append(abs_url)

        # Last-resort: direct fcc_id navigation URLs (no application_id required).
        # For Change-in-ID applications the exhibit page application_id may not
        # appear anywhere on the grantee search results page, but the FCC server
        # will resolve ?fcc_id=<target> to the correct exhibit list.  These are
        # added at the end so they are only reached when no application_id link
        # in the search table matched.
        for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
            direct_url = (
                f"{OET_EXHIBITS_URL}?mode=Exhibits"
                f"&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
            )
            if direct_url not in all_exhibit_urls:
                all_exhibit_urls.append(direct_url)

        if all_exhibit_urls:
            logger.info(
                'FCC OET grantee fallback candidate_count=%s (filtered=%s all=%s direct=%s) fcc_id=%s',
                len(all_exhibit_urls), len(filtered_urls), len(all_exhibit_urls),
                sum(1 for u in all_exhibit_urls if 'application_id' not in u),
                fcc_id,
            )
            return _fetch_oet_documents_via_playwright(
                fcc_id,
                candidate_urls=all_exhibit_urls,
                _allow_grantee_fallback=False,  # prevent re-entrant grantee search
            )

    return []


def _fetch_secondary_metadata_from_html_fallback(fcc_id, params):
    # Skip all network calls if both HTTP and Playwright are known-down.
    if _fcc_connection_down and _fcc_playwright_down:
        logger.info(
            'FCC secondary metadata html fallback skipped fcc_id=%s reason=connection_and_playwright_down',
            fcc_id,
        )
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
        }

    html_text, base_url = _submit_generic_search_form(fcc_id)
    if not html_text:
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
        }

    parsed = _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id)
    oet_documents = _fetch_oet_documents_from_html(
        fcc_id,
        candidate_urls=parsed.get('candidate_exhibit_urls', []),
    )

    return {
        'record_count': parsed.get('record_count', 0),
        'text_blob': parsed.get('text_blob', ''),
        'matched_keys': parsed.get('matched_keys', []),
        'test_report_candidates': [],
        'original_equipment_rows': parsed.get('original_equipment_rows', []),
        'oet_documents': oet_documents,
    }


def _fetch_oet_documents_from_html(fcc_id, candidate_urls=None):
    # Collect candidate URLs (from metadata parse) plus direct fcc_id variants.
    # curl_cffi gets 503 from all CFML endpoints (ViewExhibitReport.cfm,
    # GenericSearchResult.cfm) so we skip the HTTP loop entirely and hand
    # everything straight to the Playwright path, which uses a real browser.
    seen = set()
    deduped_urls = []
    for url in list(candidate_urls or []):
        if isinstance(url, str) and url.strip() and url not in seen:
            seen.add(url)
            deduped_urls.append(url.strip())

    for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
        url = f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
        if url not in seen:
            seen.add(url)
            deduped_urls.append(url)

    logger.info(
        "FCC OET fetch start fcc_id=%s candidate_url_count=%s",
        fcc_id,
        len(deduped_urls),
    )

    browser_docs = _fetch_oet_documents_via_playwright(fcc_id, candidate_urls=deduped_urls)
    if browser_docs:
        logger.info(
            'FCC OET browser fallback success fcc_id=%s document_count=%s',
            fcc_id,
            len(browser_docs),
        )
        return browser_docs

    return []


def _fetch_oet_documents_via_generic_search_form(fcc_id):
    html_text, base_url = _submit_generic_search_form(fcc_id)
    if not html_text:
        return []

    soup = BeautifulSoup(html_text, 'html.parser')

    detail_urls = []
    direct_docs = []
    seen_detail = set()
    seen_direct = set()
    for link in soup.find_all('a', href=True):
        href = str(link.get('href') or '').strip()
        if not href:
            continue

        lower_href = href.lower()
        absolute = urljoin(base_url, href)
        if 'viewattachment.cfm' in lower_href or 'viewexhibitreport.cfm' in lower_href:
            if absolute not in seen_detail:
                seen_detail.add(absolute)
                detail_urls.append(absolute)
        elif 'getattachment.cfm' in lower_href or 'genericexhibit.cfm' in lower_href:
            if absolute not in seen_direct:
                seen_direct.add(absolute)
                direct_docs.append(
                    {
                        'view_attachment': _strip_html_tags(link.get_text(' ', strip=True) or '') or 'FCC Attachment',
                        'exhibit_type': '',
                        'date_submitted_to_fcc': '',
                        'display_type': 'pdf',
                        'date_available': '',
                        'document_url': absolute,
                    }
                )

    documents = []
    seen_docs = set()
    for doc in direct_docs:
        key = (doc.get('document_url', ''), doc.get('view_attachment', ''))
        if key in seen_docs:
            continue
        seen_docs.add(key)
        documents.append(doc)

    for detail_url in detail_urls[:20]:
        try:
            detail_response = _fcc_request_with_retry('get', detail_url, impersonate='chrome124', timeout=20)
        except Exception:
            logger.exception("FCC OET form fallback detail fetch failed fcc_id=%s url=%s", fcc_id, detail_url)
            continue

        if detail_response.status_code != 200:
            logger.info(
                "FCC OET form fallback detail non-200 fcc_id=%s status=%s url=%s",
                fcc_id,
                detail_response.status_code,
                detail_url,
            )
            continue

        extracted = _extract_oet_documents_from_html(detail_response.text or '', base_url=detail_url)
        extracted += _extract_oet_documents_from_attachment_html(detail_response.text or '', base_url=detail_url)
        for doc in extracted:
            key = (doc.get('document_url', ''), doc.get('view_attachment', ''))
            if key in seen_docs:
                continue
            seen_docs.add(key)
            documents.append(doc)

    return documents


def _is_fcc_authoritative_url(url):
    host = (urlparse(url).hostname or '').lower()
    return host.endswith('fcc.gov')


def _build_oet_document_filename(fcc_id, view_attachment, document_url, display_type):
    parsed = urlparse(document_url or '')
    path_name = Path(parsed.path).name
    stem = path_name or _safe_filename(view_attachment or '') or 'oet_document'
    stem = _safe_filename(stem) or 'oet_document'

    display = (display_type or '').strip().lower()
    display_ext = f'.{display}' if display in {'pdf', 'doc', 'docx', 'txt'} else '.pdf'

    suffix = Path(stem).suffix.lower()
    if not suffix:
        candidate = f"{stem}{display_ext}"
    elif suffix in {'.cfm', '.php', '.asp', '.aspx', '.html', '.htm'}:
        candidate = f"{Path(stem).stem}{display_ext}"
    else:
        candidate = stem

    prefix = _safe_filename(fcc_id or 'fccid') or 'fccid'
    final_name = f"{prefix}_{candidate}"
    return final_name[:220]


def _download_oet_document_bytes(url, referer_url=''):
    if not _is_fcc_authoritative_url(url):
        logger.warning("FCC OET download skipped non-authoritative url=%s", url)
        return b''

    headers = None
    if referer_url:
        headers = {
            'Referer': referer_url,
            'User-Agent': _generic_search_headers()['User-Agent'],
        }

    try:
        response = _fcc_request_with_retry('get', url, impersonate='chrome124', timeout=25, headers=headers)
    except Exception:
        logger.exception("FCC OET document download failed url=%s", url)
        return b''

    if response.status_code != 200:
        logger.info("FCC OET document download non-200 url=%s status=%s", url, response.status_code)
        return b''

    content = response.content or b''
    if not content:
        return b''

    content_prefix = content[:200].lstrip().lower()
    if (
        b'you are not authorized to access this page' in content_prefix
        or content_prefix.startswith(b'<!doctype html')
        or content_prefix.startswith(b'<html')
    ):
        logger.info('FCC OET document download rejected url=%s referer=%s', url, referer_url)
        return b''

    return content


def _classify_oet_document_type(exhibit_type, view_attachment):
    blob = ' '.join(
        part.strip().lower()
        for part in (exhibit_type or '', view_attachment or '')
        if part and part.strip()
    )
    if not blob:
        return ''

    if 'test report' in blob:
        return RadioManual.DocType.TEST_REPORT
    if 'change in identification' in blob or 'change letter' in blob:
        return RadioManual.DocType.CHANGE_IN_ID
    if 'authorization' in blob or 'grant of equipment authorization' in blob:
        return RadioManual.DocType.AUTHORIZATION
    if 'firmware' in blob:
        return RadioManual.DocType.FIRMWARE
    if 'cps' in blob or 'programming software' in blob:
        return RadioManual.DocType.CPS
    if 'manual' in blob or 'user guide' in blob or 'instruction' in blob:
        return RadioManual.DocType.MANUAL
    return ''


def _sync_manual_record_for_oet_document(radio, fcc_id, oet_doc):
    doc_type = _classify_oet_document_type(oet_doc.exhibit_type, oet_doc.view_attachment)
    if not doc_type:
        return False

    source_url = (oet_doc.document_url or '').strip()
    if not source_url and not oet_doc.document_file:
        return False

    manual_doc = RadioManual.objects.filter(radio=radio, source_url=source_url).first()
    created = False
    if manual_doc is None:
        manual_doc = RadioManual(radio=radio, source_url=source_url)
        created = True

    # When the manual already has a PDF, all the expensive work (download,
    # parse, spec backfill) was done on the first pass.  Exit early so that
    # duplicate OET document entries in the exhibit list don't trigger a
    # second PDF parse for the same record.
    if not created and manual_doc.manual_pdf:
        return False

    manual_doc.doc_type = doc_type
    manual_doc.status = RadioManual.ProcessingStatus.LINKED
    manual_doc.extraction_confidence = 1.0
    # Merge with existing extracted_data so that spec_extraction (written by
    # _backfill_radio_specs_from_manual_doc on the first pass) is preserved.
    manual_doc.extracted_data = {
        **(manual_doc.extracted_data or {}),
        'source': 'fcc_oet_document',
        'fcc_id': fcc_id,
        'oet_document_id': oet_doc.id,
        'view_attachment': oet_doc.view_attachment,
        'exhibit_type': oet_doc.exhibit_type,
    }

    if not manual_doc.manual_pdf:
        content = b''
        filename = ''

        if oet_doc.document_file:
            filename = Path(oet_doc.document_file.name).name
            oet_doc.document_file.open('rb')
            try:
                content = oet_doc.document_file.read()
            finally:
                oet_doc.document_file.close()
        elif source_url and _is_fcc_authoritative_url(source_url):
            content = _download_oet_document_bytes(source_url)
            if content:
                filename = _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=oet_doc.view_attachment,
                    document_url=source_url,
                    display_type=oet_doc.display_type,
                )

        if not (content and filename):
            return False

        manual_doc.manual_pdf.save(filename, ContentFile(content), save=False)

    manual_doc.save()
    _backfill_radio_specs_from_manual_doc(radio, manual_doc)

    logger.info(
        'FCC manual library sync radio_id=%s fcc_id=%s manual_id=%s oet_doc_id=%s doc_type=%s created=%s source_url=%s',
        getattr(radio, 'id', None),
        fcc_id,
        manual_doc.id,
        oet_doc.id,
        manual_doc.doc_type,
        created,
        source_url,
    )
    return created


def _apply_extracted_specs_to_radio(radio, extracted_specs, source_label):
    changes = []

    field_map = {
        'freq_bands_tx': 'freq_bands_tx',
        'power_watts': 'power_watts',
        'gps': 'gps',
        'aprs': 'aprs',
        'dmr': 'dmr',
        'air_band': 'air_band',
        'cost_approx': 'cost_approx',
    }
    for extracted_key, radio_field in field_map.items():
        value = extracted_specs.get(extracted_key)
        if value and not getattr(radio, radio_field):
            setattr(radio, radio_field, value)
            changes.append(radio_field)

    battery_mah = extracted_specs.get('battery_mah')
    if battery_mah and radio.battery_mah is None:
        radio.battery_mah = battery_mah
        changes.append('battery_mah')

    if changes:
        radio.save(update_fields=changes)
        logger.info(
            'FCC spec backfill applied radio_id=%s fcc_id=%s source=%s fields=%s',
            getattr(radio, 'id', None),
            getattr(radio, 'fcc_id', '') or '',
            source_label,
            ','.join(changes),
        )

    return changes


def _extract_specs_from_saved_pdf(file_field, source_name):
    file_path = getattr(file_field, 'path', '') if file_field else ''
    if not file_path:
        return '', {}, {}

    extracted_text, extraction_meta = extract_text_from_pdf_with_metadata(file_path)
    if not extracted_text:
        return '', {}, extraction_meta

    # Strip NUL bytes; PostgreSQL rejects string literals containing 0x00.
    extracted_text = extracted_text.replace('\x00', '')
    # Strip lone surrogate code points (U+D800–U+DFFF).  PDF text extraction
    # can emit unpaired surrogates when the font encoding is corrupt; PostgreSQL
    # utf-8 rejects them with "surrogates not allowed".
    extracted_text = extracted_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
    extracted_specs = extract_specs_from_text(extracted_text, source_name=source_name)
    return extracted_text, extracted_specs, extraction_meta


def _backfill_radio_specs_from_manual_doc(radio, manual_doc):
    if not manual_doc.manual_pdf:
        return []

    existing_specs = (manual_doc.extracted_data or {}).get('spec_extraction', {})
    if existing_specs:
        source_label = manual_doc.source_url or manual_doc.manual_pdf.name or str(manual_doc.pk)
        return _apply_extracted_specs_to_radio(radio, existing_specs, source_label)

    source_name = manual_doc.manual_pdf.name or manual_doc.source_url or ''
    extracted_text, extracted_specs, extraction_meta = _extract_specs_from_saved_pdf(manual_doc.manual_pdf, source_name)
    if not extracted_text:
        return []

    manual_doc.extracted_text = extracted_text
    manual_doc.extracted_data = {
        **(manual_doc.extracted_data or {}),
        'spec_extraction': extracted_specs,
        'pdf_extraction': extraction_meta,
    }
    manual_doc.extraction_confidence = max(manual_doc.extraction_confidence or 0.0, 0.85)
    manual_doc.save(update_fields=['extracted_text', 'extracted_data', 'extraction_confidence'])

    return _apply_extracted_specs_to_radio(radio, extracted_specs, source_name)


def _backfill_radio_specs_from_test_report(radio, report):
    if not report.report_pdf:
        return []

    existing_specs = (report.extracted_data or {}).get('spec_extraction', {})
    if existing_specs:
        source_label = report.source_url or report.report_title or report.report_pdf.name or str(report.pk)
        return _apply_extracted_specs_to_radio(radio, existing_specs, source_label)

    source_name = report.report_title or report.report_pdf.name or report.source_url or ''
    _, extracted_specs, extraction_meta = _extract_specs_from_saved_pdf(report.report_pdf, source_name)
    if not extracted_specs:
        return []

    report.extracted_data = {
        **(report.extracted_data or {}),
        'spec_extraction': extracted_specs,
        'pdf_extraction': extraction_meta,
    }
    report.save(update_fields=['extracted_data'])

    return _apply_extracted_specs_to_radio(radio, extracted_specs, source_name)


def _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata, force_reload=False):
    documents = secondary_metadata.get('oet_documents', []) if secondary_metadata else []
    logger.info(
        "FCC OET sync start radio_id=%s radio_fcc_id=%s requested_fcc_id=%s metadata_doc_count=%s",
        getattr(radio, 'id', None),
        (getattr(radio, 'fcc_id', '') or '').strip(),
        fcc_id,
        len(documents),
    )

    if not documents:
        synced = 0
        manual_synced = 0
        copied_names = []
        existing_docs = RadioOETDocument.objects.filter(fcc_id__iexact=fcc_id).exclude(radio=radio)
        for existing in existing_docs:
            copied_doc, created = RadioOETDocument.objects.update_or_create(
                radio=radio,
                fcc_id=fcc_id,
                document_url=existing.document_url,
                view_attachment=existing.view_attachment,
                defaults={
                    'exhibit_type': existing.exhibit_type,
                    'date_submitted_to_fcc': existing.date_submitted_to_fcc,
                    'display_type': existing.display_type,
                    'date_available': existing.date_available,
                    'document_file': existing.document_file.name if existing.document_file else '',
                },
            )

            if (
                not copied_doc.document_file
                and existing.document_url
                and _is_fcc_authoritative_url(existing.document_url)
            ):
                content = _download_oet_document_bytes(existing.document_url)
                if content:
                    filename = _build_oet_document_filename(
                        fcc_id=fcc_id,
                        view_attachment=existing.view_attachment,
                        document_url=existing.document_url,
                        display_type=existing.display_type,
                    )
                    copied_doc.document_file.save(filename, ContentFile(content), save=True)
                    logger.info(
                        "FCC OET fallback document downloaded radio_id=%s fcc_id=%s oet_doc_id=%s filename=%s source_url=%s",
                        getattr(radio, 'id', None),
                        fcc_id,
                        copied_doc.id,
                        filename,
                        existing.document_url,
                    )

            if _sync_manual_record_for_oet_document(radio, fcc_id, copied_doc):
                manual_synced += 1

            if created:
                synced += 1
                copied_names.append((existing.view_attachment or existing.document_url or '').strip())
        logger.info(
            "FCC OET sync fallback copy radio_id=%s fcc_id=%s copied_count=%s manual_docs_synced=%s copied_docs=%s",
            getattr(radio, 'id', None),
            fcc_id,
            synced,
            manual_synced,
            copied_names[:10],
        )
        _update_radio_oet_page_url(radio, fcc_id)
        return synced

    synced = 0
    manual_synced = 0
    synced_names = []

    stale_docs = RadioOETDocument.objects.filter(radio=radio, fcc_id__iexact=fcc_id)
    stale_deleted = 0
    for stale_doc in stale_docs:
        if _is_fcc_attachment_document_url(stale_doc.document_url):
            continue
        stale_doc.delete()
        stale_deleted += 1
    if stale_deleted:
        logger.info(
            'FCC OET sync pruned invalid existing docs radio_id=%s fcc_id=%s deleted_count=%s',
            getattr(radio, 'id', None),
            fcc_id,
            stale_deleted,
        )

    # Deduplicate documents: the HTML-table parser and the bare-attachment-link parser
    # both run on the same page and can produce entries for the same attachment URL.
    # The HTML parser may populate exhibit_type/dates; the attachment parser always leaves
    # them empty.  When both match the same (document_url, view_attachment) key the
    # update_or_create in the loop below would overwrite the good metadata with blanks on
    # the second pass.  Resolve by keeping the entry with more metadata for each key.
    _dedup: dict = {}
    for _doc in documents:
        _key = (
            (_doc.get('document_url') or '').strip(),
            (_doc.get('view_attachment') or '').strip(),
        )
        if _key not in _dedup:
            _dedup[_key] = _doc
        elif _doc.get('exhibit_type') and not _dedup[_key].get('exhibit_type'):
            _dedup[_key] = _doc
    documents = list(_dedup.values())

    for document in documents:
        view_attachment = (document.get('view_attachment') or '').strip()
        document_url = (document.get('document_url') or '').strip()
        if not view_attachment and not document_url:
            continue

        defaults = {
            'exhibit_type': (document.get('exhibit_type') or '').strip(),
            'date_submitted_to_fcc': _parse_date_only(document.get('date_submitted_to_fcc')),
            'display_type': (document.get('display_type') or '').strip(),
            'date_available': _parse_date_only(document.get('date_available')),
        }
        oet_doc, _ = RadioOETDocument.objects.update_or_create(
            radio=radio,
            fcc_id=fcc_id,
            document_url=document_url,
            view_attachment=view_attachment,
            defaults=defaults,
        )

        needs_download = force_reload or not oet_doc.document_file
        if not needs_download and oet_doc.document_file:
            existing_suffix = Path(oet_doc.document_file.name or '').suffix.lower()
            expected_suffix = Path(
                _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=view_attachment,
                    document_url=document_url,
                    display_type=defaults.get('display_type', ''),
                )
            ).suffix.lower()
            if existing_suffix != expected_suffix:
                needs_download = True

        if document_url and needs_download and _is_fcc_authoritative_url(document_url):
            content = _download_oet_document_bytes(
                document_url,
                referer_url=(document.get('referer_url') or '').strip(),
            )
            if content:
                filename = _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=view_attachment,
                    document_url=document_url,
                    display_type=defaults.get('display_type', ''),
                )
                oet_doc.document_file.save(filename, ContentFile(content), save=True)
                logger.info(
                    "FCC OET document downloaded radio_id=%s fcc_id=%s oet_doc_id=%s filename=%s source_url=%s",
                    getattr(radio, 'id', None),
                    fcc_id,
                    oet_doc.id,
                    filename,
                    document_url,
                )

        if _sync_manual_record_for_oet_document(radio, fcc_id, oet_doc):
            manual_synced += 1

        synced += 1
        synced_names.append((view_attachment or document_url or '').strip())

    logger.info(
        "FCC OET sync complete radio_id=%s fcc_id=%s synced_count=%s manual_docs_synced=%s synced_docs=%s",
        getattr(radio, 'id', None),
        fcc_id,
        synced,
        manual_synced,
        synced_names[:10],
    )
    _update_radio_oet_page_url(radio, fcc_id)
    return synced


def _update_radio_oet_page_url(radio, fcc_id):
    """Derive and save the FCC EAS exhibits listing URL from stored OET document URLs.

    Only updates if radio.oet_page_url is not already set.  Uses the application_id
    embedded in any document_url on a RadioOETDocument record to reconstruct the
    exhibits listing page (mode=Exhibits) which is the stable, shareable URL.
    """
    if not radio or not getattr(radio, 'pk', None):
        return
    if getattr(radio, 'oet_page_url', ''):
        return  # Already stored from a previous sync
    for doc in RadioOETDocument.objects.filter(radio=radio, fcc_id__iexact=fcc_id).exclude(document_url='')[:20]:
        m = _OET_APP_ID_RE.search(doc.document_url)
        if m:
            app_id = m.group(1)
            url = f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500&application_id={app_id}"
            Radio.objects.filter(pk=radio.pk).update(oet_page_url=url)
            radio.oet_page_url = url
            logger.info(
                'FCC OET page URL stored radio_id=%s fcc_id=%s oet_url=%s',
                radio.pk, fcc_id, url,
            )
            return


def _safe_filename(value):    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('_')


def _build_test_report_filename(fcc_id, title, url):
    url_name = Path((url or '').split('?', 1)[0]).name
    ext = '.pdf' if not url_name.lower().endswith('.pdf') else ''
    base = _safe_filename(f"{fcc_id}_{title or 'fcc_test_report'}")
    if not base:
        base = _safe_filename(f"{fcc_id}_fcc_test_report")
    if not base.lower().endswith('.pdf'):
        base = f"{base}{ext or '.pdf'}"
    return base[:190]


def _download_report_bytes(url):
    try:
        response = _fcc_request_with_retry('get', url, impersonate='chrome124', timeout=20)
    except Exception:
        logger.exception("FCC test report download failed url=%s", url)
        return b''

    if response.status_code != 200:
        logger.info("FCC test report download non-200 url=%s status=%s", url, response.status_code)
        return b''

    content = response.content or b''
    if not content:
        return b''
    return content


def _attach_test_reports_to_radio(radio, fcc_id, secondary_metadata, force_reload=False):
    candidates = secondary_metadata.get('test_report_candidates', []) if secondary_metadata else []
    if not candidates:
        return 0

    attached = 0
    attached_files = []
    for candidate in candidates:
        source_url = (candidate.get('url') or '').strip()
        if not source_url:
            continue

        if not force_reload and RadioFCCTestReport.objects.filter(radio=radio, fcc_id__iexact=fcc_id, source_url=source_url).exists():
            continue

        content = _download_report_bytes(source_url)
        if not content:
            continue

        report = RadioFCCTestReport(
            radio=radio,
            fcc_id=fcc_id,
            source_url=source_url,
            report_title=(candidate.get('title') or '').strip(),
            product_designation=(candidate.get('product_designation') or '').strip(),
            extracted_data={
                'source': 'fcc_secondary_metadata',
                'candidate': candidate,
            },
        )
        filename = _build_test_report_filename(fcc_id, report.report_title, source_url)
        report.report_pdf.save(filename, ContentFile(content), save=False)
        report.save()
        _backfill_radio_specs_from_test_report(radio, report)
        attached += 1
        attached_files.append(filename)
        logger.info(
            "FCC test report attached radio_id=%s fcc_id=%s report_id=%s url=%s title=%s designation=%s",
            radio.id,
            fcc_id,
            report.id,
            source_url,
            report.report_title,
            report.product_designation,
        )

    logger.info(
        "FCC test report sync complete radio_id=%s fcc_id=%s attached_count=%s files=%s",
        getattr(radio, 'id', None),
        fcc_id,
        attached,
        attached_files[:10],
    )

    return attached


def fetch_fcc_secondary_metadata(fcc_id):
    """Fetch additional FCC search metadata for a specific FCC ID."""
    # If both HTTP and Playwright are known-down for this sync run, skip all
    # network calls immediately — nothing will succeed and each attempt burns
    # 30–90 seconds of Playwright timeouts.
    if _fcc_connection_down and _fcc_playwright_down:
        logger.info(
            'FCC metadata fetch skipped fcc_id=%s reason=connection_and_playwright_down',
            fcc_id,
        )
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
        }

    grantee_code, product_code = split_fcc_id(fcc_id)
    if not grantee_code or not product_code:
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
        }

    params = {
        'grantee_code': grantee_code,
        'product_code': product_code,
        'product_exact_match': '',
        'RequestTimeout': '500',
        'outputformat': 'XML',
        'show_records': '25',
        'fetchfrom': '0',
        'calledFromFrame': 'N',
        'eas_apps_only': 'Y',
    }
    html_fallback_params = dict(params)
    html_fallback_params.pop('outputformat', None)

    try:
        response = _fcc_request_with_retry(
            'get',
            GENERIC_SEARCH_URL,
            params=params,
            impersonate="chrome124",
            timeout=15,
        )
    except _FCCConnectionDownError:
        logger.info('FCC metadata fetch skipped fcc_id=%s reason=connection_down', fcc_id)
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)
    except Exception:
        logger.exception("FCC metadata fetch failed fcc_id=%s", fcc_id)
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    if response.status_code != 200:
        logger.info(
            "FCC metadata fetch non-200 fcc_id=%s status=%s",
            fcc_id,
            response.status_code,
        )
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    try:
        data = xmltodict.parse(response.text)
    except Exception:
        logger.exception("FCC metadata parse failed fcc_id=%s", fcc_id)
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    target_key = _extract_fcc_key(fcc_id)
    matched_records = []
    matched_keys = set()
    original_equipment_rows = []
    candidate_exhibit_urls = []
    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )

        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        blob = _dict_text_blob(node)
        if not blob:
            continue

        for key in node.keys():
            matched_keys.add(str(key))
        matched_records.append(blob)

        for url in _extract_urls_from_payload(node):
            clean_url = unescape(url)
            if 'ViewExhibitReport.cfm' in clean_url:
                candidate_exhibit_urls.append(clean_url)

        application_purpose = (node.get('application_purpose') or node.get('applicationPurpose') or '').strip()
        if _is_original_equipment_purpose(application_purpose):
            original_equipment_rows.append(
                {
                    'grant_date': (node.get('grant_date') or node.get('grantDate') or '').strip(),
                    'application_purpose': application_purpose,
                    'lower_freq_mhz': (node.get('lower_freq_mhz') or node.get('lowerFreqMHz') or '').strip(),
                    'upper_freq_mhz': (node.get('upper_freq_mhz') or node.get('upperFreqMHz') or '').strip(),
                }
            )

    oet_documents = _extract_oet_documents_from_xml(data, fcc_id)
    if not oet_documents:
        oet_documents = _fetch_oet_documents_from_html(fcc_id, candidate_urls=candidate_exhibit_urls)

    return {
        'record_count': len(matched_records),
        'text_blob': ' || '.join(matched_records),
        'matched_keys': sorted(matched_keys),
        'test_report_candidates': _extract_test_report_candidates(data, fcc_id),
        'original_equipment_rows': original_equipment_rows,
        'oet_documents': oet_documents,
    }


def _allowlist_match_terms(primary_record, secondary_metadata, allowlist_terms):
    sources = [
        (primary_record.get('FCCId', '') or ''),
        (primary_record.get('grantee', '') or ''),
        (primary_record.get('applicationPurpose', '') or ''),
        (primary_record.get('grantDate', '') or ''),
        (secondary_metadata.get('text_blob', '') or ''),
    ]
    text = ' | '.join(str(v) for v in sources if v).upper()
    return [term for term in allowlist_terms if term in text]


def _clean_query(value):
    return (value or '').strip().upper().replace(' ', '')


def _exact_grantee_query(value):
    """Return an exact grantee code when query is a standalone valid grantee code."""
    cleaned = _clean_query(value)
    if not cleaned or '-' in cleaned:
        return ''
    if cleaned[0].isalpha() and len(cleaned) == 3:
        return cleaned
    if cleaned[0].isdigit() and cleaned[0] in '23456789' and len(cleaned) == 5:
        return cleaned
    return ''


def _parse_datetime_value(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)):
        try:
            numeric_value = float(value)
            if numeric_value <= 0:
                return None
            if numeric_value > 1_000_000_000_000:
                numeric_value = numeric_value / 1000.0
            return datetime.fromtimestamp(numeric_value, tz=datetime_timezone.utc)
        except Exception:
            return None

    text = str(value).strip()
    if not text:
        return None

    dt = parse_datetime(text)
    if dt:
        return dt

    if text.endswith('Z'):
        dt = parse_datetime(f"{text[:-1]}+00:00")
        if dt:
            return dt

    parsed_date = parse_date(text)
    if parsed_date:
        return datetime.combine(parsed_date, datetime_time.min, tzinfo=datetime_timezone.utc)

    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            candidate = datetime.strptime(text, fmt)
            if fmt.endswith("%S"):
                return candidate
            return datetime.combine(candidate.date(), datetime_time.min)
        except ValueError:
            continue

    return None


def _extract_record_last_modified_datetime(primary_record):
    if not isinstance(primary_record, dict):
        return None

    canonical = {
        str(key).lower(): value
        for key, value in primary_record.items()
    }
    preferred_keys = (
        'lastmodifieddate',
        'lastmodified',
        'modifieddate',
        'datemodified',
        'lastupdatedate',
        'lastupdate',
        'modificationdate',
    )

    candidate_values = []
    for key in preferred_keys:
        if key in canonical:
            candidate_values.append(canonical[key])

    if not candidate_values:
        for key, value in canonical.items():
            if 'modif' in key or 'lastupdate' in key or 'updated' in key:
                candidate_values.append(value)

    for value in candidate_values:
        parsed = _parse_datetime_value(value)
        if not parsed:
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        else:
            parsed = parsed.astimezone(datetime_timezone.utc)
        return parsed

    return None


def _should_skip_supporting_lookup(primary_record, last_lookup_at, start_date=None):
    """Return (should_skip, record_last_modified).

    Primary path: skip if the FCC record's last-modified date is on or before the
    radio's last lookup timestamp (nothing new to pull).

    Fallback path (FCC returns 503 so no last-modified date is available): if a
    start_date window is active and the radio was already looked up *within* that
    window, there is nothing new to process — skip to avoid redundant re-processing
    across consecutive daily syncs.
    """
    if not last_lookup_at:
        return False, None

    record_last_modified = _extract_record_last_modified_datetime(primary_record)

    lookup_dt = last_lookup_at
    if timezone.is_naive(lookup_dt):
        lookup_dt = timezone.make_aware(lookup_dt, datetime_timezone.utc)
    else:
        lookup_dt = lookup_dt.astimezone(datetime_timezone.utc)

    if record_last_modified is not None:
        # Normal path: compare FCC record age to last lookup.
        return record_last_modified <= lookup_dt, record_last_modified

    # No last-modified date (FCC returned 503 / Playwright fallback with no metadata).
    # Fall back to the date-window heuristic: if the radio was already processed
    # on or after start_date, no new data could have arrived within this sync window.
    if start_date is not None:
        start_dt = start_date
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt, datetime_timezone.utc)
        else:
            start_dt = start_dt.astimezone(datetime_timezone.utc)
        if lookup_dt >= start_dt:
            return True, None

    return False, None


def _stamp_lookup_timestamp(radio, looked_up_at):
    if radio.last_fccid_lookup_at == looked_up_at:
        return
    radio.last_fccid_lookup_at = looked_up_at
    radio.save(update_fields=['last_fccid_lookup_at'])


def _ensure_grantee_brand_and_manufacturer(grantee_code, grantee_name):
    normalized_grantee_code = normalize_grantee_code(grantee_code)
    normalized_grantee_name = (grantee_name or '').strip()
    if not normalized_grantee_code or not normalized_grantee_name:
        return None, None

    brand = _find_existing_grantee_brand(normalized_grantee_code, normalized_grantee_name)
    matching_blank_brand = _find_matching_blank_code_brand(
        normalized_grantee_name,
        exclude_brand_id=brand.id if brand is not None else None,
    )

    if brand is None:
        if matching_blank_brand is not None:
            brand = matching_blank_brand
            matching_blank_brand = None

    if brand is None:
        brand = Brand.objects.create(
            name=normalized_grantee_name,
            grantee_code=normalized_grantee_code,
            full_name=normalized_grantee_name,
        )
    else:
        update_fields = []
        if not brand.grantee_code:
            brand.grantee_code = normalized_grantee_code
            update_fields.append('grantee_code')

        brand_full_name_key = _normalize_brand_identity(brand.full_name)
        grantee_name_key = _normalize_brand_identity(normalized_grantee_name)
        if not brand.full_name or (matching_blank_brand is not None and brand_full_name_key != grantee_name_key):
            brand.full_name = normalized_grantee_name
            update_fields.append('full_name')
        if not brand.alias and matching_blank_brand is not None and matching_blank_brand.alias:
            brand.alias = matching_blank_brand.alias
            update_fields.append('alias')
        if update_fields:
            brand.save(update_fields=update_fields)

    manufacturer = Manufacturer.objects.filter(full_name__iexact=normalized_grantee_name).first()
    if manufacturer is None:
        manufacturer = Manufacturer.objects.create(
            full_name=normalized_grantee_name,
            alias=brand.alias or brand.name,
        )
    elif not manufacturer.alias and (brand.alias or brand.name):
        manufacturer.alias = brand.alias or brand.name
        manufacturer.save(update_fields=['alias'])

    manufacturer.brands.add(brand)
    return brand, manufacturer

def fetch_and_sync_fcc_id(fcc_id_query, start_date=None, end_date=None, force_reload=False):
    """
    Fetches FCC ID data using curl_cffi and saves it to the database.

    Args:
        fcc_id_query:  FCC ID or grantee code to query.
        start_date:    Optional date/datetime — only grants on or after this date are returned.
                       Passed to the FCC API as ``startDate=MM/DD/YYYY``.
        end_date:      Optional date/datetime — only grants on or before this date are returned.
                       Passed to the FCC API as ``endDate=MM/DD/YYYY``.
        force_reload:  When True, skip stale-lookup checks and re-download all documents
                       even if they are already stored.  Intended for use when the FCC has
                       issued an update to a previously synced ID.

    Returns (count_added, count_updated, messages)
    """
    messages = []
    ignored_grantee_codes = set(IgnoredGrantee.ignored_codes())
    query_grantee_code = _exact_grantee_query(fcc_id_query)
    if not query_grantee_code:
        query_grantee_code, _ = split_fcc_id(_clean_query(fcc_id_query))
    query_grantee_code = normalize_grantee_code(query_grantee_code)
    if query_grantee_code and query_grantee_code in ignored_grantee_codes:
        message = f"Skipped FCC query '{fcc_id_query}' because grantee {query_grantee_code} is on the ignore list."
        messages.append(message)
        logger.info(
            "FCC sync skipped ignored query query=%s ignored_grantee=%s",
            fcc_id_query,
            query_grantee_code,
        )
        return 0, 0, messages
    request_url = f"{URL}fccId={fcc_id_query}"
    if start_date is not None:
        # Accept either a date or datetime object.
        sd = start_date.date() if hasattr(start_date, 'date') else start_date
        request_url += f"&startDate={sd.strftime('%m/%d/%Y')}"
    if end_date is not None:
        ed = end_date.date() if hasattr(end_date, 'date') else end_date
        request_url += f"&endDate={ed.strftime('%m/%d/%Y')}"
    messages.append(f"Querying FCC API: {request_url}")
    logger.info("FCC sync request started query=%s start_date=%s end_date=%s", fcc_id_query, start_date, end_date)

    # Reset the connection-down flags at the start of each sync so that a
    # temporary FCC outage from a prior sync doesn't block a new one.
    global _fcc_connection_down, _fcc_playwright_down
    _fcc_connection_down = False
    _fcc_playwright_down = False

    try:
        response = _fcc_request_with_retry('get', request_url, impersonate="chrome124", timeout=15)
    except Exception as e:
        messages.append(f"Request failed with error: {e}")
        logger.exception("FCC sync request failed query=%s", fcc_id_query)
        return 0, 0, messages
    
    if response.status_code != 200:
        messages.append(f"Error: Received status code {response.status_code}")
        logger.warning("FCC sync non-200 status query=%s status=%s", fcc_id_query, response.status_code)
        return 0, 0, messages

    try:
        data = xmltodict.parse(response.text)
        wrapper = data.get("fCCIDInfoes", {})
        if not wrapper:
            logger.info("FCC sync empty wrapper query=%s", fcc_id_query)
            return 0, 0, messages
        result = wrapper.get("fccidInfo", [])
    except Exception as e:
        messages.append(f"Failed to parse XML response: {e}")
        logger.exception("FCC sync parse failed query=%s", fcc_id_query)
        return 0, 0, messages

    records = [result] if isinstance(result, dict) else (result if result else [])
    if not records:
        logger.info(
            "FCC sync no records returned query=%s start_date=%s end_date=%s — skipping",
            fcc_id_query,
            start_date,
            end_date,
        )
        messages.append(
            f"No grant applications returned from FCC for '{fcc_id_query}' "
            + (f"since {start_date.strftime('%m/%d/%Y')}" if start_date else "(full history)")
            + ". Nothing to process."
        )
        return 0, 0, messages
    exact_grantee = _exact_grantee_query(fcc_id_query)
    # When the user submits a specific full FCC ID (e.g. "2AN62-GC5"), the allowlist
    # is too aggressive: filings like "Change in Identification" contain no radio
    # keywords but are legitimate updates to an existing device grant.
    # Only enforce the allowlist for bulk grantee-code scans.
    is_specific_fcc_id = '-' in _clean_query(fcc_id_query)
    allowlist_terms = _radio_allowlist_terms()
    skipped_non_exact = 0
    skipped_ignored_grantee = 0
    skipped_non_radio = 0
    skipped_stale_lookup = 0
    attached_test_reports = 0
    synced_oet_documents = 0
    metadata_cache = {}
    lookup_started_at = timezone.now()

    count_added = 0
    count_updated = 0
    logger.info("FCC sync parsing records query=%s record_count=%s", fcc_id_query, len(records))
    for idx, res in enumerate(records, start=1):
        fcc_id = res.get('FCCId', '')
        if not fcc_id:
            continue

        grantee_code, product_code = split_fcc_id(fcc_id)
        grantee_code = normalize_grantee_code(grantee_code)
        if not product_code:
            product_code = fcc_id

        if grantee_code and grantee_code in ignored_grantee_codes:
            skipped_ignored_grantee += 1
            logger.info(
                "FCC ingest skipped ignored grantee source=fcc_api query=%s ignored_grantee=%s fcc_id=%s",
                fcc_id_query,
                grantee_code,
                fcc_id,
            )
            continue

        # FCC endpoint may return prefix-like results for grantee searches.
        # Enforce exact grantee code matching when query is a standalone grantee code.
        if exact_grantee and grantee_code != exact_grantee:
            skipped_non_exact += 1
            logger.info(
                "FCC sync skipped non-exact grantee query=%s expected_grantee=%s got_grantee=%s fcc_id=%s",
                fcc_id_query,
                exact_grantee,
                grantee_code,
                fcc_id,
            )
            continue

        raw_brand_name = (res.get("grantee", '') or '').strip()
        if not raw_brand_name:
            raw_brand_name = grantee_code

        authoritative_brand, authoritative_manufacturer = _ensure_grantee_brand_and_manufacturer(
            grantee_code,
            raw_brand_name,
        )
        resolved_authoritative_brand_name = _resolve_authoritative_radio_brand_name(
            authoritative_brand,
            grantee_code,
            raw_brand_name,
        )

        validation = validate_fcc_brand_assignment(fcc_id, raw_brand_name)
        brand_val = validation.get('resolved_brand_name') or raw_brand_name or grantee_code
        if brand_val == raw_brand_name and resolved_authoritative_brand_name:
            brand_val = resolved_authoritative_brand_name

        if validation.get('status') == 'white_label_possible':
            logger.info(
                "FCC validation white-label candidate source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s grantee_brand=%s provided_brand=%s resolved_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('grantee_brand_name', ''),
                validation.get('provided_brand_name', ''),
                brand_val,
            )
        elif validation.get('status') == 'unknown_grantee':
            logger.warning(
                "FCC validation unknown grantee source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('provided_brand_name', ''),
            )
        elif validation.get('status') == 'invalid_fcc_id':
            logger.warning(
                "FCC validation invalid id source=fcc_api query=%s record_index=%s fcc_id=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('provided_brand_name', ''),
            )

        existing_radios_with_fcc = list(Radio.objects.filter(fcc_id__iexact=fcc_id))
        existing_radio_by_brand_model = None
        if not existing_radios_with_fcc:
            existing_radio_by_brand_model = Radio.objects.filter(brand=brand_val, model=product_code).first()

        stale_lookup_radios = {}
        has_processable_radio = False

        for radio in existing_radios_with_fcc:
            should_skip, record_last_modified = _should_skip_supporting_lookup(
                res, radio.last_fccid_lookup_at, start_date=start_date,
            )
            if force_reload:
                should_skip = False
            stale_lookup_radios[radio.id] = (should_skip, record_last_modified)
            if not should_skip:
                has_processable_radio = True

        if existing_radio_by_brand_model:
            should_skip, record_last_modified = _should_skip_supporting_lookup(
                res,
                existing_radio_by_brand_model.last_fccid_lookup_at,
                start_date=start_date,
            )
            if force_reload:
                should_skip = False
            stale_lookup_radios[existing_radio_by_brand_model.id] = (should_skip, record_last_modified)
            if not should_skip:
                has_processable_radio = True

        if not existing_radios_with_fcc and not existing_radio_by_brand_model:
            has_processable_radio = True

        if not has_processable_radio:
            # When a date filter is active and the FCC record hasn't changed since
            # the last lookup, there is genuinely nothing new for this grant — skip
            # all secondary processing and move on to the next record.
            if start_date is not None:
                for radio in existing_radios_with_fcc:
                    skipped_stale_lookup += 1
                    _, record_last_modified = stale_lookup_radios.get(radio.id, (False, None))
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        record_last_modified.isoformat() if record_last_modified else '',
                        radio.last_fccid_lookup_at.isoformat() if radio.last_fccid_lookup_at else '',
                    )
                if existing_radio_by_brand_model:
                    skipped_stale_lookup += 1
                    _, record_last_modified = stale_lookup_radios.get(existing_radio_by_brand_model.id, (False, None))
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        existing_radio_by_brand_model.id,
                        existing_radio_by_brand_model.brand,
                        existing_radio_by_brand_model.model,
                        fcc_id,
                        record_last_modified.isoformat() if record_last_modified else '',
                        existing_radio_by_brand_model.last_fccid_lookup_at.isoformat() if existing_radio_by_brand_model.last_fccid_lookup_at else '',
                    )
                continue

            # No date filter (full-history scan): still retry OET doc sync so missing
            # exhibit links can be backfilled on previously processed radios.
            secondary_metadata = metadata_cache.get(fcc_id)
            if secondary_metadata is None:
                secondary_metadata = fetch_fcc_secondary_metadata(fcc_id)
                metadata_cache[fcc_id] = secondary_metadata

            for radio in existing_radios_with_fcc:
                synced_oet_documents += _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata, force_reload=force_reload)
                _stamp_lookup_timestamp(radio, lookup_started_at)
                skipped_stale_lookup += 1
                _, record_last_modified = stale_lookup_radios.get(radio.id, (False, None))
                logger.info(
                    "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                    fcc_id_query,
                    radio.id,
                    radio.brand,
                    radio.model,
                    fcc_id,
                    record_last_modified.isoformat() if record_last_modified else '',
                    radio.last_fccid_lookup_at.isoformat() if radio.last_fccid_lookup_at else '',
                )
            if existing_radio_by_brand_model:
                synced_oet_documents += _sync_oet_documents_for_radio(existing_radio_by_brand_model, fcc_id, secondary_metadata, force_reload=force_reload)
                _stamp_lookup_timestamp(existing_radio_by_brand_model, lookup_started_at)
                skipped_stale_lookup += 1
                _, record_last_modified = stale_lookup_radios.get(existing_radio_by_brand_model.id, (False, None))
                logger.info(
                    "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                    fcc_id_query,
                    existing_radio_by_brand_model.id,
                    existing_radio_by_brand_model.brand,
                    existing_radio_by_brand_model.model,
                    fcc_id,
                    record_last_modified.isoformat() if record_last_modified else '',
                    existing_radio_by_brand_model.last_fccid_lookup_at.isoformat() if existing_radio_by_brand_model.last_fccid_lookup_at else '',
                )
            continue

        secondary_metadata = metadata_cache.get(fcc_id)
        if secondary_metadata is None:
            secondary_metadata = fetch_fcc_secondary_metadata(fcc_id)
            metadata_cache[fcc_id] = secondary_metadata

        matched_terms = _allowlist_match_terms(res, secondary_metadata, allowlist_terms)
        if allowlist_terms and not matched_terms and not is_specific_fcc_id:
            # Even for non-radio classifications, ingest OET exhibits for existing FCC-linked radios.
            for radio in existing_radios_with_fcc:
                should_skip, _ = stale_lookup_radios.get(radio.id, (False, None))
                if should_skip:
                    continue
                synced_oet_documents += _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata, force_reload=force_reload)
                _stamp_lookup_timestamp(radio, lookup_started_at)
            skipped_non_radio += 1
            logger.info(
                "FCC ingest skipped record source=fcc_api query=%s fcc_id=%s reason=no_radio_allowlist_match allow_terms=%s primary_purpose=%s metadata_record_count=%s metadata_keys=%s",
                fcc_id_query,
                fcc_id,
                ','.join(allowlist_terms),
                res.get('applicationPurpose', ''),
                secondary_metadata.get('record_count', 0),
                ','.join(secondary_metadata.get('matched_keys', [])),
            )
            continue

        original_equipment_summary = _extract_original_equipment_summary(res, secondary_metadata)

        # Format new details for notes
        grant_date = res.get("grantDate", "N/A")
        app_purpose = res.get("applicationPurpose", "N/A")
        new_notes = f"FCC Grant Date: {grant_date} | Purpose: {app_purpose}"

        # "Change in Identification" means the grantee name changed after initial filing —
        # the device was built under one company and is now sold under another, i.e. white label.
        is_change_in_id = 'change in identification' in (app_purpose or '').lower()

        # Check if Radio already exists
        if existing_radios_with_fcc:
            for radio in existing_radios_with_fcc:
                should_skip, record_last_modified = stale_lookup_radios.get(radio.id, (False, None))
                if should_skip:
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                    skipped_stale_lookup += 1
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        record_last_modified.isoformat() if record_last_modified else '',
                        radio.last_fccid_lookup_at.isoformat() if radio.last_fccid_lookup_at else '',
                    )
                    continue

                has_changes = False
                if new_notes not in radio.notes:
                    radio.notes = f"{new_notes}\n{radio.notes}".strip()
                    has_changes = True

                if is_change_in_id and not radio.is_a_whitelabel:
                    radio.is_a_whitelabel = True
                    has_changes = True
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, radio.id, radio.brand, radio.model, fcc_id,
                    )

                derived_intro_year = original_equipment_summary.get('intro_year')
                if derived_intro_year and radio.intro_year != derived_intro_year:
                    radio.intro_year = derived_intro_year
                    has_changes = True

                derived_freq_bands_tx = original_equipment_summary.get('freq_bands_tx', '')
                if derived_freq_bands_tx and radio.freq_bands_tx != derived_freq_bands_tx:
                    radio.freq_bands_tx = derived_freq_bands_tx
                    has_changes = True

                if brand_val and radio.brand != brand_val:
                    radio_brand_key = _normalize_brand_identity(radio.brand)
                    raw_brand_key = _normalize_brand_identity(raw_brand_name)
                    if not radio_brand_key or radio_brand_key == raw_brand_key:
                        radio.brand = brand_val
                        has_changes = True

                if authoritative_manufacturer and radio.manufacturer_id != authoritative_manufacturer.id:
                    radio.manufacturer = authoritative_manufacturer
                    has_changes = True

                if has_changes:
                    radio.last_fccid_lookup_at = lookup_started_at
                    radio.save()
                    count_updated += 1
                    logger.info(
                        "FCC ingest update source=fcc_api query=%s action=update_by_fcc_id radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s intro_year=%s freq_bands_tx=%s is_whitelabel=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        validation.get('status', ''),
                        radio.intro_year,
                        radio.freq_bands_tx,
                        radio.is_a_whitelabel,
                    )
                else:
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                attached_test_reports += _attach_test_reports_to_radio(radio, fcc_id, secondary_metadata, force_reload=force_reload)
                synced_oet_documents += _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata, force_reload=force_reload)
        else:
            if existing_radio_by_brand_model:
                should_skip, record_last_modified = stale_lookup_radios.get(existing_radio_by_brand_model.id, (False, None))
                if should_skip:
                    _stamp_lookup_timestamp(existing_radio_by_brand_model, lookup_started_at)
                    skipped_stale_lookup += 1
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        existing_radio_by_brand_model.id,
                        existing_radio_by_brand_model.brand,
                        existing_radio_by_brand_model.model,
                        fcc_id,
                        record_last_modified.isoformat() if record_last_modified else '',
                        existing_radio_by_brand_model.last_fccid_lookup_at.isoformat() if existing_radio_by_brand_model.last_fccid_lookup_at else '',
                    )
                    continue

                # Update the existing radio instead of creating a duplicate
                if not existing_radio_by_brand_model.fcc_id:
                    existing_radio_by_brand_model.fcc_id = fcc_id
                if new_notes not in existing_radio_by_brand_model.notes:
                    existing_radio_by_brand_model.notes = f"{new_notes}\n{existing_radio_by_brand_model.notes}".strip()

                if is_change_in_id and not existing_radio_by_brand_model.is_a_whitelabel:
                    existing_radio_by_brand_model.is_a_whitelabel = True
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, existing_radio_by_brand_model.id,
                        existing_radio_by_brand_model.brand, existing_radio_by_brand_model.model, fcc_id,
                    )

                derived_intro_year = original_equipment_summary.get('intro_year')
                if derived_intro_year and existing_radio_by_brand_model.intro_year != derived_intro_year:
                    existing_radio_by_brand_model.intro_year = derived_intro_year

                derived_freq_bands_tx = original_equipment_summary.get('freq_bands_tx', '')
                if derived_freq_bands_tx and existing_radio_by_brand_model.freq_bands_tx != derived_freq_bands_tx:
                    existing_radio_by_brand_model.freq_bands_tx = derived_freq_bands_tx

                if brand_val and existing_radio_by_brand_model.brand != brand_val:
                    radio_brand_key = _normalize_brand_identity(existing_radio_by_brand_model.brand)
                    raw_brand_key = _normalize_brand_identity(raw_brand_name)
                    if not radio_brand_key or radio_brand_key == raw_brand_key:
                        existing_radio_by_brand_model.brand = brand_val

                if authoritative_manufacturer and existing_radio_by_brand_model.manufacturer_id != authoritative_manufacturer.id:
                    existing_radio_by_brand_model.manufacturer = authoritative_manufacturer

                existing_radio_by_brand_model.last_fccid_lookup_at = lookup_started_at
                existing_radio_by_brand_model.save()
                count_updated += 1
                logger.info(
                    "FCC ingest update source=fcc_api query=%s action=update_by_brand_model radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s intro_year=%s freq_bands_tx=%s",
                    fcc_id_query,
                    existing_radio_by_brand_model.id,
                    brand_val,
                    product_code,
                    fcc_id,
                    validation.get('status', ''),
                    existing_radio_by_brand_model.intro_year,
                    existing_radio_by_brand_model.freq_bands_tx,
                )
                attached_test_reports += _attach_test_reports_to_radio(existing_radio_by_brand_model, fcc_id, secondary_metadata, force_reload=force_reload)
                synced_oet_documents += _sync_oet_documents_for_radio(existing_radio_by_brand_model, fcc_id, secondary_metadata, force_reload=force_reload)
            else:
                created_radio = Radio.objects.create(
                    brand=brand_val,
                    model=product_code,
                    manufacturer=authoritative_manufacturer,
                    fcc_id=fcc_id,
                    notes=new_notes,
                    is_a_whitelabel=is_change_in_id,
                    intro_year=original_equipment_summary.get('intro_year'),
                    freq_bands_tx=original_equipment_summary.get('freq_bands_tx', ''),
                    last_fccid_lookup_at=lookup_started_at,
                )
                if is_change_in_id:
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, created_radio.id, brand_val, product_code, fcc_id,
                    )
                count_added += 1
                logger.info(
                    "FCC ingest create source=fcc_api query=%s action=create radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s inferred_grantee=%s intro_year=%s freq_bands_tx=%s",
                    fcc_id_query,
                    created_radio.id,
                    brand_val,
                    product_code,
                    fcc_id,
                    validation.get('status', ''),
                    validation.get('inferred_grantee_code', ''),
                    created_radio.intro_year,
                    created_radio.freq_bands_tx,
                )
                attached_test_reports += _attach_test_reports_to_radio(created_radio, fcc_id, secondary_metadata, force_reload=force_reload)
                synced_oet_documents += _sync_oet_documents_for_radio(created_radio, fcc_id, secondary_metadata, force_reload=force_reload)

    if exact_grantee and skipped_non_exact:
        messages.append(
            f"Filtered {skipped_non_exact} non-exact grantee matches while enforcing exact grantee code {exact_grantee}."
        )
    if skipped_ignored_grantee:
        messages.append(
            f"Skipped {skipped_ignored_grantee} FCC record(s) because their grantee code is on the ignore list."
        )
    if skipped_non_radio:
        messages.append(
            f"Skipped {skipped_non_radio} records that did not match FCC_RADIO_ALLOWLIST_TERMS ({','.join(allowlist_terms)})."
        )
    if skipped_stale_lookup:
        messages.append(
            f"Skipped {skipped_stale_lookup} radio records because FCC last-modified data was not newer than the prior lookup timestamp."
        )
    if attached_test_reports:
        messages.append(f"Attached {attached_test_reports} FCC test report files.")
    if synced_oet_documents:
        messages.append(f"Synced {synced_oet_documents} OET exhibit documents.")
    messages.append(f"Successfully processed {len(records)} records for {fcc_id_query}.")
    logger.info(
        "FCC sync completed query=%s added=%s updated=%s exact_grantee=%s skipped_non_exact=%s skipped_non_radio=%s skipped_stale_lookup=%s attached_test_reports=%s synced_oet_documents=%s",
        fcc_id_query,
        count_added,
        count_updated,
        exact_grantee,
        skipped_non_exact,
        skipped_non_radio,
        skipped_stale_lookup,
        attached_test_reports,
        synced_oet_documents,
    )
    return count_added, count_updated, messages
