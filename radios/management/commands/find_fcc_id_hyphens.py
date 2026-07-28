"""Fix not-found FCC IDs by comparing stripped (no-hyphen) forms locally.

Reads a CSV of not-found FCC IDs (from ``Validate All FCC IDs vs FCC Database``
output) and compares each against valid FCC IDs already in the local database
(those with OET documents downloaded).  If the stripped forms match, the
not-found radio record is a duplicate with wrong hyphen placement and is
deleted.  Use ``--dry-run`` to preview without deleting.
"""

import csv
import logging

from django.core.management.base import BaseCommand

from radios.models import Radio, RadioOETDocument

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Match not-found FCC IDs against local valid FCC IDs by comparing "
        "stripped (no-hyphen) forms.  Deletes duplicate radios that only "
        "differ in hyphen placement."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'input_csv',
            help="Path to the nofinds CSV (NOT_FOUND,FCC_ID,company,product_code)",
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Preview matches without deleting anything",
        )

    def handle(self, *args, **options):
        input_path = options['input_csv']
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no deletions will be made"))

        # Read nofinds CSV
        nofinds = self._read_csv(input_path)
        self.stdout.write(f"Read {len(nofinds)} not-found FCC IDs from {input_path}")

        # Build {stripped_fcc_id: corrected_fcc_id} from valid radios
        valid_map = self._build_valid_map()
        self.stdout.write(
            f"Built lookup of {len(valid_map)} valid FCC IDs "
            f"(radios with OET documents)"
        )

        matched = []
        unmatched = []
        deleted = 0

        for row in nofinds:
            nofind_fcc = row['fcc_id']
            stripped = self._strip(nofind_fcc)

            if stripped in valid_map:
                corrected = valid_map[stripped]
                radio_ids = self._find_radios_by_fcc(nofind_fcc)
                radio_list = ', '.join(str(r) for r in radio_ids)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"MATCH: {nofind_fcc} → {corrected}  "
                        f"(radio IDs: {radio_list})"
                    ),
                )

                if not dry_run and radio_ids:
                    count, _details = Radio.objects.filter(
                        id__in=radio_ids,
                    ).delete()
                    deleted += count
                    self.stdout.write(f"  Deleted {count} record(s)")

                matched.append({
                    'original': nofind_fcc,
                    'corrected': corrected,
                    'company': row.get('company', ''),
                    'product_code': row.get('product_code', ''),
                    'radio_ids': radio_list,
                })
            else:
                unmatched.append(nofind_fcc)

        self.stdout.write(
            f"\nSummary: {len(matched)} matched, "
            f"{len(unmatched)} still not found, "
            f"{deleted} records deleted",
        )

    @staticmethod
    def _strip(fcc_id):
        """Remove all hyphens and uppercase."""
        return fcc_id.replace('-', '').strip().upper()

    def _build_valid_map(self):
        """Build {stripped_fcc_id: original_fcc_id} for radios with OET docs."""
        valid_ids = (
            RadioOETDocument.objects
            .exclude(radio__fcc_id='')
            .values_list('radio__fcc_id', flat=True)
            .distinct()
        )
        mapping = {}
        for fcc_id in valid_ids:
            if fcc_id:
                stripped = self._strip(fcc_id)
                mapping.setdefault(stripped, fcc_id)
        return mapping

    def _find_radios_by_fcc(self, fcc_id):
        """Return list of radio IDs that have this exact FCC ID."""
        return list(
            Radio.objects
            .filter(fcc_id__iexact=fcc_id)
            .values_list('id', flat=True)
        )

    def _read_csv(self, path):
        """Read the nofinds CSV and return list of dicts."""
        rows = []
        with open(path, newline='') as f:
            reader = csv.reader(f)
            for line in reader:
                if len(line) < 2:
                    continue
                rows.append({
                    'status': line[0].strip() if len(line) > 0 else '',
                    'fcc_id': line[1].strip() if len(line) > 1 else '',
                    'company': line[2].strip() if len(line) > 2 else '',
                    'product_code': line[3].strip() if len(line) > 3 else '',
                })
        return rows
