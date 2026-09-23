from django.db import migrations
from django.db.models import Q


OEM_MANUFACTURERS = [
    'PO FUNG ELECTRONIC (HK) INTERNATIONAL GROUP COMPANY LIMITED',
    'Standard Communications Corp.',
]


def backfill_oem_brands(apps, schema_editor):
    """Link downstream brands to their OEM manufacturer via the brands M2M.

    The ``Manufacturer.brands`` relation records every brand label an OEM
    sells under, but several OEMs were only linked implicitly through
    ``Radio.manufacturer``.  This backfills the M2M from the brands that
    actually appear on radios built by the OEM.
    """
    del schema_editor
    Brand = apps.get_model('radios', 'Brand')
    Manufacturer = apps.get_model('radios', 'Manufacturer')
    Radio = apps.get_model('radios', 'Radio')

    for mfr_name in OEM_MANUFACTURERS:
        mfr = Manufacturer.objects.filter(
            full_name__iexact=mfr_name,
        ).first()
        if mfr is None:
            continue

        radio_brands = (
            Radio.objects.filter(manufacturer=mfr)
            .exclude(brand='')
            .values_list('brand', flat=True)
            .distinct()
        )
        for raw_label in radio_brands:
            brand_label = (raw_label or '').strip()
            if not brand_label:
                continue

            brand = Brand.objects.filter(
                Q(name__iexact=brand_label) | Q(alias__iexact=brand_label),
            ).first()
            if brand is None:
                brand, _ = Brand.objects.get_or_create(name=brand_label)

            mfr.brands.add(brand)


def reverse_backfill(apps, schema_editor):
    """No-op reverse: leave the linked brands in place."""
    del apps, schema_editor


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0048_seed_sifang_abbree_oem'),
    ]

    operations = [
        migrations.RunPython(backfill_oem_brands, reverse_backfill),
    ]
