import csv
from django.core.management.base import BaseCommand
from radios.models import Brand


class Command(BaseCommand):
    help = 'Import brands and their FCC Grantee Codes from CSV file'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to the CSV file with brands (Name, Grantee_Code, Full_Name, Website, Country)'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing brands before importing'
        )

    def handle(self, *args, **options):
        csv_file = options['csv_file']
        
        if options['clear']:
            self.stdout.write('Clearing existing brands...')
            Brand.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Cleared existing brands'))
        
        self.stdout.write(f'Importing brands from {csv_file}...')
        
        created_count = 0
        updated_count = 0
        error_count = 0
        
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    try:
                        name = row.get('Name', '').strip()
                        grantee_code = row.get('Grantee_Code', '').strip()
                        
                        if not name or not grantee_code:
                            continue
                        
                        # Get or create brand
                        brand, created = Brand.objects.update_or_create(
                            grantee_code=grantee_code,
                            defaults={
                                'name': name,
                                'full_name': row.get('Full_Name', '').strip(),
                                'website': row.get('Website', '').strip(),
                                'country': row.get('Country', '').strip(),
                                'notes': row.get('Notes', '').strip(),
                            }
                        )
                        
                        if created:
                            created_count += 1
                            self.stdout.write(f'Created: {brand}')
                        else:
                            updated_count += 1
                            self.stdout.write(f'Updated: {brand}')
                    
                    except Exception as e:
                        error_count += 1
                        self.stdout.write(
                            self.style.ERROR(
                                f'Error importing {row.get("Name")} ({row.get("Grantee_Code")}): {str(e)}'
                            )
                        )
        
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f'File not found: {csv_file}'))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error reading file: {str(e)}'))
            return
        
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write(self.style.SUCCESS('Import complete!'))
        self.stdout.write(self.style.SUCCESS(f'  Created: {created_count}'))
        self.stdout.write(self.style.SUCCESS(f'  Updated: {updated_count}'))
        if error_count > 0:
            self.stdout.write(self.style.WARNING(f'  Errors: {error_count}'))
        self.stdout.write(self.style.SUCCESS('='*60))
