from django.shortcuts import render, redirect
from django.contrib import messages
import logging
from .forms import ImportGranteeXMLForm
from .models import Radio, Brand, IgnoredGrantee
from .fcc_validation import validate_fcc_brand_assignment
from .fcc_utils import _classify_fcc_device
from .accounts_decorators import staff_required
import xml.etree.ElementTree as ET
import os
import re
import json
import base64

RESULTS_XML = os.path.join('data', 'results.xml')
logger = logging.getLogger(__name__)

# Known two-way radio frequency bands (MHz).  Used as a fallback when
# the XML export lacks rule_parts and emission_designator fields.
_RADIO_FREQ_BANDS = [
    (26.0, 28.0),       # HF CB
    (118.0, 137.0),     # Aviation VHF
    (136.0, 174.0),     # VHF LMR / Marine / MURS
    (400.0, 520.0),     # UHF LMR / GMRS / FRS
    (700.0, 800.0),     # LTE / PoC radios
    (806.0, 941.0),     # 800/900 MHz LMR
    (902.0, 928.0),     # 900 MHz ISM / LoRa
]

# Keywords that indicate a non-radio device operating in radio-frequency
# bands — wireless microphones (Part 74H), in-ear monitors (Part 74H),
# audio transmitters, etc.  Matched case-insensitively against applicant
# name, brand name, and application purpose text from the XML row.
_NON_RADIO_DEVICE_KEYWORDS = [
    'WIRELESS MICROPHONE', 'WIRELESS MIC',
    'HANDHELD MICROPHONE', 'HANDHELD MIC',
    'BODY-PACK', 'BODYPACK', 'BODY PACK',
    'IN-EAR MONITOR', 'IN EAR MONITOR', 'IEM',
    'AUDIO TRANSMITTER', 'AUDIO RECEIVER',
    'INTERCOM BELTPACK', 'INTERCOM HEADSET',
    'WIRELESS AUDIO', 'WIRELESS GUITAR',
    'STUDIO MONITOR', 'MONITORING SYSTEM',
    'PROFESSIONAL MICROPHONE', 'PROFESSIONAL AUDIO',
    'WIRELESS TOUR GUIDE', 'TOUR GUIDE SYSTEM',
    'ASSISTIVE LISTENING', 'HEARING ASSISTANCE',
    'SIMULTANEOUS INTERPRETATION',
    'CONFERENCE MICROPHONE', 'CONFERENCE SYSTEM',
    'UHF MICROPHONE', 'VHF MICROPHONE',
    'WIRELESS EARPHONE', 'WIRELESS HEADPHONE',
    'STAGE MONITOR', 'WIRELESS STAGE',
    'LECTERN MICROPHONE', 'GOOSENECK MICROPHONE',
    'LAVALIER MICROPHONE', 'LAVALIER MIC',
]


def _is_likely_non_radio_device(data):
    """Return True if the XML row describes a non-radio audio device.

    Wireless microphones (Part 74H), IEMs, and similar audio gear
    operate in UHF bands that overlap with two-way radio spectrum.
    Without rule_parts data in the XML, we identify these by
    manufacturer names, brand names, and application purpose text
    that contain audio/microphone keywords.
    """
    text_parts = [
        (data.get('applicant_name', '') or '').upper(),
        (data.get('brand', '') or '').upper(),
        (data.get('application_purpose', '') or '').upper(),
    ]
    combined = ' | '.join(text_parts)
    for keyword in _NON_RADIO_DEVICE_KEYWORDS:
        if keyword in combined:
            return True
    return False


def _is_radio_frequency_range(lower_str, upper_str):
    """Return True if the frequency range overlaps known radio bands."""
    try:
        lower = float(lower_str or 0)
        upper = float(upper_str or 0)
    except (ValueError, TypeError):
        return False
    if lower <= 0 or upper <= 0:
        return False
    if lower > upper:
        lower, upper = upper, lower
    for band_low, band_high in _RADIO_FREQ_BANDS:
        if lower <= band_high and upper >= band_low:
            return True
    return False


