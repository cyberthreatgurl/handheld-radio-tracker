#!/usr/bin/env python3
"""Standalone utility: validate FCC grantee code → grantee name mappings.

Queries the FCC API for each unique grantee code found in radio FCC IDs,
compares the returned legal company name against local Brand records, and
generates an HTML report of any mismatches.

Usage:
    source venv/bin/activate
    python validate_grantee_mappings.py > grantee_audit.html
"""

import re
from pathlib import Path

# ── Django setup ──────────────────────────────────────────────
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'radio_database.settings')
BASE_DIR = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(BASE_DIR))

django.setup()
# ──────────────────────────────────────────────────────────────

from radios.models import Brand, Radio
from radios.fcc_id_utils import split_fcc_id
from curl_cffi import requests


# ── Helpers ───────────────────────────────────────────────────

def _normalize(text):
    """Lowercase, strip, collapse whitespace for comparison."""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text.strip().lower())


def fetch_fcc_grantee_name(grantee_code):
    """Query the FCC getFCCIDList API for a single record to extract
    the legal grantee company name. Returns (name, error_string)."""
    url = f'https://apps.fcc.gov/OETLabServices/getFCCIDList?fccId={grantee_code}'
    try:
        resp = requests.get(url, impersonate='chrome124', timeout=15)
    except Exception as e:
        return None, f'HTTP error: {e}'

    if resp.status_code != 200:
        return None, f'HTTP {resp.status_code}'

    import xmltodict
    try:
        data = xmltodict.parse(resp.text)
    except Exception as e:
        return None, f'XML parse error: {e}'

    wrapper = data.get('fCCIDInfoes') or {}
    records = (wrapper.get('fccidInfo') or [])
    if isinstance(records, dict):
        records = [records]
    if not records:
        return None, 'No records found'

    grantee_name = (records[0].get('grantee', '') or '').strip()
    if not grantee_name:
        return None, 'Grantee field empty'
    return grantee_name, None


def build_report():
    """Collect all unique grantee codes from radio FCC IDs, compare
    against FCC API data, and return (issues, brand_map)."""
    # Collect unique grantee codes
    grantee_to_brands = {}  # grantee_code → [Brand objects]
    grantee_to_sample_fcc = {}  # grantee_code → sample FCC ID

    for radio in Radio.objects.exclude(fcc_id='').exclude(fcc_id__isnull=True).iterator():
        grantee, _ = split_fcc_id(radio.fcc_id)
        if not grantee:
            continue
        if grantee not in grantee_to_brands:
            grantee_to_brands[grantee] = set()
            grantee_to_sample_fcc[grantee] = radio.fcc_id
        # Find brands matching this grantee
        for b in Brand.objects.filter(grantee_code__iexact=grantee):
            grantee_to_brands[grantee].add(b)

    issues = []

    for grantee_code, local_brands in sorted(grantee_to_brands.items()):
        sample_fcc = grantee_to_sample_fcc[grantee_code]

        if not local_brands:
            issues.append({
                'grantee_code': grantee_code,
                'sample_fcc': sample_fcc,
                'severity': 'ERROR',
                'detail': f'Grantee code {grantee_code} has no Brand record! '
                          f'Radio FCC IDs like {sample_fcc} cannot be mapped to any brand.',
                'suggested_fix': f'Create a Brand record with grantee_code="{grantee_code}" '
                                 f'or register this grantee.',
            })
            continue

        # Query FCC API for the real grantee name
        fcc_name, error = fetch_fcc_grantee_name(grantee_code)
        if error:
            issues.append({
                'grantee_code': grantee_code,
                'sample_fcc': sample_fcc,
                'severity': 'WARNING',
                'detail': f'Could not verify grantee {grantee_code} via FCC API: {error}',
                'suggested_fix': 'Run again later when FCC is reachable.',
            })
            continue

        # Check each local brand for a name match
        for b in local_brands:
            # Check brand name similarity to FCC grantee name
            norm_fcc = _normalize(fcc_name)
            norm_brand = _normalize(b.name)
            norm_full = _normalize(b.full_name)
            norm_alias = _normalize(b.alias)

            # Strip punctuation to catch comma/period-only differences
            def _strip_punct(s):
                return re.sub(r'[^\w\s]', '', s)
            nf_strip = _strip_punct(norm_fcc)
            nb_strip = _strip_punct(norm_brand)
            nfull_strip = _strip_punct(norm_full) if norm_full else ''

            name_match = (
                norm_fcc == norm_brand
                or norm_fcc == norm_full
                or norm_fcc == norm_alias
                or (norm_brand and norm_brand in norm_fcc)
                or (norm_fcc and norm_fcc in norm_brand)
                or (norm_full and norm_full in norm_fcc)
                or (norm_fcc in norm_full)
                or nf_strip == nb_strip
                or nf_strip == nfull_strip
            )

            if not name_match:
                issues.append({
                    'grantee_code': grantee_code,
                    'sample_fcc': sample_fcc,
                    'severity': 'MISMATCH',
                    'detail': (
                        f'Brand "{b.name}" (alias="{b.alias}", '
                        f'full_name="{b.full_name}") has grantee_code={grantee_code}, '
                        f'but FCC says the grantee is "{fcc_name}".'
                    ),
                    'fcc_name': fcc_name,
                    'suggested_fix': (
                        f'Option A: Change Brand "{b.name}" name→"{fcc_name}" '
                        f'Option B: Set Brand "{b.name}" full_name→"{fcc_name}" '
                        f'Option C: Remove grantee_code from this Brand if it belongs elsewhere'
                    ),
                })

    return issues, grantee_to_brands, grantee_to_sample_fcc


