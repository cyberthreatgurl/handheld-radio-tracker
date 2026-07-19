from typing import Optional, Tuple


def _clean_fcc_id(value: Optional[str]) -> str:
    return (value or '').strip().upper().replace(' ', '')


def _clean_grantee(value: Optional[str]) -> str:
    return (value or '').strip().upper().replace(' ', '')


def _infer_grantee_len(compact_fcc_id: str) -> int:
    if not compact_fcc_id:
        return 0

    first = compact_fcc_id[0]
    # FCC rule: grantee starts with A-Z => 3 chars, starts with 2-9 => 5 chars.
    if 'A' <= first <= 'Z':
        return 3
    if '2' <= first <= '9':
        return 5
    return 0


def _validate_grantee_code(grantee_code: str) -> bool:
    """Return True if grantee_code passes FCC character constraints.

    FCC grantee codes start with A-Z (3 chars, all letters) or 2-9
    (5 chars, may contain any digit 2-9).  Neither type may contain
    '1' or '0' anywhere in the code.
    """
    if not grantee_code:
        return False
    first = grantee_code[0]
    if 'A' <= first <= 'Z':
        expected_len = 3
        # 3-char alpha codes must be all letters (no digits at all)
        if not all('A' <= c <= 'Z' for c in grantee_code):
            return False
    elif '2' <= first <= '9':
        expected_len = 5
    else:
        return False
    if len(grantee_code) != expected_len:
        return False
    return '1' not in grantee_code and '0' not in grantee_code


def split_fcc_id(
    fcc_id: Optional[str],
    preferred_grantee_code: Optional[str] = None,
) -> Tuple[str, str]:
    """Split FCC ID into (grantee_code, product_code) using FCC syntax rules."""
    cleaned = _clean_fcc_id(fcc_id)
    preferred = _clean_grantee(preferred_grantee_code)

    if not cleaned:
        return '', ''

    if '-' in cleaned:
        hyphen_prefix, hyphen_suffix = cleaned.split('-', 1)
        inferred_len = _infer_grantee_len(hyphen_prefix)
        # When prefix is longer than expected grantee length, the excess
        # belongs to the product code (e.g. Y23DM-568 → grantee=Y23, product=DM-568).
        if inferred_len and len(hyphen_prefix) > inferred_len:
            return hyphen_prefix[:inferred_len], hyphen_prefix[inferred_len:] + '-' + hyphen_suffix
        # When prefix is shorter than expected (e.g. "2A-3456" where grantee should
        # be 5 chars), absorb the missing characters from the start of the suffix.
        if inferred_len and len(hyphen_prefix) < inferred_len:
            needed = inferred_len - len(hyphen_prefix)
            if len(hyphen_suffix) >= needed:
                grantee_code = hyphen_prefix + hyphen_suffix[:needed]
                product_code = hyphen_suffix[needed:]
                if _validate_grantee_code(grantee_code):
                    return grantee_code, product_code
        return hyphen_prefix.strip(), hyphen_suffix.strip()

    if preferred and cleaned.startswith(preferred) and len(cleaned) > len(preferred):
        return preferred, cleaned[len(preferred):]

    inferred_len = _infer_grantee_len(cleaned)
    if inferred_len and len(cleaned) > inferred_len:
        return cleaned[:inferred_len], cleaned[inferred_len:]

    return cleaned, ''


def normalize_fcc_id_for_lookup(
    fcc_id: Optional[str],
    preferred_grantee_code: Optional[str] = None,
) -> str:
    grantee_code, product_code = split_fcc_id(fcc_id, preferred_grantee_code=preferred_grantee_code)
    if not grantee_code or not product_code:
        return ''
    return f'{grantee_code}-{product_code}'
