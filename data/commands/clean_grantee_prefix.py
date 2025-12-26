from django.core.management.base import BaseCommand
from radios.models import Radio

class Command(BaseCommand):
    help = "Remove grantee code prefix from radio model names for a given brand and grantee code, merging duplicates."

    def add_arguments(self, parser):
        parser.add_argument('brand', type=str, help='Brand name (e.g., PO FUNG ELECTRONIC (HK) INTERNATONAL GROUP COMPANY LIMITED)')
        parser.add_argument('grantee_code', type=str, help='Grantee code prefix to remove (e.g., 2AJGM)')

    def handle(self, *args, **options):
        brand = options['brand']
        grantee_code = options['grantee_code']
        radios = Radio.objects.filter(brand=brand, model__startswith=grantee_code)
        count = 0
        merged = 0
        for radio in radios:
            new_model = radio.model[len(grantee_code):].lstrip('-').strip()
            if new_model != radio.model:
                # Check for duplicate
                try:
                    existing = Radio.objects.get(brand=brand, model=new_model)
                    # Merge: keep the most complete, merge notes
                    fields = [
                        'fcc_id','intro_year','freq_bands_tx','power_watts','satellite_tracking','harmonic_suppression','gps','aprs','air_band','dmr','display','battery_mah','cost_approx','rebadges_clones','website','notes'
                    ]
                    for f in fields:
                        val = getattr(existing, f)
                        if not val:
                            setattr(existing, f, getattr(radio, f))
                    # Merge notes
                    if radio.notes and radio.notes not in (existing.notes or ''):
                        existing.notes = (existing.notes or '') + '\n' + radio.notes
                    existing.save()
                    radio.delete()
                    merged += 1
                except Radio.DoesNotExist:
                    radio.model = new_model
                    radio.save()
                    count += 1
        self.stdout.write(self.style.SUCCESS(f"Cleaned grantee code prefix from {count} radios and merged {merged} duplicates for brand '{brand}'."))
