import csv
import re

def parse_markdown_table(md_file, csv_file):
    """
    Convert markdown table to CSV.
    """
    with open(md_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Find header row (starts with |)
    rows = []
    headers = None
    
    for line in lines:
        line = line.strip()
        if not line or not line.startswith('|'):
            continue
        
        # Skip separator lines (contains :--- or similar)
        if ':---' in line or '---' in line:
            continue
        
        # Parse the row
        cells = [cell.strip() for cell in line.split('|')]
        # Remove empty first and last elements (from leading/trailing |)
        cells = [cell for cell in cells if cell]
        
        if headers is None:
            headers = cells
        else:
            rows.append(cells)
    
    # Write to CSV
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    
    print(f"Converted {len(rows)} rows from {md_file} to {csv_file}")
    print(f"Columns: {', '.join(headers)}")

if __name__ == "__main__":
    parse_markdown_table('master.md', 'new_added_master.csv')
