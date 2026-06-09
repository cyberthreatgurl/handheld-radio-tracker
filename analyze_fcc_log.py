#!/usr/bin/env python3
"""
analyze_fcc_log.py — FCC sync log analyzer.

Usage:
  # Analyze a single log file
  python analyze_fcc_log.py logs/radio_tracker.log

  # Compare today vs yesterday (find re-processed IDs)
  python analyze_fcc_log.py logs/radio_tracker.log logs/radio_tracker.log.2026-06-04

  # Only show overlap section
  python analyze_fcc_log.py logs/radio_tracker.log logs/radio_tracker.log.2026-06-04 --overlap-only
"""

import re
import sys
import argparse
from collections import defaultdict
from datetime import datetime


# ── Log line patterns ────────────────────────────────────────────────────────

RE_TIMESTAMP = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+')
RE_SYNC_STARTED  = re.compile(r'FCC sync request started query=(\S+)')
RE_SYNC_COMPLETE = re.compile(
    r'FCC sync completed query=(\S+) '
    r'added=(\d+) updated=(\d+) exact_grantee=(\S+) '
    r'skipped_non_exact=(\d+) skipped_non_radio=(\d+) '
    r'skipped_stale_lookup=(\d+) attached_test_reports=(\d+) '
    r'synced_oet_documents=(\d+)'
)
RE_OET_FETCH   = re.compile(r'FCC OET fetch start fcc_id=(\S+) candidate_url_count=(\d+)')
RE_OET_SUCCESS = re.compile(
    r'FCC (?:browser fallback OET success|OET browser fallback success) '
    r'fcc_id=(\S+)'
)
RE_OET_SYNC_COMPLETE = re.compile(
    r'FCC OET sync complete radio_id=(\d+) fcc_id=(\S+) '
    r'synced_count=(\d+)'
)
RE_STALE_SKIP  = re.compile(
    r'FCC ingest skipped stale lookup source=fcc_api query=(\S+) '
    r'radio_id=(\d+) .* fcc_id=(\S+)'
)
RE_NON_RADIO   = re.compile(r'FCC ingest skipped record .* query=(\S+) fcc_id=(\S+) reason=no_radio_allowlist_match')
RE_TIMEOUT_ERR = re.compile(r'FCC browser OET page load failed fcc_id=(\S+)')
RE_CIRCUIT_BRK = re.compile(r'FCC browser OET circuit breaker triggered fcc_id=(\S+)')
RE_503         = re.compile(r'FCC (?:metadata fetch|generic search form) non-200 fcc_id=(\S+) status=503')
RE_GRANTEE_SEARCH_FAIL = re.compile(r'FCC browser fallback search failed fcc_id=(\S+)')


def parse_ts(line):
    m = RE_TIMESTAMP.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    return None


def parse_log(path):
    """Parse a single log file and return a structured summary dict."""
    grantees_started = {}       # query -> first_seen_ts
    grantees_completed = {}     # query -> {added, updated, skipped_stale, oet_docs, ...}
    oet_fetches = {}            # fcc_id -> ts
    oet_successes = set()       # fcc_id
    oet_synced = defaultdict(int)  # fcc_id -> total docs synced across radios
    stale_skips = defaultdict(list)  # query -> [fcc_id, ...]
    non_radio_skips = defaultdict(list)
    timeouts = defaultdict(int)   # fcc_id -> count
    circuit_breaks = set()
    five03s = defaultdict(int)    # fcc_id -> count
    grantee_search_fails = set()

    first_ts = None
    last_ts = None

    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            ts = parse_ts(line)
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            m = RE_SYNC_STARTED.search(line)
            if m:
                q = m.group(1)
                if q not in grantees_started:
                    grantees_started[q] = ts
                continue

            m = RE_SYNC_COMPLETE.search(line)
            if m:
                grantees_completed[m.group(1)] = {
                    'added':          int(m.group(2)),
                    'updated':        int(m.group(3)),
                    'skipped_non_exact': int(m.group(5)),
                    'skipped_non_radio': int(m.group(6)),
                    'skipped_stale':  int(m.group(7)),
                    'oet_docs':       int(m.group(9)),
                    'ts':             ts,
                }
                continue

            m = RE_OET_FETCH.search(line)
            if m:
                fid = m.group(1)
                if fid not in oet_fetches:
                    oet_fetches[fid] = ts
                continue

            m = RE_OET_SUCCESS.search(line)
            if m:
                oet_successes.add(m.group(1))
                continue

            m = RE_OET_SYNC_COMPLETE.search(line)
            if m:
                oet_synced[m.group(2)] += int(m.group(3))
                continue

            m = RE_STALE_SKIP.search(line)
            if m:
                stale_skips[m.group(1)].append(m.group(3))
                continue

            m = RE_NON_RADIO.search(line)
            if m:
                non_radio_skips[m.group(1)].append(m.group(2))
                continue

            m = RE_TIMEOUT_ERR.search(line)
            if m:
                timeouts[m.group(1)] += 1
                continue

            m = RE_CIRCUIT_BRK.search(line)
            if m:
                circuit_breaks.add(m.group(1))
                continue

            m = RE_503.search(line)
            if m:
                five03s[m.group(1)] += 1
                continue

            m = RE_GRANTEE_SEARCH_FAIL.search(line)
            if m:
                grantee_search_fails.add(m.group(1))
                continue

    return {
        'path': path,
        'first_ts': first_ts,
        'last_ts': last_ts,
        'grantees_started': grantees_started,
        'grantees_completed': grantees_completed,
        'oet_fetches': oet_fetches,            # fcc_id -> ts
        'oet_successes': oet_successes,
        'oet_synced': oet_synced,
        'stale_skips': stale_skips,
        'non_radio_skips': non_radio_skips,
        'timeouts': timeouts,
        'circuit_breaks': circuit_breaks,
        'five03s': five03s,
        'grantee_search_fails': grantee_search_fails,
    }


