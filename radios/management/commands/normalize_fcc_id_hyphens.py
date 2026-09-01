"""Normalize stored FCC IDs to the canonical GRANTEE-PRODUCT form.

Some records were persisted with the raw FCC API value, which omits the
hyphen between the grantee code and product code (e.g. ``K44524000``
instead of ``K44-524000``), or with the hyphen at the wrong position
(e.g. ``2AJGMUV-82`` instead of ``2AJGM-UV82``).

This command rewrites every FCC ID field to the correctly-hyphenated
``GRANTEE-PRODUCT`` spelling so that all rows share one canonical form.

Run with ``--dry-run`` (default) to preview, then ``--apply`` to write.
"""

import logging

# pylint: disable=no-member
# no-member: Django ORM metaclass-based managers are undetectable by pylint
from django.core.management.base import BaseCommand

from radios.fcc_id_utils import (
    canonical_fcc_id,
    fcc_id_stripped_expression,
    strip_fcc_id_hyphens,
)
from radios.models import (
    Radio, RadioCertification, RadioFCCTestReport, RadioOETDocument,
)

logger = logging.getLogger(__name__)

_MODELS = (
    ('Radio.fcc_id', Radio),
    ('RadioOETDocument.fcc_id', RadioOETDocument),
    ('RadioFCCTestReport.fcc_id', RadioFCCTestReport),
    ('RadioCertification.fcc_id', RadioCertification),
)


class Command(BaseCommand):
    """Canonicalize hyphen placement in stored FCC IDs."""

    help = (
        'Normalize stored FCC IDs to the canonical GRANTEE-PRODUCT form. '
        'Use --apply to write changes; default is a dry-run report.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply the normalization. Without this flag, only a report is printed.',
        )

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        if not apply_changes:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be written.'))

        totals = [self._normalize_model(label, model, apply_changes) for label, model in _MODELS]
        self._report_radio_collisions()

        grand_total = sum(total for total, _changed in totals)
        grand_changed = sum(changed for _total, changed in totals)
        summary = (
            f'Summary: {grand_changed}/{grand_total} FCC ID field(s) '
            'would be normalized' if not apply_changes else
            f'Summary: normalized {grand_changed}/{grand_total} FCC ID field(s)'
        )
        self.stdout.write(self.style.SUCCESS(summary))

    def _normalize_model(self, label, model, apply_changes):
        """Canonicalize one model's fcc_id column and return (total, changed)."""
        mapping = {}
        total = 0

        for (fcc_id,) in model.objects.exclude(fcc_id='').values_list('fcc_id'):
            cleaned = (fcc_id or '').strip().upper()
            if not cleaned:
                continue
            total += 1
            canonical = canonical_fcc_id(cleaned)
            if canonical and canonical != cleaned:
                mapping[cleaned] = canonical

        self.stdout.write(
            f'{label}: {total} rows, {len(mapping)} to normalize',
        )

        if apply_changes:
            # Pure data hygiene — skip save() side effects entirely.
            for old, canonical in mapping.items():
                model.objects.filter(fcc_id__iexact=old).update(fcc_id=canonical)

        return total, len(mapping)

    def _report_radio_collisions(self):
        """Warn about Radio rows that now share a canonical FCC ID."""
        collisions = set()
        seen = {}
        for (fcc_id,) in Radio.objects.exclude(fcc_id='').values_list('fcc_id'):
            key = canonical_fcc_id(fcc_id)
            if not key:
                continue
            if key in seen and seen[key] != fcc_id:
                collisions.add(key)
            seen[key] = fcc_id

        if not collisions:
            return

        self.stdout.write(self.style.WARNING(
            'Potential duplicates after normalization '
            f'({len(collisions)} shared canonical FCC ID(s)):'
        ))
        for key in sorted(collisions)[:50]:
            radios = list(
                Radio.objects.annotate(
                    _fcc_stripped=fcc_id_stripped_expression('fcc_id'),
                ).filter(
                    _fcc_stripped__iexact=strip_fcc_id_hyphens(key),
                ).values_list('id', 'brand', 'model', 'fcc_id'),
            )
            self.stdout.write(f'  {key}:')
            for rid, brand, model, stored_fcc in radios:
                self.stdout.write(
                    f'    id={rid} brand={brand} model={model} '
                    f'fcc_id={stored_fcc}',
                )
