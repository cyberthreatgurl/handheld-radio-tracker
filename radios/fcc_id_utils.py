import re
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
    if re.match(r'^[A-Z]$', first):
        return 3
    if re.match(r'^[2-9]$', first):
        return 5
    return 0


def split_fcc_id(fcc_id: Optional[str], preferred_grantee_code: Optional[str] = None) -> Tuple[str, str]:
    """Split FCC ID into (grantee_code, product_code) using FCC syntax rules."""
    cleaned = _clean_fcc_id(fcc_id)
    preferred = _clean_grantee(preferred_grantee_code)

    if not cleaned:
        return '', ''

    if '-' in cleaned:
        grantee_code, product_code = cleaned.split('-', 1)
        return grantee_code.strip(), product_code.strip()

    if preferred and cleaned.startswith(preferred) and len(cleaned) > len(preferred):
        return preferred, cleaned[len(preferred):]

    inferred_len = _infer_grantee_len(cleaned)
    if inferred_len and len(cleaned) > inferred_len:
        return cleaned[:inferred_len], cleaned[inferred_len:]

    return cleaned, ''


def normalize_fcc_id_for_lookup(fcc_id: Optional[str], preferred_grantee_code: Optional[str] = None) -> str:
    grantee_code, product_code = split_fcc_id(fcc_id, preferred_grantee_code=preferred_grantee_code)
    if not grantee_code or not product_code:
        return ''
    return f'{grantee_code}-{product_code}'
