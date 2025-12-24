import requests
from bs4 import BeautifulSoup
import time

def scrape_product_names(url):
    """
    Scrape product names from the eham.net reviews page.
    
    Args:
        url: The URL to scrape
        
    Returns:
        List of product names
    """
    # Set headers to mimic a real browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    try:
        # Send GET request
        print(f"Fetching URL: {url}")
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the table - look for common table patterns
        product_names = []
        
        # Method 1: Look for table with Product Name header
        table = soup.find('table')
        if table:
            # Find all rows
            rows = table.find_all('tr')
            
            # Find the column index for "Product Name"
            header_row = rows[0] if rows else None
            product_name_index = None
            
            if header_row:
                headers = header_row.find_all(['th', 'td'])
                for idx, header in enumerate(headers):
                    if 'Product Name' in header.get_text(strip=True):
                        product_name_index = idx
                        break
            
            # Extract product names from the column
            if product_name_index is not None:
                for row in rows[1:]:  # Skip header row
                    cells = row.find_all(['td', 'th'])
                    if len(cells) > product_name_index:
                        product_name = cells[product_name_index].get_text(strip=True)
                        if product_name:
                            product_names.append(product_name)
            else:
                # If we can't find the header, try to extract all product links/names
                print("Could not find 'Product Name' column, trying alternative methods...")
                for row in rows:
                    # Look for links that might be product names
                    links = row.find_all('a')
                    for link in links:
                        text = link.get_text(strip=True)
                        if text and len(text) > 3:  # Filter out very short text
                            product_names.append(text)
        
        # Method 2: If no table found, look for product links
        if not product_names:
            print("No table found, looking for product links...")
            product_links = soup.find_all('a', href=lambda x: x and '/reviews/' in x)
            for link in product_links:
                text = link.get_text(strip=True)
                if text and len(text) > 3:
                    product_names.append(text)
        
        return product_names
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the page: {e}")
        return []
    except Exception as e:
        print(f"Error parsing the page: {e}")
        return []


def main():
    url = "https://www.eham.net/reviews/view-category/49?sort=-activeReviewsCount"
    
    print("Starting web scraping...")
    product_names = scrape_product_names(url)
    
    if product_names:
        print(f"\nFound {len(product_names)} product names:\n")
        print("-" * 60)
        for idx, name in enumerate(product_names, 1):
            print(f"{idx}. {name}")
        print("-" * 60)
        
        # Optionally save to file
        with open('product_names.txt', 'w', encoding='utf-8') as f:
            for name in product_names:
                f.write(f"{name}\n")
        print("\nProduct names saved to 'product_names.txt'")
    else:
        print("\nNo product names found. The page structure may have changed or access was blocked.")


if __name__ == "__main__":
    main()
