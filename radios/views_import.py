from django.shortcuts import render, redirect
from django.contrib import messages
from .forms import ImportGranteeXMLForm
from .models import Radio, Brand
import xml.etree.ElementTree as ET
import os
import re
import json
import base64

RESULTS_XML = os.path.join('data', 'results.xml')


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


def import_grantee_radios(request):
    if request.method == 'POST':
        # Check if this is confirmation of a preview (radio_data passed via hidden field)
        if 'confirm_import' in request.POST and 'radio_data_b64' in request.POST:
            radio_data_b64 = request.POST.get('radio_data_b64', '')
            overwrite = request.POST.get('overwrite_records') == 'on'
            try:
                radio_data_json = base64.b64decode(radio_data_b64).decode('utf-8')
                radio_list = json.loads(radio_data_json)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                messages.error(request, f"Invalid import data. Please try again. ({e})")
                return redirect('import_grantee_radios')
            
            created_count = 0
            updated_count = 0
            skipped_count = 0
            total_records = len(radio_list)
            
            # Get grantee info from first record
            grantee_code = radio_list[0]['grantee_code'] if radio_list else ''
            grantee_name = radio_list[0]['brand'] if radio_list else ''
            
            for data in radio_list:
                brand = data['brand']
                model = data['model']
                g_code = data['grantee_code']
                fcc_id = f"{g_code}{model}" if '-' not in model else f"{g_code}-{model}"
                if overwrite:
                    radio, created = Radio.objects.update_or_create(
                        brand=brand, model=model,
                        defaults={'fcc_id': fcc_id}
                    )
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1
                else:
                    if not Radio.objects.filter(brand=brand, model=model).exists():
                        Radio.objects.create(brand=brand, model=model, fcc_id=fcc_id)
                        created_count += 1
                    else:
                        skipped_count += 1
            
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
                messages.error(request, f"XML parsing error: {e}")
                return render(request, 'radios/import_grantee_radios.html', {'form': form})
            
            # Load grantee code -> name map
            grantee_map = load_grantee_map(RESULTS_XML)
            
            # Aggregate all rows by (brand, model)
            radio_data = {}
            for row in root.findall('Row'):
                fcc_id = row.findtext('fcc_id', '').strip()
                if not fcc_id:
                    continue
                grantee_code, model = parse_fcc_id(fcc_id, grantee_map)
                if not grantee_code or not model:
                    continue
                brand_name = grantee_map.get(grantee_code, grantee_code)
                key = (brand_name, grantee_code, model)
                if key not in radio_data:
                    radio_data[key] = {
                        'brand': brand_name,
                        'grantee_code': grantee_code,
                        'model': model,
                    }
            
            # Show preview with radio data stored as base64-encoded JSON for confirmation
            preview = list(radio_data.values())
            radio_data_json = json.dumps(preview)
            radio_data_b64 = base64.b64encode(radio_data_json.encode('utf-8')).decode('ascii')
            return render(request, 'radios/import_grantee_radios.html', {
                'form': form,
                'preview': preview,
                'overwrite': overwrite,
                'radio_data_b64': radio_data_b64,
            })
    else:
        form = ImportGranteeXMLForm()
    return render(request, 'radios/import_grantee_radios.html', {'form': form})
