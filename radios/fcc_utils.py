import xmltodict
import logging
from curl_cffi import requests
from radios.models import Radio
from radios.fcc_id_utils import split_fcc_id
from radios.fcc_validation import validate_fcc_brand_assignment

URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"
logger = logging.getLogger(__name__)


def _clean_query(value):
    return (value or '').strip().upper().replace(' ', '')


def _exact_grantee_query(value):
    """Return an exact grantee code when query is a standalone valid grantee code."""
    cleaned = _clean_query(value)
    if not cleaned or '-' in cleaned:
        return ''
    if cleaned[0].isalpha() and len(cleaned) == 3:
        return cleaned
    if cleaned[0].isdigit() and cleaned[0] in '23456789' and len(cleaned) == 5:
        return cleaned
    return ''

def fetch_and_sync_fcc_id(fcc_id_query):
    """
    Fetches FCC ID data using curl_cffi and saves it to the database.
    Returns (count_added, count_updated, messages)
    """
    messages = []
    request_url = f"{URL}fccId={fcc_id_query}"
    messages.append(f"Querying FCC API: {request_url}")
    logger.info("FCC sync request started query=%s", fcc_id_query)
    
    try:
        response = requests.get(request_url, impersonate="chrome124", timeout=15)
    except Exception as e:
        messages.append(f"Request failed with error: {e}")
        logger.exception("FCC sync request failed query=%s", fcc_id_query)
        return 0, 0, messages
    
    if response.status_code != 200:
        messages.append(f"Error: Received status code {response.status_code}")
        logger.warning("FCC sync non-200 status query=%s status=%s", fcc_id_query, response.status_code)
        return 0, 0, messages

    try:
        data = xmltodict.parse(response.text)
        wrapper = data.get("fCCIDInfoes", {})
        if not wrapper:
            logger.info("FCC sync empty wrapper query=%s", fcc_id_query)
            return 0, 0, messages
        result = wrapper.get("fccidInfo", [])
    except Exception as e:
        messages.append(f"Failed to parse XML response: {e}")
        logger.exception("FCC sync parse failed query=%s", fcc_id_query)
        return 0, 0, messages

    records = [result] if isinstance(result, dict) else (result if result else [])
    exact_grantee = _exact_grantee_query(fcc_id_query)
    skipped_non_exact = 0

    count_added = 0
    count_updated = 0
    logger.info("FCC sync parsing records query=%s record_count=%s", fcc_id_query, len(records))
    for idx, res in enumerate(records, start=1):
        fcc_id = res.get('FCCId', '')
        if not fcc_id:
            continue

        grantee_code, product_code = split_fcc_id(fcc_id)
        if not product_code:
            product_code = fcc_id

        # FCC endpoint may return prefix-like results for grantee searches.
        # Enforce exact grantee code matching when query is a standalone grantee code.
        if exact_grantee and grantee_code != exact_grantee:
            skipped_non_exact += 1
            logger.info(
                "FCC sync skipped non-exact grantee query=%s expected_grantee=%s got_grantee=%s fcc_id=%s",
                fcc_id_query,
                exact_grantee,
                grantee_code,
                fcc_id,
            )
            continue

        raw_brand_name = (res.get("grantee", '') or '').strip()
        if not raw_brand_name:
            raw_brand_name = grantee_code

        validation = validate_fcc_brand_assignment(fcc_id, raw_brand_name)
        brand_val = validation.get('resolved_brand_name') or raw_brand_name or grantee_code

        if validation.get('status') == 'white_label_possible':
            logger.info(
                "FCC validation white-label candidate source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s grantee_brand=%s provided_brand=%s resolved_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('grantee_brand_name', ''),
                validation.get('provided_brand_name', ''),
                brand_val,
            )
        elif validation.get('status') == 'unknown_grantee':
            logger.warning(
                "FCC validation unknown grantee source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('provided_brand_name', ''),
            )
        elif validation.get('status') == 'invalid_fcc_id':
            logger.warning(
                "FCC validation invalid id source=fcc_api query=%s record_index=%s fcc_id=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('provided_brand_name', ''),
            )
        
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
                    logger.info(
                        "FCC ingest update source=fcc_api query=%s action=update_by_fcc_id radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        validation.get('status', ''),
                    )
        else:
            existing_radio = Radio.objects.filter(brand=brand_val, model=product_code).first()
            if existing_radio:
                # Upate the existing radio instead of creating a duplicate
                if not existing_radio.fcc_id:
                    existing_radio.fcc_id = fcc_id
                if new_notes not in existing_radio.notes:
                    existing_radio.notes = f"{new_notes}\n{existing_radio.notes}".strip()
                existing_radio.save()
                count_updated += 1
                logger.info(
                    "FCC ingest update source=fcc_api query=%s action=update_by_brand_model radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s",
                    fcc_id_query,
                    existing_radio.id,
                    brand_val,
                    product_code,
                    fcc_id,
                    validation.get('status', ''),
                )
            else:
                created_radio = Radio.objects.create(
                    brand=brand_val,
                    model=product_code,
                    fcc_id=fcc_id,
                    notes=new_notes
                )
                count_added += 1
                logger.info(
                    "FCC ingest create source=fcc_api query=%s action=create radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s inferred_grantee=%s",
                    fcc_id_query,
                    created_radio.id,
                    brand_val,
                    product_code,
                    fcc_id,
                    validation.get('status', ''),
                    validation.get('inferred_grantee_code', ''),
                )

    if exact_grantee and skipped_non_exact:
        messages.append(
            f"Filtered {skipped_non_exact} non-exact grantee matches while enforcing exact grantee code {exact_grantee}."
        )
    messages.append(f"Successfully processed {len(records)} records for {fcc_id_query}.")
    logger.info(
        "FCC sync completed query=%s added=%s updated=%s exact_grantee=%s skipped_non_exact=%s",
        fcc_id_query,
        count_added,
        count_updated,
        exact_grantee,
        skipped_non_exact,
    )
    return count_added, count_updated, messages
