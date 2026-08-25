# pylint: disable=no-member, broad-except, too-many-branches
# pylint: disable=too-many-return-statements, too-many-locals
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# broad-except: intentionally broad at network/service boundaries
# too-many-*: parsing/dispatch functions naturally branch over many field kinds
"""
Website import engine for radio product pages.

Fetches a manufacturer product page, extracts brand/model/part-number and
capability specs using layered strategies (JSON-LD structured data, meta
tags, label/value spec pairs, full-text regex), upserts the matching Radio
record, and optionally downloads the manual PDF.

The extraction is deliberately format-agnostic: no single manufacturer's
product page is structured the same way, so each layer is a heuristic that
contributes fields and a precedence order decides which value wins.
"""
import logging
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .manual_extraction import (
    _extract_from_title,
    _extract_json_ld_objects,
    _extract_meta_content,
    extract_specs_from_text,
)
from .models import (
    Brand,
    Radio,
    RadioManual,
    RadioServiceType,
)

logger = logging.getLogger(__name__)


_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

_MODEL_RE = re.compile(
    r'\b([A-Za-z]{1,5}[- ]?[A-Za-z]?\d{1,4}[A-Za-z0-9-]{0,6})\b'
)
_MODEL_EXCLUSIONS = {
    'ip67', 'ip68', 'ip65', 'ip54', 'ipx7', 'ipx4', 'ipx8',
}

_DOMAIN_BRAND_HINTS = {
    'baofengradio.com': 'Baofeng',
    'baofengtech.com': 'BTECH',
    'radtels.com': 'Radtel',
    'tidradio.com': 'TIDRADIO',
    'abbree.com': 'Abbree',
    'retevis.com': 'Retevis',
    'radiooddity.com': 'Radioddity',
    'radioddity.com': 'Radioddity',
}

_LABEL_PATTERNS = {
    'channels': [r'memory\s*channels', r'channel\s*capacity', r'channels'],
    'power_watts': [r'output\s*power', r'transmit\s*power', r'rf\s*power', r'power'],
    'battery_mah': [r'battery\s*capacity', r'battery'],
    'fcc_id': [r'fcc\s*id'],
    'display': [r'display', r'screen'],
    'display_color': [r'display\s*color', r'screen\s*color', r'color\s*display'],
    'part_number': [r'sku', r'product\s*code', r'part\s*number', r'model\s*no'],
    'cost_approx': [r'price', r'cost'],
    'freq_bands_tx': [r'frequency\s*range', r'frequency', r'band'],
    'gps': [r'gps', r'gnss'],
    'aprs': [r'aprs'],
    'dmr': [r'dmr'],
    'air_band': [r'air\s*band', r'airband'],
    'bluetooth': [r'bluetooth'],
    'noaa': [r'noaa'],
    'usb_chargeable': [r'usb.*charg', r'charg.*usb'],
    'usb_programmable': [r'usb.*program', r'programming'],
}

_STRING_FIELDS = {
    'fcc_id': 'fcc_id',
    'freq_bands_tx': 'freq_bands_tx',
    'power_watts': 'power_watts',
    'gps': 'gps',
    'aprs': 'aprs',
    'air_band': 'air_band',
    'dmr': 'dmr',
    'display': 'display',
    'display_color': 'display_color',
    'part_number': 'part_number',
    'cost_approx': 'cost_approx',
}

_BOOL_FIELDS = {
    'bluetooth': 'bluetooth',
    'noaa': 'noaa_wx',
    'usb_chargeable': 'usb_chargeable',
    'usb_programmable': 'usb_programmable',
    'is_toy': 'is_toy',
}

_INT_FIELDS = {
    'channels': 'channels',
    'battery_mah': 'battery_mah',
}

_CONFIDENCE_FIELDS = {
    'brand': 0.15,
    'model': 0.25,
    'fcc_id': 0.2,
    'freq_bands_tx': 0.1,
    'power_watts': 0.1,
    'gps': 0.05,
    'aprs': 0.05,
    'dmr': 0.05,
    'channels': 0.05,
}


