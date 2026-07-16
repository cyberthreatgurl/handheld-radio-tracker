# Radio Tracker

Radio Tracker is a Django application and data-processing toolkit for building and maintaining a handheld and mobile radio database. It combines a web UI, Django admin, FCC ingestion workflows, OET exhibit/document capture, CSV/XML import tools, and a set of standalone helper scripts used during data cleanup and enrichment.

The project supports two main workflows:

- Operating the Django web application for browsing, editing, importing, and syncing radio records.
- Running one-off or batch utilities to scrape, normalize, merge, audit, or import source data.

## What the application does

The application stores and manages radio records with fields such as:

- brand and model
- FCC ID
- radio type
- manufacturer / white-label relationships
- frequency bands, power, GPS, APRS, DMR, air band, display, battery, pricing, notes
- linked FCC test reports, manuals, and OET exhibit documents

It can also enrich radios from FCC sources:

- query FCC grant metadata using the FCC API
- look up secondary FCC exhibit metadata from the FCC EAS/OET site
- download OET attachment files such as manuals, test reports, photos, and letters
- promote selected FCC documents into the document library
- backfill radio specs from downloaded PDF manuals and reports when possible

## Main features

- Django web UI for CRUD operations on radios and brands
- Django admin for bulk editing and review
- FCC sync for a single FCC ID, all known FCC IDs, or all known grantees
- Incremental grantee sync using the last successful sync timestamp
- OET document ingestion and local file download
- Manual/XML/CSV import and merge utilities
- Consistency checking and audit commands
- Tailwind-based frontend

## Repository layout

```text
radio-tracker/
├── manage.py
├── README.md
├── README_DJANGO.md
├── TROUBLESHOOTING.md
├── requirements.txt
├── requirements_django.txt
├── radio_database/                 # Django project settings
├── radios/                         # Main Django app
├── theme/                          # Tailwind app
├── data/                           # Source CSV/XML data files
├── artifacts/                      # Downloaded manuals, reports, firmware, etc.
├── backup/                         # SQL/database backups
├── logs/                           # Application logs
└── *.py                            # Standalone helper scripts
```

## Prerequisites

- Python 3.10 to 3.13
- PostgreSQL
- Node.js and npm for Tailwind
- Google Chrome installed locally for the most reliable FCC OET retrieval

The project includes Playwright in `requirements.txt`. FCC OET retrieval uses headless Chrome by default so browser windows never appear during syncs. Set `FCC_PLAYWRIGHT_HEADLESS=0` if you need a visible browser for debugging.

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd radio-tracker
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browser support

```bash
python -m playwright install chromium
```

Note:

- The FCC fallback prefers system Chrome when available.
- Chromium is still worth installing because Playwright may fall back to it.

### 5. Configure PostgreSQL

Create a database and make sure the Django settings can connect to it.

Example:

```bash
createdb radio_database
```

### 6. Configure environment variables

The Django settings read environment variables from `.env` via `python-dotenv`.

Common database settings:

```bash
DB_NAME=radio_database
DB_USER=your_postgres_user
DB_PASSWORD=
DB_HOST=localhost
DB_PORT=5432
```

Optional runtime settings:

```bash
LOGGING=true
LOGS_DIR=logs
MANUALS_DIR=artifacts/manuals
FCC_TEST_REPORTS_DIR=artifacts/test_reports
FCC_RADIO_ALLOWLIST_TERMS=TRANSCEIVER,TRANSMITTER,RECEIVER,MURS,ORIGINAL EQUIPMENT
FCC_PLAYWRIGHT_HEADLESS=0
```

Notes:

- `FCC_RADIO_ALLOWLIST_TERMS` controls which FCC records are treated as relevant radio equipment.
- `FCC_PLAYWRIGHT_HEADLESS` — the Playwright browser runs **headless by default** (no visible window). Set to `0` / `false` / `no` to show the browser during FCC OET fetches, which is useful for debugging timeout or form-submission failures.

