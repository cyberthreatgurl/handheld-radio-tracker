Here's the consolidated wireframe. I've organized it into three phases by risk and impact:

---

## Wireframe: FCC Sync & Parser Improvements

### Phase 1 — Parser Hardening (low risk, fcc_id_utils.py)

| # | Change | Location | Lines |
|---|---|---|---|
| **P1.1** | Unify char validation: `re.match(r'^[A-Z]$')` → `'A' <= c <= 'Z'` in both `split_fcc_id()` and `_exact_grantee_query()` | `fcc_id_utils.py:13-14`, `fcc_utils.py:2457-2459` | 4 |
| **P1.2** | Short-prefix edge case: when hyphen-prefix is shorter than grantee length, absorb from suffix | `fcc_id_utils.py:39-43` | 8 |
| **P1.3** | Add no-`1`/no-`0` validation on extracted grantee codes | `fcc_id_utils.py:split_fcc_id()` | 15 |

### Phase 2a — Quick Wins (low risk, fcc_utils.py + views.py)

| # | Change | Pattern | Savings |
|---|---|---|---|
| **P2a.1** | Cache `_ensure_grantee_brand_and_manufacturer` results per `(grantee_code, brand_name)` | Local dict before record loop | ~seconds |
| **P2a.2** | Aggregate stale-skip logging: 1 line per grantee instead of 1 per radio | Counting query before `continue` | Log reduction |
| **P2a.3** | Promote `metadata_cache` to module-level `_sync_metadata_cache`; add `reset_sync_metadata_cache()` call from views.py | Module-level dict | Minutes |

### Phase 2b — High Impact (medium risk, fcc_utils.py + views.py)

| # | Change | Pattern | Estimated Time Saved |
|---|---|---|---|
| **P2b.1** | **Playwright browser pool**: module-level `_playwright_instance`, reuse across all calls, cleanup in `finally` | Add `_get_playwright_instance()` / `_close_playwright_instance()`; modify `_submit_generic_search_form_via_playwright()` and `_fetch_oet_documents_via_playwright()` to use shared instance | **2-5 min per bulk sync** |
| **P2b.2** | **OET doc de-dup per FCC ID**: track synced FCC IDs in module-level set; sync once, copy to sibling radios | New `_copy_oet_docs_between_radios()` helper | **1-3 min per bulk sync** |

### Phase 2c — Optional (medium risk, views.py)

| # | Change | Pattern | Impact |
|---|---|---|---|
| **P2c.1** | **Parallel grantee processing**: `ThreadPoolExecutor(max_workers=4)` with `close_old_connections()` per thread | Replace sequential `for` loop | **3-5x throughput** |

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