# pylint: disable=no-member
"""Purge OET documents that were mis-attributed to the wrong FCC ID.

When the FCC exhibit page for a Change-in-ID (or family) application lists
documents belonging to another FCC ID (e.g. the original equipment), the
ingestion pipeline can attach those documents to the wrong radio.  This command
removes OET documents (and their derived RadioManual records) whose URL
already belongs to a different, known FCC ID.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from radios.fcc_id_utils import fcc_id_stripped_expression, strip_fcc_id_hyphens
from radios.models import RadioManual, RadioOETDocument


class Command(BaseCommand):
    """Delete cross-FCC-ID OET documents from a contaminated FCC ID."""

    help = (
        'Delete OET documents under --fcc-id whose URL belongs to '
        '--source-fcc-id (the true owner).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--fcc-id', type=str, required=True,
            help='Contaminated FCC ID (e.g. 2AZVI-T67).',
        )
        parser.add_argument(
            '--source-fcc-id', type=str, required=True,
            help='True owner FCC ID (e.g. 2AZVIJC-8629).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be deleted without deleting.',
        )

    def handle(self, *args, **options):
        fcc_id = (options['fcc_id'] or '').strip()
        source_fcc_id = (options['source_fcc_id'] or '').strip()
        dry_run = bool(options['dry_run'])

        if not fcc_id or not source_fcc_id:
            self.stderr.write('Both --fcc-id and --source-fcc-id are required.')
            return

        source_urls = set(
            RadioOETDocument.objects.annotate(
                _fcc_stripped=fcc_id_stripped_expression('fcc_id'),
            ).filter(
                _fcc_stripped__iexact=strip_fcc_id_hyphens(source_fcc_id),
            )
            .exclude(document_url='')
            .values_list('document_url', flat=True)
        )
        if not source_urls:
            self.stdout.write(
                f'No OET documents found for source FCC ID {source_fcc_id}.',
            )
            return

        contaminated = list(
            RadioOETDocument.objects.annotate(
                _fcc_stripped=fcc_id_stripped_expression('fcc_id'),
            ).filter(
                _fcc_stripped__iexact=strip_fcc_id_hyphens(fcc_id),
                document_url__in=source_urls,
            )
        )
        manual_qs = RadioManual.objects.filter(
            source_url__in=source_urls,
        ).filter(
            Q(radio__fcc_id__iexact=fcc_id)
            | Q(radio__isnull=True, manual_pdf__contains=f'{fcc_id}_')
        )
        manuals = list(manual_qs)

        self.stdout.write(
            f'Source FCC ID {source_fcc_id}: {len(source_urls)} document URL(s).',
        )
        self.stdout.write(
            f'Contaminated FCC ID {fcc_id}: {len(contaminated)} OET document(s) '
            f'and {len(manuals)} derived manual record(s) to remove.',
        )
        for doc in contaminated:
            self.stdout.write(
                f'  OET pk={doc.pk} radio_id={doc.radio_id} '
                f'view={doc.view_attachment!r} url={doc.document_url}',
            )
        for manual in manuals:
            self.stdout.write(
                f'  Manual pk={manual.pk} radio_id={manual.radio_id} '
                f'url={manual.source_url}',
            )

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — no changes made.'))
            return

        deleted_docs, _ = RadioOETDocument.objects.filter(
            pk__in=[doc.pk for doc in contaminated],
        ).delete()
        deleted_manuals, _ = RadioManual.objects.filter(
            pk__in=[manual.pk for manual in manuals],
        ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f'Deleted {deleted_docs} OET document(s) and '
                f'{deleted_manuals} manual record(s).',
            ),
        )
