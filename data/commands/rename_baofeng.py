from django.core.management.base import BaseCommand
from radios.models import Radio

class Command(BaseCommand):
    help = "Rename all radios with brand 'Baofeng' to the official applicant name."

    def handle(self, *args, **options):
        old_brand = 'Baofeng'
        new_brand = 'PO FUNG ELECTRONIC (HK) INTERNATONAL GROUP COMPANY LIMITED'
        qs = Radio.objects.filter(brand=old_brand)
        count = qs.update(brand=new_brand)
        self.stdout.write(self.style.SUCCESS(f"Renamed {count} radios from '{old_brand}' to '{new_brand}'."))