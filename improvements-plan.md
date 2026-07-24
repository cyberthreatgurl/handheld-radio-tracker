Here's the consolidated wireframe. I've organized it into three phases by risk and impact:

---

## ✅ Completed Features (July 2026)

These capabilities have been implemented on the `improvements` branch:

| Feature | Files | Summary |
|---|---|---|
| **Service Type Auto-Assignment** | `fcc_utils.py`, `models.py`, migrations `0035-0038` | Auto-detects FCC rule parts from TCB Form 731 Report, Generic Search, and CID chains. Maps to `RadioServiceType` M2M (GMRS, FRS, Amateur, Commercial, Marine, Aviation, PoC, Part 15B/C). |
| **TCB Report Rule Parts Parsing** | `fcc_utils.py` | Fetches the FCC `GetTcb731Report.do` page and parses the Equipment Specifications "Rule Parts" column. Authoritative source for `15B`, `90`, `95E`, etc. |
| **CID Chain Resolution** | `fcc_utils.py`, `backfill_cid_radios.py` | Follows Change-in-Identification filings to the original FCC ID. Merges original rule parts into re-label radio metadata. Management command for batch backfill. |
| **Amateur Radio Detection** | `fcc_utils.py` | Detects Part 15B/15C devices with blank TX fields and amateur-band frequencies (144-148 MHz, 420-450 MHz). |
| **Website & YouTube Scraping** | `manual_extraction.py`, `fcc_utils.py`, `views.py` | Domain-specific parsers (Retevis, Radioddity, Temu, AliExpress) + generic JSON-LD/meta/table parser. YouTube transcript extraction. "Scrape Website" button on edit page. Only fills empty fields — never overwrites. |
| **Unified Document Display** | `radio_detail.html`, `radio_form.html` | Single "All Documents" section combining OET documents, user uploads, and FCC test reports. |
| **OET Page URL Persistence** | `fcc_utils.py`, `views.py` | `application_id` extraction with URL-encoded character support. Stored as `oet_page_url` on Radio. |

---

## Wireframe: FCC Sync & Parser Improvements

### Phase 1 — Parser Hardening (low risk, fcc_id_utils.py)

| # | Change | Location | Lines | Status |
|---|---|---|---|---|
| **P1.1** | Unify char validation: `re.match(r'^[A-Z]$')` → `'A' <= c <= 'Z'` in both `split_fcc_id()` and `_exact_grantee_query()` | `fcc_id_utils.py:13-14`, `fcc_utils.py:2457-2459` | 4 | ✅ Done |
| **P1.2** | Short-prefix edge case: when hyphen-prefix is shorter than grantee length, absorb from suffix | `fcc_id_utils.py:39-43` | 8 | ✅ Done |
| **P1.3** | Add no-`1`/no-`0` validation on extracted grantee codes | `fcc_id_utils.py:split_fcc_id()` | 15 | ✅ Done |

### Phase 2a — Quick Wins (low risk, fcc_utils.py + views.py)

| # | Change | Pattern | Savings | Status |
|---|---|---|---|---|
| **P2a.1** | Cache `_ensure_grantee_brand_and_manufacturer` results per `(grantee_code, brand_name)` | Local dict before record loop | ~seconds | ✅ Done |
| **P2a.2** | Aggregate stale-skip logging: 1 line per grantee instead of 1 per radio | Counting query before `continue` | Log reduction | ✅ Done |
| **P2a.3** | Promote `metadata_cache` to module-level `_sync_metadata_cache`; add `reset_sync_metadata_cache()` call from views.py | Module-level dict | Minutes | ✅ Done |

### Phase 2b — High Impact (medium risk, fcc_utils.py + views.py)

| # | Change | Pattern | Estimated Time Saved | Status |
|---|---|---|---|---|
| **P2b.1** | **Playwright browser pool**: module-level `_playwright_instance`, reuse across all calls, cleanup in `finally` | Add `_get_playwright_instance()` / `_close_playwright_instance()`; modify `_submit_generic_search_form_via_playwright()` and `_fetch_oet_documents_via_playwright()` to use shared instance | **2-5 min per bulk sync** | ✅ Done |
| **P2b.2** | **OET doc de-dup per FCC ID**: track synced FCC IDs in module-level set; sync once, copy to sibling radios | New `_copy_oet_docs_between_radios()` helper | **1-3 min per bulk sync** | ✅ Done |

### Phase 2c — Optional (medium risk, views.py)

| # | Change | Pattern | Impact | Status |
|---|---|---|---|---|
| **P2c.1** | **Parallel grantee processing**: `ThreadPoolExecutor(max_workers=4)` with `close_old_connections()` per thread | Replace sequential `for` loop | **3-5x throughput** | ✅ Done |

---

### Data Flow After Changes

```
sync_all_grantees_view
  │
  ├── reset_sync_metadata_cache()
  ├── _close_playwright_instance()          [NEW — P2b.1]
  │
  ├── Phase 1: known grantees
  │   └── ThreadPoolExecutor (4 workers)    [NEW — P2c.1]
  │       └── fetch_and_sync_fcc_id(code)
  │           ├── Parser: split_fcc_id()    [HARDENED — P1.1-3]
  │           ├── _ensure_cache[(code,name)] [NEW — P2a.1]
  │           ├── metadata from _sync_metadata_cache  [NEW — P2a.3]
  │           ├── OET sync once per FCC ID  [NEW — P2b.2]
  │           ├── shared Playwright browser [NEW — P2b.1]
  │           └── aggregated stale logging  [NEW — P2a.2]
  │
  ├── Phase 2: discover unknown grantees    (existing)
  │
  └── finally:
      └── _close_playwright_instance()      [NEW — P2b.1]
```

### Files Modified Summary

| File | Changes |
|---|---|
| fcc_id_utils.py | P1.1, P1.2, P1.3 |
| fcc_utils.py | P1.1 (re-export), P2a.1, P2a.2, P2a.3, P2b.1, P2b.2 |
| views.py | P2a.3, P2b.1, P2c.1 |

### Recommended Implementation Order

1. **P1.1** + **P1.2** (5 min, trivial)
2. **P2a.1** + **P2a.2** + **P2a.3** (10 min, quick wins)
3. **P2b.1** + **P2b.2** (35 min, high impact)
4. **P1.3** (10 min, low-priority validation)
5. **P2c.1** (15 min, optional throughput boost)