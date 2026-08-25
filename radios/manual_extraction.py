import re
import json
import logging
import urllib3
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .fcc_id_utils import normalize_fcc_id_for_lookup
from .models import Radio

# Suppress "InsecureRequestWarning" when falling back to verify=False for
# sites with self-signed or misconfigured SSL certificates.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def _clean_text(value):
    return (value or '').strip()


def _has_enough_text(text, min_chars=80):
    if not text:
        return False
    alnum = re.sub(r'[^A-Za-z0-9]+', '', text)
    return len(alnum) >= min_chars


def _extract_text_via_ocr(file_path, max_pages=12):
    """OCR fallback for scanned PDF manuals."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError:
        return ''

    text_chunks = []
    try:
        pdf = pdfium.PdfDocument(file_path)
        page_count = len(pdf)
        logger.info("Manual OCR attempt file=%s pages=%s max_pages=%s", file_path, page_count, max_pages)
        for page_index in range(min(page_count, max_pages)):
            page = pdf[page_index]
            bitmap = page.render(scale=2.0)
            pil_image = bitmap.to_pil()
            text_chunks.append(pytesseract.image_to_string(pil_image) or '')
    except (OSError, ValueError, RuntimeError):
        logger.exception("Manual OCR failed file=%s", file_path)
        return ''

    logger.info("Manual OCR completed file=%s text_length=%s", file_path, len('\\n'.join(text_chunks)))
    return '\n'.join(text_chunks)


def extract_text_from_pdf(file_path):
    """Extract plaintext from a PDF manual file."""
    text, _ = extract_text_from_pdf_with_metadata(file_path)
    return text


def extract_text_from_pdf_with_metadata(file_path):
    """Extract plaintext from a PDF manual file with metadata on extraction method."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import DependencyError as PdfDependencyError
    except ImportError:
        logger.warning("PDF parser missing pypdf file=%s", file_path)
        return '', {'method': 'none', 'reason': 'pypdf_missing'}

    text_chunks = []
    direct_failure_reason = ''
    try:
        reader = PdfReader(file_path)
        logger.info("Manual parse attempt file=%s pages=%s", file_path, len(reader.pages))
        for page in reader.pages:
            text_chunks.append(page.extract_text() or '')
    except PdfDependencyError:
        direct_failure_reason = 'pdf_crypto_dependency_missing'
        logger.warning(
            "Manual direct PDF parse needs cryptography for AES file=%s; falling back to OCR",
            file_path,
        )
    except Exception:
        logger.exception("Manual direct PDF parse failed file=%s", file_path)
        direct_failure_reason = 'pdf_parse_error'

    direct_text = '\n'.join(text_chunks)
    if _has_enough_text(direct_text):
        logger.info("Manual parse used embedded text file=%s text_length=%s", file_path, len(direct_text))
        return direct_text, {'method': 'embedded_text', 'text_length': len(direct_text)}

    # OCR fallback when PDF has little/no embedded text.
    ocr_text = _extract_text_via_ocr(file_path)
    if _has_enough_text(ocr_text):
        logger.info("Manual parse used OCR fallback file=%s text_length=%s", file_path, len(ocr_text))
        meta = {'method': 'ocr', 'text_length': len(ocr_text)}
        if direct_failure_reason:
            meta['direct_parse_reason'] = direct_failure_reason
        return ocr_text, meta

    logger.info("Manual parse fallback insufficient text file=%s direct_len=%s ocr_len=%s", file_path, len(direct_text), len(ocr_text))
    meta = {
        'method': 'embedded_text_low_confidence',
        'text_length': len(direct_text),
        'ocr_text_length': len(ocr_text),
    }
    if direct_failure_reason:
        meta['direct_parse_reason'] = direct_failure_reason
    return direct_text, meta


def _extract_first(pattern, text, flags=re.IGNORECASE):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ''


def _detect_bands(text):
    bands = set()
    ranges = re.findall(r'(\d{2,4}(?:\.\d+)?)\s*[-~to]{1,3}\s*(\d{2,4}(?:\.\d+)?)\s*mhz', text, flags=re.IGNORECASE)
    for lower, upper in ranges:
        try:
            lval = float(lower)
            uval = float(upper)
        except ValueError:
            continue
        if lval < 300 and uval > 30:
            bands.add('VHF')
        if lval < 1000 and uval >= 300:
            bands.add('UHF')
    if 'vhf' in text.lower():
        bands.add('VHF')
    if 'uhf' in text.lower():
        bands.add('UHF')
    return ', '.join(sorted(bands))


