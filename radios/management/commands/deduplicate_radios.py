from django.core.management.base import BaseCommand
from radios.models import Radio
from collections import defaultdict

class Command(BaseCommand):
    help = "Deduplicate radios by (brand, model), merging notes and keeping the most complete record."

    def handle(self, *args, **options):
        grouped = defaultdict(list)
        for radio in Radio.objects.all():
            grouped[(radio.brand, radio.model)].append(radio)
        deduped = 0
        for (brand, model), radios in grouped.items():
            if len(radios) > 1:
                # Keep the one with the most non-empty fields
                def score(r):
                    return sum(bool(getattr(r, f)) for f in [
                        'fcc_id','intro_year','freq_bands_tx','power_watts','satellite_tracking','harmonic_suppression','gps','aprs','air_band','dmr','display','battery_mah','cost_approx','rebadges_clones','website','notes'])
                radios = sorted(radios, key=score, reverse=True)
                keep = radios[0]
                # Merge notes from all
                merged_notes = '\n'.join(r.notes for r in radios if r.notes)
                keep.notes = merged_notes
                keep.save()
                # Delete the rest
                for r in radios[1:]:
                    r.delete()
                deduped += len(radios) - 1
        self.stdout.write(self.style.SUCCESS(f"Deduplicated {deduped} radios by (brand, model)."))
