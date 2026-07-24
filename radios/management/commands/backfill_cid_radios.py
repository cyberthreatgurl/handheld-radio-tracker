"""Management command to backfill Change-in-Identification (CID) radios.

CID filings are re-labels: the FCC ID is a new brand/model sold under an
existing certified device's grant.  The original FCC ID holds the actual
technical data (rule parts, power, emission designators).

This command:
  1. Finds radios whose FCC records indicate a CID filing.
  2. Extracts the original FCC ID from the application purpose text.
  3. Fetches the original FCC ID's metadata (rule parts, service types).
  4. Assigns service types and white-label status to the re-labeled radio.
  5. Optionally performs a full FCC re-sync to refresh CID data.

Usage:
  python manage.py backfill_cid_radios                # dry-run
  python manage.py backfill_cid_radios --apply        # apply changes
  python manage.py backfill_cid_radios --apply --sync-fcc  # re-sync first
  python manage.py backfill_cid_radios --fcc-id 2ASNSRB48P  # single radio
"""

import logging
import time

from django.core.management.base import BaseCommand

from radios.models import Radio
from radios.fcc_utils import (
    _extract_original_fcc_id_from_cid,
    fetch_fcc_secondary_metadata,
    _assign_service_types_from_rule_parts,
    _sync_metadata_cache,
)

logger = logging.getLogger(__name__)


def _detect_cid_radios(fcc_id_filter=None):
    """Find radios that are likely Change-in-Identification filings.

    Checks the radio's notes field for 'Change in Identification' or
    'Original FCC ID' keywords.  Also checks if the radio was flagged
    as a white label during a previous sync.

    Args:
        fcc_id_filter: Optional specific FCC ID to check.

    Returns:
        QuerySet of Radio objects.
    """
    from django.db.models import Q

    qs = Radio.objects.exclude(fcc_id__exact='').exclude(fcc_id__isnull=True)

    if fcc_id_filter:
        qs = qs.filter(fcc_id__iexact=fcc_id_filter)
    else:
        # Find radios whose notes mention CID or original FCC ID
        qs = qs.filter(
            Q(notes__icontains='Change in Identification')
            | Q(notes__icontains='Original FCC ID')
            | Q(notes__icontains='change in identification')
            | Q(is_a_whitelabel=True),
        )

    return qs.distinct().order_by('brand', 'model')