def _extract_fcc_part_from_standards(text):
    """Extract FCC Part number from STANDARD(S) line in test report text.

    Handles formats like:
        STANDARD(S).   :  FCC Part 15
        Test Standard(s): FCC Part 95E
        STANDARD(S).   :  FCC Part 15B
        Standard: FCC Part 90

    Returns a set of cleaned FCC rule part strings (e.g. {'Part 15B', 'Part 90'}).
    """
    rule_parts = set()
    if not text:
        return rule_parts

    # Match the STANDARD(S) line and capture everything after it until end of line.
    # Handles colons, spaces, and various label formats including PDF artifacts
    # where "STANDARD" may be split as "ST ANDARD".
    pattern = re.compile(
        r'(?:ST\s*ANDARD|STANDARD|TEST\s*STANDARD|APPLICABLE\s*STANDARD)'
        r'\s*(?:\(S\))?\s*[:.]?\s*'
        r'(.+?)(?:\n|$)',
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    for match in matches:
        # Extract "FCC Part XXX" from the captured text
        part_matches = re.findall(
            r'FCC\s+Part\s+(\d+[A-Za-z]*(?:\s*Subpart\s+[A-Za-z])?)',
            match,
            re.IGNORECASE,
        )
        for part_num in part_matches:
            part_num = part_num.strip()
            # Normalize: "15B" or "15 B" -> "Part 15B"
            cleaned = re.sub(r'\s+', ' ', part_num).strip()
            rule_parts.add(f'Part {cleaned}')

        # Also catch "Part 15" by itself (without "FCC" prefix in some formats)
        part_matches2 = re.findall(
            r'(?:^|[^A-Za-z])Part\s+(\d+[A-Za-z]*(?:\s*Subpart\s+[A-Za-z])?)',
            match,
            re.IGNORECASE,
        )
        for part_num in part_matches2:
            part_num = part_num.strip()
            cleaned = re.sub(r'\s+', ' ', part_num).strip()
            rule_parts.add(f'Part {cleaned}')

    return rule_parts


def extract_specs_from_text(text, source_name=''):
    """Extract normalized radio specs from manual text."""
    logger.info("Spec parse attempt source=%s text_length=%s", source_name, len(text or ''))
    lowered = text.lower()

    brand = _extract_first(r'(?:brand|manufacturer)\s*[:\-]\s*([A-Za-z0-9\.,&\-\s]{2,80})', text)
    manufacturer = _extract_first(r'manufacturer\s*[:\-]\s*([A-Za-z0-9\.,&\-\s]{2,120})', text)
    model = _extract_first(r'(?:model(?:\s*no\.?|\s*number)?)\s*[:\-]\s*([A-Za-z0-9\-]{2,40})', text)
    fcc_id = _extract_first(r'fcc\s*id\s*[:#\-]?\s*([A-Za-z0-9\-]{3,50})', text)
    if fcc_id:
        fcc_id = normalize_fcc_id_for_lookup(fcc_id) or fcc_id.upper().strip()

    power = _extract_first(r'(\d{1,3}(?:\.\d+)?)\s*w(?:att)?s?', text)
    power_watts = f'{power}W' if power else ''

    aprs = 'Yes' if 'aprs' in lowered else ''
    gps = 'Yes' if (' gps' in lowered or 'gnss' in lowered) else ''
    dmr = 'Yes' if re.search(r'\bdmr\b', lowered) else ''
    air_band = 'Yes' if ('air band' in lowered or 'airband' in lowered or 'aviation band' in lowered) else ''
    battery = _extract_first(r'(\d{3,5})\s*m\s*ah', text)
    cost = _extract_first(r'\$\s*(\d{2,5}(?:\.\d{1,2})?)', text)
    cost_approx = f'${cost}' if cost else ''
    freq_bands_tx = _detect_bands(text)

    # New capability fields (website spec import)
    channels = _extract_first(
        r'(\d{1,5})\s*(?:memory\s+)?ch(?:annels?)?\b', text,
    )
    part_number = _extract_first(
        r'(?:sku|product\s*code|part\s*(?:number|no\.?))\s*[:#]?\s*'
        r'([A-Za-z0-9][A-Za-z0-9\-\s/.]{2,60})',
        text,
    )
    noaa = 'noaa' in lowered
    bluetooth = 'bluetooth' in lowered
    usb_chargeable = ('usb' in lowered and 'charg' in lowered)
    usb_programmable = (
        ('usb' in lowered and 'program' in lowered)
        or 'programming cable' in lowered
        or 'usb cable' in lowered
    )
    is_toy = bool(re.search(r'\btoy\b', lowered))

    if re.search(
        r'color\s*(?:tft\s*)?(?:display|screen)|full-?color|color\s*screen',
        lowered,
    ):
        display_color = 'Color'
    elif 'monochrome' in lowered or 'black and white' in lowered:
        display_color = 'Monochrome'
    else:
        display_color = ''

    # Extract FCC rule parts from STANDARD(S) lines (for test reports)
    fcc_rule_parts = _extract_fcc_part_from_standards(text)

    # Fallbacks from filename/title text
    if not model and source_name:
        model = _extract_first(r'([A-Za-z]{1,6}-?\d{2,5}[A-Za-z0-9\-]*)', source_name)

    extracted = {
        'brand': _clean_text(brand),
        'manufacturer': _clean_text(manufacturer),
        'model': _clean_text(model),
        'fcc_id': _clean_text(fcc_id),
        'freq_bands_tx': _clean_text(freq_bands_tx),
        'aprs': aprs,
        'gps': gps,
        'dmr': dmr,
        'air_band': air_band,
        'power_watts': _clean_text(power_watts),
        'battery_mah': int(battery) if battery else None,
        'cost_approx': _clean_text(cost_approx),
        'fcc_rule_parts': sorted(fcc_rule_parts) if fcc_rule_parts else [],
        'channels': int(channels) if channels else None,
        'display_color': _clean_text(display_color),
        'part_number': _clean_text(part_number),
        'noaa': noaa,
        'bluetooth': bluetooth,
        'usb_chargeable': usb_chargeable,
        'usb_programmable': usb_programmable,
        'is_toy': is_toy,
    }
    logger.info("Spec parse result source=%s model=%s fcc_id=%s bands=%s", source_name, extracted.get('model', ''), extracted.get('fcc_id', ''), extracted.get('freq_bands_tx', ''))
    return extracted


def _extract_meta_content(soup, attr, value):
    tag = soup.find('meta', attrs={attr: value})
    if not tag:
        return ''
    return _clean_text(tag.get('content', ''))


def _extract_json_ld_objects(soup):
    objects = []
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        raw = (script.string or script.get_text() or '').strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list):
            objects.extend(parsed)
        elif isinstance(parsed, dict):
            if isinstance(parsed.get('@graph'), list):
                objects.extend(parsed['@graph'])
            else:
                objects.append(parsed)
    return objects


