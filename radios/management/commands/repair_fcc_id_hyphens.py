"""Repair FCC IDs whose hyphen placement was previously re-derived.

An earlier normalization inserted a grantee/product separator hyphen into FCC
IDs that already carried their own hyphens inside the product code (e.g.
``YC2VEV-V8`` was rewritten to ``YC2-VEV-V8``).  The FCC does not consistently
separate the grantee and product codes, so the FCC's own spelling should be
preserved.

This command re-fetches the authoritative FCC ID spelling from the FCC API
(one request per distinct grantee code) and corrects a stored value only when
the FCC returns exactly one spelling for that ID that differs from the stored
value.  Run with ``--dry-run`` (default) to preview, then ``--apply`` to write.
"""

import logging

# pylint: disable=no-member, broad-exception-caught
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# broad-exception-caught: network/parse boundaries in this script
import xmltodict
from curl_cffi import requests as curl_requests
from django.core.management.base import BaseCommand

from radios.fcc_id_utils import split_fcc_id, strip_fcc_id_hyphens
from radios.models import (
    Radio, RadioCertification, RadioFCCTestReport, RadioOETDocument,
)

logger = logging.getLogger(__name__)

_MODELS = (Radio, RadioOETDocument, RadioFCCTestReport, RadioCertification)


class Command(BaseCommand):
    """Restore the FCC's own FCC ID spelling for re-derived IDs."""

    help = (
        'Repair FCC IDs whose hyphen placement was previously rewritten. '
        'Use --apply to write; default is a dry-run report.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply corrections. Without this flag, only a report is printed.',
        )
        parser.add_argument(
            '--limit-grantee',
            type=str,
            help='Only process radios for a single grantee code (debugging).',
        )

    def handle(self, *args, **options):  # pylint: disable=too-many-locals, too-many-branches
        apply_changes = bool(options['apply'])
        limit_grantee = (options['limit_grantee'] or '').strip().upper()
        if not apply_changes:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be written.'))

        # Distinct stored FCC IDs with a hyphen, grouped by grantee code.
        stored = {}
        for (fcc_id,) in Radio.objects.exclude(fcc_id='').values_list('fcc_id'):
            cleaned = (fcc_id or '').strip().upper()
            if '-' not in cleaned:
                continue
            grantee, _product = split_fcc_id(cleaned)
            if not grantee:
                continue
            if limit_grantee and grantee != limit_grantee:
                continue
            stored.setdefault(grantee, set()).add(cleaned)

        corrections = {}
        for grantee, ids in sorted(stored.items()):
            fcc_map = self._fetch_fcc_spellings(grantee)
            if not fcc_map:
                continue
            for current in ids:
                key = strip_fcc_id_hyphens(current)
                spellings = fcc_map.get(key)
                if not spellings or current in spellings:
                    continue
                if len(spellings) == 1:
                    corrections[current] = next(iter(spellings))
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f'  {current}: ambiguous FCC spellings {sorted(spellings)} — skipped'
                        )
                    )

        if not corrections:
            self.stdout.write(self.style.SUCCESS('No corrections needed.'))
            return

        self.stdout.write(
            f'Found {len(corrections)} distinct FCC ID(s) to repair:'
        )
        for current, corrected in sorted(corrections.items()):
            self.stdout.write(f'  {current} -> {corrected}')

        if apply_changes:
            self._apply_corrections(corrections)
        else:
            self.stdout.write(
                self.style.WARNING('Dry run — run with --apply to write corrections.')
            )

    def _apply_corrections(self, corrections):
        """Rewrite the corrected FCC IDs across all FCC-ID-bearing models."""
        total = 0
        for model in _MODELS:
            changed = 0
            for current, corrected in corrections.items():
                changed += model.objects.filter(
                    fcc_id__iexact=current,
                ).update(fcc_id=corrected)
            if changed:
                self.stdout.write(
                    f'  {model.__name__}: corrected {changed} record(s)'
                )
            total += changed
        self.stdout.write(self.style.SUCCESS(f'Corrected {total} record(s).'))

    @staticmethod
    def _fetch_fcc_spellings(grantee):
        """Return {stripped_fcc_id: set(spellings)} for one grantee from the FCC."""
        url = f'https://apps.fcc.gov/OETLabServices/getFCCIDList?fccId={grantee}'
        try:
            response = curl_requests.get(url, impersonate='chrome124', timeout=20)
        except Exception as exc:  # noqa: BLE001 — network boundary
            logger.warning('FCC repair lookup failed grantee=%s error=%s', grantee, exc)
            return {}

        if response.status_code != 200:
            logger.warning(
                'FCC repair lookup non-200 grantee=%s status=%s',
                grantee, response.status_code,
            )
            return {}

        try:
            data = xmltodict.parse(response.text)
        except Exception as exc:
            logger.warning('FCC repair parse failed grantee=%s error=%s', grantee, exc)
            return {}

        if not isinstance(data, dict):
            return {}
        wrapper = data.get('fCCIDInfoes') or {}
        info = wrapper.get('fccidInfo', []) if isinstance(wrapper, dict) else []
        if isinstance(info, dict):
            info = [info]

        fcc_map = {}
        for record in info:
            if not isinstance(record, dict):
                continue
            fcc_id = (record.get('FCCId') or '').strip().upper()
            if not fcc_id:
                continue
            key = strip_fcc_id_hyphens(fcc_id)
            fcc_map.setdefault(key, set()).add(fcc_id)
        return fcc_map
