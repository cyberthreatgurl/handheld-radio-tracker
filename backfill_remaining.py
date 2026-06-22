"""One-shot: Backfill remaining grant_dates from existing OET docs."""
import django, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'radio_database.settings'
django.setup()

from radios.models import Radio, RadioOETDocument
from django.db.models import Min

need = list(Radio.objects.exclude(fcc_id='').filter(grant_date__isnull=True))
updated = 0

for r in need:
    agg = RadioOETDocument.objects.filter(
        radio=r, date_submitted_to_fcc__isnull=False
    ).aggregate(earliest=Min('date_submitted_to_fcc'))
    grant_date = agg['earliest']

    if not grant_date:
        agg2 = RadioOETDocument.objects.filter(
            fcc_id__iexact=r.fcc_id, date_submitted_to_fcc__isnull=False
        ).aggregate(earliest=Min('date_submitted_to_fcc'))
        grant_date = agg2['earliest']

    if grant_date:
        r.grant_date = grant_date
        r.save(update_fields=['grant_date'])
        updated += 1

print(f'Updated: {updated}')
print(f'Still missing: {Radio.objects.exclude(fcc_id="").filter(grant_date__isnull=True).count()}')