def _price_from_json_ld(json_ld_objects):
    for obj in json_ld_objects:
        offers = obj.get('offers') if isinstance(obj, dict) else None
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = offers.get('price') or offers.get('lowPrice')
            currency = offers.get('priceCurrency', 'USD')
            if price:
                prefix = '$' if str(currency).upper() in {'USD', 'US$'} else f"{currency} "
                return f"{prefix}{price}"
    return ''


def _price_from_text(page_text):
    amount = _extract_first(r'\$\s*(\d{2,5}(?:\.\d{1,2})?)', page_text)
    return f'${amount}' if amount else ''


def _domain_from_url(url):
    domain = urlparse(url).netloc.lower().strip()
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


def _extract_from_title(title):
    # Common pattern: "Brand Model ..."
    model = _extract_first(r'([A-Za-z]{1,8}-?\d{2,6}[A-Za-z0-9\-]*)', title)
    first_word = title.split()[0] if title else ''
    brand = first_word if re.match(r'^[A-Za-z][A-Za-z0-9\-]{1,30}$', first_word or '') else ''
    return {'brand': _clean_text(brand), 'model': _clean_text(model)}


def _parse_temu(soup, title, page_text):
    json_ld = _extract_json_ld_objects(soup)
    title_data = _extract_from_title(title)
    return {
        'brand': title_data.get('brand', ''),
        'model': title_data.get('model', ''),
        'cost_approx': _price_from_json_ld(json_ld) or _price_from_text(page_text),
        'source_hint': 'temu',
    }


