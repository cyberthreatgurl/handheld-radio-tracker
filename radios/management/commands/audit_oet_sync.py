from collections import Counter

from django.core.management.base import BaseCommand

from radios.fcc_utils import fetch_and_sync_fcc_id
from radios.models import Brand, Radio, RadioOETDocument


def _distinct_non_empty_fcc_ids():
    return Radio.objects.exclude(fcc_id__isnull=True).exclude(fcc_id__exact='').values_list('fcc_id', flat=True).distinct()


def _distinct_non_empty_grantees():
    return Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code__exact='').values_list('grantee_code', flat=True).distinct()


def _summarize_fcc_id(fcc_id):
    radios = list(Radio.objects.filter(fcc_id__iexact=fcc_id).order_by('id'))
    documents = RadioOETDocument.objects.filter(fcc_id__iexact=fcc_id)
    sample_pairs = list(documents.order_by('exhibit_type', 'view_attachment').values_list('view_attachment', 'exhibit_type')[:3])
    return {
        'fcc_id': fcc_id,
        'radio_label': str(radios[0]) if radios else 'NONE',
        'radio_count': len(radios),
        'oet_count': documents.count(),
        'with_file_count': documents.exclude(document_file='').count(),
        'sample_pairs': sample_pairs,
    }


def _summarize_grantee(grantee_code):
    radios = list(Radio.objects.filter(fcc_id__istartswith=grantee_code).order_by('id'))
    radio_ids_with_oet = set(
        RadioOETDocument.objects.filter(radio__in=radios)
        .exclude(radio__isnull=True)
        .values_list('radio_id', flat=True)
    )
    radios_with_oet = sum(1 for radio in radios if radio.id in radio_ids_with_oet)
    return {
        'grantee_code': grantee_code,
        'matching_radio_count': len(radios),
        'radios_with_oet': radios_with_oet,
        'radios_without_oet': len(radios) - radios_with_oet,
    }


class Command(BaseCommand):
    help = 'Audit OET document coverage for FCC IDs and grantee codes, with optional sync-first sampling.'

    def add_arguments(self, parser):
        parser.add_argument('--fcc-id', action='append', default=[], help='Audit a specific FCC ID. Repeatable.')
        parser.add_argument('--grantee', action='append', default=[], help='Audit a specific FCC grantee code. Repeatable.')
        parser.add_argument('--random-fcc-ids', type=int, default=0, help='Audit N random FCC IDs from the Radio table.')
        parser.add_argument('--random-grantees', type=int, default=0, help='Audit N random grantee codes from the Brand table.')
        parser.add_argument('--sync-first', action='store_true', help='Run fetch_and_sync_fcc_id() before auditing each FCC ID or grantee.')
        parser.add_argument('--missing-only', action='store_true', help='Only print FCC ID rows with 0 OET documents.')
        parser.add_argument('--limit', type=int, default=0, help='Cap the number of FCC ID rows printed after filtering.')

    def handle(self, *args, **options):
        explicit_fcc_ids = [value.strip() for value in options['fcc_id'] if value and value.strip()]
        explicit_grantees = [value.strip() for value in options['grantee'] if value and value.strip()]
        random_fcc_ids = max(0, int(options['random_fcc_ids'] or 0))
        random_grantees = max(0, int(options['random_grantees'] or 0))
        sync_first = bool(options['sync_first'])
        missing_only = bool(options['missing_only'])
        limit = max(0, int(options['limit'] or 0))

        fcc_ids = list(dict.fromkeys(explicit_fcc_ids))
        if random_fcc_ids:
            fcc_ids.extend(list(_distinct_non_empty_fcc_ids().order_by('?')[:random_fcc_ids]))
        if not fcc_ids and not explicit_grantees and not random_grantees:
            fcc_ids = list(_distinct_non_empty_fcc_ids().order_by('fcc_id'))
        fcc_ids = list(dict.fromkeys(fcc_ids))

        grantees = list(dict.fromkeys(explicit_grantees))
        if random_grantees:
            grantees.extend(list(_distinct_non_empty_grantees().order_by('?')[:random_grantees]))
        grantees = list(dict.fromkeys(grantees))

        fcc_summaries = []
        for fcc_id in fcc_ids:
            if sync_first:
                fetch_and_sync_fcc_id(fcc_id)
            fcc_summaries.append(_summarize_fcc_id(fcc_id))

        filtered_fcc_summaries = [
            item for item in fcc_summaries
            if not missing_only or item['oet_count'] == 0
        ]
        if limit:
            filtered_fcc_summaries = filtered_fcc_summaries[:limit]

        if fcc_summaries:
            buckets = Counter()
            for item in fcc_summaries:
                count = item['oet_count']
                if count == 0:
                    buckets['0'] += 1
                elif count <= 5:
                    buckets['1-5'] += 1
                elif count <= 10:
                    buckets['6-10'] += 1
                else:
                    buckets['11+'] += 1

            self.stdout.write('FCC ID audit:')
            self.stdout.write(
                f"Breakdown 0={buckets['0']} 1-5={buckets['1-5']} 6-10={buckets['6-10']} 11+={buckets['11+']}"
            )
            for item in filtered_fcc_summaries:
                self.stdout.write(
                    f"FCC {item['fcc_id']} | radio={item['radio_label']} | radios={item['radio_count']} | "
                    f"oet={item['oet_count']} | files={item['with_file_count']} | sample={item['sample_pairs']}"
                )

        grantee_summaries = []
        for grantee_code in grantees:
            if sync_first:
                fetch_and_sync_fcc_id(grantee_code)
            grantee_summaries.append(_summarize_grantee(grantee_code))

        if grantee_summaries:
            self.stdout.write('Grantee audit:')
            for item in grantee_summaries:
                self.stdout.write(
                    f"GRANTEE {item['grantee_code']} | radios={item['matching_radio_count']} | "
                    f"with_oet={item['radios_with_oet']} | zero_oet={item['radios_without_oet']}"
                )
