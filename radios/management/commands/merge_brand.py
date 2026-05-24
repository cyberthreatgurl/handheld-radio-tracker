from django.core.management.base import BaseCommand
from radios.models import Brand, Radio

class Command(BaseCommand):
    help = 'Merge a duplicate/alias brand into a primary canonical brand.'

    def add_arguments(self, parser):
        parser.add_argument('primary_brand_name', type=str, help='The exact name of the canonical brand to keep')
        parser.add_argument('secondary_brand_name', type=str, help='The exact name of the duplicate brand to merge and delete')

    def handle(self, *args, **options):
        primary_name = options['primary_brand_name']
        secondary_name = options['secondary_brand_name']

        try:
            primary = Brand.objects.get(name=primary_name)
        except Brand.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Primary brand "{primary_name}" not found.'))
            return

        try:
            secondary = Brand.objects.get(name=secondary_name)
        except Brand.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Secondary brand "{secondary_name}" not found.'))
            return

        # Migrate all radios from secondary to primary
        updated = Radio.objects.filter(brand=secondary.name).update(brand=primary.name)
        self.stdout.write(self.style.SUCCESS(f'Updated {updated} radios from "{secondary.name}" to "{primary.name}".'))

        # Set the alias if it's not set
        if not primary.alias:
             primary.alias = secondary.name
             primary.save()
             self.stdout.write(self.style.SUCCESS(f'Set alias of "{primary.name}" to "{secondary.name}".'))

        # Copy grantee code if primary doesn't have it
        if not primary.grantee_code and secondary.grantee_code:
            primary.grantee_code = secondary.grantee_code
            primary.save()
            self.stdout.write(self.style.SUCCESS(f'Copied Grantee Code "{secondary.grantee_code}" to primary brand.'))

        # Delete the secondary
        secondary.delete()
        self.stdout.write(self.style.SUCCESS(f'Successfully merged and deleted duplicate brand "{secondary_name}".'))
