import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Brand
from .oem_relationships import apply_oem_mapping_for_brand


logger = logging.getLogger(__name__)


@receiver(post_save, sender=Brand)
def apply_oem_mapping_on_brand_save(sender, instance, created, update_fields, **kwargs):
    """
    When a user manually associates a grantee brand to a parent OEM brand,
    automatically propagate that relationship to existing radios.
    """
    if not instance.parent_brand_id:
        return

    # Run when record is created, when update fields are unknown, or when relevant fields changed.
    if update_fields is not None:
        relevant = {'parent_brand', 'grantee_code'}
        if not (set(update_fields) & relevant):
            return

    try:
        result = apply_oem_mapping_for_brand(instance)
        logger.info(
            "OEM mapping trigger source=brand_save brand_id=%s brand=%s matched=%s updated=%s created=%s",
            instance.pk,
            instance.name,
            result.get('matched', 0),
            result.get('updated', 0),
            created,
        )
    except Exception:
        logger.exception(
            "OEM mapping trigger failed source=brand_save brand_id=%s brand=%s",
            instance.pk,
            instance.name,
        )
