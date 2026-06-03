from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q

from radios.models import Brand, Manufacturer, Radio, delete_radios_and_related


class Command(BaseCommand):
    help = 'Find and optionally remove orphaned radios and manufacturers left behind after brand deletions.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--brand',
            action='append',
            default=[],
            help='Restrict cleanup to orphaned radios with this brand name. Repeatable.',
        )
        parser.add_argument(
            '--grantee',
            action='append',
            default=[],
            help='Restrict cleanup to orphaned radios whose FCC ID starts with this grantee code. Repeatable.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Delete the matching orphaned radios and manufacturers. Without this flag the command is a dry run.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=20,
            help='Maximum number of orphaned radio/manufacturer rows to print (default: 20). Use 0 for no limit.',
        )

    def handle(self, *args, **options):
        brand_filters = [value.strip() for value in options['brand'] if value and value.strip()]
        grantee_filters = [value.strip().upper() for value in options['grantee'] if value and value.strip()]
        apply_changes = bool(options['apply'])
        limit = max(0, int(options['limit'] or 0))
        filtered_run = bool(brand_filters or grantee_filters)

        orphaned_radios = self._build_orphaned_radio_queryset(brand_filters, grantee_filters)
        orphaned_radio_ids = list(orphaned_radios.values_list('id', flat=True))

        self.stdout.write(f'Orphaned radios found: {len(orphaned_radio_ids)}')
        radio_samples = orphaned_radios.order_by('brand', 'model', 'id')
        if limit:
            radio_samples = radio_samples[:limit]
        for radio in radio_samples:
            self.stdout.write(
                f'Radio {radio.pk} | brand={radio.brand} | model={radio.model} | fcc_id={radio.fcc_id or ""}'
            )

        manufacturer_candidates = self._build_orphaned_manufacturer_queryset(
            orphaned_radio_ids,
            scoped=filtered_run,
        )
        manufacturer_ids = list(manufacturer_candidates.values_list('id', flat=True))

        self.stdout.write(f'Orphaned manufacturers found: {len(manufacturer_ids)}')
        manufacturer_samples = manufacturer_candidates.order_by('full_name', 'id')
        if limit:
            manufacturer_samples = manufacturer_samples[:limit]
        for manufacturer in manufacturer_samples:
            self.stdout.write(
                f'Manufacturer {manufacturer.pk} | name={manufacturer.full_name} | radios={manufacturer.radio_count}'
            )

        if not apply_changes:
            self.stdout.write(self.style.WARNING('Dry run only. Re-run with --apply to delete matching records.'))
            return

        with transaction.atomic():
            delete_summary = delete_radios_and_related(orphaned_radios)
            deleted_manufacturers = Manufacturer.objects.filter(id__in=manufacturer_ids).delete()[0]

        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned radios: {delete_summary["radios_deleted"]}'))
        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned manuals: {delete_summary["manuals_deleted"]}'))
        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned test reports: {delete_summary["test_reports_deleted"]}'))
        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned OET documents: {delete_summary["oet_documents_deleted"]}'))
        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned firmware entries: {delete_summary["firmware_deleted"]}'))
        self.stdout.write(self.style.SUCCESS(f'Deleted orphaned manufacturers: {deleted_manufacturers}'))

    def _build_orphaned_radio_queryset(self, brand_filters, grantee_filters):
        queryset = Radio.objects.annotate(
            brand_exists=Exists(
                Brand.objects.filter(name__iexact=OuterRef('brand'))
            )
        ).filter(brand_exists=False)

        if brand_filters:
            brand_query = Q()
            for brand_name in brand_filters:
                brand_query |= Q(brand__iexact=brand_name)
            queryset = queryset.filter(brand_query)

        if grantee_filters:
            grantee_query = Q()
            for grantee_code in grantee_filters:
                grantee_query |= Q(fcc_id__istartswith=grantee_code)
            queryset = queryset.filter(grantee_query)

        return queryset.distinct()

    def _build_orphaned_manufacturer_queryset(self, orphaned_radio_ids, scoped=False):
        if orphaned_radio_ids:
            linked_manufacturer_ids = Radio.objects.filter(id__in=orphaned_radio_ids).exclude(
                manufacturer__isnull=True
            ).values_list('manufacturer_id', flat=True)
            base_queryset = Manufacturer.objects.filter(id__in=linked_manufacturer_ids)
        elif scoped:
            base_queryset = Manufacturer.objects.none()
        else:
            base_queryset = Manufacturer.objects.all()

        return base_queryset.annotate(
            brand_count=Count('brands', distinct=True),
            radio_count=Count('manufactured_models', distinct=True),
            matching_radio_count=Count(
                'manufactured_models',
                filter=Q(manufactured_models__id__in=orphaned_radio_ids),
                distinct=True,
            ),
        ).filter(brand_count=0).filter(
            radio_count=F('matching_radio_count') if orphaned_radio_ids else 0,
        ).distinct()