### 7. Run Django migrations

```bash
python manage.py migrate
```

### 8. Install Tailwind dependencies

```bash
python manage.py tailwind install
```

### 9. Create a superuser

```bash
python manage.py createsuperuser
```

## Running the application

Start Tailwind in one terminal:

```bash
source venv/bin/activate
python manage.py tailwind start
```

Start Django in another:

```bash
source venv/bin/activate
python manage.py runserver
```

URLs:

- App: `http://127.0.0.1:8000/`
- Admin: `http://127.0.0.1:8000/admin/`

## How to use the web app

Key pages:

- `/` dashboard with stats and FCC sync actions
- `/radios/` list, search, filter, and browse radios
- `/radios/<id>/` radio detail page with FCC/OET/manual/test report sections
- `/radios/<id>/edit/` edit a radio
- `/brands/` browse and edit brands
- `/admin/` advanced administration

Important sync actions:

- Submit a single FCC ID or FCC grantee code from the dashboard/web form
- Use the "Update All Known Grantees" flow to run incremental grantee sync

## FCC and OET sync behavior

The FCC sync logic lives primarily in `radios/fcc_utils.py`.

What happens during sync:

1. Query FCC grant metadata via the FCC API.
2. Normalize FCC ID and grantee/product variants.
3. Update or create matching `Radio` records.
4. Attempt secondary FCC EAS/OET lookup for exhibits.
5. Download OET attachments into `artifacts/oet_documents/`.
6. Promote selected OET documents into the manual/test-report library.
7. Attempt spec extraction from downloaded PDFs.

Important operational note:

- The FCC OET site frequently returns `503` or blocks non-browser requests.
- This repository falls back to Playwright/Chrome for the exhibit flow, running **headless by default**.
- **Stale-skip guard:** when a radio was already processed within the current sync date window, it is skipped automatically — even if the FCC API returned 503 and could not provide a record last-modified date. This prevents redundant full re-processing on consecutive daily syncs.
- A Playwright circuit-breaker fires after 2 consecutive page-load timeouts per FCC ID, abandoning remaining direct-URL attempts and falling through to the grantee-search fallback to avoid burning ~30 s per URL when the FCC site is unreachable.

## Cron / scheduled usage

Two useful scheduled workflows are:

### Incremental grantee sync

```bash
source /path/to/radio-tracker/venv/bin/activate
cd /path/to/radio-tracker
python manage.py sync_fcc --all-grantees
```

This uses `FCCSyncState` to fetch only grants since the last successful grantee sync unless `--full-history` is supplied.

### Post-sync OET audit

```bash
source /path/to/radio-tracker/venv/bin/activate
cd /path/to/radio-tracker
python manage.py audit_oet_sync --missing-only --limit 100
```

This prints FCC IDs that still have zero OET documents after sync and is useful for reviewing cron runs.

## Core Django management commands

### FCC and OET commands

`python manage.py sync_fcc --fcc-id <FCCID>`

- Sync a single FCC ID or grantee code.

Examples:

```bash
python manage.py sync_fcc --fcc-id 2A4FBTDBL-1
python manage.py sync_fcc --fcc-id XH8
```

`python manage.py sync_fcc --all-existing`

- Re-sync all distinct FCC IDs already present in the database.

`python manage.py sync_fcc --all-grantees`

- Sync all known grantee codes from the `Brand` table using incremental date filtering.
- Pass `--ignore-grantees=ICOM,MOTOROLA,YAESU` to skip ad-hoc grantee codes on the command line.
- Grantee codes stored in the **Sync-Skipped Grantee IDs** admin page are also excluded automatically.

`python manage.py sync_fcc --all-grantees --full-history`

- Re-run all known grantees without date filtering.

`python manage.py audit_oet_sync`

- Audit OET coverage for FCC IDs or grantees.

Examples:

