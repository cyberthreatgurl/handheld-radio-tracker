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
from radios.fcc_id_utils import split_fcc_id
from radios.fcc_utils import _sanitize_fcc_xml

XML_PATH = 'authorization_search_results.xml'  # Update path if needed
RESULTS_XML = os.path.join('data', 'results.xml')


def _parse_xml_file(filepath):
    """Parse an XML file, sanitizing malformed content before parsing.

    FCC XML responses often contain unescaped ampersands in company
    names and addresses, which cause xml.etree.ElementTree to fail.
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()
    sanitized = _sanitize_fcc_xml(raw)
    return ET.fromstring(sanitized)


def load_grantee_map(results_xml):
    root = _parse_xml_file(results_xml)
    grantee_map = {}
    for row in root.findall('Row'):
        code = row.findtext('grantee_code', '').strip()
        name = row.findtext('grantee_name', '').strip()
        if code:
            grantee_map[code] = name
    return grantee_map

def _is_radio_frequency(lower_mhz, upper_mhz):
    """Return True if the frequency range overlaps known two-way radio bands.

    Non-radio devices operate in bands like 2.4 GHz WiFi (2402-2480),
    5 GHz WiFi (5180-5825), NFC (13.56), cellular (700-2100 without
    radio rule parts), etc.  This filter keeps only devices whose
    operating frequencies are characteristic of two-way radios.
    """
    try:
        lower = float(lower_mhz or 0)
        upper = float(upper_mhz or 0)
    except (ValueError, TypeError):
        return False
    if lower <= 0 or upper <= 0:
        return False
    if lower > upper:
        lower, upper = upper, lower

    # Known two-way radio frequency bands (MHz).
    # Any overlap with these bands suggests a two-way radio transmitter.
    RADIO_BANDS = [
        (26.0, 28.0),       # HF CB
        (118.0, 137.0),     # Aviation VHF
        (136.0, 174.0),     # VHF LMR / Marine / MURS
        (400.0, 520.0),     # UHF LMR / GMRS / FRS
        (700.0, 800.0),     # LTE / PoC radios
        (806.0, 941.0),     # 800/900 MHz LMR
        (902.0, 928.0),     # 900 MHz ISM / LoRa
    ]
    for band_low, band_high in RADIO_BANDS:
        if lower <= band_high and upper >= band_low:
            return True
    return False


# Keywords that identify wireless microphones, IEMs, and other
# non-radio audio gear operating in radio-frequency bands (Part 74H).
_NON_RADIO_AUDIO_KEYWORDS = [
    'WIRELESS MICROPHONE', 'WIRELESS MIC', 'HANDHELD MICROPHONE',
    'BODY-PACK', 'BODYPACK', 'BODY PACK', 'IN-EAR MONITOR',
    'IN EAR MONITOR', 'IEM', 'AUDIO TRANSMITTER', 'AUDIO RECEIVER',
    'INTERCOM BELTPACK', 'INTERCOM HEADSET', 'WIRELESS AUDIO',
    'WIRELESS GUITAR', 'PROFESSIONAL MICROPHONE', 'PROFESSIONAL AUDIO',
    'WIRELESS TOUR GUIDE', 'TOUR GUIDE SYSTEM', 'ASSISTIVE LISTENING',
    'HEARING ASSISTANCE', 'SIMULTANEOUS INTERPRETATION',
    'CONFERENCE MICROPHONE', 'CONFERENCE SYSTEM', 'UHF MICROPHONE',
    'VHF MICROPHONE', 'WIRELESS EARPHONE', 'WIRELESS HEADPHONE',
    'STAGE MONITOR', 'WIRELESS STAGE', 'LECTERN MICROPHONE',
    'GOOSENECK MICROPHONE', 'LAVALIER MICROPHONE', 'LAVALIER MIC',
    'LPAS DEVICE', 'DIGITAL HYBRID WIRELESS',
]

# Known wireless microphone / professional audio manufacturers.
_NON_RADIO_AUDIO_MANUFACTURERS = [
    'LECTROSONICS', 'SENNHEISER', 'SHURE',
    'AUDIO-TECHNICA', 'AUDIO TECHNICA', 'AKG ACOUSTICS', 'AKG',
    'BEYERDYNAMIC', 'COUNTRYMAN', 'DPA MICROPHONES',
    'ELECTRO-VOICE', 'ELECTRO VOICE', 'LINE 6', 'MI PRO', 'MIPRO',
    'NEUMANN', 'RANE', 'SABINE', 'SAMSON TECHNOLOGIES', 'SAMSON',
    'SONY PROFESSIONAL', 'TELEX', 'WISYCOM', 'ZAXCOM',
    'CLEAR-COM', 'CLEAR COM', 'RIEDEL', 'ALTEC LANSING',
    'BEHRINGER', 'BOSE', 'DBX', 'KLARK TEKNIK', 'MACKIE',
    'MIDAS', 'PRESONUS', 'QSC', 'ROLAND', 'SOUNDCRAFT',
    'STUDER', 'TASCAM', 'YAMAHA PRO AUDIO', 'MC2 AUDIO',
]


def _is_likely_non_radio_audio(applicant_name, brand, purpose):
    """Return True if the record describes a wireless mic/audio device."""
    text = (
        f"{(applicant_name or '').upper()} | "
        f"{(brand or '').upper()} | "
        f"{(purpose or '').upper()}"
    )
    for keyword in _NON_RADIO_AUDIO_KEYWORDS:
        if keyword in text:
            return True
    for mfr in _NON_RADIO_AUDIO_MANUFACTURERS:
        if mfr in text:
            return True
    return False


def _resolve_brand(grantee_code, grantee_map):
    """Resolve a grantee code to a brand name.

    Checks the XML grantee map first, then falls back to the Brand
    model in the database, then uses the grantee code itself.
    """
    # 1. XML grantee map
    name = grantee_map.get(grantee_code)
    if name:
        return name

    # 2. Brand model in DB (look up by grantee_code)
    brand = Brand.objects.filter(
        grantee_code__iexact=grantee_code,
    ).only('name').first()
    if brand:
        return brand.name

    # 3. Fall back to grantee code itself
    return grantee_code


def parse_fcc_xml(xml_path, grantee_map):
    root = _parse_xml_file(xml_path)
    radios = []
    seen_fcc_ids = set()
    skipped_non_radio = 0
    for row in root.findall('Row'):
        fcc_id = row.findtext('fcc_id', '').strip()
        if not fcc_id:
            continue
        if fcc_id in seen_fcc_ids:
            continue
        seen_fcc_ids.add(fcc_id)

        grantee_code, model = split_fcc_id(fcc_id)
        if not grantee_code or not model:
            print(f"Skipping unparseable FCC ID: {fcc_id}")
            continue

        lower_freq = row.findtext('lower_freq_mhz', '').strip()
        upper_freq = row.findtext('upper_freq_mhz', '').strip()

        # Filter: only import devices operating in radio frequency bands
        if not _is_radio_frequency(lower_freq, upper_freq):
            skipped_non_radio += 1
            continue

        # Denylist: wireless microphones and audio gear (Part 74H)
        # share UHF spectrum with two-way radios but are not radios.
        applicant = row.findtext('applicant_name', '').strip()
        purpose = row.findtext('application_purpose', '').strip()
        brand_name = _resolve_brand(grantee_code, grantee_map)
        if _is_likely_non_radio_audio(applicant, brand_name, purpose):
            skipped_non_radio += 1
            continue

        grant_date = row.findtext('grant_date', '').strip()
        notes = (
            f"FCC Grant Date: {grant_date}; Purpose: {purpose}; "
            f"Freq: {lower_freq}-{upper_freq} MHz"
        )
        radios.append({
            'brand': brand_name,
            'model': model,
            'fcc_id': fcc_id,
            'notes': notes,
        })
    print(f"Skipped {skipped_non_radio} non-radio devices (frequency-based filter).")
    return radios


def ingest_radios(radios):
    count = 0
    skipped_existing = 0
    for radio_data in radios:
        # Check by FCC ID first (most precise match)
        if Radio.objects.filter(fcc_id__iexact=radio_data['fcc_id']).exists():
            skipped_existing += 1
            continue
        # Check by brand + model
        if Radio.objects.filter(
            brand=radio_data['brand'],
            model=radio_data['model'],
        ).exists():
            skipped_existing += 1
            continue
        Radio.objects.create(
            brand=radio_data['brand'],
            model=radio_data['model'],
            fcc_id=radio_data['fcc_id'],
            notes=radio_data['notes'],
        )
        count += 1
    print(f"Imported {count} new radios (skipped {skipped_existing} already existing).")

if __name__ == '__main__':
    grantee_map = load_grantee_map(RESULTS_XML)
    radios = parse_fcc_xml(XML_PATH, grantee_map)
    ingest_radios(radios)