def _clean(value):
    return (value or '').strip()


def _domain_from_url(url):
    return urlparse(url or '').netloc.lower().replace('www.', '')


def _is_public_http_url(url):
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (OSError, socket.gaierror):
        return False
    for info in infos:
        try:
            ip_addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip_addr.is_private or ip_addr.is_loopback or ip_addr.is_link_local
            or ip_addr.is_reserved or ip_addr.is_multicast
        ):
            return False
    return True


def fetch_page(url):
    """Fetch and parse a product page, returning a dict or None."""
    if not _is_public_http_url(url):
        logger.warning("Site import URL rejected url=%s", url)
        return None
    try:
        response = requests.get(url, timeout=15, headers=_BROWSER_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Site import fetch failed url=%s", url)
        return None
    if not response.text:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.title.string.strip() if soup.title and soup.title.string else ''
    page_text = soup.get_text(' ', strip=True)
    return {'soup': soup, 'title': title, 'page_text': page_text, 'url': url}


def _parse_json_ld(objects):
    data = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get('@type', '')
        if isinstance(obj_type, list):
            obj_type = ' '.join(obj_type)
        is_product = 'product' in obj_type.lower()

        brand_obj = obj.get('brand')
        if isinstance(brand_obj, dict):
            brand = (brand_obj.get('name') or '').strip()
        elif isinstance(brand_obj, str):
            brand = brand_obj.strip()
        else:
            brand = ''
        if brand and not data.get('brand'):
            data['brand'] = brand

        name = (obj.get('name') or '').strip()
        if name and is_product and not data.get('name'):
            data['name'] = name

        sku = obj.get('sku') or obj.get('mpn') or ''
        if sku and not data.get('part_number'):
            data['part_number'] = str(sku).strip()

        desc = (obj.get('description') or '').strip()
        if desc and not data.get('description'):
            data['description'] = desc

        offers = obj.get('offers')
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = offers.get('price') or offers.get('lowPrice')
            currency = offers.get('priceCurrency', 'USD')
            if price and not data.get('cost_approx'):
                if str(currency).upper() in ('USD', 'US$'):
                    data['cost_approx'] = f"${price}"
                else:
                    data['cost_approx'] = f"{currency} {price}"
    return data


def extract_spec_pairs(soup):
    """Extract label/value pairs from common HTML spec structures."""
    pairs = []
    seen = set()

    def add(label, value):
        label = _clean(label).rstrip(':').strip()
        value = _clean(value)
        if not label or not value or len(label) > 60:
            return
        dedupe_key = (label.lower(), value.lower())
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        pairs.append((label, value))

    for row in soup.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        if len(cells) >= 2:
            add(
                cells[0].get_text(' ', strip=True),
                cells[1].get_text(' ', strip=True),
            )

    for dl in soup.find_all('dl'):
        dts = dl.find_all('dt')
        dds = dl.find_all('dd')
        for dt_el, dd_el in zip(dts, dds):
            add(
                dt_el.get_text(' ', strip=True),
                dd_el.get_text(' ', strip=True),
            )

    for container in soup.find_all(['div', 'li', 'section']):
        classes = ' '.join(container.get('class', []))
        if not re.search(r'spec|param|feature|attribute', classes, re.IGNORECASE):
            continue
        text = container.get_text(' ', strip=True)
        match = re.match(r'^([A-Za-z][A-Za-z0-9 /().-]{1,40}?)[:]\s*(.+)$', text)
        if match:
            add(match.group(1), match.group(2))

    return pairs


def _label_to_field(label):
    lowered = label.lower().strip()
    for field, patterns in _LABEL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lowered):
                return field
    return ''


