from django.core.management.base import BaseCommand
from radios.models import Radio, Brand

class Command(BaseCommand):
    help = "Globally rename a brand in both Radio and Brand tables."

    def add_arguments(self, parser):
        parser.add_argument('old_name', type=str, help='Current brand name to rename')
        parser.add_argument('new_name', type=str, help='New brand name to use')

    def handle(self, *args, **options):
        old_name = options['old_name']
        new_name = options['new_name']
        radio_count = Radio.objects.filter(brand=old_name).update(brand=new_name)
        brand_count = Brand.objects.filter(name=old_name).update(name=new_name)
        self.stdout.write(self.style.SUCCESS(
            f"Renamed {radio_count} radios and {brand_count} brands from '{old_name}' to '{new_name}'."
        ))