def render_html(issues, brand_map, sample_fccs):
    """Render issues as a standalone HTML page."""
    ok_count = len(brand_map) - len(issues)
    total = len(brand_map)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FCC Grantee Code Audit</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
  h1 {{ color: #1a1a2e; }}
  .summary {{ display: flex; gap: 20px; margin: 20px 0; }}
  .card {{ background: white; border-radius: 8px; padding: 20px; flex: 1; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .card h2 {{ margin: 0 0 10px 0; font-size: 14px; color: #666; }}
  .card .number {{ font-size: 36px; font-weight: bold; }}
  .ok .number {{ color: #22c55e; }}
  .warn .number {{ color: #f59e0b; }}
  .err .number {{ color: #ef4444; }}

  .issue {{ background: white; border-radius: 8px; padding: 16px 20px; margin: 12px 0;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-left: 4px solid #ccc; }}
  .issue.ERROR {{ border-left-color: #ef4444; }}
  .issue.WARNING {{ border-left-color: #f59e0b; }}
  .issue.MISMATCH {{ border-left-color: #3b82f6; }}
  .issue h3 {{ margin: 0 0 6px 0; font-size: 16px; }}
  .issue .code {{ font-family: monospace; background: #f0f0f0; padding: 2px 6px; border-radius: 3px; }}
  .issue .detail {{ color: #444; margin: 8px 0; line-height: 1.5; }}
  .issue .fix {{ background: #f0f9ff; padding: 10px; border-radius: 4px; margin-top: 8px; font-size: 13px; }}
  .issue .fix strong {{ color: #2563eb; }}

  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid #e5e7eb; }}
  th {{ background: #f8fafc; font-size: 13px; color: #666; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }}
  .badge.ok {{ background: #dcfce7; color: #166534; }}
  .badge.err {{ background: #fee2e2; color: #991b1b; }}
  .badge.warn {{ background: #fef3c7; color: #92400e; }}
  .badge.mismatch {{ background: #dbeafe; color: #1e40af; }}
</style>
</head>
<body>
  <h1>FCC Grantee Code Audit</h1>
  <p>Validating {total} unique grantee codes against FCC API data.</p>

  <div class="summary">
    <div class="card ok">
      <h2>OK / Verified</h2>
      <div class="number">{ok_count}</div>
    </div>
    <div class="card warn">
      <h2>Warnings (FCC unreachable)</h2>
      <div class="number">{sum(1 for i in issues if i['severity']=='WARNING')}</div>
    </div>
    <div class="card err">
      <h2>Errors (missing brand)</h2>
      <div class="number">{sum(1 for i in issues if i['severity']=='ERROR')}</div>
    </div>
    <div class="card" style="border-left:4px solid #3b82f6">
      <h2>Mismatches</h2>
      <div class="number" style="color:#3b82f6">{sum(1 for i in issues if i['severity']=='MISMATCH')}</div>
    </div>
  </div>
'''
    if issues:
        html += '<h2>Issues Found</h2>'
        for issue in issues:
            sev = issue['severity']
            sev_label = {'ERROR': 'Error', 'WARNING': 'Warning', 'MISMATCH': 'Name Mismatch'}[sev]
            html += f'''
  <div class="issue {sev}">
    <h3>
      <span class="badge {sev.lower()}">{sev_label}</span>
      Grantee <span class="code">{issue["grantee_code"]}</span>
      (sample: <span class="code">{issue["sample_fcc"]}</span>)
    </h3>
    <div class="detail">{issue["detail"]}</div>
    <div class="fix"><strong>Suggested fix:</strong> {issue["suggested_fix"]}</div>
  </div>'''

    html += '''
  <h2>All Grantee Codes</h2>
  <table>
    <tr><th>Grantee Code</th><th>Local Brand(s)</th><th>Sample FCC ID</th><th>Status</th></tr>'''

    for grantee_code in sorted(brand_map.keys()):
        brands = brand_map[grantee_code]
        brand_names = ', '.join(sorted(b.name for b in brands)) if brands else '<em>None</em>'
        sample = sample_fccs.get(grantee_code, '')
        issue = next((i for i in issues if i['grantee_code'] == grantee_code), None)
        if issue:
            status = f'<span class="badge {issue["severity"].lower()}">{issue["severity"]}</span>'
        else:
            status = '<span class="badge ok">Verified</span>'

        html += f'''<tr><td><code>{grantee_code}</code></td><td>{brand_names}</td><td><code>{sample}</code></td><td>{status}</td></tr>'''

    html += '''
  </table>
  <p style="color:#888; font-size:12px; margin-top:20px;">
    Generated by validate_grantee_mappings.py &mdash; queries live FCC API for grantee names.
  </p>
</body>
</html>'''
    return html


# ── Main ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    print('Fetching FCC grantee data...', file=sys.stderr)
    issues, brand_map, sample_fccs = build_report()
    html = render_html(issues, brand_map, sample_fccs)
    print(html)
    print(f'Done. {len(issues)} issues found.', file=sys.stderr)