def _actor_label(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        return str(user)
    return 'anonymous'


def sanitize_xml_content(content):
    """
    Sanitize XML content to fix common issues like unescaped ampersands.
    FCC XML files often have '&' instead of '&amp;' in company names.
    """
    # Replace unescaped & with &amp; (but not already-escaped entities like &amp; &lt; &gt; &quot; &apos;)
    # This regex finds & not followed by amp; lt; gt; quot; apos; or #
    content = re.sub(r'&(?!(amp|lt|gt|quot|apos|#)\b)', '&amp;', content)
    return content


def load_grantee_map(results_xml):
    tree = ET.parse(results_xml)
    root = tree.getroot()
    grantee_map = {}
    for row in root.findall('Row'):
        code = row.findtext('grantee_code', '').strip()
        name = row.findtext('grantee_name', '').strip()
        if code:
            grantee_map[code] = name
    return grantee_map


def parse_fcc_id(fcc_id, grantee_map):
    fcc_id = fcc_id.strip()
    grantee_code = None
    for code in sorted(grantee_map.keys(), key=len, reverse=True):
        if fcc_id.startswith(code):
            grantee_code = code
            break
    if not grantee_code:
        return None, fcc_id
    # Remove grantee code prefix, and if next char is a dash, remove it too
    model = fcc_id[len(grantee_code):].lstrip('-').strip()
    return grantee_code, model


def freq_range_to_band(lower, upper):
    try:
        l = float(lower)
        u = float(upper)
    except Exception:
        return set()
    bands = set()
    # HF: 0-30 MHz
    if l < 30 or u <= 30 or (l <= 30 <= u):
        bands.add("HF")
    # VHF: 30-300 MHz
    if (l < 300 and u > 30) or (l <= 300 <= u) or (l >= 30 and u <= 300):
        bands.add("VHF")
    # UHF: 300-1000 MHz
    if (l < 1000 and u > 300) or (l <= 1000 <= u) or (l >= 300 and u <= 1000):
        bands.add("UHF")
    return bands


@staff_required
def import_grantee_radios(request):
    if request.method == 'POST':
        logger.info("User action xml_import submit actor=%s", _actor_label(request))
        # Check if this is confirmation of a preview (radio_data passed via hidden field)
        if 'confirm_import' in request.POST and 'radio_data_b64' in request.POST:
            radio_data_b64 = request.POST.get('radio_data_b64', '')
            overwrite = request.POST.get('overwrite_records') == 'on'
            try:
                radio_data_json = base64.b64decode(radio_data_b64).decode('utf-8')
                radio_list = json.loads(radio_data_json)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                logger.exception("XML import confirmation decode error actor=%s", _actor_label(request))
                messages.error(request, f"Invalid import data. Please try again. ({e})")
                return redirect('import_grantee_radios')
            
            created_count = 0
            updated_count = 0
            skipped_count = 0
            total_records = len(radio_list)
            
            # Get grantee info from first record
            grantee_code = radio_list[0]['grantee_code'] if radio_list else ''
            grantee_name = radio_list[0]['brand'] if radio_list else ''
            grantee_country = radio_list[0].get('country', '') if radio_list else ''

            if grantee_code and IgnoredGrantee.is_ignored(grantee_code):
                logger.info(
                    "XML import skipped ignored grantee actor=%s grantee_code=%s grantee_name=%s",
                    _actor_label(request),
                    grantee_code,
                    grantee_name,
                )
                messages.warning(
                    request,
                    f"Skipped import for grantee {grantee_code} because it is on the ignore list.",
                )
                return redirect('import_grantee_radios')
            
            # Update or create Brand record for this grantee
            if grantee_code and grantee_name:
                try:
                    brand_obj = Brand.objects.get(grantee_code=grantee_code)
                    # Grantee code exists. Update country if missing.
                    if not brand_obj.country and grantee_country:
                        brand_obj.country = grantee_country
                        brand_obj.save(update_fields=['country'])
                except Brand.DoesNotExist:
                    # Grantee code doesn't exist. Check by name, alias, or full_name.
                    from django.db.models import Q
                    brand_obj = Brand.objects.filter(
                        Q(name__iexact=grantee_name) | 
                        Q(alias__iexact=grantee_name) | 
                        Q(full_name__iexact=grantee_name)
                    ).first()
                    
                    if brand_obj:
                        # Match found. Update grantee code and country if missing.
                        brand_obj.grantee_code = grantee_code
                        if not brand_obj.country and grantee_country:
                            brand_obj.country = grantee_country
                        brand_obj.save(update_fields=['grantee_code', 'country'])
                    else:
                        # Neither exists, create a new one.
                        Brand.objects.create(
                            name=grantee_name,
                            grantee_code=grantee_code,
                            country=grantee_country
                        )
            
            for data in radio_list:
                brand = data['brand']
                model = data['model']
                g_code = data['grantee_code']
                fcc_id = f"{g_code}{model}" if '-' not in model else f"{g_code}-{model}"

                validation = validate_fcc_brand_assignment(fcc_id, brand)
                if validation.get('status') == 'white_label_possible':
                    logger.info(
                        "FCC validation white-label candidate source=xml_import actor=%s fcc_id=%s inferred_grantee=%s grantee_brand=%s provided_brand=%s resolved_brand=%s",
                        _actor_label(request),
                        fcc_id,
                        validation.get('inferred_grantee_code', ''),
                        validation.get('grantee_brand_name', ''),
                        validation.get('provided_brand_name', ''),
                        validation.get('resolved_brand_name', ''),
                    )
                elif validation.get('status') == 'invalid_fcc_id':
                    logger.warning(
                        "FCC validation invalid id source=xml_import actor=%s fcc_id=%s provided_brand=%s",
                        _actor_label(request),
                        fcc_id,
                        validation.get('provided_brand_name', ''),
                    )

                resolved_brand = validation.get('resolved_brand_name') or brand

                # ── Classifier check: skip non-radio devices ──
                # Build secondary metadata from the XML row's frequency data
                # so the FCC-field-based classifier can check rule parts,
                # frequency ranges, and emission designators.
                lower = data.get('lower_freq_mhz', '').strip()
                upper = data.get('upper_freq_mhz', '').strip()
                oe_rows = []
                if lower and upper:
                    oe_rows.append({
                        'grant_date': data.get('grant_date', ''),
                        'lower_freq_mhz': lower,
                        'upper_freq_mhz': upper,
                        'power_output': data.get('power_output', ''),
                        'emission_designator': data.get('emission_designator', ''),
                    })
                sec_meta = {
                    'text_blob': '',
                    'rule_parts': [],
                    'original_equipment_rows': oe_rows,
                }
                primary = {
                    'FCCId': fcc_id,
                    'grantee': brand,
                    'applicationPurpose': data.get('application_purpose', ''),
                }
                is_radio, _classifier_tags = _classify_fcc_device(primary, sec_meta)

                # Fallback: the XML export format omits rule_parts and
                # emission_designator, so the FCC-field-based classifier
                # may reject valid radios.  Check frequency ranges against
                # known two-way radio bands as a secondary signal.
                if not is_radio and lower and upper:
                    is_radio = _is_radio_frequency_range(lower, upper)

                if not is_radio:
                    skipped_count += 1
                    logger.info(
                        "XML import classifier rejected non-radio "
                        "fcc_id=%s brand=%s model=%s",
                        fcc_id, brand, model,
                    )
                    continue

                if overwrite:
                    radio_obj, created = Radio.objects.update_or_create(
                        brand=resolved_brand, model=model,
                        defaults={'fcc_id': fcc_id}
                    )
                    if created:
                        created_count += 1
                        logger.info(
                            "FCC ingest create source=xml_import actor=%s radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s overwrite=%s",
                            _actor_label(request),
                            radio_obj.id,
                            resolved_brand,
                            model,
                            fcc_id,
                            validation.get('status', ''),
                            overwrite,
                        )
                    else:
                        updated_count += 1
                        logger.info(
                            "FCC ingest update source=xml_import actor=%s radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s overwrite=%s",
                            _actor_label(request),
                            radio_obj.id,
                            resolved_brand,
                            model,
                            fcc_id,
                            validation.get('status', ''),
                            overwrite,
                        )
                else:
                    if not Radio.objects.filter(brand=resolved_brand, model=model).exists():
                        created_radio = Radio.objects.create(brand=resolved_brand, model=model, fcc_id=fcc_id)
                        created_count += 1
                        logger.info(
                            "FCC ingest create source=xml_import actor=%s radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s overwrite=%s",
                            _actor_label(request),
                            created_radio.id,
                            resolved_brand,
                            model,
                            fcc_id,
                            validation.get('status', ''),
                            overwrite,
                        )
                    else:
                        skipped_count += 1
                        logger.info(
                            "FCC ingest skip source=xml_import actor=%s brand=%s model=%s fcc_id=%s reason=exists validation_status=%s overwrite=%s",
                            _actor_label(request),
                            resolved_brand,
                            model,
                            fcc_id,
                            validation.get('status', ''),
                            overwrite,
                        )

            logger.info(
                "XML import confirmation result actor=%s total=%s created=%s updated=%s skipped=%s overwrite=%s",
                _actor_label(request),
                total_records,
                created_count,
                updated_count,
                skipped_count,
                overwrite,
            )
            
            # Build detailed success message
            msg_parts = [f"Grantee {grantee_code} ({grantee_name}): Processed {total_records} records"]
            if created_count:
                msg_parts.append(f"{created_count} new radios added")
            if updated_count:
                msg_parts.append(f"{updated_count} existing radios updated")
            if skipped_count:
                msg_parts.append(f"{skipped_count} duplicates skipped")
            messages.success(request, " • ".join(msg_parts))
            return redirect('import_grantee_radios')
        
        # Initial upload - parse XML and show preview
        form = ImportGranteeXMLForm(request.POST, request.FILES)
        if form.is_valid():
            xml_file = form.cleaned_data['xml_file']
            overwrite = form.cleaned_data.get('overwrite_records', False)
            logger.info(
                "XML upload attempt actor=%s filename=%s size=%s overwrite=%s",
                _actor_label(request),
                getattr(xml_file, 'name', ''),
                getattr(xml_file, 'size', 0),
                overwrite,
            )
            
            # Read and sanitize XML content
            try:
                xml_content = xml_file.read().decode('utf-8', errors='replace')
            except Exception:
                xml_content = xml_file.read().decode('iso-8859-1', errors='replace')
            
            xml_content = sanitize_xml_content(xml_content)
            
            # Parse sanitized XML
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError as e:
                logger.exception("XML parse error actor=%s filename=%s", _actor_label(request), getattr(xml_file, 'name', ''))
                messages.error(request, f"XML parsing error: {e}")
                return render(request, 'radios/import_grantee_radios.html', {'form': form})
            
            # Load grantee code -> name map
            grantee_map = load_grantee_map(RESULTS_XML)
            
            # Aggregate all rows by (brand, model), also capture country from first row
            radio_data = {}
            grantee_country = ''
            for row in root.findall('Row'):
                fcc_id = row.findtext('fcc_id', '').strip()
                if not fcc_id:
                    continue
                grantee_code, model = parse_fcc_id(fcc_id, grantee_map)
                if not grantee_code or not model:
                    continue
                brand_name = grantee_map.get(grantee_code, grantee_code)
                # Capture country from first valid row
                if not grantee_country:
                    grantee_country = row.findtext('country', '').strip()
                key = (brand_name, grantee_code, model)
                if key not in radio_data:
                    radio_data[key] = {
                        'brand': brand_name,
                        'grantee_code': grantee_code,
                        'model': model,
                        'country': grantee_country,
                        # FCC technical fields for classifier
                        'application_purpose': row.findtext(
                            'application_purpose', '',
                        ).strip(),
                        'grant_date': row.findtext('grant_date', '').strip(),
                        'lower_freq_mhz': row.findtext(
                            'lower_freq_mhz', '',
                        ).strip(),
                        'upper_freq_mhz': row.findtext(
                            'upper_freq_mhz', '',
                        ).strip(),
                        'power_output': row.findtext(
                            'power_output', '',
                        ).strip(),
                        'emission_designator': row.findtext(
                            'emission_designator', '',
                        ).strip(),
                    }
            
            # Show preview with radio data stored as base64-encoded JSON for confirmation
            preview = list(radio_data.values())
            logger.info("XML preview generated actor=%s records=%s", _actor_label(request), len(preview))
            radio_data_json = json.dumps(preview)
            radio_data_b64 = base64.b64encode(radio_data_json.encode('utf-8')).decode('ascii')
            return render(request, 'radios/import_grantee_radios.html', {
                'form': form,
                'preview': preview,
                'overwrite': overwrite,
                'radio_data_b64': radio_data_b64,
            })
    else:
        logger.info("User action xml_import view actor=%s", _actor_label(request))
        form = ImportGranteeXMLForm()
    return render(request, 'radios/import_grantee_radios.html', {'form': form})