class Command(BaseCommand):
    help = (
        'Backfill Change-in-Identification radios: follow the chain from '
        'a re-label FCC ID to the original certified grant, then apply '
        'rule parts, service types, and white-label status.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply changes (default is dry-run).',
        )
        parser.add_argument(
            '--sync-fcc',
            action='store_true',
            help='Re-sync FCC data before checking CID status.',
        )
        parser.add_argument(
            '--fcc-id',
            help='Process only this specific FCC ID.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        sync_fcc = options['sync_fcc']
        fcc_id_filter = options.get('fcc_id')

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    'DRY RUN — use --apply to actually save changes\n'
                )
            )

        radios = _detect_cid_radios(fcc_id_filter=fcc_id_filter)
        total = radios.count()
        self.stdout.write(f'Found {total} potential CID radios')

        if total == 0:
            return

        processed = 0
        updated = 0
        skipped_no_orig = 0
        skipped_already_set = 0
        errors = 0

        # Clear the sync metadata cache between runs
        _sync_metadata_cache.clear()

        for radio in radios:
            processed += 1
            fcc_id = (radio.fcc_id or '').strip()

            # Optionally re-sync FCC data first to get fresh CID info
            if sync_fcc:
                from radios.fcc_utils import fetch_and_sync_fcc_id
                self.stdout.write(
                    f'  [{processed}/{total}] Syncing {fcc_id} ...',
                    ending='',
                )
                try:
                    fetch_and_sync_fcc_id(
                        fcc_id,
                        force_reload=False,
                        honor_skip_lists=False,
                    )
                    radio.refresh_from_db()
                    self.stdout.write(' done')
                except Exception:
                    self.stdout.write(' FAILED')
                    errors += 1
                    continue

            # Extract original FCC ID from notes or re-fetch
            orig_fcc_id = self._find_original_fcc_id(radio)
            if not orig_fcc_id:
                skipped_no_orig += 1
                if fcc_id_filter or processed <= 5:
                    self.stdout.write(
                        f'  [{processed}/{total}] {radio.brand} {radio.model} '
                        f'({fcc_id}): no original FCC ID found — skip'
                    )
                continue

            # Check if service types already assigned
            existing_types = list(
                radio.service_types.values_list('name', flat=True),
            )
            has_meaningful_types = any(
                t not in ('Part 15 Subpart B', 'Part 15 Subpart C')
                for t in existing_types
            )

            # Fetch original FCC ID metadata
            self.stdout.write(
                f'  [{processed}/{total}] {radio.brand} {radio.model} '
                f'({fcc_id}) -> original {orig_fcc_id}',
                ending='',
            )

            try:
                orig_metadata = fetch_fcc_secondary_metadata(orig_fcc_id)
            except Exception:
                self.stdout.write(' FETCH FAILED')
                errors += 1
                continue

            orig_rule_parts = orig_metadata.get('rule_parts', [])
            if not orig_rule_parts:
                self.stdout.write(' (no rule parts from original)')
                skipped_no_orig += 1
                continue

            self.stdout.write(
                f' rule_parts={orig_rule_parts}'
                + (f' existing_types={existing_types}' if existing_types else ''),
            )

            if apply_changes:
                changes = []

                # Assign service types from original's rule parts
                assigned = _assign_service_types_from_rule_parts(
                    radio, orig_rule_parts,
                )
                if assigned:
                    changes.extend(
                        f'service_type:{name}' for name in assigned
                    )

                # Mark as white label if not already
                if not radio.is_a_whitelabel:
                    radio.is_a_whitelabel = True
                    radio.save(update_fields=['is_a_whitelabel'])
                    changes.append('is_a_whitelabel=True')

                # Store original FCC ID in notes if not already present
                orig_note = f'Original FCC ID: {orig_fcc_id}'
                if orig_note not in (radio.notes or ''):
                    radio.notes = (
                        f'{orig_note}\n{radio.notes or ""}'.strip()
                    )
                    radio.save(update_fields=['notes'])
                    changes.append('notes:original_fcc_id')

                if changes:
                    updated += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'    -> applied: {", ".join(changes)}',
                        ),
                    )
                else:
                    skipped_already_set += 1
                    self.stdout.write('    -> no changes needed')
            else:
                if has_meaningful_types:
                    skipped_already_set += 1
                else:
                    updated += 1

            # Rate limit: be gentle to the FCC API
            if processed % 5 == 0:
                time.sleep(1)

        # Summary
        self.stdout.write('')
        self.stdout.write(f'Total radios checked: {processed}')
        self.stdout.write(f'  Updated/assigned:    {updated}')
        self.stdout.write(f'  Already had types:   {skipped_already_set}')
        self.stdout.write(f'  No original ID found:{skipped_no_orig}')
        self.stdout.write(f'  Errors:              {errors}')

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    '\nDRY RUN complete.  Use --apply to save changes.'
                )
            )

    def _find_original_fcc_id(self, radio):
        """Find the original FCC ID for a CID radio.

        Checks multiple sources:
        1. Radio notes (parsed from FCC filing)
        2. FCC API re-query (getFCCIDList)
        """
        # Strategy 1: Extract from notes
        notes = radio.notes or ''
        orig = _extract_original_fcc_id_from_cid(notes)
        if orig:
            return orig.upper()

        # Strategy 2: Re-query the FCC API for the full application purpose
        from curl_cffi import requests
        import xmltodict

        url = (
            'https://apps.fcc.gov/OETLabServices/getFCCIDList'
            f'?fccId={radio.fcc_id}'
        )
        try:
            response = requests.get(
                url,
                impersonate='chrome124',
                timeout=15,
            )
            if response.status_code == 200:
                data = xmltodict.parse(response.text)
                wrapper = data.get('fCCIDInfoes', {})
                result = wrapper.get('fccidInfo', [])
                records = (
                    [result] if isinstance(result, dict)
                    else (result if result else [])
                )
                for rec in records:
                    purpose = rec.get('applicationPurpose', '')
                    orig = _extract_original_fcc_id_from_cid(purpose)
                    if orig:
                        return orig.upper()
        except Exception:
            logger.debug(
                'CID backfill API query failed for fcc_id=%s',
                radio.fcc_id,
            )

        return ''