```bash
python manage.py audit_oet_sync --random-fcc-ids 20
python manage.py audit_oet_sync --random-fcc-ids 20 --sync-first
python manage.py audit_oet_sync --grantee XH8 --grantee POD
python manage.py audit_oet_sync --missing-only --limit 50
```

`python manage.py backfill_oet_documents --fcc-id <FCCID> --source-file <saved.html|saved.csv>`

- Import OET rows from saved FCC exhibit HTML or CSV when the live FCC site is unavailable.

### Import and cleanup commands

`python manage.py import_radios <csv_file> [--clear]`

- Bulk import radio records from CSV.

`python manage.py import_brands <csv_file> [--clear]`

- Bulk import brand/grantee data from CSV.

`python manage.py import_grantees`

- Parse grantee data into `Brand` records.

`python manage.py sync_radio_brands`

- Ensure every brand referenced by a radio exists in the `Brand` table.

`python manage.py deduplicate_radios`

- Merge duplicate radios by brand/model.

`python manage.py merge_brand <primary_name> <secondary_name>`

- Merge one brand into another.

`python manage.py merge_brand_radios <source_brand> <target_brand>`

- Move radios from one brand to another.

`python manage.py rename_brand_global <old_name> <new_name>`

- Rename a brand everywhere.

`python manage.py clean_grantee_prefix <brand> <grantee_code>`

- Remove grantee prefixes from model names for a brand.

`python manage.py rename_baofeng`

- Normalize and merge Baofeng-related naming patterns.

`python manage.py check_db_consistency`

- Run database audits across brands, radios, manufacturers, hierarchy, and FCC IDs.

`python manage.py cleanup_duplicate_brands --dry-run`

- Preview duplicate blank-code Brand rows that can be safely removed.

`python manage.py cleanup_duplicate_brands --force`

- Actually delete duplicate blank-code Brand rows after canonicalization.

### Sync-Skipped Grantee IDs

**Purpose:** Skip known-stable grantees during bulk FCC sync to reduce API calls and runtime.

**Mechanisms:**

1. **Django Admin** (`/admin/radios/syncskippedgrantee/`) — add grantee codes like `ICOM`, `MOTOROLA`, `YAESU` to skip them on every `--all-grantees` sync. Persisted in the database.
2. **CLI** (`--ignore-grantees=ICOM,MOTOROLA`) — ad-hoc skip for a single run, merged with any DB-stored codes.

Unlike `IgnoredGrantee` (which completely blocks import of those grantees), Sync-Skipped Grantees keep their existing radios in the database — they're just not queried during bulk sync.

## FCC grantee repair and maintenance

### Overview

During FCC sync operations, radios may end up with null `manufacturer_id` fields if the FCC grantee metadata doesn't exactly match existing Brand records. This section documents the repair strategies and maintenance commands to handle these cases.

The repair logic lives in `radios/fcc_utils.py` and handles three distinct scenarios:

1. **Bucket-1: Blank-code Brand backfill** - FCC grantee matches an existing Brand by name/alias, but that Brand has no `grantee_code` yet
2. **Bucket-2: Duplicate Brand absorption** - Both a coded Brand (with `grantee_code`) and a blank-code Brand (without `grantee_code`) exist for the same legal entity
3. **Reseller/OEM cases** - Radio's brand name differs from the FCC grantee's legal entity because of white-label/reseller relationships

### Bucket-1: Backfill blank-code Brands

**Problem:** A Brand row exists with the correct name/alias but has no `grantee_code` set. The FCC sync can't link it automatically.

**Solution:** The `_find_existing_grantee_brand()` helper uses normalized matching to handle:
- Punctuation variants (e.g., "Vertex Standard USA, Inc." vs "Vertex Standard USA Inc")
- Spacing differences
- Case variations

**How it works:**
1. First checks for exact `grantee_code` match
2. Falls back to case-insensitive name/alias/full_name matching
3. Falls back to normalized identity matching (strips all punctuation/spaces)

**Result:** The blank-code Brand gets its `grantee_code` populated, and radios get their `manufacturer_id` assigned.

