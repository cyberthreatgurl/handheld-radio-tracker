from django.db import migrations


def seed_sifang_abbree_oem(apps, schema_editor):
    """Seed the Sifang (2A4R6) OEM <-> Abbree (2AU74) brand relationship.

    Quanzhou Sifang Communication Device Co., Ltd. is the ODM/OEM that
    builds hardware for the Abbree brand.  This links the two so the site
    can show "who builds" vs "who brands/distributes".
    """
    Brand = apps.get_model('radios', 'Brand')
    Manufacturer = apps.get_model('radios', 'Manufacturer')
    Radio = apps.get_model('radios', 'Radio')

    # 1. Ensure the Sifang OEM brand exists and carries its grantee code.
    sifang_brand, _ = Brand.objects.get_or_create(
        name='Quanzhou Sifang Communication Device Co.,Ltd.',
        defaults={
            'grantee_code': '2A4R6',
            'alias': 'Sifang',
            'country': 'China',
        },
    )
    brand_updates = {}
    if not sifang_brand.grantee_code:
        brand_updates['grantee_code'] = '2A4R6'
    if not sifang_brand.alias:
        brand_updates['alias'] = 'Sifang'
    if not sifang_brand.country:
        brand_updates['country'] = 'China'
    if brand_updates:
        for field, value in brand_updates.items():
            setattr(sifang_brand, field, value)
        sifang_brand.save(update_fields=list(brand_updates))

    # 2. Ensure the Sifang Manufacturer exists and is linked to the brand.
    sifang_mfr, _ = Manufacturer.objects.get_or_create(
        full_name='Quanzhou Sifang Communication Device Co.,Ltd.',
        defaults={'alias': 'Sifang', 'country': 'China'},
    )
    mfr_updates = {}
    if not sifang_mfr.alias or sifang_mfr.alias == sifang_mfr.full_name:
        mfr_updates['alias'] = 'Sifang'
    if not sifang_mfr.country:
        mfr_updates['country'] = 'China'
    if mfr_updates:
        for field, value in mfr_updates.items():
            setattr(sifang_mfr, field, value)
        sifang_mfr.save(update_fields=list(mfr_updates))
    sifang_mfr.brands.add(sifang_brand)

    # 3. Link Abbree (2AU74) as a brand the Sifang OEM manufactures for.
    abbree_brand = Brand.objects.filter(grantee_code__iexact='2AU74').first()
    if abbree_brand is not None:
        sifang_mfr.brands.add(abbree_brand)

        abbree_label = abbree_brand.alias or abbree_brand.name
        vendors = [
            item.strip()
            for item in (sifang_brand.white_label_vendors or '').split(',')
            if item.strip()
        ]
        if abbree_label not in vendors and abbree_brand.name not in vendors:
            vendors.append(abbree_label)
            sifang_brand.white_label_vendors = ', '.join(vendors)
            sifang_brand.save(update_fields=['white_label_vendors'])

    # 4. Tag Sifang's TM8118 with its Abbree rebadge (AR-8118).
    for radio in Radio.objects.filter(
        brand__iexact=sifang_brand.name,
        model__iexact='TM8118',
    ):
        rebadges = (radio.rebadges_clones or '').strip()
        if 'AR-8118' not in rebadges:
            radio.rebadges_clones = (
                f"{rebadges}, Abbree AR-8118"
                if rebadges
                else "Abbree AR-8118"
            )
            radio.save(update_fields=['rebadges_clones'])

    # 5. Wire radios carrying Sifang's grantee code to the OEM manufacturer.
    for radio in Radio.objects.filter(fcc_id__istartswith='2A4R6'):
        changed = []
        if radio.manufacturer_id != sifang_mfr.id:
            radio.manufacturer = sifang_mfr
            changed.append('manufacturer')
        brand_key = (radio.brand or '').strip().lower()
        if (
            brand_key
            and brand_key != sifang_brand.name.strip().lower()
            and not radio.is_a_whitelabel
        ):
            radio.is_a_whitelabel = True
            changed.append('is_a_whitelabel')
        if changed:
            radio.save(update_fields=changed)


def reverse_seed(apps, schema_editor):
    """No-op reverse: leave the seeded reference data in place."""
    del apps, schema_editor


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0047_radio_ip_rating'),
    ]

    operations = [
        migrations.RunPython(seed_sifang_abbree_oem, reverse_seed),
    ]