def _parse_value_for_field(field, value):
    text = _clean(value)
    if not text:
        return None
    lowered = text.lower()

    if field == 'channels':
        match = re.search(r'\d+', text)
        return int(match.group()) if match else None
    if field == 'battery_mah':
        match = re.search(r'(\d{3,5})\s*m\s*ah', text, re.IGNORECASE)
        return int(match.group(1)) if match else None
    if field == 'power_watts':
        match = re.search(r'(\d+(?:\.\d+)?)\s*w', text, re.IGNORECASE)
        return f"{match.group(1)}W" if match else _clean(text)
    if field in ('bluetooth', 'noaa', 'usb_chargeable', 'usb_programmable'):
        return True
    if field in ('gps', 'aprs', 'dmr', 'air_band'):
        return 'Yes'
    if field == 'display_color':
        if re.search(r'color|tft', lowered):
            return 'Color'
        if 'mono' in lowered:
            return 'Monochrome'
        return None
    return _clean(text)[:200]


def _parse_spec_pairs(pairs):
    data = {}
    for label, value in pairs:
        field = _label_to_field(label)
        if not field or data.get(field):
            continue
        parsed = _parse_value_for_field(field, value)
        if parsed not in (None, '', False):
            data[field] = parsed
    return data


def _merge_specs(*layers):
    merged = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if value and not merged.get(key):
                merged[key] = value
    return merged


def _resolve_brand(structured, title, url):
    brand = _clean(structured.get('brand'))
    if not brand:
        brand = _extract_from_title(title).get('brand', '')
    if not brand:
        brand = _DOMAIN_BRAND_HINTS.get(_domain_from_url(url), '')
    return brand


def _url_slug_model(url):
    path = urlparse(url).path.strip('/')
    slug = path.rsplit('/', 1)[-1]
    return re.sub(r'[-_]+', ' ', slug)


def _derive_model(*candidates):
    for candidate in candidates:
        if not candidate:
            continue
        for match in _MODEL_RE.finditer(str(candidate)):
            token = match.group(1).strip()
            if token.lower() in _MODEL_EXCLUSIONS:
                continue
            return token
    return ''


def _detect_service_hints(text):
    lowered = (text or '').lower()
    hints = []
    if re.search(r'\bgmrs\b', lowered):
        hints.append('GMRS')
    if re.search(r'\bfrs\b', lowered) or 'pmr446' in lowered:
        hints.append('FRS')
    if re.search(r'\bham\b', lowered) or 'amateur' in lowered:
        hints.append('Amateur')
    return hints


def _compute_confidence(specs):
    score = 0.0
    for field, weight in _CONFIDENCE_FIELDS.items():
        if specs.get(field):
            score += weight
    return round(min(score, 1.0), 3)


def _discover_manual_pdf_urls(soup, page_url):
    candidates = []
    seen = set()
    for link in soup.find_all('a', href=True):
        href = urljoin(page_url, link.get('href'))
        text = link.get_text(' ', strip=True).lower()
        href_lower = href.lower()
        is_pdf = href_lower.endswith('.pdf') or '.pdf?' in href_lower
        is_manual = bool(re.search(r'manual|user\s*guide|instruction|download', text))
        if (is_pdf or is_manual) and href not in seen:
            seen.add(href)
            candidates.append(href)
    return candidates


def _discover_download_pages(soup, page_url):
    pages = []
    seen = set()
    for link in soup.find_all('a', href=True):
        href = urljoin(page_url, link.get('href'))
        text = link.get_text(' ', strip=True).lower()
        href_lower = href.lower()
        if re.search(
            r'/pages/(download|support|firmware|manual)|/software|/downloads|/support',
            href_lower,
        ) or re.search(r'download|manual|firmware', text):
            if href != page_url and href not in seen:
                seen.add(href)
                pages.append(href)
    return pages