def _parse_radioddity(soup, title, page_text):
    json_ld = _extract_json_ld_objects(soup)
    og_title = _extract_meta_content(soup, 'property', 'og:title') or title
    title_data = _extract_from_title(og_title)

    return {
        'brand': title_data.get('brand', '') or 'Radioddity',
        'model': title_data.get('model', ''),
        'cost_approx': (
            _extract_meta_content(soup, 'property', 'product:price:amount')
            or _price_from_json_ld(json_ld)
            or _price_from_text(page_text)
        ),
        'source_hint': 'radiooddity/radioddity',
    }


def _parse_aliexpress(soup, title, page_text):
    json_ld = _extract_json_ld_objects(soup)
    title_data = _extract_from_title(title)
    return {
        'brand': title_data.get('brand', ''),
        'model': title_data.get('model', ''),
        'cost_approx': _price_from_json_ld(json_ld) or _price_from_text(page_text),
        'source_hint': 'aliexpress',
    }


def _parse_retevis(soup, _title, page_text):
    """Parse Retevis product pages for radio specs.

    Retevis pages have structured spec sections with labels like
    'Frequency Range', 'Output Power', 'Battery Capacity', etc.
    """
    data = {'source_hint': 'retevis'}

    # Strategy 1: Look for spec rows (label / value pairs)
    spec_map = {}
    # Common spec patterns: <td>Label</td><td>Value</td> or <dt>Label</dt><dd>Value</dd>
    for row in soup.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        if len(cells) >= 2:
            label = cells[0].get_text(' ', strip=True).rstrip(':').lower()
            value = cells[1].get_text(' ', strip=True)
            if label and value and len(label) < 60:
                spec_map[label] = value

    # Strategy 2: Look for definition lists
    for dl in soup.find_all('dl'):
        dts = dl.find_all('dt')
        dds = dl.find_all('dd')
        for dt_el, dd_el in zip(dts, dds):
            label = dt_el.get_text(' ', strip=True).rstrip(':').lower()
            value = dd_el.get_text(' ', strip=True)
            if label and value and len(label) < 60:
                spec_map[label] = value

    # Strategy 3: Look for spec divs with label/value structure
    for container in soup.find_all(['div', 'li'], class_=lambda c: c and any(
        w in (c or '').lower() for w in ('spec', 'param', 'feature', 'attribute')
    )):
        text = container.get_text(' ', strip=True)
        match = re.match(r'([A-Za-z][A-Za-z\s]+?)[:\s]+(.+)', text)
        if match:
            label = match.group(1).strip().rstrip(':').lower()
            value = match.group(2).strip()
            if label and value and len(label) < 60:
                spec_map[label] = value

    # Map known spec labels to our fields
    _LABEL_MAP = {
        'frequency range': 'freq_bands_tx',
        'frequency': 'freq_bands_tx',
        'tx frequency': 'freq_bands_tx',
        'output power': 'power_watts',
        'power output': 'power_watts',
        'transmit power': 'power_watts',
        'rf power': 'power_watts',
        'battery capacity': 'battery_mah',
        'battery': 'battery_mah',
        'battery type': 'battery_mah',
        'waterproof': 'waterproof_rating',
        'waterproof rating': 'waterproof_rating',
        'ip rating': 'waterproof_rating',
        'gps': 'gps',
        'gnss': 'gps',
        'weight': 'weight',
        'dimensions': 'dimensions',
        'channel capacity': 'channel_capacity',
        'modulation': 'modulation',
    }

    for label, value in spec_map.items():
        for key, field in _LABEL_MAP.items():
            if key in label:
                if field == 'battery_mah':
                    match = re.search(r'(\d{3,5})\s*m\s*ah', value, re.IGNORECASE)
                    if match:
                        data[field] = int(match.group(1))
                elif field == 'power_watts':
                    if not data.get(field):
                        match = re.search(r'(\d+(?:\.\d+)?)\s*w', value, re.IGNORECASE)
                        if match:
                            data[field] = f"{match.group(1)}W"
                elif field == 'freq_bands_tx':
                    if not data.get(field):
                        data[field] = _clean_text(value)
                elif field == 'gps':
                    if 'yes' in value.lower() or 'gps' in value.lower():
                        data[field] = 'Yes'
                elif field == 'waterproof_rating':
                    match = re.search(r'IP(\d{2})', value, re.IGNORECASE)
                    if match:
                        data['waterproof_rating'] = f"IP{match.group(1)}"
                break

    # Fallback: extract specs from image alt text (Retevis uses info-graphic images)
    if not data.get('freq_bands_tx') or not data.get('power_watts'):
        img_keywords = {
            'freq_bands_tx': [
                r'(VHF|UHF|136[-–]\d{3}\s*MHz|400[-–]\d{3}\s*MHz|FRS|GMRS)',
            ],
            'power_watts': [r'(\d+W)', r'(\d+\s*Watts?)'],
            'gps': [r'(GPS|GNSS)'],
            'dmr': [r'(DMR|Digital Mobile Radio)'],
        }
        for img in soup.find_all('img', alt=True):
            alt = img.get('alt', '')
            for field, patterns in img_keywords.items():
                if not data.get(field):
                    for pattern in patterns:
                        match = re.search(pattern, alt, re.IGNORECASE)
                        if match:
                            if field == 'power_watts':
                                data[field] = match.group(1).replace(' ', '')
                            elif field in ('freq_bands_tx',):
                                data[field] = _clean_text(match.group(1))
                            elif field in ('gps', 'dmr'):
                                data[field] = 'Yes'
                            break

    # USB-C detection
    if 'type-c' in page_text.lower() or 'usb c' in page_text.lower():
        data['usb_c_charging'] = True

    return data


