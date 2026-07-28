"""Custom template tags and filters for the radios app."""

import re
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Parenthetical annotations to strip: "(Clone)", "(Variant)", "(Likely)", etc.
_PAREN_STRIP_RE = re.compile(r'\s*\([^)]*\)\s*$')
# FCC ID pattern: 3-5 uppercase alphanum, hyphen, then model
_FCC_ID_RE = re.compile(r'^[A-Z0-9]{3,5}-')
# URL / domain pattern
_URL_RE = re.compile(r'\.(com|org|net|io|co|us)\b', re.IGNORECASE)


@register.filter
def dictget(dictionary, key):
    """Template filter to safely access dict keys.

    Usage: ``{{ my_dict|dictget:key_name }}``
    Returns the value for *key* from *dictionary*, or ``None`` if missing.
    """
    return dictionary.get(key)


@register.filter
def linkify_rebadges(text):
    """Turn brand+model pairs in rebadges/clones text into links to radio records.

    Parses the raw text, splits on commas / newlines / ' / ' delimiters,
    and wraps recognized brand-model pairs in ``<a>`` tags pointing to
    the matching radio's detail page.  Unrecognized segments pass through
    as plain text.

    Usage: ``{{ radio.rebadges_clones|linkify_rebadges }}``
    """
    if not text or not text.strip():
        return ''

    from radios.models import Radio  # pylint: disable=import-outside-toplevel

    # Build a case-insensitive brand→list-of-radios lookup once
    # Build brand→radios lookup + per-brand-word index for fuzzy matching
    known = {}          # brand_lower -> [Radio, ...]
    brand_word_map = {}  # word -> [brand_lower, ...]
    all_radios = []      # (brand_lower, model_lower, Radio) for model-only fallback
    for radio_obj in Radio.objects.only('id', 'brand', 'model').iterator():
        brand_lower = radio_obj.brand.strip().lower()
        known.setdefault(brand_lower, []).append(radio_obj)
        for word in brand_lower.split():
            brand_word_map.setdefault(word, []).append(brand_lower)
        all_radios.append((brand_lower, radio_obj.model.strip().lower(), radio_obj))

    # Split on commas, newlines, and " / "
    raw_segments = re.split(r'[,\n\r]+|\s*/\s*', text)
    result_parts = []

    for raw in raw_segments:
        segment = raw.strip()
        if not segment:
            continue

        # Skip FCC IDs like "2AJGM-UV5RPRO"
        if _FCC_ID_RE.match(segment):
            result_parts.append(segment)
            continue

        # Skip URLs / domains like "icomamerica.com"
        if _URL_RE.search(segment):
            result_parts.append(segment)
            continue

        # Strip parenthetical annotations for lookup only
        clean = _PAREN_STRIP_RE.sub('', segment).strip()

        # Try to find a matching radio
        linked = _match_and_link(clean, known, brand_word_map, all_radios)
        result_parts.append(linked if linked else segment)

    return mark_safe(', '.join(result_parts))


def _match_and_link(segment, known, brand_word_map, all_radios):
    """Try to match *segment* as ``Brand Model`` and return an HTML link string.

    Matching is case-insensitive and handles shorthand brand names like
    "BTech" → "BTECH (BaoFeng Tech)" via word-level prefix lookup.  If the
    brand prefix matched but the model wasn't found under that brand,
    searches all brands that *contain* the matched prefix for the model.

    Returns the anchor HTML if found, or ``None`` if no match.
    """
    segment_lower = segment.lower().strip()
    words = segment.split()

    for prefix_len in range(min(len(words), 4), 0, -1):
        prefix = ' '.join(words[:prefix_len])
        matched_brands = _find_matching_brands(prefix, known, brand_word_map)
        if not matched_brands:
            continue

        model_part = ' '.join(words[prefix_len:]).strip()
        if not model_part:
            continue

        # Try exact brand-in-list + model match first
        for brand_key in matched_brands:
            link = _try_link(brand_key, model_part, known)
            if link:
                return link

        # Model not found under matched brands — widen search to brands
        # that *contain* the prefix (e.g., "Baofeng" in
        # "FUJIAN NAN'AN BAOFENG ELECTRONICS CO.,LTD.").
        from radios.models import Radio as RadioModel  # pylint: disable=import-outside-toplevel,redefined-outer-name,reimported
        prefix_lower = prefix.lower()
        for radio_obj in (
            RadioModel.objects.only('id', 'brand', 'model')
            .filter(model__iexact=model_part, brand__icontains=prefix_lower)
            .iterator()
        ):
            return _format_link(radio_obj)

    # No brand matched at all — try model-only search
    for _brand_lower, model_lower, radio_obj in all_radios:
        if model_lower == segment_lower:
            return _format_link(radio_obj)

    return None


def _find_matching_brands(prefix, known, brand_word_map):
    """Find brand keys matching *prefix* (case-insensitive).

    First tries exact dict lookup, then starts-with, then word-level matching
    for shorthand brand names like "BTech" → "BTECH (BaoFeng Tech)".
    """
    prefix_lower = prefix.lower()

    # Exact match
    if prefix_lower in known:
        return [prefix_lower]

    # Brand starts with prefix
    matches = [b for b in known if b.startswith(prefix_lower)]
    if matches:
        return matches

    # Word-level: each word of prefix must be a prefix of some word in brand
    prefix_words = prefix_lower.split()
    matches = []
    for brand_key in known:
        brand_words = brand_key.split()
        if all(
            any(bw.startswith(pw) for bw in brand_words)
            for pw in prefix_words
        ):
            matches.append(brand_key)
    return matches[:5]  # limit fuzzy matches


def _try_link(brand_key, model_part, known):
    """Try to find a radio with *brand_key* and *model_part* in *known*.

    Returns HTML anchor string or None.
    """
    candidates = known[brand_key]

    if not model_part:
        return None

    model_lower = model_part.lower()

    for radio_obj in candidates:
        if radio_obj.model.strip().lower() == model_lower:
            return _format_link(radio_obj)

    return None


def _format_link(radio_obj):
    """Return an HTML anchor linking to the radio's detail page."""
    return (
        '<a href="/radios/{pk}/" '
        'class="text-indigo-600 hover:text-indigo-900 hover:underline">'
        '{brand} {model}</a>'
    ).format(pk=radio_obj.pk, brand=radio_obj.brand, model=radio_obj.model)
