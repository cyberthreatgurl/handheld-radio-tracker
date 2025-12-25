import csv
from django.core.management.base import BaseCommand
from radios.models import Radio


class Command(BaseCommand):
    help = 'Import radios from CSV file (merged_master_with_fcc.csv)'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to the CSV file to import'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing radios before importing'
        )

    def handle(self, *args, **options):
        csv_file = options['csv_file']
        
        if options['clear']:
            self.stdout.write('Clearing existing radios...')
            Radio.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Cleared existing radios'))
        
        self.stdout.write(f'Importing from {csv_file}...')
        
        created_count = 0
        updated_count = 0
        error_count = 0
        
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    try:
                        # Skip rows with empty brand or model
                        brand = row.get('Brand', '').strip()
                        model = row.get('Model', '').strip()
                        
                        if not brand or not model:
                            continue
                        
                        # Parse intro year - handle empty strings
                        intro_year_str = row.get('Intro Year', '').strip()
                        intro_year = None
                        if intro_year_str and intro_year_str.replace('.', '').isdigit():
                            intro_year = int(float(intro_year_str))
                        
                        # Parse battery capacity - handle empty strings
                        battery_str = row.get('Battery (mAh)', '').strip()
                        battery_mah = None
                        if battery_str:
                            # Extract just the number (e.g., "3100" from "3100 (Adv)")
                            import re
                            match = re.search(r'\d+', battery_str)
                            if match:
                                battery_mah = int(match.group())
                        
                        # Get or create radio
                        radio, created = Radio.objects.update_or_create(
                            brand=brand,
                            model=model,
                            defaults={
                                'fcc_id': row.get('FCC_ID', '').strip(),
                                'intro_year': intro_year,
                                'freq_bands_tx': row.get('Freq. Bands (TX)', '').strip(),
                                'power_watts': row.get('Power (W)', '').strip(),
                                'frequency_range': row.get('Frequency Range', '').strip(),
                                'power_output': row.get('Power Output', '').strip(),
                                'modulation': row.get('Modulation', '').strip(),
                                'bands': row.get('Bands', '').strip(),
                                'digital_modes': row.get('Digital Modes', '').strip(),
                                'channels': row.get('Channels', '').strip(),
                                'satellite_tracking': row.get('Satellite Tracking', '').strip(),
                                'harmonic_suppression': row.get('Harmonic Suppression Status', '').strip(),
                                'gps': row.get('GPS', '').strip(),
                                'aprs': row.get('APRS', '').strip(),
                                'air_band': row.get('Air Band', '').strip(),
                                'dmr': row.get('DMR', '').strip(),
                                'display': row.get('Display', '').strip(),
                                'battery_mah': battery_mah,
                                'cost_approx': row.get('Cost (Approx)', '').strip(),
                                'rebadges_clones': row.get('Known Rebadges / Clones', '').strip(),
                                'website': row.get('Website', '').strip(),
                                'notes': '',
                            }
                        )
                        
                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                        
                        if (created_count + updated_count) % 100 == 0:
                            self.stdout.write(f'Processed {created_count + updated_count} radios...')
                    
                    except Exception as e:
                        error_count += 1
                        self.stdout.write(
                            self.style.ERROR(
                                f'Error importing {row.get("Brand")} {row.get("Model")}: {str(e)}'
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
