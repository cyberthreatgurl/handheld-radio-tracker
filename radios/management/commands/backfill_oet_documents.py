import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from radios.fcc_utils import _extract_oet_documents_from_html, _parse_date_only
from radios.fcc_id_utils import fcc_id_stripped_expression, strip_fcc_id_hyphens
from radios.models import Radio, RadioOETDocument


def _normalize_header(value):
    return ''.join(ch for ch in (value or '').lower() if ch.isalnum())


def _first_present(row, aliases):
    for alias in aliases:
        if alias in row and str(row.get(alias, '')).strip():
            return str(row.get(alias, '')).strip()
    return ''


def _parse_oet_rows_from_csv(file_path):
    rows = []
    with file_path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            normalized = {
                _normalize_header(key): (value or '').strip()
                for key, value in raw.items()
            }
            row = {
                'view_attachment': _first_present(
                    normalized,
                    ['viewattachment', 'attachment', 'filename', 'documentname'],
                ),
                'exhibit_type': _first_present(
                    normalized,
                    ['exhibittype', 'type', 'documenttype'],
                ),
                'date_submitted_to_fcc': _first_present(
                    normalized,
                    ['datesubmittedtofcc', 'datesubmitted', 'submitteddate'],
                ),
                'display_type': _first_present(
                    normalized,
                    ['displaytype', 'filetype', 'mime', 'format'],
                ),
                'date_available': _first_present(
                    normalized,
                    ['dateavailable', 'availabledate'],
                ),
                'document_url': _first_present(
                    normalized,
                    ['documenturl', 'attachmenturl', 'url', 'href'],
                ),
            }
            if any(row.values()):
                rows.append(row)
    return rows


class Command(BaseCommand):
    help = 'Backfill OET documents for an FCC ID from a saved FCC exhibits HTML or CSV file.'

    def add_arguments(self, parser):
        parser.add_argument('--fcc-id', type=str, required=True, help='FCC ID (e.g. 2AJGM-NA32UV).')
        parser.add_argument(
            '--source-file',
            type=str,
            required=True,
            help='Path to a saved FCC exhibits HTML (.html/.htm) or CSV (.csv) file.',
        )
        parser.add_argument(
            '--base-url',
            type=str,
            default='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm',
            help='Base URL used to resolve relative links found in HTML sources.',
        )
        parser.add_argument(
            '--purge-existing',
            action='store_true',
            help='Delete existing OET docs for this FCC ID on matching radios before import.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and report rows without writing to the database.',
        )

    def handle(self, *args, **options):
        fcc_id = (options['fcc_id'] or '').strip()
        source_path = Path(options['source_file']).expanduser()
        base_url = (options['base_url'] or '').strip()
        purge_existing = bool(options['purge_existing'])
        dry_run = bool(options['dry_run'])

        if not fcc_id:
            raise CommandError('Missing --fcc-id value.')
        if not source_path.exists() or not source_path.is_file():
            raise CommandError(f'Source file not found: {source_path}')

        radios = list(
            Radio.objects.annotate(
                _fcc_stripped=fcc_id_stripped_expression('fcc_id'),
            ).filter(_fcc_stripped__iexact=strip_fcc_id_hyphens(fcc_id))
        )
        if not radios:
            raise CommandError(f'No radios found with fcc_id={fcc_id}.')

        suffix = source_path.suffix.lower()
        if suffix in {'.html', '.htm'}:
            html_text = source_path.read_text(encoding='utf-8', errors='ignore')
            parsed_rows = _extract_oet_documents_from_html(html_text, base_url=base_url)
        elif suffix == '.csv':
            parsed_rows = _parse_oet_rows_from_csv(source_path)
        else:
            raise CommandError('Unsupported source format. Use .html/.htm or .csv.')

        if not parsed_rows:
            self.stdout.write(self.style.WARNING('No OET rows were parsed from the source file.'))
            return

        self.stdout.write(
            f'Parsed {len(parsed_rows)} OET rows from {source_path.name} for FCC ID {fcc_id}. '
            f'Matching radios: {len(radios)}'
        )

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run enabled; no database writes performed.'))
            return

        if purge_existing:
            deleted_count, _ = RadioOETDocument.objects.filter(
                radio__in=radios,
                fcc_id__iexact=fcc_id,
            ).delete()
            self.stdout.write(f'Purged {deleted_count} existing OET rows before import.')

        created = 0
        updated = 0
        skipped = 0

        for radio in radios:
            for row in parsed_rows:
                view_attachment = (row.get('view_attachment') or '').strip()
                document_url = (row.get('document_url') or '').strip()
                if not view_attachment and not document_url:
                    skipped += 1
                    continue

                defaults = {
                    'exhibit_type': (row.get('exhibit_type') or '').strip(),
                    'date_submitted_to_fcc': _parse_date_only(row.get('date_submitted_to_fcc')),
                    'display_type': (row.get('display_type') or '').strip(),
                    'date_available': _parse_date_only(row.get('date_available')),
                }

                _, was_created = RadioOETDocument.objects.update_or_create(
                    radio=radio,
                    fcc_id=fcc_id,
                    document_url=document_url,
                    view_attachment=view_attachment,
                    defaults=defaults,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Backfill complete for {fcc_id}: created={created}, '
                f'updated={updated}, skipped={skipped}.'
            )
        )
