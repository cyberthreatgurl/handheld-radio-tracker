import re
import json
import logging
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .fcc_id_utils import normalize_fcc_id_for_lookup
from .models import Radio

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
    except ImportError:
        logger.warning("PDF parser missing pypdf file=%s", file_path)
        return '', {'method': 'none', 'reason': 'pypdf_missing'}

    text_chunks = []
    try:
        reader = PdfReader(file_path)
        logger.info("Manual parse attempt file=%s pages=%s", file_path, len(reader.pages))
        for page in reader.pages:
            text_chunks.append(page.extract_text() or '')
    except (OSError, ValueError):
        logger.exception("Manual direct PDF parse failed file=%s", file_path)
        return '', {'method': 'none', 'reason': 'pdf_parse_error'}

    direct_text = '\n'.join(text_chunks)
    if _has_enough_text(direct_text):
        logger.info("Manual parse used embedded text file=%s text_length=%s", file_path, len(direct_text))
        return direct_text, {'method': 'embedded_text', 'text_length': len(direct_text)}

    # OCR fallback when PDF has little/no embedded text.
    ocr_text = _extract_text_via_ocr(file_path)
    if _has_enough_text(ocr_text):
        logger.info("Manual parse used OCR fallback file=%s text_length=%s", file_path, len(ocr_text))
        return ocr_text, {'method': 'ocr', 'text_length': len(ocr_text)}

    logger.info("Manual parse fallback insufficient text file=%s direct_len=%s ocr_len=%s", file_path, len(direct_text), len(ocr_text))
    return direct_text, {'method': 'embedded_text_low_confidence', 'text_length': len(direct_text), 'ocr_text_length': len(ocr_text)}


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
    cost = _extract_first(r'\$\s*(\d{2,5}(?:\.\d{1,2})?)', text)
    cost_approx = f'${cost}' if cost else ''
    freq_bands_tx = _detect_bands(text)

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
        'power_watts': _clean_text(power_watts),
        'cost_approx': _clean_text(cost_approx),
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


def enrich_specs_from_product_url(url):
    """Framework hook for website enrichment (phase 2 adapters start here)."""
    if not url:
        return {}

    logger.info("Web enrichment API call start url=%s", url)

    try:
        response = requests.get(
            url,
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; RadioTrackerBot/1.0)'},
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Web enrichment API call failed url=%s", url)
        return {}

    soup = BeautifulSoup(response.text, 'html.parser')
    page_text = soup.get_text(' ', strip=True)
    title = soup.title.string if soup.title and soup.title.string else ''

    extracted = extract_specs_from_text(page_text, source_name=title)
    extracted['website'] = url
    extracted['source_domain'] = _domain_from_url(url)

    # Domain-specific parsers
    domain = extracted['source_domain']
    domain_data = {}
    if domain.endswith('temu.com'):
        domain_data = _parse_temu(soup, title, page_text)
    elif domain.endswith('radiooddity.com') or domain.endswith('radioddity.com'):
        domain_data = _parse_radioddity(soup, title, page_text)
    elif domain.endswith('aliexpress.com'):
        domain_data = _parse_aliexpress(soup, title, page_text)
    else:
        domain_data = {'source_hint': 'generic'}

    for key, value in domain_data.items():
        if value and not extracted.get(key):
            extracted[key] = value

    if 'source_hint' not in extracted:
        extracted['source_hint'] = domain_data.get('source_hint', 'generic')

    logger.info("Web enrichment parsed url=%s domain=%s source_hint=%s", url, extracted.get('source_domain', ''), extracted.get('source_hint', ''))

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
