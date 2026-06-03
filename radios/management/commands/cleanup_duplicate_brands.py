import re
from django.core.management.base import BaseCommand
from django.db.models import Q
from radios.models import Brand, Radio


def _normalize_brand_identity(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


class Command(BaseCommand):
    help = 'Clean up duplicate blank-code Brand rows that have matching coded Brand counterparts'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be deleted without actually deleting',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Actually perform the deletion (required unless --dry-run)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']

        if not dry_run and not force:
            self.stdout.write(
                self.style.ERROR(
                    'Must specify either --dry-run (to preview) or --force (to delete). '
                    'Run with --help for details.'
                )
            )
            return

        # Find all blank-code Brand rows
        blank_code_brands = Brand.objects.filter(
            Q(grantee_code__isnull=True) | Q(grantee_code__exact='')
        )

        deletion_candidates = []

        for blank_brand in blank_code_brands:
            # Build normalized keys for this blank Brand
            blank_keys = set()
            for value in (blank_brand.name, blank_brand.alias, blank_brand.full_name):
                key = _normalize_brand_identity(value)
                if key:
                    blank_keys.add(key)

            if not blank_keys:
                continue

            # Look for coded Brands with matching normalized names
            coded_brands = Brand.objects.exclude(
                Q(grantee_code__isnull=True) | Q(grantee_code__exact='')
            ).exclude(pk=blank_brand.pk)

            matching_coded_brand = None
            for coded_brand in coded_brands:
                for value in (coded_brand.name, coded_brand.alias, coded_brand.full_name):
                    if _normalize_brand_identity(value) in blank_keys:
                        matching_coded_brand = coded_brand
                        break
                if matching_coded_brand:
                    break

            if not matching_coded_brand:
                continue

            # Safety check: verify no radios are pointing at this blank Brand's exact name
            radio_count = Radio.objects.filter(brand__iexact=blank_brand.name).count()
            if radio_count > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f'SKIP: Blank Brand "{blank_brand.name}" (id={blank_brand.id}) has {radio_count} '
                        f'radios pointing at it. Coded counterpart: "{matching_coded_brand.name}" '
                        f'(code={matching_coded_brand.grantee_code})'
                    )
                )
                continue

            deletion_candidates.append({
                'blank_brand': blank_brand,
                'coded_brand': matching_coded_brand,
            })

        if not deletion_candidates:
            self.stdout.write(self.style.SUCCESS('No duplicate blank-code Brand rows found to clean up.'))
            return

        self.stdout.write(
            self.style.WARNING(
                f'Found {len(deletion_candidates)} duplicate blank-code Brand rows eligible for deletion:\n'
            )
        )

        for candidate in deletion_candidates:
            blank = candidate['blank_brand']
            coded = candidate['coded_brand']
            self.stdout.write(
                f'  - DELETE: Brand "{blank.name}" (id={blank.id}, no grantee_code)\n'
                f'    REASON: Matches coded Brand "{coded.name}" (id={coded.id}, code={coded.grantee_code})'
            )

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'\n[DRY RUN] Would delete {len(deletion_candidates)} Brand rows. '
                    'Run with --force to actually delete.'
                )
            )
        else:
            deleted_count = 0
            for candidate in deletion_candidates:
                blank = candidate['blank_brand']
                blank.delete()
                deleted_count += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully deleted {deleted_count} duplicate blank-code Brand rows.'
                )
            )
