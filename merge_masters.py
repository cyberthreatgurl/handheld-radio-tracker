import csv
from collections import defaultdict

def load_csv_models(filename):
    """Load models from CSV file and return as list of dicts"""
    models = []
    with open(filename, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize brand to Title Case for consistency
            row['Brand'] = row['Brand'].strip().title()
            row['Model'] = row['Model'].strip()
            models.append(row)
    return models

def merge_and_deduplicate(models_list1, models_list2):
    """Merge two lists of models and remove duplicates based on Brand+Model"""
    # Use a dict with (Brand, Model) tuple as key to eliminate duplicates
    unique_models = {}
    
    for model in models_list1 + models_list2:
        key = (model['Brand'], model['Model'].upper())
        if key not in unique_models:
            unique_models[key] = model
    
    # Convert back to list and sort by Brand, then Model
    merged = list(unique_models.values())
    merged.sort(key=lambda x: (x['Brand'].lower(), x['Model'].upper()))
    
    return merged

def main():
    print("Loading master.csv...")
    master_models = load_csv_models('master.csv')
    print(f"  Found {len(master_models)} models")
    
    print("Loading new_added_master.csv...")
    new_added_models = load_csv_models('new_added_master.csv')
    print(f"  Found {len(new_added_models)} models")
    
    print("\nMerging and removing duplicates...")
    merged_models = merge_and_deduplicate(master_models, new_added_models)
    print(f"  Result: {len(merged_models)} unique models")
    
    # Get all column names from both files
    all_columns = set()
    for model in master_models + new_added_models:
        all_columns.update(model.keys())
    
    # Ensure Brand and Model are first
    columns = ['Brand', 'Model']
    remaining_cols = sorted([col for col in all_columns if col not in columns])
    columns.extend(remaining_cols)
    
    # Write merged results
    output_file = 'merged_master.csv'
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(merged_models)
    
    print(f"\n✓ Merged data saved to {output_file}")
    print(f"  Total unique models: {len(merged_models)}")
    print(f"  Duplicates removed: {len(master_models) + len(new_added_models) - len(merged_models)}")
    
    # Show some statistics
    brand_counts = defaultdict(int)
    for model in merged_models:
        brand_counts[model['Brand']] += 1
    
    print(f"\nTop brands by model count:")
    for brand, count in sorted(brand_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {brand}: {count}")

if __name__ == '__main__':
    main()