def _parse_generic_product_page(soup, _title, page_text):
    """Parse a generic e-commerce product page using structured data patterns.

    Handles sites without domain-specific parsers by looking for:
    1. JSON-LD Product schema (@type: Product)
    2. Meta tags (og:description, description, keywords)
    3. Common spec table patterns
    4. Page text fallback
    """
    data = {'source_hint': 'generic_product_page'}

    # Strategy 1: JSON-LD Product schema
    json_ld = _extract_json_ld_objects(soup)
    for obj in json_ld:
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get('@type', '')
        if isinstance(obj_type, list):
            obj_type = ' '.join(obj_type)
        if 'product' not in obj_type.lower():
            continue

        # Extract description
        desc = obj.get('description', '')
        if desc:
            data['description'] = _clean_text(desc)
            # Parse description for specs too
            desc_specs = extract_specs_from_text(desc, source_name='json-ld')
            for k, v in desc_specs.items():
                if v and not data.get(k):
                    data[k] = v

        # Extract brand
        brand_obj = obj.get('brand', {})
        if isinstance(brand_obj, dict):
            brand_name = brand_obj.get('name', '')
        else:
            brand_name = str(brand_obj) if brand_obj else ''
        if brand_name and not data.get('brand'):
            data['brand'] = _clean_text(brand_name)

        # Extract model / name
        name = obj.get('name', '')
        if name and not data.get('model'):
            data['model'] = _clean_text(name)

        # Extract price
        offers = obj.get('offers')
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = offers.get('price')
            if price and not data.get('cost_approx'):
                currency = offers.get('priceCurrency', '')
                if str(currency).upper() == 'USD':
                    data['cost_approx'] = f"${price}"
                else:
                    data['cost_approx'] = str(price)

    # Strategy 2: Meta tags
    for meta_name in ('description', 'keywords', 'og:description'):
        content = _extract_meta_content(soup, 'name', meta_name)
        if content:
            meta_specs = extract_specs_from_text(content, source_name=f'meta:{meta_name}')
            for k, v in meta_specs.items():
                if v and not data.get(k):
                    data[k] = v

    # Strategy 3: Look for spec tables/lists with label:value patterns
    spec_pattern = re.compile(
        r'(frequency|power|watt|battery|mah|gps|aprs|dmr|'
        r'weight|dimension|water|ip\d|channel|modulation|'
        r'band|range)',
        re.IGNORECASE,
    )
    # Check <tr> rows with 2 cells
    for row in soup.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        if len(cells) == 2:
            label = cells[0].get_text(' ', strip=True).lower()
            value = cells[1].get_text(' ', strip=True)
            if spec_pattern.search(label) and value:
                if not data.get('freq_bands_tx') and 'freq' in label:
                    data['freq_bands_tx'] = _clean_text(value)
                if not data.get('power_watts') and ('power' in label or 'watt' in label):
                    match = re.search(r'(\d+(?:\.\d+)?)\s*w', value, re.IGNORECASE)
                    if match:
                        data['power_watts'] = f"{match.group(1)}W"
                if not data.get('battery_mah') and 'batt' in label:
                    match = re.search(r'(\d{3,5})\s*m\s*ah', value, re.IGNORECASE)
                    if match:
                        data['battery_mah'] = int(match.group(1))

    # Strategy 4: Fallback to full page text
    if not data.get('freq_bands_tx') and not data.get('power_watts'):
        text_specs = extract_specs_from_text(page_text, source_name='page_text')
        for k, v in text_specs.items():
            if v and not data.get(k):
                data[k] = v

    # USB-C detection
    if 'type-c' in page_text.lower() or 'usb c' in page_text.lower():
        data['usb_c_charging'] = True

    return data


