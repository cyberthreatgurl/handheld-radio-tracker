"""
Migration: Change Radio.manufacturer FK from Brand → Manufacturer.

1. Creates Manufacturer records for the 9 Brand records that had no
   corresponding Manufacturer row.
2. Remaps every Radio.manufacturer_id (Brand PK) to the correct
   Manufacturer PK.
3. Alters the FK column to point at radios_manufacturer.
"""
from django.db import migrations, models
import django.db.models.deletion


# ---------------------------------------------------------------------------
# Brand PK  →  Manufacturer PK  (for the 33 brands that already matched)
# ---------------------------------------------------------------------------
BRAND_TO_MFR = {
    1:   2,   # Alinco         → Alinco Inc.
    2:   3,   # Anytone        → Quanzhou Anytone Technology Co. Ltd.
    3:   4,   # Azden          → Azden Corporation
    5:   5,   # Baojie         → Quanzhou Baojie Electronic Co. Ltd.
    34:  6,   # Bintolk        → Quanzhou Binte Electronic Technology Co., Ltd.
    6:   8,   # Connect Systems→ Connect Systems Inc.
    10:  10,  # Kenwood        → Kenwood Corporation
    96:  12,  # Lisheng (Fujian) → Lisheng (Fujian) …
    9:   13,  # Luiton         → Xiamen Radtel Electronics Co. Ltd.
    18:  13,  # Xiamen Radtel  → Xiamen Radtel Electronics Co. Ltd.
    11:  14,  # Marantz        → Marantz America Inc.
    12:  15,  # Maxon          → Maxon America Inc.
    13:  16,  # Midland        → Midland Radio Corporation
    14:  17,  # Motorola       → Motorola Solutions Inc.
    15:  18,  # Puxing         → Puxing Electronics Co. Ltd.
    91:  19,  # Qixiang        → Qixiang Electron Science & Technology …
    16:  20,  # Quansheng      → Quanzhou Quansheng Electronics Co. Ltd.
    130: 7,   # Quanzhou Chierda → Quanzhou Chierda …
    120: 33,  # Quanzhou Kaili → Quanzhou Walion Electronics Co. Ltd.
    17:  22,  # Radio Shack    → RadioShack Corporation
    19:  23,  # Retevis        → Quanzhou Retevis Technology Co. Ltd.
    21:  24,  # Rexon          → Rexon Electronics Corp.
    22:  25,  # Ritron         → Ritron Inc.
    23:  26,  # Ruyage         → Fujian Ruyage Digital Technology Co. Ltd.
    82:  36,  # Shenzhen Zastone → Quanzhou Zastone Telecommunication …
    25:  29,  # Standard       → Standard Communications Corp.
    27:  31,  # TYT            → Quanzhou TYT Electronics Co. Ltd.
    7:   30,  # Tera           → Quanzhou Hongxun Electronic Technology …
    26:  30,  # Tidradio       → Quanzhou Hongxun Electronic Technology …
    49:  32,  # VERO GLOBAL    → Vero Global Communication Co., Ltd.
    30:  34,  # Wouxun         → Quanzhou Wouxun Electronics Co., Ltd.
    28:  35,  # Yaesu          → Yaesu Musen Co. Ltd.
    31:  36,  # Zastone        → Quanzhou Zastone Telecommunication …
}

# ---------------------------------------------------------------------------
# Brand PK  →  (full_name, alias)  for the 9 that need new Manufacturer rows
# ---------------------------------------------------------------------------
NEW_MANUFACTURERS = {
    125: ("Advanced Electronic Applications", "AEA"),
    77:  ("Fujian Senhaix Electronic Technology Co., Ltd", "Senhaix"),
    94:  ("HENAN ESHOW ELECTRONIC COMMERCE CO., LTD", "ESHOW"),
    107: ("Hiroyasu", "Hiroyasu"),
    8:   ("Icom Incorporated", "Icom"),
    97:  ("Lisheng Communications Co., Ltd.", "Lisheng"),
    102: ("PO FUNG ELECTRONIC (HK) INTERNATIONAL GROUP COMPANY LIMITED", "Baofeng"),
    100: ("TYT ELECTRONICS CO., LTD", "TYT"),
    105: ("Xiamen Aorui Electronic Co., Ltd.", "Aorui"),
}


def remap_manufacturer_fk(apps, schema_editor):
    Radio = apps.get_model('radios', 'Radio')
    Manufacturer = apps.get_model('radios', 'Manufacturer')

    # 1. Create missing Manufacturer records and record their PKs.
    brand_to_mfr = dict(BRAND_TO_MFR)
    for brand_pk, (full_name, alias) in NEW_MANUFACTURERS.items():
        mfr, _ = Manufacturer.objects.get_or_create(
            full_name=full_name,
            defaults={'alias': alias},
        )
        brand_to_mfr[brand_pk] = mfr.pk

    # 2. Remap Radio.manufacturer_id in bulk, one Manufacturer at a time.
    for brand_pk, mfr_pk in brand_to_mfr.items():
        Radio.objects.filter(manufacturer_id=brand_pk).update(manufacturer_id=mfr_pk)

    # 3. Any remaining rows that pointed to a Brand PK not in our map get NULL.
    #    Collect all Manufacturer PKs that now exist so we can detect strays.
    valid_mfr_pks = set(Manufacturer.objects.values_list('pk', flat=True))
    Radio.objects.exclude(
        manufacturer_id__isnull=True
    ).exclude(
        manufacturer_id__in=valid_mfr_pks
    ).update(manufacturer_id=None)


def reverse_remap(apps, schema_editor):
    # Reversing this migration is intentionally a no-op: we cannot reliably
    # recover the original Brand PKs after the column has been altered.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0022_radiomanual_doc_type'),
    ]

    operations = [
        # Step A: Drop the existing Brand FK constraint (db_constraint=False removes
        #         enforcement while leaving the column and state intact).
        migrations.AlterField(
            model_name='radio',
            name='manufacturer',
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                help_text='(interim — no constraint)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='manufactured_models',
                to='radios.brand',
            ),
        ),

        # Step B: Remap integer values from Brand PKs → Manufacturer PKs.
        migrations.RunPython(remap_manufacturer_fk, reverse_code=reverse_remap),

        # Step C: Point the FK at Manufacturer with full constraint enforcement.
        migrations.AlterField(
            model_name='radio',
            name='manufacturer',
            field=models.ForeignKey(
                blank=True,
                help_text='The legal manufacturing entity that built this radio',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='manufactured_models',
                to='radios.manufacturer',
            ),
        ),
    ]
