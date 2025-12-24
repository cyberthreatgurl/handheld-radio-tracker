import csv

def load_csv_models(filename):
    """Load models from CSV file."""
    models = set()
    with open(filename, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = row.get('Brand', '').strip()
            model = row.get('Model', '').strip()
            if brand and model:
                # Normalize for comparison
                models.add((brand.title(), model.upper()))
    return models

def main():
    print("=" * 80)
    print("VERIFYING CONVERSION FROM master.csv TO new_added_master.csv")
    print("=" * 80)
    
    # Load both files
    print("\nLoading master.csv...")
    master_models = load_csv_models('master.csv')
    print(f"Found {len(master_models)} models in master.csv")
    
    print("\nLoading new_added_master.csv...")
    new_added_models = load_csv_models('new_added_master.csv')
    print(f"Found {len(new_added_models)} models in new_added_master.csv")
    
    # Find missing models
    missing_from_new = master_models - new_added_models
    extra_in_new = new_added_models - master_models
    
    print("\n" + "=" * 80)
    if not missing_from_new and not extra_in_new:
        print("✓ SUCCESS: All models match perfectly!")
        print(f"  Both files contain the same {len(master_models)} models")
    else:
        if missing_from_new:
            print(f"✗ WARNING: {len(missing_from_new)} models from master.csv are MISSING in new_added_master.csv:")
            print("-" * 80)
            for brand, model in sorted(missing_from_new):
                print(f"  {brand}: {model}")
        
        if extra_in_new:
            print(f"\n✗ NOTE: {len(extra_in_new)} EXTRA models in new_added_master.csv not in master.csv:")
            print("-" * 80)
            for brand, model in sorted(extra_in_new):
                print(f"  {brand}: {model}")
    
    print("=" * 80)

if __name__ == "__main__":
    main()
