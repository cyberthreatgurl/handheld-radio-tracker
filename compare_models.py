import csv
from collections import defaultdict

def normalize_model(model):
    """
    Normalize model names for comparison by removing common variations.
    """
    if not model:
        return ""
    
    # Convert to uppercase for comparison
    normalized = model.upper().strip()
    
    # Remove common suffixes/variations that might differ between sources
    # But keep the core model number
    return normalized


def load_master_models(master_file):
    """
    Load models from master.csv.
    Returns a set of normalized (brand, model) tuples.
    """
    master_models = set()
    brand_models = defaultdict(set)
    
    with open(master_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = row['Brand'].strip()
            model = row['Model'].strip()
            
            if brand and model:
                # Normalize brand to Title Case for consistency
                normalized_brand = brand.title()
                normalized_model = normalize_model(model)
                master_models.add((normalized_brand, normalized_model))
                brand_models[normalized_brand].add(normalized_model)
    
    return master_models, brand_models


def load_parsed_products(products_file):
    """
    Load products from products_parsed.csv.
    Returns list of (product_name, manufacturer, model) tuples.
    """
    products = []
    
    with open(products_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            product_name = row['product_name'].strip()
            manufacturer = row['manufacturer'].strip()
            model = row['model'].strip()
            
            if manufacturer and model:
                products.append((product_name, manufacturer, model))
    
    return products


def find_new_models(products_file='products_parsed.csv', master_file='master.csv'):
    """
    Compare products_parsed.csv with master.csv and find new models.
    """
    print("Loading master.csv...")
    master_models, brand_models = load_master_models(master_file)
    print(f"Found {len(master_models)} models in master.csv")
    
    print("\nLoading products_parsed.csv...")
    parsed_products = load_parsed_products(products_file)
    print(f"Found {len(parsed_products)} products in products_parsed.csv")
    
    # Find new models
    new_models = []
    
    for product_name, manufacturer, model in parsed_products:
        normalized_model = normalize_model(model)
        # Normalize manufacturer to Title Case for comparison
        normalized_manufacturer = manufacturer.title()
        
        # Check if this brand/model combination exists in master
        if (normalized_manufacturer, normalized_model) not in master_models:
            new_models.append({
                'product_name': product_name,
                'manufacturer': normalized_manufacturer,
                'model': model
            })
    
    return new_models, master_models, brand_models


def main():
    print("=" * 100)
    print("COMPARING products_parsed.csv WITH master.csv")
    print("=" * 100)
    
    new_models, master_models, brand_models = find_new_models()
    
    if not new_models:
        print("\n✓ All models from products_parsed.csv are already in master.csv!")
        return
    
    print(f"\n" + "=" * 100)
    print(f"FOUND {len(new_models)} NEW MODELS NOT IN master.csv")
    print("=" * 100)
    
    # Group by manufacturer for better readability
    by_manufacturer = defaultdict(list)
    for item in new_models:
        by_manufacturer[item['manufacturer']].append(item)
    
    # Print grouped by manufacturer
    for manufacturer in sorted(by_manufacturer.keys()):
        models = by_manufacturer[manufacturer]
        print(f"\n{manufacturer} ({len(models)} models):")
        print("-" * 80)
        for item in sorted(models, key=lambda x: x['model']):
            print(f"  {item['model']:<30} (from: {item['product_name']})")
    
    # Save to CSV for easy importing
    output_file = 'new_models_to_add.csv'
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['manufacturer', 'model']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for item in sorted(new_models, key=lambda x: (x['manufacturer'], x['model'])):
            writer.writerow({
                'manufacturer': item['manufacturer'],
                'model': item['model']
            })
    
    print("\n" + "=" * 100)
    print(f"New models saved to: {output_file}")
    print("=" * 100)
    
    # Show summary by manufacturer
    print("\nSUMMARY BY MANUFACTURER:")
    print("-" * 80)
    for manufacturer in sorted(by_manufacturer.keys()):
        count = len(by_manufacturer[manufacturer])
        print(f"  {manufacturer:<30} {count:>3} new models")


if __name__ == "__main__":
    main()
