from django.core.management.base import BaseCommand
from radios.models import Radio, Brand

class Command(BaseCommand):
    help = 'Ensure all unique brands in radios table exist in brands table.'

    def handle(self, *args, **options):
        radio_brands = set(Radio.objects.values_list('brand', flat=True))
        existing_brands = set(Brand.objects.values_list('name', flat=True))
        created = 0
        for brand_name in radio_brands:
            if brand_name and brand_name not in existing_brands:
                Brand.objects.create(name=brand_name)
                self.stdout.write(self.style.SUCCESS(f'Created brand: {brand_name}'))
                created += 1
        if created == 0:
            self.stdout.write(self.style.SUCCESS('All radio brands already exist in brands table.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Added {created} new brands.'))
