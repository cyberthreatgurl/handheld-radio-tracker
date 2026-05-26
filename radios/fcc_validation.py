import re
from typing import Dict

from django.db.models import Q

from .fcc_id_utils import split_fcc_id


def _normalize_text(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _brand_identity_keys(brand) -> set:
    keys = set()
    for value in (brand.name, brand.alias, brand.full_name):
        key = _normalize_text(value)
        if key:
            keys.add(key)
    return keys


def _is_valid_grantee_code(grantee_code: str) -> bool:
    code = (grantee_code or '').strip().upper()
    if not code:
        return False

    first = code[0]
    if 'A' <= first <= 'Z':
        return len(code) == 3
    if first in '23456789':
        return len(code) == 5
    return False


def validate_fcc_brand_assignment(fcc_id: str, brand_name: str) -> Dict[str, str]:
    """
    Validate if a brand assignment is consistent with FCC grantee data.

    Returns a dict with status and the preferred resolved brand name:
    - status: ok | white_label_possible | unknown_grantee | invalid_fcc_id
    - resolved_brand_name: recommended brand name to store
    - inferred_grantee_code: parsed from FCC ID when possible
    - grantee_brand_name: Brand mapped by grantee code when known
    """
    provided_brand = (brand_name or '').strip()
    inferred_grantee_code, _product_code = split_fcc_id(fcc_id)

    result = {
        'status': 'invalid_fcc_id',
        'resolved_brand_name': provided_brand,
        'inferred_grantee_code': inferred_grantee_code,
        'grantee_brand_name': '',
        'provided_brand_name': provided_brand,
    }

    if not inferred_grantee_code or not _is_valid_grantee_code(inferred_grantee_code):
        return result

    from .models import Brand

    grantee_brand = Brand.objects.filter(grantee_code__iexact=inferred_grantee_code).only(
        'id', 'name', 'alias', 'full_name', 'grantee_code'
    ).first()
    if grantee_brand:
        result['grantee_brand_name'] = grantee_brand.name

    if not provided_brand:
        if grantee_brand:
            result['status'] = 'ok'
            result['resolved_brand_name'] = grantee_brand.name
        else:
            result['status'] = 'unknown_grantee'
            result['resolved_brand_name'] = inferred_grantee_code
        return result

    if not grantee_brand:
        result['status'] = 'unknown_grantee'
        result['resolved_brand_name'] = provided_brand
        return result

    provided_key = _normalize_text(provided_brand)
    if provided_key and provided_key in _brand_identity_keys(grantee_brand):
        result['status'] = 'ok'
        result['resolved_brand_name'] = grantee_brand.name
        return result

    provided_brand_obj = Brand.objects.filter(
        Q(name__iexact=provided_brand) | Q(alias__iexact=provided_brand) | Q(full_name__iexact=provided_brand)
    ).only('id').first()

    if provided_brand_obj and provided_brand_obj.id == grantee_brand.id:
        result['status'] = 'ok'
        result['resolved_brand_name'] = grantee_brand.name
        return result

    # With heavy industry white-labeling, keep provided brand when FCC syntax is valid.
    result['status'] = 'white_label_possible'
    result['resolved_brand_name'] = provided_brand
    return result