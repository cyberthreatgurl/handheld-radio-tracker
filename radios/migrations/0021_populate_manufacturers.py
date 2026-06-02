"""
Data migration: seed the Manufacturer table from Brand records whose full_name
contains a recognised corporate-entity suffix (Inc, Ltd, Corp, etc.).

For each qualifying Brand:
  - A Manufacturer is created with full_name = brand.full_name
  - alias is set to brand.name  (the short commercial label)
  - The brand is linked via the M2M  manufacturer.brands
"""

import re

from django.db import migrations

# Suffixes that unambiguously identify a legal entity, not a brand label.
_CORPORATE_RE = re.compile(
    r'\b(Inc\.?|Ltd\.?|L\.?L\.?C\.?|Corp\.?|Corporation|Co\.,?\s*Ltd\.?|'
    r'Co\.,?\s*Limited|GmbH|S\.A\.|PLC|Limited)\b',
    re.IGNORECASE,
)


def _seed_manufacturers(apps, schema_editor):
    Brand = apps.get_model('radios', 'Brand')
    Manufacturer = apps.get_model('radios', 'Manufacturer')

    for brand in Brand.objects.exclude(full_name='').order_by('name'):
        if not _CORPORATE_RE.search(brand.full_name):
            continue

        mfr, _ = Manufacturer.objects.get_or_create(
            full_name=brand.full_name,
            defaults={
                'alias': brand.name,
                'country': brand.country,
                'website': brand.website,
                'notes': brand.notes,
            },
        )
        mfr.brands.add(brand)


def _unseed_manufacturers(apps, schema_editor):
    Manufacturer = apps.get_model('radios', 'Manufacturer')
    Manufacturer.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0020_manufacturer_model'),
    ]

    operations = [
        migrations.RunPython(_seed_manufacturers, reverse_code=_unseed_manufacturers),
    ]
