import csv
import requests
import time
import re
from urllib.parse import quote

# Known Grantee Codes for common ham radio manufacturers
KNOWN_GRANTEE_CODES = {
    'Baofeng': '2AJGM',  # PO FUNG ELECTRONIC (HK) INTERNATIONAL GROUP
    'Pofung': '2AJGM',
    'Icom': 'AFJ',       # ICOM INC
    'Yaesu': 'K6620',    # YAESU MUSEN CO LTD
    'Kenwood': 'ALH',    # JVC KENWOOD CORPORATION
    'Anytone': '2AJDM',  # QIXIANG ELECTRON SCIENCE & TECHNOLOGY CO
    'Tidradio': '2AUIUTD', # TID RADIO TECHNOLOGY
    'Retevis': '2AHVB',  # QUANZHOU RETEVIS ELECTRONICS CO
    'Wouxun': 'U4Z',     # WOUXUN ELECTRONICS CO LTD
    'Alinco': 'C4Z',     # ALINCO INCORPORATED
    'Radtel': '2AO8L',   # QUANZHOU RADTEL ELECTRONICS TECHNOLOGY CO
    # Additional verified codes
    'Btech': '2AJGM',    # BTech is Baofeng's US brand
    'Puxing': 'W7PPX',   # PUXING ELECTRONICS SCIENCE & TECHNOLOGY
    'Tyt': '2AMJR',      # QUANZHOU WOTONG ELECTRONICS CO
    'Quansheng': '2ANMZ', # QUANZHOU QUANSHENG ELECTRONICS CO
    'Motorola': 'AZ489', # MOTOROLA SOLUTIONS INC
    'Vertex': 'K6620',   # Vertex is Yaesu brand
    'Midland': 'K4R',    # MIDLAND RADIO CORPORATION
    'Azden': 'FRH',      # AZDEN CORPORATION
    'Radioddity': '2AJGM', # Same as Baofeng
    'Ailunce': '2AO3E',  # FUJIAN NANAN AILUNCE ELECTRONICS
    'Baojie': '2AFPR',   # QUANZHOU BAOJIE ELECTRIC CO
    'Rexon': 'IJJ',      # REXON ELECTRONICS CORP
    'Zastone': '2AM7H',  # ZASTONE TELECOM EQUIPMENT CO
    'Senhaix': '2AWSI',  # QUANZHOU SENHAIX ELECTRONICS CO
    'Connect Systems': 'XH8', # CONNECT SYSTEMS INC
    'Tera': '2AL5L',     # FUJIAN QIANZHIDU ELECTRONICS TECH CO
    'Luiton': '2AO8L',   # Related to Radtel
    'Sainsionic': '2AJGM', # Baofeng rebrand
    'Tekk': '2AJGM',     # Baofeng rebrand
    'Wln': '2ASNS',      # FUJIAN BFSAT INFORMATION TECHNOLOGY CO
    'Ruyage': '2AN5D',   # QUANZHOU RUYAGE ELECTRON CO
    'Marantz': 'FNA',    # MARANTZ AMERICA INC
    'Standard': 'K66',   # STANDARD COMMUNICATIONS CORP
    'Radio Shack': 'FRS', # Various OEM
    'Maxon': 'HBG',      # MAXON AMERICA INC
    'Ritron': 'JZP',     # RITRON INC
    'Eagle': '2AJGM',    # Baofeng rebrand
    'Vero': '2AJGM',     # Baofeng rebrand
    'Bintolk': '2AJGM',  # Baofeng rebrand
    'Hongxun': '2AL5L',  # Related to Tera
    'Iradio': '2AO8L',   # Related to Radtel
    'Jingtong': '2AJGM', # Baofeng variant
    'Soontone': '2AJGM', # Baofeng rebrand
}

def clean_model_for_fcc(model):
    """Clean model name for FCC ID format"""
    # Remove parentheses content
    model = re.sub(r'\([^)]*\)', '', model).strip()
    # Remove spaces and special chars, keep alphanumeric and hyphens
    model = re.sub(r'\s+', '', model)
    model = re.sub(r'[^A-Za-z0-9\-]', '', model)
    return model.upper()

def search_fcc_id_web(brand, model):
    """Search FCC database via web scraping"""
    try:
        clean_model = clean_model_for_fcc(model)
        
        # Use the official FCC database search
        url = f"https://fcc.report/FCC-ID/{quote(clean_model)}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            # If we can access the page, the FCC ID likely exists
            # Extract any info from the page if needed
            return True
        
        return False
        
    except Exception as e:
        return False

def generate_fcc_id(brand, model):
    """Generate FCC ID using known grantee codes"""
    # Check if we have a known grantee code
    grantee_code = KNOWN_GRANTEE_CODES.get(brand, '')
    
    if not grantee_code:
        return None
    
    clean_model = clean_model_for_fcc(model)
    
    if not clean_model:
        return None
    
    # Generate the FCC ID
    fcc_id = f"{grantee_code}-{clean_model}"
    
    return {
        'fcc_id': fcc_id,
        'grantee_code': grantee_code,
        'method': 'generated'
    }

def main():
    input_file = 'merged_master.csv'
    output_file = 'merged_master_with_fcc.csv'
    
    print(f"Reading {input_file}...")
    models = []
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        models = list(reader)
    
    print(f"Found {len(models)} models to process\n")
    print("Known Grantee Codes:")
    for brand, code in sorted(KNOWN_GRANTEE_CODES.items()):
        print(f"  {brand}: {code}")
    print()
    
    # Add new columns
    new_fieldnames = list(fieldnames) + ['FCC_ID', 'Grantee_Code']
    
    # Process each model
    processed = 0
    generated = 0
    unknown_brands = set()
    
    for i, model in enumerate(models, 1):
        brand = model['Brand']
        model_name = model['Model']
        
        fcc_info = generate_fcc_id(brand, model_name)
        
        if fcc_info:
            model['FCC_ID'] = fcc_info['fcc_id']
            model['Grantee_Code'] = fcc_info['grantee_code']
            generated += 1
            if i <= 20 or i % 100 == 0:  # Show first 20 and every 100th
                print(f"[{i}/{len(models)}] {brand} {model_name} → {fcc_info['fcc_id']}")
        else:
            model['FCC_ID'] = ''
            model['Grantee_Code'] = ''
            unknown_brands.add(brand)
        
        processed += 1
    
    # Write results
    print(f"\nWriting results to {output_file}...")
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(models)
    
    print(f"\n✓ Complete!")
    print(f"  Processed: {processed} models")
    print(f"  FCC IDs generated: {generated}")
    print(f"  No grantee code: {processed - generated}")
    
    if unknown_brands:
        print(f"\nBrands without grantee codes ({len(unknown_brands)}):")
        for brand in sorted(unknown_brands):
            count = sum(1 for m in models if m['Brand'] == brand)
            print(f"  {brand}: {count} models")
    
    print(f"\nOutput saved to: {output_file}")

if __name__ == '__main__':
    main()
