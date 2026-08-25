# pylint: disable=no-member
"""Management command to import a radio from a product page URL."""
from django.core.management.base import BaseCommand

from radios.site_import import extract_from_url, upsert_radio_from_url


class Command(BaseCommand):
    """Import radios from manufacturer product page URLs."""

    help = 'Import a radio (and optional manual) from a product page URL'

    def add_arguments(self, parser):
        parser.add_argument(
            'urls', nargs='*', type=str,
            help='Product page URLs to import',
        )
        parser.add_argument(
            '--url', action='append', dest='url_options', default=[],
            help='Product page URL to import (repeatable)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Extract and print the result without writing to the database',
        )

    def handle(self, *args, **options):
        urls = options['urls'] + options['url_options']
        if not urls:
            self.stderr.write('No URLs provided.')
            return

        for url in urls:
            self.stdout.write(f'== {url} ==')
            if options['dry_run']:
                result = extract_from_url(url)
                self._print_extraction(result)
            else:
                report = upsert_radio_from_url(url, apply=True)
                self._print_report(report)

    def _print_extraction(self, result):
        self.stdout.write(
            f"brand={result['brand']} model={result['model']} "
            f"part_number={result['part_number']}",
        )
        self.stdout.write(f"errors={result['errors']}")
        self.stdout.write(f"service_hints={result['service_hints']}")
        self.stdout.write(f"manual_urls={result['manual_urls']}")
        self.stdout.write('specs:')
        for key in sorted(result['specs']):
            self.stdout.write(f'  {key}: {result["specs"][key]}')

    def _print_report(self, report):
        if report['errors']:
            self.stderr.write(f"errors={report['errors']}")
            return
        action = 'Created' if report['radio_created'] else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {report['brand']} {report['model']}",
            ),
        )
        self.stdout.write(
            f"part_number={report['part_number']} "
            f"confidence={report['confidence']}",
        )
        self.stdout.write(f"updated_fields={report['updated_fields']}")
        self.stdout.write(f"service_types_added={report['service_types_added']}")
        self.stdout.write(f"manuals={report['manuals']}")
