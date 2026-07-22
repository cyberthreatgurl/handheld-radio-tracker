"""Management command to backfill grant_date on Radio records.

Two modes:
1. **Local only** (default, no API calls): extracts earliest OET submission
   date from existing RadioOETDocument records linked to each radio.
2. **With FCC sync** (--sync-fcc): for radios that still lack a grant_date
   after the local pass, trigger a full FCC API query (fetch_and_sync_fcc_id)
   for that FCC ID to pull OET documents, then extract the date.

Heuristic: ``date_submitted_to_fcc`` is used as the grant_date because it
maps to the date the filing was lodged with the FCC, which is the most
reliable indicator of when the device was authorized.
"""

import logging
import time
from datetime import date as date_type

from django.core.management.base import BaseCommand
from django.db.models import Min

from radios.models import Radio, RadioOETDocument

logger = logging.getLogger(__name__)


def _earliest_oet_date_for_fcc_id(fcc_id):
    """Return the earliest date_submitted_to_fcc across all OET docs
    matching this FCC ID, or None."""
    agg = RadioOETDocument.objects.filter(
        fcc_id__iexact=fcc_id,
        date_submitted_to_fcc__isnull=False,
    ).aggregate(earliest=Min('date_submitted_to_fcc'))
    return agg.get('earliest') or None


def _earliest_oet_date_for_radio(radio):
    """Return the earliest date_submitted_to_fcc across OET docs linked
    to this specific Radio record, or None."""
    agg = RadioOETDocument.objects.filter(
        radio=radio,
        date_submitted_to_fcc__isnull=False,
    ).aggregate(earliest=Min('date_submitted_to_fcc'))
    return agg.get('earliest') or None


class Command(BaseCommand):
    help = (
        'Backfill grant_date on Radio records from existing OET document '
        'dates (local) and optionally via FCC API re-query (--sync-fcc).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync-fcc',
            action='store_true',
            help=(
                'After local backfill, trigger FCC API sync for radios '
                'that still lack a grant_date.  This is slow — one HTTP '
                'call per FCC ID.'
            ),
        )
        parser.add_argument(
            '--fcc-id',
            type=str,
            help='Backfill only this specific FCC ID.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without writing anything.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Commit batch size (default 500).',
        )

    def handle(self, *args, **options):
        import os
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
        # pylint: disable=too-many-locals
        sync_fcc = options.get('sync_fcc', False)
        single_fcc_id = options.get('fcc_id')
        dry_run = options.get('dry_run', False)
        batch_size = options.get('batch_size', 500)

        # ── Phase 1: Local backfill from existing OET docs ──────
        radios = Radio.objects.exclude(fcc_id='').filter(grant_date__isnull=True)
        if single_fcc_id:
            radios = radios.filter(fcc_id__iexact=single_fcc_id)

        total = radios.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS('No radios need backfill. Nothing to do.'))
            return

        self.stdout.write(
            f'Phase 1 — Local backfill from existing OET docs: {total} radios'
        )

        updated_local = 0
        still_missing = 0
        batch_updates = []
        radio_ids_fcc_after_local = []

        for radio in radios.iterator(chunk_size=200):
            grant_date = _earliest_oet_date_for_radio(radio)

            # Fallback: search by FCC_ID across all OET docs,
            # not just those linked to this specific radio.
            if grant_date is None:
                grant_date = _earliest_oet_date_for_fcc_id(radio.fcc_id)

            if grant_date is not None:
                if not dry_run:
                    radio.grant_date = grant_date
                    batch_updates.append(radio)
                updated_local += 1
            else:
                still_missing += 1
                if sync_fcc:
                    radio_ids_fcc_after_local.append(radio.id)

            if len(batch_updates) >= batch_size and not dry_run:
                Radio.objects.bulk_update(batch_updates, ['grant_date'])
                self.stdout.write(f'  ... committed {len(batch_updates)} updates')
                batch_updates = []

        if batch_updates and not dry_run:
            Radio.objects.bulk_update(batch_updates, ['grant_date'])
            self.stdout.write(f'  ... committed {len(batch_updates)} updates')

        self.stdout.write(
            self.style.SUCCESS(
                f'Phase 1 complete: {updated_local} updated, '
                f'{still_missing} still missing grant_date.'
                if not dry_run else
                f'Phase 1 dry-run: {updated_local} would be updated, '
                f'{still_missing} would still be missing.'
            )
        )

        # ── Phase 2: FCC API re-query (optional) ───────────────
        if sync_fcc and radio_ids_fcc_after_local:
            self.stdout.write(
                f'Phase 2 — FCC API sync for {len(radio_ids_fcc_after_local)} '
                f'radios still missing grant_date...'
            )
            from radios.fcc_utils import fetch_and_sync_fcc_id
            from radios.fcc_id_utils import split_fcc_id

            synced_grantee_codes = set()
            updated_fcc = 0
            skipped = 0

            for rid in radio_ids_fcc_after_local:
                r = Radio.objects.get(pk=rid)
                grantee_code, _ = split_fcc_id(r.fcc_id)
                query = grantee_code if grantee_code else r.fcc_id

                if query in synced_grantee_codes:
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f'  Would sync {query} ({r.brand} {r.model})')
                    continue

                self.stdout.write(f'  Syncing grantee {query} ({r.brand} {r.model})...')
                try:
                    fetch_and_sync_fcc_id(query)
                except Exception as exc:
                    self.stderr.write(
                        self.style.ERROR(f'  Error syncing {query}: {exc}')
                    )
                    continue

                synced_grantee_codes.add(query)
                updated_fcc += 1
                time.sleep(0.3)  # Rate-limit

            # Re-extract grant_date from newly synced OET docs
            newly_dated = 0
            still_gap = 0
            for rid in radio_ids_fcc_after_local:
                r = Radio.objects.get(pk=rid)
                if r.grant_date is not None:
                    newly_dated += 1
                    continue
                grant_date = _earliest_oet_date_for_radio(r)
                if grant_date is not None:
                    if not dry_run:
                        r.grant_date = grant_date
                        r.save(update_fields=['grant_date'])
                    newly_dated += 1
                else:
                    still_gap += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f'Phase 2 complete: {updated_fcc} FCC IDs synced, '
                    f'{newly_dated} newly dated, '
                    f'{still_gap} still lacking grant_date.'
                )
            )

        # ── Summary ────────────────────────────────────────────
        remaining = Radio.objects.exclude(fcc_id='').filter(grant_date__isnull=True).count()
        if remaining == 0:
            self.stdout.write(self.style.SUCCESS('All radios now have a grant_date!'))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f'{remaining} radios still missing grant_date. '
                    f'Run with --sync-fcc to fetch via FCC API.'
                )
            )
