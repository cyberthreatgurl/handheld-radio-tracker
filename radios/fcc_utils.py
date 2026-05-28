import xmltodict
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from html import unescape
from datetime import datetime, time as datetime_time, timezone as datetime_timezone
from pathlib import Path
from urllib.parse import urljoin
from curl_cffi import requests
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from radios.models import Radio, RadioFCCTestReport, RadioOETDocument
from radios.fcc_id_utils import split_fcc_id
from radios.fcc_validation import validate_fcc_brand_assignment

URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"
GENERIC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm"
OET_EXHIBITS_URL = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
logger = logging.getLogger(__name__)

DEFAULT_RADIO_ALLOWLIST_TERMS = "TRANSCEIVER,TRANSMITTER,RECEIVER,MURS,ORIGINAL EQUIPMENT"
RADIO_ALLOWLIST_ENV_NAME = "FCC_RADIO_ALLOWLIST_TERMS"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


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

    for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', html_text or '', flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 5:
            continue

        first_cell = cells[0]
        link_match = re.search(r'href=["\']([^"\']+)["\']', first_cell, flags=re.IGNORECASE)
        document_url = urljoin(base_url, link_match.group(1).strip()) if link_match else ''

        view_attachment = _strip_html_tags(first_cell)
        exhibit_type = _strip_html_tags(cells[1])
        date_submitted = _strip_html_tags(cells[2])
        display_type = _strip_html_tags(cells[3])
        date_available = _strip_html_tags(cells[4])

        if not any((view_attachment, exhibit_type, date_submitted, display_type, date_available, document_url)):
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
            }
        )

    return documents


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
            url = urljoin(base_url, href.strip())
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

    return {
        'record_count': len(matched_records),
        'text_blob': ' || '.join(matched_records),
        'matched_keys': sorted(matched_keys),
        'original_equipment_rows': original_equipment_rows,
        'candidate_exhibit_urls': candidate_exhibit_urls,
    }


def _fetch_secondary_metadata_from_html_fallback(fcc_id, params):
    try:
        response = requests.get(GENERIC_SEARCH_URL, params=params, impersonate='chrome124', timeout=15)
    except Exception:
        logger.exception('FCC HTML metadata fallback fetch failed fcc_id=%s', fcc_id)
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
        }

    if response.status_code != 200:
        logger.info(
            'FCC HTML metadata fallback non-200 fcc_id=%s status=%s',
            fcc_id,
            response.status_code,
        )
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
        }

    parsed = _extract_secondary_metadata_from_generic_search_html(response.text or '', GENERIC_SEARCH_URL, fcc_id)
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
    compact_fcc = _extract_fcc_key(fcc_id)
    urls = []
    for url in candidate_urls or []:
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())

    urls.append(f"{OET_EXHIBITS_URL}?mode=Exhibits&calledFromFrame=N&fcc_id={fcc_id}")
    if compact_fcc:
        urls.append(f"{OET_EXHIBITS_URL}?mode=Exhibits&calledFromFrame=N&fcc_id={compact_fcc}")

    deduped_urls = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped_urls.append(url)

    for url in deduped_urls:
        try:
            response = requests.get(url, impersonate='chrome124', timeout=15)
        except Exception:
            logger.exception("FCC OET HTML fetch failed fcc_id=%s url=%s", fcc_id, url)
            continue

        if response.status_code != 200:
            logger.info("FCC OET HTML fetch non-200 fcc_id=%s status=%s url=%s", fcc_id, response.status_code, url)
            continue

        documents = _extract_oet_documents_from_html(response.text or '', base_url=url)
        if documents:
            return documents

    return []


def _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata):
    documents = secondary_metadata.get('oet_documents', []) if secondary_metadata else []
    if not documents:
        return 0

    synced = 0
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
        RadioOETDocument.objects.update_or_create(
            radio=radio,
            fcc_id=fcc_id,
            document_url=document_url,
            view_attachment=view_attachment,
            defaults=defaults,
        )
        synced += 1

    return synced


def _safe_filename(value):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('_')


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
        response = requests.get(url, impersonate='chrome124', timeout=20)
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


def _attach_test_reports_to_radio(radio, fcc_id, secondary_metadata):
    candidates = secondary_metadata.get('test_report_candidates', []) if secondary_metadata else []
    if not candidates:
        return 0

    attached = 0
    for candidate in candidates:
        source_url = (candidate.get('url') or '').strip()
        if not source_url:
            continue

        if RadioFCCTestReport.objects.filter(radio=radio, fcc_id__iexact=fcc_id, source_url=source_url).exists():
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
        attached += 1
        logger.info(
            "FCC test report attached radio_id=%s fcc_id=%s report_id=%s url=%s title=%s designation=%s",
            radio.id,
            fcc_id,
            report.id,
            source_url,
            report.report_title,
            report.product_designation,
        )

    return attached


def fetch_fcc_secondary_metadata(fcc_id):
    """Fetch additional FCC search metadata for a specific FCC ID."""
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
        'product_exact_match': 'on',
        'outputformat': 'XML',
        'show_records': '25',
        'fetchfrom': '0',
        'calledFromFrame': 'N',
        'eas_apps_only': 'Y',
    }
    html_fallback_params = dict(params)
    html_fallback_params.pop('outputformat', None)

    try:
        response = requests.get(GENERIC_SEARCH_URL, params=params, impersonate="chrome124", timeout=15)
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
            if 'ViewExhibitReport.cfm' in url:
                candidate_exhibit_urls.append(url)

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


