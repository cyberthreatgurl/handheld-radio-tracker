"""
Script to ingest FCC XML search results for radios, using results.xml for grantee code lookup.
"""
import os
import django
import xml.etree.ElementTree as ET
from datetime import datetime

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'radio_database.settings')
django.setup()

from radios.models import Radio, Brand

XML_PATH = 'authorization_search_results.xml'  # Update path if needed
RESULTS_XML = os.path.join('data', 'results.xml')

def load_grantee_map(results_xml):
    tree = ET.parse(results_xml)
    root = tree.getroot()
    grantee_map = {}
    for row in root.findall('Row'):
        code = row.findtext('grantee_code', '').strip()
        name = row.findtext('grantee_name', '').strip()
        if code:
            grantee_map[code] = name
    return grantee_map

def parse_fcc_xml(xml_path, grantee_map):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    radios = []
    for row in root.findall('Row'):
        fcc_id = row.findtext('fcc_id', '').strip()
        if not fcc_id or '-' not in fcc_id:
            continue
        grantee_code, model = fcc_id.split('-', 1)
        grantee_code = grantee_code.strip()
        model = model.strip()
        brand_name = grantee_map.get(grantee_code, grantee_code)
        grant_date = row.findtext('grant_date', '').strip()
        lower_freq = row.findtext('lower_freq_mhz', '').strip()
        upper_freq = row.findtext('upper_freq_mhz', '').strip()
        purpose = row.findtext('application_purpose', '').strip()
        notes = f"FCC Grant Date: {grant_date}; Purpose: {purpose}; Freq: {lower_freq}-{upper_freq} MHz"
        radios.append({
            'brand': brand_name,
            'model': model,
            'fcc_id': fcc_id,
            'notes': notes,
        })
    return radios

def ingest_radios(radios):
    count = 0
    for radio in radios:
        if Radio.objects.filter(brand=radio['brand'], model=radio['model']).exists():
            continue
        Radio.objects.create(
            brand=radio['brand'],
            model=radio['model'],
            fcc_id=radio['fcc_id'],
            notes=radio['notes'],
        )
        count += 1
    print(f"Imported {count} new radios.")

if __name__ == '__main__':
    grantee_map = load_grantee_map(RESULTS_XML)
    radios = parse_fcc_xml(XML_PATH, grantee_map)
    ingest_radios(radios)
