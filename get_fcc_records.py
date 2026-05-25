import csv
from curl_cffi import requests
import xmltodict

# API Endpoint
URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"

def fetch_fcc_data(grantee_code="", product_code=""):
    """Queries the FCC EAS HTTP GET API and returns a list of matching records."""
    
    # Construct the URL with the parameter
    request_url = f"{URL}fccId={grantee_code}" if grantee_code else URL
    
    print(f"Querying FCC API at {request_url}")
    print(f"Querying FCC with Grantee Code: {grantee_code or 'Any'}...")
    try:
        # Use curl_cffi to perfectly impersonate a real Chrome browser's network fingerprint!
        response = requests.get(request_url, impersonate="chrome124", timeout=15)
        print(f"Received response with status code: {response.status_code}")
    except Exception as e:
        print(f"Request failed with error: {e}")
        return []
    
    if response.status_code != 200:
        print(f"Error: Received status code {response.status_code}")
        print(f"Response text preview: {response.text[:500]}")
        return []

    # Parse XML response
    try:
        data = xmltodict.parse(response.text)
        # Navigate the new simpler HTTP GET response tree: <fCCIDInfoes><fccidInfo>...
        wrapper = data.get("fCCIDInfoes", {})
        result = wrapper.get("fccidInfo", [])
    except Exception as e:
        print(f"Failed to parse XML response: {e}")
        return []

    # Normalize single vs multiple returns
    if isinstance(result, dict):
        return [result]
    return result if result else []

def main():
    print("Starting FCC record retrieval script...")
    
    # Example: Searching for all products under specific common grantee codes
    grantee_codes = ["2AJGM", "2ANPO", "AFJ", "ALH"] 
    print(f"Targeting grantee codes: {grantee_codes}")
    all_records = []
    
    for code in grantee_codes:
        results = fetch_fcc_data(grantee_code=code)
        for res in results:
            if isinstance(res, dict):
                # Using the keys returned from the HTTP GET response
                fcc_id = res.get('FCCId', '')
                print(f"Found FCC ID: {fcc_id} | Grant Date: {res.get('grantDate', 'N/A')}")
                all_records.append({
                    "FCC ID": fcc_id,
                    "Grantee Code": code, # We feed this in manually
                    "Grantee Name": res.get("grantee", ""),
                    "Application Purpose": res.get("applicationPurpose", ""),
                    "Grant Date": res.get("grantDate", "")
                })

    # Save to CSV
    if all_records:
        keys = ["FCC ID", "Grantee Code", "Grantee Name", "Application Purpose", "Grant Date"]
        filename = "fcc_radio_ids.csv"
        with open(filename, 'w', newline='') as output_file:
            dict_writer = csv.DictWriter(output_file, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(all_records)
        print(f"\nSuccessfully saved {len(all_records)} records to {filename}")
    else:
        print("No records found.")

if __name__ == "__main__":
    main()