### Bucket-2: Absorb duplicate Brand variants

**Problem:** Two Brand rows exist for the same legal entity:
- One with `grantee_code` set (canonical)
- One without `grantee_code` (duplicate variant)

**Example:** 
- Coded Brand: "Tidradio" (code=2AWL3)
- Blank Brand: "Quanzhou longtuo electronic technology co. ,Ltd" (no code)

**Solution:** The `_resolve_authoritative_radio_brand_name()` helper detects when a duplicate exists and returns the canonical coded Brand's name.

**How it works:**
1. `_find_matching_blank_code_brand()` searches for a blank-code Brand matching the FCC grantee name
2. If found, and its normalized identity matches the coded Brand's identity, it's a duplicate
3. `fetch_and_sync_fcc_id()` updates `Radio.brand` to the canonical coded Brand name
4. The blank-code Brand row becomes orphaned (no radios reference it)

**Result:** Radios point to the canonical coded Brand name consistently.

### Reseller/OEM case preservation

**Problem:** Some radios legitimately use a different brand name than the FCC grantee's legal entity.

**Example:**
- FCC grantee: VO6 "FUJIAN NEW CENTURY COMMUNICATIONS CO., LTD"
- Radio brand: "Kydera" (the reseller/marketing brand)
- Coded Brand for VO6 does not exist in the database

**Solution:** The `_resolve_authoritative_radio_brand_name()` helper **only** canonicalizes when a duplicate blank-code Brand variant exists. If no duplicate exists, it preserves the original radio brand name.

**How it detects reseller cases:**
1. Checks if the coded Brand's normalized name matches the grantee name → if not, different grantee
2. Checks if a blank-code Brand variant exists for the grantee name
3. If **no** blank-code variant exists → reseller case, preserve original brand name
4. If blank-code variant **does** exist → duplicate case, use canonical coded Brand name

**Result:** Reseller brands like Kydera/VO6 remain distinct from the FCC legal entity.

### Identifying cases that need repair

Check for radios with null manufacturers:

```python
from radios.models import Radio
from radios.fcc_id_utils import split_fcc_id

radios = Radio.objects.exclude(fcc_id='').filter(manufacturer__isnull=True)
for radio in radios:
    grantee_code, _ = split_fcc_id(radio.fcc_id)
    print(f"{grantee_code} - {radio.brand} - {radio.model}")
```

Group by grantee code to see patterns:

```bash
# Use a debug script or Django shell
python manage.py shell
```

### Running targeted repairs

For bucket-1 style repairs (exact-match cases):

```python
from radios.fcc_utils import _ensure_grantee_brand_and_manufacturer
from radios.models import Radio

grantee_code = 'AXI'
sample_brand = Radio.objects.filter(fcc_id__istartswith=grantee_code).first().brand
brand, manufacturer = _ensure_grantee_brand_and_manufacturer(grantee_code, sample_brand)

if manufacturer:
    Radio.objects.filter(
        fcc_id__istartswith=grantee_code,
        manufacturer__isnull=True
    ).update(manufacturer=manufacturer)
```

For bucket-2 style repairs (duplicate Brand canonicalization):

```python
from radios.fcc_utils import _resolve_authoritative_radio_brand_name
from radios.models import Brand, Radio

grantee_code = '2AWL3'
radios = Radio.objects.filter(fcc_id__istartswith=grantee_code)

for radio in radios:
    coded_brand = Brand.objects.filter(grantee_code__iexact=grantee_code).first()
    if coded_brand:
        canonical_name = _resolve_authoritative_radio_brand_name(
            coded_brand, grantee_code, radio.brand
        )
        if canonical_name != radio.brand:
            radio.brand = canonical_name
            radio.save()
```

### Cleaning up duplicate blank-code Brands

After bucket-2 repairs canonicalize radios to coded Brand names, the blank-code Brand variants become orphaned (no radios reference them).

**Safe cleanup command:**

Preview what would be deleted (recommended first):

