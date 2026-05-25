import xmltodict
from curl_cffi import requests
from radios.models import Radio, Brand

URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"

def fetch_and_sync_fcc_id(fcc_id_query):
    """
    Fetches FCC ID data using curl_cffi and saves it to the database.
    Returns (count_added, count_updated, messages)
    """
    messages = []
    request_url = f"{URL}fccId={fcc_id_query}"
    messages.append(f"Querying FCC API: {request_url}")
    
    try:
        response = requests.get(request_url, impersonate="chrome124", timeout=15)
    except Exception as e:
        messages.append(f"Request failed with error: {e}")
        return 0, 0, messages
    
    if response.status_code != 200:
        messages.append(f"Error: Received status code {response.status_code}")
        return 0, 0, messages

    try:
        data = xmltodict.parse(response.text)
        wrapper = data.get("fCCIDInfoes", {})
        if not wrapper:
            return 0, 0, messages
        result = wrapper.get("fccidInfo", [])
    except Exception as e:
        messages.append(f"Failed to parse XML response: {e}")
        return 0, 0, messages

    records = [result] if isinstance(result, dict) else (result if result else [])

    count_added = 0
    count_updated = 0
    for res in records:
        fcc_id = res.get('FCCId', '')
        if not fcc_id:
            continue

        grantee_code = ""
        product_code = ""
        if '-' in fcc_id:
            parts = fcc_id.split('-', 1)
            grantee_code = parts[0].strip()
            product_code = parts[1].strip()
        else:
            grantee_code = fcc_id[:5] if len(fcc_id) > 5 else fcc_id
            product_code = fcc_id
            
        brand_name = res.get("grantee", grantee_code)
        
        # Format new details for notes
        grant_date = res.get("grantDate", "N/A")
        app_purpose = res.get("applicationPurpose", "N/A")
        new_notes = f"FCC Grant Date: {grant_date} | Purpose: {app_purpose}"

        # Check if Radio already exists
        radio_qs = Radio.objects.filter(fcc_id__iexact=fcc_id)
        if radio_qs.exists():
            for radio in radio_qs:
                if new_notes not in radio.notes:
                    radio.notes = f"{new_notes}\n{radio.notes}".strip()
                    radio.save()
                    count_updated += 1
        else:
            # Try to map Grantee to existing Brand
            mapped_brand = Brand.objects.filter(grantee_code__iexact=grantee_code).first()
            brand_val = mapped_brand.name if mapped_brand else brand_name
                
            existing_radio = Radio.objects.filter(brand=brand_val, model=product_code).first()
            if existing_radio:
                # Upate the existing radio instead of creating a duplicate
                if not existing_radio.fcc_id:
                    existing_radio.fcc_id = fcc_id
                if new_notes not in existing_radio.notes:
                    existing_radio.notes = f"{new_notes}\n{existing_radio.notes}".strip()
                existing_radio.save()
                count_updated += 1
            else:
                Radio.objects.create(
                    brand=brand_val,
                    model=product_code,
                    fcc_id=fcc_id,
                    notes=new_notes
                )
                count_added += 1

    messages.append(f"Successfully processed {len(records)} records for {fcc_id_query}.")
    return count_added, count_updated, messages
