# pylint: disable=no-member
# no-member: Django ORM metaclass-based managers are undetectable by pylint
import logging
import re

from django.db.models import Q

from .models import Radio


logger = logging.getLogger(__name__)


def _normalize(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _is_white_label_for_oem(radio_brand_name, oem_brand):
    radio_key = _normalize(radio_brand_name)
    if not radio_key:
        return False

    oem_keys = {_normalize(oem_brand.name)}
    if oem_brand.alias:
        oem_keys.add(_normalize(oem_brand.alias))

    return radio_key not in oem_keys


def apply_oem_mapping_for_brand(child_brand):
    """
    Apply parent-brand OEM mapping to existing radios for a grantee brand.

    This lets users manually set relationships like:
    child grantee brand (e.g., WLN) -> parent OEM (e.g., Quanzhou Kaili Electronics Co),
    then backfill existing radios so manufacturer and white-label attributes are consistent.
    """
    if not child_brand or not child_brand.parent_brand:
        return {'matched': 0, 'updated': 0}

    oem_brand = child_brand.parent_brand
    code = (child_brand.grantee_code or '').strip().upper()

    # Resolve the Manufacturer record for the parent OEM brand via its M2M,
    # falling back to the child brand's linked manufacturer.
    oem_manufacturer = oem_brand.manufacturers.first()
    if oem_manufacturer is None:
        oem_manufacturer = child_brand.manufacturers.first()

    query = Q(manufacturer__brands=child_brand)
    if code:
        query |= Q(fcc_id__istartswith=code)

    radios = Radio.objects.filter(query).distinct()
    matched = radios.count()
    updated = 0

    for radio in radios.iterator():
        changed_fields = []

        if (
            oem_manufacturer is not None
            and radio.manufacturer_id != oem_manufacturer.id
        ):
            radio.manufacturer = oem_manufacturer
            changed_fields.append('manufacturer')

        if _is_white_label_for_oem(radio.brand, oem_brand) and not radio.is_a_whitelabel:
            radio.is_a_whitelabel = True
            changed_fields.append('is_a_whitelabel')

        if changed_fields:
            changed_fields.append('updated_at')
            radio.save(update_fields=changed_fields)
            updated += 1

    logger.info(
        "OEM mapping applied child_brand_id=%s child_brand=%s parent_brand_id=%s parent_brand=%s grantee_code=%s matched=%s updated=%s",
        child_brand.pk,
        child_brand.name,
        oem_brand.pk,
        oem_brand.name,
        code,
        matched,
        updated,
    )

    return {'matched': matched, 'updated': updated}