def enrich_specs_from_product_url(url):
    """Fetch and parse a product page for radio specifications.

    Downloads the HTML at *url*, extracts visible text, runs the generic
    spec-extraction pipeline, then applies any domain-specific parsers
    (Temu, Radioddity, AliExpress, Retevis) on top.

    Every significant data point on the way in and out of the pipeline is
    logged so the operator can diagnose scraping problems (missing fields,
    parser failures, or empty pages) from the logfile alone.
    """
    if not url:
        logger.info("Web enrichment skipped — no URL provided")
        return {}

    domain = _domain_from_url(url)
    logger.info(
        "Web enrichment fetch start url=%s domain=%s",
        url, domain,
    )

    # ── HTTP fetch ───────────────────────────────────────────────────
    try:
        response = requests.get(
            url,
            timeout=15,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (compatible; RadioTrackerBot/1.0)'
                ),
            },
        )
        response.raise_for_status()
    except requests.exceptions.SSLError:
        logger.warning(
            "Web enrichment SSL failed, retrying without verification "
            "url=%s domain=%s", url, domain,
        )
        try:
            response = requests.get(
                url,
                timeout=15,
                verify=False,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (compatible; RadioTrackerBot/1.0)'
                    ),
                },
            )
            response.raise_for_status()
            logger.info(
                "Web enrichment fetch ok (no-verify) url=%s domain=%s "
                "status=%s", url, domain, response.status_code,
            )
        except requests.RequestException:
            logger.exception(
                "Web enrichment fetch failed url=%s domain=%s", url, domain,
            )
            return {}
    except requests.RequestException:
        logger.exception(
            "Web enrichment fetch failed url=%s domain=%s", url, domain,
        )
        return {}

    content_type = response.headers.get('Content-Type', '')
    content_length = len(response.content or b'')
    logger.info(
        "Web enrichment fetch ok url=%s domain=%s "
        "status=%s content_type=%s content_bytes=%s",
        url, domain, response.status_code,
        content_type, content_length,
    )

    # ── Text extraction ──────────────────────────────────────────────
    soup = BeautifulSoup(response.text, 'html.parser')
    title = (
        soup.title.string.strip()
        if soup.title and soup.title.string
        else ''
    )
    page_text = soup.get_text(' ', strip=True)
    text_len = len(page_text)
    logger.info(
        "Web enrichment text extracted url=%s domain=%s "
        "title=%s text_chars=%s",
        url, domain, title[:120] if title else '(none)', text_len,
    )

    if text_len < 100:
        logger.warning(
            "Web enrichment short page url=%s domain=%s "
            "text_chars=%s — page may be JS-rendered or paywalled",
            url, domain, text_len,
        )

    # ── Generic spec extraction ──────────────────────────────────────
    extracted = extract_specs_from_text(page_text, source_name=title)
    extracted['website'] = url
    extracted['source_domain'] = domain
    logger.info(
        "Web enrichment generic parse url=%s domain=%s "
        "fields_found=%s keys=%s",
        url, domain,
        sum(1 for v in extracted.values() if v),
        ','.join(k for k, v in extracted.items() if v),
    )

    # ── Domain-specific parser ───────────────────────────────────────
    domain_data = {}
    parser_name = 'none'
    if domain.endswith('temu.com'):
        parser_name = 'temu'
        domain_data = _parse_temu(soup, title, page_text)
    elif domain.endswith('radiooddity.com') or domain.endswith('radioddity.com'):
        parser_name = 'radioddity'
        domain_data = _parse_radioddity(soup, title, page_text)
    elif domain.endswith('aliexpress.com'):
        parser_name = 'aliexpress'
        domain_data = _parse_aliexpress(soup, title, page_text)
    elif domain.endswith('retevis.com'):
        parser_name = 'retevis'
        domain_data = _parse_retevis(soup, title, page_text)
    else:
        parser_name = 'generic_product_page'
        domain_data = _parse_generic_product_page(soup, title, page_text)

    if domain_data:
        logger.info(
            "Web enrichment domain parser url=%s domain=%s "
            "parser=%s fields_found=%s keys=%s",
            url, domain, parser_name,
            sum(1 for v in domain_data.values() if v),
            ','.join(k for k, v in domain_data.items() if v),
        )
    else:
        logger.info(
            "Web enrichment domain parser url=%s domain=%s "
            "parser=%s — returned no data",
            url, domain, parser_name,
        )

    # ── Merge and final summary ──────────────────────────────────────
    for key, value in domain_data.items():
        if value and not extracted.get(key):
            extracted[key] = value

    if 'source_hint' not in extracted:
        extracted['source_hint'] = domain_data.get(
            'source_hint', 'generic',
        )

    # Log every non-empty field so the operator can see exactly what was
    # extracted from the page without needing to open the URL manually.
    final_fields = {
        k: v for k, v in sorted(extracted.items())
        if v and k not in {'website', 'source_domain'}
    }
    logger.info(
        "Web enrichment complete url=%s domain=%s parser=%s "
        "total_fields=%s extracted=%s",
        url, domain, parser_name,
        len(final_fields),
        final_fields,
    )

    return extracted