def _should_skip_supporting_lookup(primary_record, last_lookup_at):
    if not last_lookup_at:
        return False, None

    record_last_modified = _extract_record_last_modified_datetime(primary_record)
    if not record_last_modified:
        return False, None

    lookup_dt = last_lookup_at
    if timezone.is_naive(lookup_dt):
        lookup_dt = timezone.make_aware(lookup_dt, datetime_timezone.utc)
    else:
        lookup_dt = lookup_dt.astimezone(datetime_timezone.utc)

    return record_last_modified <= lookup_dt, record_last_modified


def _stamp_lookup_timestamp(radio, looked_up_at):
    if radio.last_fccid_lookup_at == looked_up_at:
        return
    radio.last_fccid_lookup_at = looked_up_at
    radio.save(update_fields=['last_fccid_lookup_at'])

def fetch_and_sync_fcc_id(fcc_id_query):
    """
    Fetches FCC ID data using curl_cffi and saves it to the database.
    Returns (count_added, count_updated, messages)
    """
    messages = []
    request_url = f"{URL}fccId={fcc_id_query}"
    messages.append(f"Querying FCC API: {request_url}")
    logger.info("FCC sync request started query=%s", fcc_id_query)
    
    try:
        response = requests.get(request_url, impersonate="chrome124", timeout=15)
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
    exact_grantee = _exact_grantee_query(fcc_id_query)
    allowlist_terms = _radio_allowlist_terms()
    skipped_non_exact = 0
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
        if not product_code:
            product_code = fcc_id

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

        validation = validate_fcc_brand_assignment(fcc_id, raw_brand_name)
        brand_val = validation.get('resolved_brand_name') or raw_brand_name or grantee_code

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
            should_skip, record_last_modified = _should_skip_supporting_lookup(res, radio.last_fccid_lookup_at)
            stale_lookup_radios[radio.id] = (should_skip, record_last_modified)
            if not should_skip:
                has_processable_radio = True

        if existing_radio_by_brand_model:
            should_skip, record_last_modified = _should_skip_supporting_lookup(
                res,
                existing_radio_by_brand_model.last_fccid_lookup_at,
            )
            stale_lookup_radios[existing_radio_by_brand_model.id] = (should_skip, record_last_modified)
            if not should_skip:
                has_processable_radio = True

        if not existing_radios_with_fcc and not existing_radio_by_brand_model:
            has_processable_radio = True

        if not has_processable_radio:
            for radio in existing_radios_with_fcc:
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
        if allowlist_terms and not matched_terms:
            # Even for non-radio classifications, ingest OET exhibits for existing FCC-linked radios.
            for radio in existing_radios_with_fcc:
                should_skip, _ = stale_lookup_radios.get(radio.id, (False, None))
                if should_skip:
                    continue
                synced_oet_documents += _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata)
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

                derived_intro_year = original_equipment_summary.get('intro_year')
                if derived_intro_year and radio.intro_year != derived_intro_year:
                    radio.intro_year = derived_intro_year
                    has_changes = True

                derived_freq_bands_tx = original_equipment_summary.get('freq_bands_tx', '')
                if derived_freq_bands_tx and radio.freq_bands_tx != derived_freq_bands_tx:
                    radio.freq_bands_tx = derived_freq_bands_tx
                    has_changes = True

                if has_changes:
                    radio.last_fccid_lookup_at = lookup_started_at
                    radio.save()
                    count_updated += 1
                    logger.info(
                        "FCC ingest update source=fcc_api query=%s action=update_by_fcc_id radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s intro_year=%s freq_bands_tx=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        validation.get('status', ''),
                        radio.intro_year,
                        radio.freq_bands_tx,
                    )
                else:
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                attached_test_reports += _attach_test_reports_to_radio(radio, fcc_id, secondary_metadata)
                synced_oet_documents += _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata)
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

                # Upate the existing radio instead of creating a duplicate
                if not existing_radio_by_brand_model.fcc_id:
                    existing_radio_by_brand_model.fcc_id = fcc_id
                if new_notes not in existing_radio_by_brand_model.notes:
                    existing_radio_by_brand_model.notes = f"{new_notes}\n{existing_radio_by_brand_model.notes}".strip()

                derived_intro_year = original_equipment_summary.get('intro_year')
                if derived_intro_year and existing_radio_by_brand_model.intro_year != derived_intro_year:
                    existing_radio_by_brand_model.intro_year = derived_intro_year

                derived_freq_bands_tx = original_equipment_summary.get('freq_bands_tx', '')
                if derived_freq_bands_tx and existing_radio_by_brand_model.freq_bands_tx != derived_freq_bands_tx:
                    existing_radio_by_brand_model.freq_bands_tx = derived_freq_bands_tx

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
                attached_test_reports += _attach_test_reports_to_radio(existing_radio_by_brand_model, fcc_id, secondary_metadata)
                synced_oet_documents += _sync_oet_documents_for_radio(existing_radio_by_brand_model, fcc_id, secondary_metadata)
            else:
                created_radio = Radio.objects.create(
                    brand=brand_val,
                    model=product_code,
                    fcc_id=fcc_id,
                    notes=new_notes,
                    intro_year=original_equipment_summary.get('intro_year'),
                    freq_bands_tx=original_equipment_summary.get('freq_bands_tx', ''),
                    last_fccid_lookup_at=lookup_started_at,
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
                attached_test_reports += _attach_test_reports_to_radio(created_radio, fcc_id, secondary_metadata)
                synced_oet_documents += _sync_oet_documents_for_radio(created_radio, fcc_id, secondary_metadata)

    if exact_grantee and skipped_non_exact:
        messages.append(
            f"Filtered {skipped_non_exact} non-exact grantee matches while enforcing exact grantee code {exact_grantee}."
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