```bash
python manage.py cleanup_duplicate_brands --dry-run
```

Actually delete duplicates:

```bash
python manage.py cleanup_duplicate_brands --force
```

**Safety guarantees:**
- Only deletes blank-code Brands (no `grantee_code` set)
- Only deletes if a matching coded Brand exists (by normalized name)
- Only deletes if **zero** radios reference the blank-code Brand's exact name
- Skips any Brand that still has radios pointing at it

**Common output:**
```
SKIP: Blank Brand "Baofeng" (id=106) has 1 radios pointing at it. 
      Coded counterpart: "PO FUNG ELECTRONIC..." (code=2AJGM)

DELETE: Brand "Quanzhou longtuo electronic technology co. ,Ltd" (id=142)
        REASON: Matches coded Brand "Tidradio" (code=2AWL3)
```

The "SKIP" entries indicate Brands that are still in use and protected. The "DELETE" entries are safe to remove because all radios now use the canonical coded Brand name.

### Key functions in fcc_utils.py

- `_normalize_brand_identity(value)` - Strips punctuation/spaces/case for fuzzy matching
- `_find_existing_grantee_brand(grantee_code, grantee_name)` - Finds Brand by code or name with fallback matching
- `_find_matching_blank_code_brand(grantee_name)` - Finds blank-code Brand variants for duplicate detection
- `_resolve_authoritative_radio_brand_name(brand, grantee_code, grantee_name)` - Returns canonical name for duplicates, preserves reseller cases
- `_ensure_grantee_brand_and_manufacturer(grantee_code, grantee_name)` - Main repair helper that handles both bucket-1 and bucket-2 logic

### Testing repairs

The test suite in `radios/tests.py` includes `FCCGranteeBrandManufacturerSyncTest` with coverage for:
- Bucket-1 exact-match and normalized matching
- Bucket-2 duplicate Brand absorption
- Reseller case preservation
- Brand name canonicalization logic

Run tests with:

```bash
python manage.py test radios.tests.FCCGranteeBrandManufacturerSyncTest
```

## Standalone utility scripts

These scripts are mostly one-off helpers for data preparation and legacy import workflows. Run them from the repository root with the virtual environment activated.

### Data gathering and FCC lookup helpers

`python get_fcc_records.py`

- Query the FCC API for hardcoded grantee codes and print/save results.

`python search_fcc.py`

- Perform a lightweight external FCC-related search.

`python find_grantee_codes.py`

- Try to discover grantee codes for brands that are still missing them.

`python add_fcc_ids.py`

- Apply known FCC/grantee mappings to source data.

`additional_grantee_codes.py`

- Reference mapping file used by other scripts.

### Data conversion and comparison

`python convert_md_to_csv.py`

- Convert `master.md` into CSV output.

`python compare_models.py`

- Compare model sets between CSV sources.

`python merge_masters.py`

- Merge and deduplicate master CSV data.

`python verify_conversion.py`

- Verify that markdown/CSV conversion completed correctly.

### HTML/product parsing utilities

`python scrape_product_names.py`

- Scrape product names from eHam-style review pages.

`python parse_html.py`

- Parse and normalize manufacturer/model text extracted from HTML.

### Django-aware ingestion / cleanup scripts

`python ingest_fcc_xml_radios.py`

- Parse FCC XML data and ingest it into the Django database.

`python merge_brands_script.py`

- Merge hardcoded duplicate brands in the database.

## Typical setup and data workflows

