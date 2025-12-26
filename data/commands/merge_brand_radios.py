from django.core.management.base import BaseCommand
from radios.models import Radio

class Command(BaseCommand):
    help = "Merge all radios from a source brand into a target brand, deduplicating by (brand, model)."

    def add_arguments(self, parser):
        parser.add_argument('source_brand', type=str, help='Brand name to merge from (e.g., Baofeng)')
        parser.add_argument('target_brand', type=str, help='Brand name to merge into (e.g., PO FUNG ELECTRONIC (HK) INTERNATONAL GROUP COMPANY LIMITED)')

    def handle(self, *args, **options):
        source = options['source_brand']
        target = options['target_brand']
        deduped = 0
        # For each model in the source brand
        for radio in Radio.objects.filter(brand=source):
            # Check if a radio with the same model exists in the target brand
            try:
                target_radio = Radio.objects.get(brand=target, model=radio.model)
                # Merge: keep the most complete, merge notes
                fields = [
                    'fcc_id','intro_year','freq_bands_tx','power_watts','satellite_tracking','harmonic_suppression','gps','aprs','air_band','dmr','display','battery_mah','cost_approx','rebadges_clones','website','notes'
                ]
                for f in fields:
                    val = getattr(target_radio, f)
                    if not val:
                        setattr(target_radio, f, getattr(radio, f))
                # Merge notes
                if radio.notes and radio.notes not in (target_radio.notes or ''):
                    target_radio.notes = (target_radio.notes or '') + '\n' + radio.notes
                target_radio.save()
                radio.delete()
                deduped += 1
            except Radio.DoesNotExist:
                # No conflict, just update brand
                radio.brand = target
                radio.save()
        self.stdout.write(self.style.SUCCESS(f"Merged and deduplicated {deduped} radios from '{source}' into '{target}'."))
