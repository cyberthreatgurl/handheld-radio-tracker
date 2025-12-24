# Web Scraper for eHam.net Product Names

This script scrapes product names from the eHam.net reviews page.

## Setup

1. Install the required dependencies:
```bash
pip install -r requirements.txt
```

Or if using the virtual environment:
```bash
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

Run the script:
```bash
python scrape_product_names.py
```

The script will:
- Fetch the webpage with proper headers
- Extract all product names from the "Product Name" column
- Display them in the console
- Save them to `product_names.txt`

## Note

If the website blocks the request, you may need to:
- Use a different User-Agent string
- Add delays between requests
- Use a headless browser like Selenium or Playwright for JavaScript-rendered content
