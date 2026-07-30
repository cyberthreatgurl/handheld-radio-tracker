#!/usr/bin/env python
"""Debug script: show which allowlist/denylist terms match a given FCC ID.

Usage:
    python debug_allowlist.py 2A25L-LC008          # single FCC ID
    python debug_allowlist.py 2A25L-LC008 2AJGM-UV5R  # multiple
    python debug_allowlist.py --grantee 2A25L       # all records for a grantee
"""
import argparse
import os
import sys

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'radio_database.settings')

import django
django.setup()

from radios.fcc_utils import (
    _radio_allowlist_terms,
    _radio_denylist_terms,
    _allowlist_match_terms,
    _denylist_match_terms,
    _primary_record_matches_allowlist,
    fetch_fcc_secondary_metadata,
    _sync_metadata_cache,
    split_fcc_id,
)


def analyze_fcc_id(fcc_id, index=0):
    """Print a detailed breakdown of allowlist/denylist matches for an FCC ID."""
    allowlist = _radio_allowlist_terms()
    denylist = _radio_denylist_terms()

    print(f"\n{'=' * 72}")
    print(f"FCC ID: {fcc_id}")
    if index:
        print(f"Index:  {index}")
    print(f"{'=' * 72}")

    grantee_code, product_code = split_fcc_id(fcc_id)
    print(f"Grantee: {grantee_code}  Product: {product_code}")

    # Build a minimal primary record for analysis
    primary_record = {
        'FCCId': fcc_id,
        'grantee': grantee_code,
        'applicationPurpose': '',
        'grantDate': '',
    }

    # Quick pre-check (same logic used before expensive secondary fetch)
    quick_match = _primary_record_matches_allowlist(primary_record, allowlist)
    print(f"\nQuick pre-check (grantee + purpose): {'PASS' if quick_match else 'FAIL'}")

    if quick_match:
        # Show which terms matched in the quick check
        sources = [
            primary_record.get('grantee', '') or '',
            primary_record.get('applicationPurpose', '') or '',
        ]
        text = ' | '.join(str(v) for v in sources if v).upper()
        matched = [t for t in allowlist if t in text]
        print(f"  Quick-matched terms: {matched}")

    # Fetch secondary metadata (expensive: curl_cffi + possible Playwright)
    print("\nFetching secondary metadata from FCC ...")
    sec_metadata = fetch_fcc_secondary_metadata(fcc_id)

    if not sec_metadata or not sec_metadata.get('text_blob'):
        print("  No secondary metadata found (record may not exist at FCC).")
        return

    print(f"  Record count: {sec_metadata.get('record_count', 0)}")
    print(f"  Rule parts:   {sec_metadata.get('rule_parts', [])}")
    text_blob = sec_metadata.get('text_blob', '')
    print(f"  Text blob ({len(text_blob)} chars): {text_blob[:300]}...")

    # Full allowlist check
    matched_terms = _allowlist_match_terms(primary_record, sec_metadata, allowlist)
    print(f"\n--- ALLOWLIST ({len(allowlist)} terms) ---")
    if matched_terms:
        print(f"  MATCHED ({len(matched_terms)}):")
        for term in matched_terms:
            print(f"    ✓ {term}")
    else:
        print("  NO MATCHES — this FCC ID would be SKIPPED")

    # Show non-matching terms for context (just a sample)
    non_matched = [t for t in allowlist if t not in matched_terms]
    if non_matched:
        print(f"  Non-matched ({len(non_matched)} terms, showing first 20):")
        for term in non_matched[:20]:
            print(f"    ✗ {term}")

    # Denylist check
    denied_terms = _denylist_match_terms(primary_record, sec_metadata, denylist)
    print(f"\n--- DENYLIST ({len(denylist)} terms) ---")
    if denied_terms:
        print(f"  MATCHED DENYLIST ({len(denied_terms)}):")
        for term in denied_terms:
            print(f"    ⚠ {term}")
    else:
        print("  No denylist matches")

    # Summary
    print(f"\n--- VERDICT ---")
    if quick_match and not matched_terms:
        print("  Quick pre-check PASSED but full allowlist FAILED")
        print("  → Secondary metadata had NO radio keywords")
        print("  → This FCC ID would be SKIPPED (correctly)")
    elif matched_terms and denied_terms:
        print("  MATCHED ALLOWLIST but also MATCHED DENYLIST → SKIPPED (denylist)")
    elif matched_terms:
        print(f"  ALLOWLIST MATCH → would be INGESTED as a radio")
        print(f"  (matched: {', '.join(matched_terms)})")
    else:
        print("  NO MATCHES → would be SKIPPED")

    # Print the full text blob the matching runs against for manual inspection
    print(f"\n--- FULL MATCHING TEXT ---")
    sources = [
        primary_record.get('FCCId', '') or '',
        primary_record.get('grantee', '') or '',
        primary_record.get('applicationPurpose', '') or '',
        primary_record.get('grantDate', '') or '',
        sec_metadata.get('text_blob', '') or '',
    ]
    full_text = ' | '.join(str(v) for v in sources if v).upper()
    print(full_text[:2000])
    if len(full_text) > 2000:
        print(f"... ({len(full_text) - 2000} more chars)")


def main():
    parser = argparse.ArgumentParser(
        description='Debug allowlist/denylist matching for FCC IDs',
    )
    parser.add_argument(
        'fcc_ids', nargs='*',
        help='FCC ID(s) to analyze',
    )
    parser.add_argument(
        '--grantee', '-g',
        help='Grantee code — fetches ALL FCC IDs for this grantee (use sparingly)',
    )
    args = parser.parse_args()

    if not args.fcc_ids and not args.grantee:
        parser.error('Specify at least one FCC ID or --grantee')

    # Handle grantee query
    if args.grantee:
        from radios.fcc_utils import fetch_and_sync_fcc_id
        # Use a cheap fetch to get the FCC IDs for this grantee
        print(f"Fetching FCC records for grantee {args.grantee} ...")
        try:
            added, updated, msgs = fetch_and_sync_fcc_id(
                args.grantee,
                honor_skip_lists=False,
            )
            for msg in msgs:
                print(f"  {msg}")
        except Exception as exc:
            print(f"  Error fetching grantee: {exc}")

    for idx, fcc_id in enumerate(args.fcc_ids, 1):
        try:
            analyze_fcc_id(fcc_id.strip().upper(), index=idx)
        except Exception as exc:
            print(f"\n  ERROR analyzing {fcc_id}: {exc}")
            import traceback
            traceback.print_exc()


if __name__ == '__main__':
    main()
