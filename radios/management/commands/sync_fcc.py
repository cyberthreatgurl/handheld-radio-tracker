import time
from django.core.management.base import BaseCommand
from radios.models import Radio
from radios.fcc_utils import fetch_and_sync_fcc_id

class Command(BaseCommand):
    help = 'Syncs Radio records from the FCC EAS API using curl_cffi to bypass WAF'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fcc-id',
            type=str,
            help='Fetch and sync a specific FCC ID (e.g. 2AJGM-UV5R or 2AJGM)',
        )
        parser.add_argument(
            '--all-existing',
            action='store_true',
            help='Process all existing distinct FCC IDs in the database',
        )

    def handle(self, *args, **options):
        single_id = options['fcc_id']
        all_existing = options['all_existing']

        if single_id:
            self.stdout.write(f"Fetching updates for FCC ID: {single_id}")
            added, updated, messages = fetch_and_sync_fcc_id(single_id)
            for msg in messages:
                self.stdout.write(msg)
            self.stdout.write(self.style.SUCCESS(f"Finished processing. Added {added}, updated {updated} records.\n"))
            
        elif all_existing:
            self.stdout.write("Fetching updates for all existing FCC IDs in Database...")
            fcc_ids = Radio.objects.exclude(fcc_id__isnull=True).exclude(fcc_id__exact='').values_list('fcc_id', flat=True).distinct()
            self.stdout.write(f"Found {fcc_ids.count()} distinct FCC IDs to process.")
            
            for index, fid in enumerate(fcc_ids, 1):
                self.stdout.write(f"[{index}/{fcc_ids.count()}] Processing {fid}...")
                added, updated, messages = fetch_and_sync_fcc_id(fid)
                if not added and not updated and len(messages) <= 1:
                    self.stdout.write(self.style.WARNING(f"No records returned for {fid}"))
                time.sleep(0.5)
        else:
            self.stdout.write(self.style.ERROR("Please provide --fcc-id <id> or --all-existing. Run with --help for details."))
