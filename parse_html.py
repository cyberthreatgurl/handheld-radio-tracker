import re
from bs4 import BeautifulSoup
import csv
import json

# Known radio brands - expand this list as needed
RADIO_BRANDS = [
    'Yaesu', 'Kenwood', 'Icom', 'ICOM', 'Baofeng', 'BaoFeng', 'Pofung',
    'Alinco', 'WOUXUN', 'Wouxun', 'TYT', 'Anytone', 'Quansheng',
    'Radioddity', 'TidRadio', 'Radio Shack', 'Motorola', 'ADI',
    'Puxing', 'FDC', 'RFinder', 'Connect Systems'
]

def extract_manufacturer_and_model(product_name):
    """
    Extract manufacturer and model from product name.
    
    Args:
        product_name: The full product name string
        
    Returns:
        Tuple of (manufacturer, model)
    """
    # Handle special cases first
    if 'Radio Shack' in product_name:
        manufacturer = 'Radio Shack'
        # Extract model after "Radio Shack"
        model = product_name.replace('Radio Shack', '').strip()
        return manufacturer, model
    
    if 'Connect Systems' in product_name:
        manufacturer = 'Connect Systems'
        model = product_name.replace('Connect Systems', '').strip()
        return manufacturer, model
    
    # Handle Baofeng/Pofung case
    if 'Baofeng/Pofung' in product_name or 'BaoFeng/Pofung' in product_name:
        manufacturer = 'Baofeng'
        # Extract model after the brand
        model = re.sub(r'^(Baofeng|BaoFeng)/Pofung\s+', '', product_name, flags=re.IGNORECASE).strip()
        return manufacturer, model
    
    # Try to find manufacturer from the list
    for brand in RADIO_BRANDS:
        # Case-insensitive search
        pattern = re.compile(r'^(' + re.escape(brand) + r')\b', re.IGNORECASE)
        match = pattern.search(product_name)
        if match:
            manufacturer = match.group(1)
            # Extract the model (everything after the manufacturer)
            model = product_name[match.end():].strip()
            # Clean up common prefixes/suffixes
            model = re.sub(r'^\s*[-–]\s*', '', model)  # Remove leading dashes
            model = re.sub(r'\s*\(.*?\)$', '', model)  # Remove trailing parentheses
            model = re.sub(r'\s+dual band.*$', '', model, flags=re.IGNORECASE)  # Remove descriptive text
            model = re.sub(r'\s+–.*$', '', model)  # Remove descriptive text after dash
            model = re.sub(r'\s+VHF.*$', '', model, flags=re.IGNORECASE)  # Remove VHF/UHF descriptions
            
            return manufacturer, model.strip()
    
    # If no manufacturer found, try splitting on first space
    parts = product_name.split(None, 1)
    if len(parts) >= 2:
        return parts[0], parts[1]
    
    return product_name, ''


def parse_html_file(filepath):
    """
    Parse the HTML file and extract product information.
    
    Args:
        filepath: Path to the HTML file
        
    Returns:
        List of dictionaries with product information
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        html_content = f.read()
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Find the products table
    table = soup.find('table', class_='table')
    if not table:
        print("Could not find the products table!")
        return []
    
    products = []
    rows = table.find('tbody').find_all('tr')
    
    for row in rows:
        cells = row.find_all('td')
        if len(cells) >= 5:
            # Get product name from the first cell
            product_link = cells[0].find('a')
            if product_link:
                product_name = product_link.get_text(strip=True)
                num_reviews = cells[1].get_text(strip=True)
                last_review = cells[2].get_text(strip=True)
                msrp = cells[3].get_text(strip=True)
                
                # Extract rating
                rating_text = cells[4].get_text(strip=True)
                rating_match = re.search(r'\(([\d.]+)\)', rating_text)
                rating = rating_match.group(1) if rating_match else ''
                
                # Extract manufacturer and model
                manufacturer, model = extract_manufacturer_and_model(product_name)
                
                # Normalize manufacturer name to Title Case
                manufacturer = manufacturer.title() if manufacturer else manufacturer
                
                products.append({
                    'product_name': product_name,
                    'manufacturer': manufacturer,
                    'model': model
                })
    
    return products


def main():
    filepath = 'products.html'
    
    print("Parsing HTML file...")
    products = parse_html_file(filepath)
    
    if not products:
        print("No products found!")
        return
    
    print(f"\nFound {len(products)} products\n")
    print("-" * 100)
    print(f"{'Product Name':<45} {'Manufacturer':<20} {'Model':<25}")
    print("-" * 100)
    
    for product in products:
        print(f"{product['product_name']:<45} {product['manufacturer']:<20} {product['model']:<25}")
    
    # Save to CSV
    csv_filename = 'products_parsed.csv'
    with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['product_name', 'manufacturer', 'model']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(products)
    
    print("-" * 100)
    print(f"\nProducts saved to '{csv_filename}'")
    
    # Also save to JSON
    json_filename = 'products_parsed.json'
    with open(json_filename, 'w', encoding='utf-8') as jsonfile:
        json.dump(products, jsonfile, indent=2, ensure_ascii=False)
    
    print(f"Products also saved to '{json_filename}'")
    
    # Print summary by manufacturer
    print("\n" + "=" * 100)
    print("SUMMARY BY MANUFACTURER")
    print("=" * 100)
    
    manufacturer_counts = {}
    for product in products:
        manufacturer = product['manufacturer']
        manufacturer_counts[manufacturer] = manufacturer_counts.get(manufacturer, 0) + 1
    
    for manufacturer, count in sorted(manufacturer_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"{manufacturer:<30} {count:>3} products")


if __name__ == "__main__":
    main()