def _download_pdf_bytes(url):
    try:
        response = requests.get(url, timeout=20, headers=_BROWSER_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("Site import manual download failed url=%s", url)
        return b''
    content = response.content or b''
    if not content.startswith(b'%PDF'):
        logger.warning("Site import manual rejected non-PDF url=%s", url)
        return b''
    return content


def _pdf_filename(url):
    name = urlparse(url).path.rsplit('/', 1)[-1]
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', name or 'manual.pdf')
    if not name.lower().endswith('.pdf'):
        name = f"{name}.pdf"
    return name[:200]


def _attach_manual_pdfs(radio, soup, page_url, apply=True):
    pdf_urls = _discover_manual_pdf_urls(soup, page_url)
    if not pdf_urls:
        for subpage in _discover_download_pages(soup, page_url)[:2]:
            sub_page = fetch_page(subpage)
            if sub_page:
                pdf_urls.extend(
                    _discover_manual_pdf_urls(sub_page['soup'], subpage),
                )

    attached = []
    for pdf_url in pdf_urls[:3]:
        if RadioManual.objects.filter(radio=radio, source_url=pdf_url).exists():
            continue
        content = _download_pdf_bytes(pdf_url)
        if not content:
            continue
        if not apply:
            attached.append(pdf_url)
            continue
        doc = RadioManual(
            radio=radio,
            doc_type=RadioManual.DocType.MANUAL,
            source_url=pdf_url,
            status=RadioManual.ProcessingStatus.LINKED,
        )
        doc.manual_pdf.save(_pdf_filename(pdf_url), ContentFile(content), save=False)
        doc.save()
        attached.append(pdf_url)
    return attached


def extract_from_url(url):
    """Fetch and extract identity + specs from a URL without touching the DB."""
    result = {
        'url': url,
        'brand': '',
        'model': '',
        'part_number': '',
        'specs': {},
        'service_hints': [],
        'manual_urls': [],
        'errors': [],
        '_soup': None,
    }
    page = fetch_page(url)
    if page is None:
        result['errors'].append('fetch_failed')
        return result

    soup = page['soup']
    title = page['title']
    page_text = page['page_text']
    result['_soup'] = soup

    structured = _parse_json_ld(_extract_json_ld_objects(soup))

    meta_specs = {}
    for meta_name in ('description', 'keywords', 'og:description'):
        content = _extract_meta_content(soup, 'name', meta_name)
        if content:
            parsed = extract_specs_from_text(
                content, source_name=f'meta:{meta_name}',
            )
            for key, value in parsed.items():
                if value and not meta_specs.get(key):
                    meta_specs[key] = value

    desc_specs = {}
    if structured.get('description'):
        desc_specs = extract_specs_from_text(
            structured['description'], source_name='json-ld-description',
        )

    pair_specs = _parse_spec_pairs(extract_spec_pairs(soup))
    text_specs = extract_specs_from_text(page_text, source_name=title)

    specs = _merge_specs(structured, desc_specs, meta_specs, pair_specs, text_specs)
    specs['website'] = url

    result['specs'] = {key: value for key, value in specs.items() if value}
    result['brand'] = _resolve_brand(structured, title, url)
    result['model'] = _derive_model(
        structured.get('name', ''),
        specs.get('model', ''),
        title,
        _url_slug_model(url),
    )
    result['part_number'] = (
        specs.get('part_number') or structured.get('part_number') or ''
    )
    result['service_hints'] = _detect_service_hints(page_text)
    result['manual_urls'] = _discover_manual_pdf_urls(soup, url)

    if not result['brand'] or not result['model']:
        result['errors'].append('missing_identity')
    return result


def _canonical_brand_name(name):
    name = _clean(name)
    if not name:
        return ''
    exact = Brand.objects.filter(name__iexact=name).first()
    if exact:
        return exact.name
    alias = Brand.objects.filter(alias__iexact=name).first()
    if alias:
        return alias.name
    return name


def _locate_radio(brand, model, part_number):
    radio = Radio.objects.filter(
        brand__iexact=brand, model__iexact=model,
    ).first()
    if radio:
        return radio, False
    if part_number:
        radio = Radio.objects.filter(part_number__iexact=part_number).first()
        if radio:
            return radio, False
    return None, True


def _apply_specs_to_radio(radio, specs, url, created, apply=True):
    changed = []

    for key, field in _STRING_FIELDS.items():
        value = specs.get(key)
        if not value:
            continue
        value = str(value)[:200]
        current = getattr(radio, field)
        if (created or not current) and str(value) != (current or ''):
            setattr(radio, field, value)
            changed.append(field)

    for key, field in _BOOL_FIELDS.items():
        if specs.get(key):
            if created or not getattr(radio, field):
                setattr(radio, field, True)
                changed.append(field)

    for key, field in _INT_FIELDS.items():
        value = specs.get(key)
        if value is None:
            continue
        try:
            int_value = int(value)
        except (TypeError, ValueError):
            continue
        current = getattr(radio, field)
        if (created or current is None) and int_value != current:
            setattr(radio, field, int_value)
            changed.append(field)

    if created or not radio.website:
        radio.website = url
        changed.append('website')

    radio.spec_source_url = url
    radio.spec_extracted_at = timezone.now()
    radio.spec_confidence = _compute_confidence(specs)
    changed += ['spec_source_url', 'spec_extracted_at', 'spec_confidence']

    if apply and changed:
        radio.save(update_fields=changed + ['updated_at'])
    return changed


def _apply_service_types(radio, hints, apply=True):
    added = []
    for hint in hints:
        service = RadioServiceType.objects.filter(name__iexact=hint).first()
        if service is None:
            continue
        if apply and not radio.service_types.filter(pk=service.pk).exists():
            radio.service_types.add(service)
        added.append(hint)
    return added


def apply_website_to_radio(radio, url, apply=True):
    """Scrape a URL and apply extracted specs to an existing Radio instance."""
    extracted = extract_from_url(url)
    if 'fetch_failed' in extracted['errors']:
        return {
            'updated_fields': [],
            'service_types_added': [],
            'manuals': [],
            'errors': extracted['errors'],
        }

    specs = extracted['specs']
    specs['website'] = url
    changed = _apply_specs_to_radio(radio, specs, url, created=False, apply=apply)
    service_added = _apply_service_types(
        radio, extracted['service_hints'], apply=apply,
    )
    manuals = _attach_manual_pdfs(
        radio, extracted.get('_soup'), url, apply=apply,
    )
    return {
        'updated_fields': changed,
        'service_types_added': service_added,
        'manuals': manuals,
        'errors': extracted['errors'],
    }


def upsert_radio_from_url(url, apply=True):
    """Fetch a product page and upsert the matching Radio record.

    Returns a report dict describing what was found, created, or updated.
    """
    report = {
        'url': url,
        'brand': '',
        'model': '',
        'part_number': '',
        'radio_id': None,
        'radio_created': False,
        'updated_fields': [],
        'service_types_added': [],
        'manuals': [],
        'confidence': None,
        'errors': [],
    }

    extracted = extract_from_url(url)
    report['errors'] = extracted['errors']
    if not extracted.get('brand') or not extracted.get('model'):
        return report

    brand_name = _canonical_brand_name(extracted['brand'])
    model = extracted['model']
    part_number = extracted['part_number']
    report.update(
        brand=brand_name, model=model, part_number=part_number,
    )

    if not apply:
        report['updated_fields'] = sorted(extracted['specs'].keys())
        report['service_types_added'] = list(extracted['service_hints'])
        report['manuals'] = list(extracted['manual_urls'])
        report['confidence'] = _compute_confidence(extracted['specs'])
        return report

    with transaction.atomic():
        brand_obj = Brand.objects.filter(name__iexact=brand_name).first()
        if brand_obj is None:
            brand_obj = Brand.objects.create(name=brand_name)

        radio, created = _locate_radio(brand_name, model, part_number)
        if radio is None:
            radio = Radio(
                brand=brand_obj.name, model=model, part_number=part_number,
            )
            radio.save()
            created = True
        report['radio_created'] = created
        report['radio_id'] = radio.pk

        changed = _apply_specs_to_radio(
            radio, extracted['specs'], url, created, apply=True,
        )
        report['updated_fields'] = changed
        report['confidence'] = radio.spec_confidence

        service_added = _apply_service_types(
            radio, extracted['service_hints'], apply=True,
        )
        report['service_types_added'] = service_added

        report['manuals'] = _attach_manual_pdfs(
            radio, extracted.get('_soup'), url, apply=True,
        )

    return report
