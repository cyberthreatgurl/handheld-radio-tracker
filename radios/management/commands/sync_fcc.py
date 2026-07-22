import time
from django.core.management.base import BaseCommand
from django.utils import timezone
from radios.models import Radio, Brand, FCCSyncState, IgnoredGrantee
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
        parser.add_argument(
            '--all-grantees',
            action='store_true',
            help=(
                'Iterate all Brand grantee codes in the database. '
                'By default uses --since-last-sync date filtering (same as the dashboard button). '
                'Pass --full-history to skip date filtering.'
            ),
        )
        parser.add_argument(
            '--since-last-sync',
            action='store_true',
            default=True,
            help=(
                'When used with --all-grantees, only fetch grants issued since the last '
                'successful sync (stored in FCCSyncState). This is the default behaviour. '
                'Use --full-history to override.'
            ),
        )
        parser.add_argument(
            '--full-history',
            action='store_true',
            help='When used with --all-grantees, ignore the last-sync date and fetch the full grant history.',
        )
        parser.add_argument(
            '--ignore-grantees',
            type=str,
            default='',
            help=(
                'Comma-separated list of grantee codes to skip during --all-grantees. '
                'These are merged with any codes already in the IgnoredGrantee database table. '
                'Example: --ignore-grantees=ICOM,MOTOROLA,YAESU'
            ),
        )

    def handle(self, *args, **options):
        import os
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
        single_id = options['fcc_id']
        all_existing = options['all_existing']
        all_grantees = options['all_grantees']
        full_history = options['full_history']

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

        elif all_grantees:
            sync_state = FCCSyncState.get_instance()
            start_date = None if full_history else sync_state.last_grantee_sync_at
            end_date = timezone.now()

            if full_history:
                self.stdout.write("Fetching full grant history for all known grantees (--full-history)...")
            elif start_date:
                self.stdout.write(
                    f"Fetching grants since last sync ({start_date.strftime('%Y-%m-%d %H:%M UTC')}) "
                    f"for all known grantees..."
                )
            else:
                self.stdout.write(
                    "No previous sync recorded — fetching full grant history for all known grantees..."
                )

            ignored_codes = IgnoredGrantee.ignored_codes()

            # Parse ad-hoc --ignore-grantees list and merge with DB-ignored codes.
            cli_ignore = options.get('ignore_grantees', '') or ''
            if cli_ignore.strip():
                extra_codes = [
                    c.strip().upper()
                    for c in cli_ignore.split(',')
                    if c.strip()
                ]
                ignored_codes = list(set(ignored_codes) | set(extra_codes))
                self.stdout.write(f"Ignoring {len(extra_codes)} ad-hoc grantee(s): {', '.join(extra_codes)}")

            grantees = Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code='')
            if ignored_codes:
                grantees = grantees.exclude(grantee_code__in=ignored_codes)
            self.stdout.write(f"Found {grantees.count()} grantees to process.")

            total_added = 0
            total_updated = 0
            for index, brand in enumerate(grantees, 1):
                self.stdout.write(f"[{index}/{grantees.count()}] Processing grantee {brand.grantee_code}...")
                added, updated, msgs = fetch_and_sync_fcc_id(
                    brand.grantee_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                total_added += added
                total_updated += updated
                if not added and not updated and len(msgs) <= 1:
                    self.stdout.write(self.style.WARNING(f"  No new records for {brand.grantee_code}"))
                time.sleep(0.5)

            # Persist the new baseline so future incremental runs stay fast.
            sync_state.last_grantee_sync_at = end_date
            sync_state.save(update_fields=['last_grantee_sync_at'])

            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Added {total_added}, updated {total_updated} records. "
                    f"Next run will start from {end_date.strftime('%Y-%m-%d %H:%M UTC')}."
                )
            )

        else:
            self.stdout.write(self.style.ERROR(
                "Please provide --fcc-id <id>, --all-existing, or --all-grantees. Run with --help for details."
            ))
