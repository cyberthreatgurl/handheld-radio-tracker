import os
import xml.etree.ElementTree as ET
from django.core.management.base import BaseCommand
from radios.models import Brand

RESULTS_XML = os.path.join('data', 'results.xml')

class Command(BaseCommand):
    help = 'Import FCC grantee codes and names from results.xml into Brand table.'

    def handle(self, *args, **options):
        tree = ET.parse(RESULTS_XML)
        root = tree.getroot()
        count = 0
        for row in root.findall('Row'):
            grantee_code = row.findtext('grantee_code', '').strip()
            grantee_name = row.findtext('grantee_name', '').strip()
            if not grantee_code or not grantee_name:
                continue
            brand, created = Brand.objects.get_or_create(
                grantee_code=grantee_code,
                defaults={'name': grantee_name}
            )
            if not created and not brand.name:
                brand.name = grantee_name
                brand.save()
            count += int(created)
        self.stdout.write(self.style.SUCCESS(f'Imported {count} new grantee codes into Brand table.'))
