"""Discover new FCC grantee codes from radio FCC IDs, FCC GenericSearch,
and saved FCC XML files.

Phase 1 scans the local Radio table for unknown grantee prefixes (instant).
Phase 2 queries the FCC GenericSearch via HTTP for new grantees by date
range, or parses a saved XML export file.
"""

import logging
import os
import time
from datetime import timedelta

# Playwright's sync API internally creates an asyncio event loop, which
# causes Django to raise SynchronousOnlyOperation when ORM calls happen
# inside the Playwright context (e.g. after browser-based FCC fallback).
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

from django.core.management.base import BaseCommand
from django.utils import timezone

from radios.fcc_utils import (
    discover_new_grantees_from_fcc,
    fetch_and_sync_fcc_id,
    _parse_grantees_from_xml,
)
from radios.models import Brand, IgnoredGrantee, SyncSkippedGrantee

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Discover new FCC grantee codes from radio FCC IDs, FCC "
        "GenericSearch date-range query, or a saved XML export file."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help="Days back for FCC GenericSearch date range (default: 30)",
        )
        parser.add_argument(
            '--xml-file',
            type=str,
            help=(
                "Path to a saved FCC GenericSearch XML export file "
                "(e.g., from the FCC website's Export button).  Parses "
                "grantee codes from the XML and syncs new ones."
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Preview discovered grantees without syncing",
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.5,
            help="Seconds between FCC API calls per grantee (default: 0.5)",
        )

    def handle(self, *args, **options):
        days = options['days']
        xml_file = options['xml_file']
        dry_run = options['dry_run']
        delay = options['delay']

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN — no sync will occur"),
            )

        discovered = set()

        # Phase 1 + Phase 2: local scan + HTTP GenericSearch
        if not xml_file:
            end_date = timezone.now()
            start_date = end_date - timedelta(days=days)
            self.stdout.write(
                f"Phase 1+2: Scanning local FCC IDs and FCC GenericSearch "
                f"from {start_date.strftime('%m/%d/%Y')} to "
                f"{end_date.strftime('%m/%d/%Y')} ({days} days)",
            )
            fcc_discovered = discover_new_grantees_from_fcc(
                start_date, end_date,
            )
            discovered.update(fcc_discovered)
            if fcc_discovered:
                self.stdout.write(
                    f"  Found {len(fcc_discovered)} grantee(s) from "
                    f"local+FCC: {', '.join(sorted(fcc_discovered)[:20])}"
                    f"{'...' if len(fcc_discovered) > 20 else ''}",
                )

        # Phase 3: parse saved FCC XML export file (if provided)
        if xml_file:
            self.stdout.write(
                f"Phase 3: Parsing FCC XML file: {xml_file}",
            )
            known_codes = set(
                Brand.objects.exclude(grantee_code__isnull=True)
                .exclude(grantee_code='')
                .values_list('grantee_code', flat=True)
            )
            known_codes = {c.strip().upper() for c in known_codes}
            ignored_codes = set(IgnoredGrantee.ignored_codes())
            skipped_codes = set(SyncSkippedGrantee.skipped_codes())
            excluded = known_codes | ignored_codes | skipped_codes

            with open(xml_file) as f:
                xml_text = f.read()
            xml_grantees = _parse_grantees_from_xml(
                xml_text, excluded,
            )
            discovered.update(xml_grantees)
            self.stdout.write(
                f"  Found {len(xml_grantees)} grantee(s) from XML: "
                f"{', '.join(sorted(xml_grantees)[:20])}"
                f"{'...' if len(xml_grantees) > 20 else ''}",
            )

        if not discovered:
            self.stdout.write(
                self.style.WARNING("No new grantees discovered."),
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Discovered {len(discovered)} new grantee(s): "
                f"{', '.join(sorted(discovered))}",
            ),
        )

        if dry_run:
            self.stdout.write("Dry run complete — no grantees synced.")
            return

        synced = 0
        failed = 0
        total = len(discovered)

        for idx, code in enumerate(sorted(discovered), 1):
            self.stdout.write(
                f"[{idx}/{total}] Syncing grantee {code} ... ",
                ending='',
            )
            try:
                added, updated, _msgs = fetch_and_sync_fcc_id(code)
                if added or updated:
                    synced += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"OK — added {added}, updated {updated}"
                        ),
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            "no radio records matched"
                        ),
                    )
            except Exception:
                failed += 1
                self.stdout.write(
                    self.style.ERROR("FAILED"),
                )
                logger.exception(
                    "Grantee discovery sync failed grantee=%s", code,
                )

            time.sleep(delay)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nComplete: {synced} synced, {failed} failed, "
                f"{total} total"
            ),
        )