def merge_extractions(manual_data, web_data):
    merged = dict(manual_data)
    for key, value in web_data.items():
        if key in {'source_domain', 'source_hint'}:
            continue
        if not merged.get(key) and value:
            merged[key] = value
    return merged


def extraction_confidence(extracted):
    score = 0.0
    weighted_fields = {
        'brand': 0.15,
        'model': 0.25,
        'fcc_id': 0.25,
        'freq_bands_tx': 0.1,
        'aprs': 0.1,
        'gps': 0.1,
        'power_watts': 0.05,
    }
    for field, weight in weighted_fields.items():
        if extracted.get(field):
            score += weight
    return round(min(score, 1.0), 3)


def candidate_matches(extracted, top_n=5):
    """Return top candidate radios with a similarity score."""
    target_brand = (extracted.get('brand') or '').lower()
    target_model = (extracted.get('model') or '').lower()
    target_fcc = (extracted.get('fcc_id') or '').lower()

    candidates = []
    radios = Radio.objects.all().only('id', 'brand', 'model', 'fcc_id')
    for radio in radios:
        brand_score = SequenceMatcher(None, target_brand, (radio.brand or '').lower()).ratio() if target_brand else 0.0
        model_score = SequenceMatcher(None, target_model, (radio.model or '').lower()).ratio() if target_model else 0.0
        fcc_score = 1.0 if (target_fcc and (radio.fcc_id or '').lower() == target_fcc) else 0.0
        score = (brand_score * 0.35) + (model_score * 0.45) + (fcc_score * 0.2)

        if score > 0:
            candidates.append({'radio': radio, 'score': round(score, 3)})

    candidates.sort(key=lambda item: item['score'], reverse=True)
    logger.info("Candidate matching completed model=%s brand=%s fcc_id=%s top_n=%s found=%s", target_model, target_brand, target_fcc, top_n, len(candidates[:top_n]))
    return candidates[:top_n]