### Start a new local environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python manage.py migrate
python manage.py createsuperuser
python manage.py tailwind install
```

### Import seed data

```bash
python manage.py import_brands data/brands.csv
python manage.py import_radios data/merged_master_with_fcc.csv
```

Adjust paths to the files you actually want to load.

### Sync one FCC ID and inspect results

```bash
python manage.py sync_fcc --fcc-id 2A4FBTDBL-1
python manage.py audit_oet_sync --fcc-id 2A4FBTDBL-1
```

### Review missing OET coverage after batch sync

```bash
python manage.py audit_oet_sync --missing-only --limit 100
```

## Testing

Run the test suite:

```bash
python manage.py test
```

For local environments where PostgreSQL test database creation is restricted, use the SQLite test settings:

```bash
python manage.py test --settings=radio_database.settings_test
```

## Sync performance analysis

### analyze_fcc_log.py

A standalone script for diagnosing FCC sync behaviour from log files.

**Single-file report** — grantee query summary, OET fetch stats, 503/timeout counts, stale-skip effectiveness, and per-grantee duration breakdown:

```bash
python analyze_fcc_log.py logs/radio_tracker.log
```

**Comparison report** — compares two log files to find FCC IDs and grantee queries that were re-processed unnecessarily:

```bash
python analyze_fcc_log.py logs/radio_tracker.log logs/radio_tracker.log.2026-06-04
```

Output includes:

- Total grantees started vs completed (shows which are still in progress)
- Radios added / updated / OET docs synced per grantee
- Count of `skipped_stale_lookup=0` warnings that indicate the stale-skip guard is not firing
- Overlap section: how many FCC IDs were re-fetched from the baseline log vs genuinely new
- Diagnosis message explaining the likely root cause when re-processing is high

**Overlap-only output** (skip the per-file summaries):

```bash
python analyze_fcc_log.py logs/radio_tracker.log logs/radio_tracker.log.2026-06-04 --overlap-only
```

### Headless vs visible Playwright browser

Playwright runs **headless by default** during OET fetches. To watch the browser for debugging a specific sync:

```bash
FCC_PLAYWRIGHT_HEADLESS=0 python manage.py sync_fcc --fcc-id AZ489FT3716
```

### Reviewing stale-skip behaviour in logs

```bash
# See which grantees had stale skips and how many
grep "skipped_stale_lookup" logs/radio_tracker.log | grep -v "skipped_stale_lookup=0"

# See individual stale-skip records
grep "FCC ingest skipped stale lookup" logs/radio_tracker.log | head -20

# Count 503 errors per run
grep -c "status=503" logs/radio_tracker.log

# Count Playwright timeout errors
grep -c "FCC browser OET page load failed" logs/radio_tracker.log

# Check if circuit-breaker fired
grep "circuit breaker triggered" logs/radio_tracker.log
```

## Logs and generated files

- App logs: `logs/radio_tracker.log`
- Downloaded OET files: `artifacts/oet_documents/`
- Promoted manuals: `artifacts/manuals/`
- Test reports: `artifacts/test_reports/`
- Firmware files: `artifacts/firmware/`

## Troubleshooting

### FCC sync returns `503` repeatedly

- This is common with the FCC EAS site.
- The code will retry with `curl_cffi` and may fall back to headless Playwright/Chrome.
- After 2 consecutive page-load timeouts for the same FCC ID, a circuit-breaker skips remaining direct-URL candidates and moves to the grantee-search fallback.
- Radios already synced within the current date window are skipped automatically even if 503s prevent obtaining a fresh FCC record last-modified date.
- To watch the browser in real time: `FCC_PLAYWRIGHT_HEADLESS=0 python manage.py sync_fcc --fcc-id <FCCID>`

### OET documents are not downloaded

- Verify the sync is reaching the FCC exhibit detail page.
- Run:

```bash
python manage.py audit_oet_sync --fcc-id <FCCID>
```

- If the live FCC site is failing, use `backfill_oet_documents` with saved HTML or CSV.

### Tailwind issues

```bash
python manage.py tailwind install
python manage.py tailwind start
```

### Database issues

- Confirm PostgreSQL is running.
- Confirm `.env` values match your local database.
- Re-run migrations if models changed.

## Related documentation

- `README_DJANGO.md` contains older Django-specific setup notes.
- `TROUBLESHOOTING.md` contains additional troubleshooting details.

This README is intended to be the primary entry point for understanding and operating the repository.
