import csv
import requests
import time
from bs4 import BeautifulSoup
from urllib.parse import quote
import re

def search_fcc_grantee(brand, model_example):
    """Try to find grantee code by searching FCC database for a specific model"""
    try:
        # Clean model name
        model_clean = re.sub(r'[^A-Za-z0-9\-]', '', model_example.replace(' ', ''))
        
        # Try fcc.report which mirrors FCC data
        search_url = f"https://fcc.report/search/{quote(brand + ' ' + model_clean)}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        response = requests.get(search_url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Look for FCC IDs in the page
            fcc_id_pattern = re.compile(r'\b([A-Z0-9]{3,5})-([A-Z0-9\-]+)\b', re.IGNORECASE)
            matches = fcc_id_pattern.findall(response.text)
            
            if matches:
                # Return the most common grantee code
                grantee_codes = [match[0] for match in matches]
                if grantee_codes:
                    most_common = max(set(grantee_codes), key=grantee_codes.count)
                    return most_common.upper()
        
        return None
        
    except Exception as e:
        return None

def main():
    input_file = 'merged_master_with_fcc.csv'
    
    print("Reading models and identifying brands without grantee codes...\n")
    
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        models = list(reader)
    
    # Find brands without grantee codes
    brands_without_codes = {}
    for model in models:
        brand = model['Brand']
        if not model['Grantee_Code']:
            if brand not in brands_without_codes:
                brands_without_codes[brand] = []
            brands_without_codes[brand].append(model['Model'])
    
    print(f"Found {len(brands_without_codes)} brands without grantee codes\n")
    print("Searching for grantee codes...\n")
    
    found_codes = {}
    
    for i, (brand, model_list) in enumerate(sorted(brands_without_codes.items()), 1):
        # Try the first model as an example
        example_model = model_list[0]
        print(f"[{i}/{len(brands_without_codes)}] {brand} (example: {example_model})...", end=' ')
        
        grantee_code = search_fcc_grantee(brand, example_model)
        
        if grantee_code:
            found_codes[brand] = grantee_code
            print(f"✓ Found: {grantee_code}")
        else:
            print(f"✗ Not found - Manual search: https://fcc.report/search/{quote(brand + ' ' + example_model)}")
        
        # Rate limiting
        time.sleep(1)
    
    # Print results
    print(f"\n{'='*60}")
    print("FOUND GRANTEE CODES:")
    print(f"{'='*60}\n")
    
    if found_codes:
        print("Add these to KNOWN_GRANTEE_CODES in add_fcc_ids.py:\n")
        for brand in sorted(found_codes.keys()):
            print(f"    '{brand}': '{found_codes[brand]}',")
    else:
        print("No grantee codes found automatically.")
    
    print(f"\n{'='*60}")
    print("BRANDS STILL NEEDING MANUAL LOOKUP:")
    print(f"{'='*60}\n")
    
    for brand in sorted(brands_without_codes.keys()):
        if brand not in found_codes:
            count = len(brands_without_codes[brand])
            example = brands_without_codes[brand][0]
            print(f"{brand} ({count} models)")
            print(f"  Example: {example}")
            print(f"  Search: https://fccid.io/{quote(brand)}")
            print()

if __name__ == '__main__':
    main()
