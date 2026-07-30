#!/usr/bin/env python
"""Test FCC-field-based classifier on existing radios.

Usage: python check_allowlist_coverage.py [--sample N] [--verbose]
"""
import os
import sys
import random
from collections import Counter

import django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "radio_database.settings")
django.setup()

from radios.models import Radio
from radios.fcc_utils import (
    _classify_fcc_device,
    _sync_metadata_cache,
    fetch_fcc_secondary_metadata,
    reset_sync_metadata_cache,
)


def main():
    sample_size = None
    verbose = '--verbose' in sys.argv
    for i, arg in enumerate(sys.argv):
        if arg == '--sample' and i + 1 < len(sys.argv):
            try:
                sample_size = int(sys.argv[i + 1])
            except ValueError:
                pass

    radios = list(Radio.objects.exclude(fcc_id='').exclude(
        fcc_id__isnull=True,
    ))
    total = len(radios)
    print("Total radios with FCC IDs: %d" % total)

    if sample_size and sample_size < total:
        radios = random.sample(radios, sample_size)
        print("Sampled %d radios" % sample_size)

    reset_sync_metadata_cache()
    radio_pass = 0
    radio_fail = 0
    tag_counter = Counter()
    failed = []

    for i, radio in enumerate(radios):
        fcc_id = radio.fcc_id.strip().upper()
        if (i + 1) % 20 == 0:
            print("  %d/%d..." % (i + 1, len(radios)))

        primary_record = {
            'FCCId': fcc_id,
            'grantee': radio.brand or '',
            'applicationPurpose': '',
            'grantDate': str(radio.grant_date or ''),
        }

        sec_meta = _sync_metadata_cache.get(fcc_id)
        if sec_meta is None:
            try:
                sec_meta = fetch_fcc_secondary_metadata(fcc_id)
                _sync_metadata_cache[fcc_id] = sec_meta
            except Exception as exc:
                failed.append((fcc_id, 'error: %s' % exc))
                continue

        is_radio, tags = _classify_fcc_device(primary_record, sec_meta)
        if is_radio:
            radio_pass += 1
            for t in tags:
                tag_counter[t] += 1
        else:
            radio_fail += 1
            failed.append((fcc_id, radio.brand or '?'))

    total_ok = radio_pass + radio_fail
    print()
    print("=" * 60)
    print("RESULTS (FCC-field-based classifier)")
    print("=" * 60)
    if total_ok:
        print("RADIO:   %d (%.1f%%)" % (radio_pass, radio_pass / total_ok * 100))
        print("NOT:     %d (%.1f%%)" % (radio_fail, radio_fail / total_ok * 100))

    if failed:
        limit = 30 if verbose else 10
        print("\nNot radio (%d):" % len(failed))
        for fid, brand in failed[:limit]:
            print("  %-30s %s" % (fid, brand))
        if len(failed) > limit:
            print("  ... +%d more" % (len(failed) - limit))

    print("\nTop classifier tags:")
    for tag, count in tag_counter.most_common(25):
        print("  %-25s %5d" % (tag, count))


if __name__ == '__main__':
    main()