def fmt_ts(ts):
    return ts.strftime('%Y-%m-%d %H:%M:%S') if ts else 'N/A'


def duration_str(a, b):
    if a and b:
        secs = int((b - a).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f'{h}h {m}m {s}s'
    return 'N/A'


def print_single_report(data, label=None):
    tag = label or data['path']
    completed = data['grantees_completed']
    started   = data['grantees_started']
    oet       = data['oet_fetches']
    stale     = data['stale_skips']
    timeouts  = data['timeouts']
    cb        = data['circuit_breaks']

    total_added   = sum(v['added']   for v in completed.values())
    total_updated = sum(v['updated'] for v in completed.values())
    total_oet     = sum(v['oet_docs'] for v in completed.values())
    total_stale   = sum(v['skipped_stale'] for v in completed.values())
    total_non_rad = sum(v['skipped_non_radio'] for v in completed.values())
    total_503     = sum(data['five03s'].values())
    total_timeouts = sum(timeouts.values())

    incomplete = set(started) - set(completed)

    print(f'\n{"="*66}')
    print(f'  LOG: {tag}')
    print(f'{"="*66}')
    print(f'  Period : {fmt_ts(data["first_ts"])} → {fmt_ts(data["last_ts"])}')
    print(f'  Runtime: {duration_str(data["first_ts"], data["last_ts"])}')
    print()
    print('  GRANTEE QUERIES')
    print(f'    Started   : {len(started)}')
    print(f'    Completed : {len(completed)}')
    if incomplete:
        print(f'    Incomplete: {len(incomplete)}  ({", ".join(sorted(incomplete))})')
    print()
    print('  GRANTEE RESULTS (across completed queries)')
    print(f'    Radios added          : {total_added}')
    print(f'    Radios updated        : {total_updated}')
    print(f'    OET docs synced       : {total_oet}')
    print(f'    Skipped (stale)       : {total_stale}')
    print(f'    Skipped (non-radio)   : {total_non_rad}')
    print()
    print('  FCC ID LEVEL')
    print(f'    Unique FCC IDs OET-fetched : {len(oet)}')
    print(f'    OET fetch successes        : {len(data["oet_successes"])}')
    fetch_fail = len(oet) - len(data['oet_successes'])
    print(f'    OET fetch failures         : {fetch_fail}')
    print()
    print('  FCC SERVER ERRORS')
    print(f'    503 responses    : {total_503}')
    print(f'    Playwright timeouts: {total_timeouts}')
    print(f'    Circuit-breaker triggers: {len(cb)}')
    print(f'    Grantee search failures : {len(data["grantee_search_fails"])}')

    print()
    print('  STALE-SKIP EFFECTIVENESS')
    if total_stale == 0:
        print('    !! skipped_stale_lookup=0 for every grantee.')
        print('    !! FCC records may be missing last-modified dates (common when')
        print('    !! FCC returns 503 and Playwright fallback provides no metadata).')
        print('    !! The stale-skip guard cannot fire without record_last_modified.')
    else:
        stale_grantees = {q: v['skipped_stale'] for q, v in completed.items() if v['skipped_stale'] > 0}
        print(f'    Grantees with at least one stale skip: {len(stale_grantees)}')
        for q, n in sorted(stale_grantees.items(), key=lambda x: -x[1])[:10]:
            print(f'      {q}: {n}')

    print()
    print('  PER-GRANTEE BREAKDOWN')
    print(f'  {"Query":<12} {"Added":>6} {"Upd":>5} {"Stale":>6} {"NonRad":>7} {"OETdocs":>8}  {"Duration":>10}')
    print(f'  {"-"*12} {"-"*6} {"-"*5} {"-"*6} {"-"*7} {"-"*8}  {"-"*10}')
    for q in sorted(completed):
        v = completed[q]
        start_ts = started.get(q)
        end_ts   = v['ts']
        dur = duration_str(start_ts, end_ts) if start_ts and end_ts else '-'
        print(f'  {q:<12} {v["added"]:>6} {v["updated"]:>5} {v["skipped_stale"]:>6} '
              f'{v["skipped_non_radio"]:>7} {v["oet_docs"]:>8}  {dur:>10}')


def print_overlap_report(a, b):
    ids_a = set(a['oet_fetches'].keys())
    ids_b = set(b['oet_fetches'].keys())
    overlap = ids_a & ids_b

    q_a = set(a['grantees_started'].keys())
    q_b = set(b['grantees_started'].keys())
    q_overlap = q_a & q_b

    print(f'\n{"="*66}')
    print('  OVERLAP / DUPLICATE PROCESSING REPORT')
    print(f'{"="*66}')
    print(f'  File A (primary) : {a["path"]}')
    print(f'  File B (baseline): {b["path"]}')
    print()
    print('  GRANTEE QUERY OVERLAP')
    print(f'    Grantees in A    : {len(q_a)}')
    print(f'    Grantees in B    : {len(q_b)}')
    print(f'    Re-processed     : {len(q_overlap)} ({100*len(q_overlap)//max(len(q_a),1)}% of A)')
    if q_overlap:
        print(f'    Re-processed queries: {", ".join(sorted(q_overlap))}')

    print()
    print('  FCC ID OET-FETCH OVERLAP')
    print(f'    Unique IDs in A  : {len(ids_a)}')
    print(f'    Unique IDs in B  : {len(ids_b)}')
    print(f'    Re-processed IDs : {len(overlap)} ({100*len(overlap)//max(len(ids_a),1)}% of A)')
    only_a = ids_a - ids_b
    only_b = ids_b - ids_a
    print(f'    Only in A (new)  : {len(only_a)}')
    print(f'    Only in B (old)  : {len(only_b)}')

    if overlap:
        print()
        print('  TOP 30 RE-PROCESSED FCC IDs (from A, also in B):')
        for fid in sorted(overlap)[:30]:
            ts_a = fmt_ts(a['oet_fetches'][fid])
            ts_b = fmt_ts(b['oet_fetches'][fid])
            print(f'    {fid:<35} A={ts_a}  B={ts_b}')
        if len(overlap) > 30:
            print(f'    ... and {len(overlap)-30} more')

    if only_a:
        print()
        print('  TOP 20 FCC IDs only in A (genuinely new this run):')
        for fid in sorted(only_a)[:20]:
            print(f'    {fid}')
        if len(only_a) > 20:
            print(f'    ... and {len(only_a)-20} more')

    print()
    print('  DIAGNOSIS')
    pct = 100 * len(overlap) // max(len(ids_a), 1)
    if pct >= 90:
        print(f'    !! {pct}% re-processing — stale-skip guard is NOT working.')
        print('    !! Most likely cause: FCC API returns 503s for primary records,')
        print('    !!   so record_last_modified cannot be extracted, and the comparison')
        print('    !!   in _should_skip_supporting_lookup returns (False, None) for')
        print('    !!   every radio, forcing full reprocessing every run.')
    elif pct >= 50:
        print(f'    WARN: {pct}% re-processing — stale-skip is partially working.')
    else:
        print(f'    OK: {pct}% re-processing — stale-skip is mostly effective.')


def main():
    parser = argparse.ArgumentParser(description='Analyze FCC sync log files.')
    parser.add_argument('primary',  help='Log file to analyze (today)')
    parser.add_argument('baseline', nargs='?', help='Older log file to compare against (yesterday)')
    parser.add_argument('--overlap-only', action='store_true',
                        help='Only print the overlap/comparison section')
    args = parser.parse_args()

    print(f'Parsing {args.primary}...')
    data_a = parse_log(args.primary)

    if args.baseline:
        print(f'Parsing {args.baseline}...')
        data_b = parse_log(args.baseline)
        if not args.overlap_only:
            print_single_report(data_a, label=f'PRIMARY: {args.primary}')
            print_single_report(data_b, label=f'BASELINE: {args.baseline}')
        print_overlap_report(data_a, data_b)
    else:
        print_single_report(data_a)

    print()


if __name__ == '__main__':
    main()
