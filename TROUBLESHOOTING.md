# Troubleshooting

## PostgreSQL Permission Denied for Django Migrations

If you encounter the error `psycopg2.errors.InsufficientPrivilege: permission denied for table django_migrations` when running `python manage.py makemigrations` or `python manage.py migrate`, it means the application database user does not have the correct permissions in PostgreSQL.

To fix this, log into your PostgreSQL database using an admin account (usually `postgres`) and grant the necessary privileges:

1. **Connect to your PostgreSQL database context:**
   ```bash
   psql -U postgres -h docker-server
   ```

2. **Connect to the specific database** (e.g., `radios`):
   ```sql
   \c radios
   ```

3. **Grant the required permissions to your database user** (e.g., `radiogod`):
   ```sql
   -- Grant usage on the public schema
   GRANT ALL ON SCHEMA public TO radiogod;

   -- Grant permissions on all existing tables
   GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO radiogod;

   -- Grant permissions on all existing sequences (required for Django AutoFields/Primary Keys)
   GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO radiogod;
   ```

4. **Set default privileges for future objects** (so new migrations don't break):
   ```sql
   -- Tell Postgres to automatically grant these permissions to radiogod for future tables
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO radiogod;

   -- Tell Postgres to automatically grant these permissions to radiogod for future sequences
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO radiogod;
   ```

## Resolving Brand Duplicates and Aliases

Occasionally, especially when importing from external sources (like FCC XML data), shell companies or differently formatted names (e.g., "PO FUNG ELECTRONIC..." vs "Baofeng") may be created as distinct records.

To safely merge a duplicate brand into an existing primary brand without violating database constraints (and while transferring all associated radios over), use the custom `merge_brand` management command:

```bash
# General syntax
python manage.py merge_brand "Canonical Brand Name" "Brand to Delete & Merge"

# Example
python manage.py merge_brand "Baofeng" "Pofung"
```

This command will:
1. Re-assign all exact radios from the secondary brand over to the primary brand.
2. Copy over the `grantee_code` to the primary brand if it is currently empty.
3. Store the secondary name as the primary brand's `alias` (if the primary brand doesn't already have one).
4. Safely delete the secondary duplicate brand.

## FCC Link Errors / FCC Website Error

If a radio FCC link opens an "FCC Website Error" page, this is often an upstream outage on the legacy FCC EAS reports endpoint (`apps.fcc.gov`) rather than a malformed FCC ID in this project.

Current app behavior:

1. FCC links use the official FCC domain endpoint:
   - `https://www.fcc.gov/oet/ea/fccid?id=<FCC_ID>`
2. FCC ID splitting/normalization is centralized in `radios/fcc_id_utils.py`.
3. Parsing follows FCC rules:
   - Leading `A-Z` => 3-character grantee code.
   - Leading `2-9` => 5-character grantee code.
   - Product code is the remaining characters and may contain dashes.

If search results still fail from `fcc.gov`, verify the FCC ID value itself and retry later in case of FCC service degradation.

## OET Documents Attached to the Wrong Radio / FCC ID

If a radio shows OET documents that clearly belong to a different FCC ID
(e.g. `2AZVI-T67` showing documents from `2AZVIJC-8629`), this is OET
cross-contamination. The FCC "Exhibits" page for a re-label / Change-in-ID
filing can also list the original equipment's attachments (photos, test
report, user manual, etc.), and the ingestion pipeline may store those under
the wrong FCC ID.

Fix it with the `purge_cross_fcc_oet_docs` management command:

```bash
# Preview first:
python manage.py purge_cross_fcc_oet_docs \
  --fcc-id 2AZVI-T67 \
  --source-fcc-id 2AZVIJC-8629 \
  --dry-run

# Then apply:
python manage.py purge_cross_fcc_oet_docs \
  --fcc-id 2AZVI-T67 \
  --source-fcc-id 2AZVIJC-8629
```

This removes the mis-attributed OET documents and the manual-library records
derived from them, leaving only the radio's own attachments. To find other
affected FCC IDs, look for OET document URLs that are stored under more than
one FCC ID — a reliable cross-contamination signal, since FCC attachment URLs
(`GetApplicationAttachment.html?id=...`) are unique per application.

## SynchronousOnlyOperation in Background FCC Sync Threads

If you see this error during an FCC sync:

```
django.core.exceptions.SynchronousOnlyOperation: You cannot call this
from an async context - use a thread or sync_to_async.
```

This happens when Django's dev server runs under ASGI and a background
`threading.Thread` inherits the async context from the parent request
handler.  The fix is built into the application — all background sync
threads (`_run_sync_fcc`, `_run_sync_all_grantees`, `_sync_single_grantee`)
set `DJANGO_ALLOW_ASYNC_UNSAFE=true` at thread entry, which is Django's
documented escape hatch for background tasks that use their own database
connection.

This error should no longer occur after the improvements branch (2026-07).

## Malformed FCC XML Parse Errors

If you see this in the logs:

```
xml.parsers.expat.ExpatError: not well-formed (invalid token)
ERROR [radios.fcc_utils] FCC metadata parse failed fcc_id=...
```

The FCC API sometimes returns XML responses with unescaped `&` characters
in company names (e.g. "Test & Measurement") and invalid XML 1.0 control
characters.  The `_sanitize_fcc_xml()` function in `radios/fcc_utils.py`
now pre-processes XML before parsing, fixing both issues and allowing the
parse to succeed.  If you still see this error after the improvements
branch, the FCC response contains corruption beyond what the sanitizer
handles — the sync will gracefully fall back to HTML-based parsing.

## Service Types Not Showing After FCC Sync

If a radio's service types are empty after running an FCC sync, check:

1. **FCC API availability** — The FCC Generic Search and TCB Report endpoints
   occasionally return 503.  Retry the sync later.

2. **CID (Change-in-Identification) filings** — If the radio is a re-brand
   (e.g. Retevis RB48P filed under `2ASNSRB48P`), the actual rule parts
   are stored under the **original FCC ID** (e.g. `2A3OORB48P`).  Run:
   ```bash
   python manage.py backfill_cid_radios --apply --sync-fcc --fcc-id <FCCID>
   ```

3. **Part 15B/15C only** — Radios certified only under Part 15B or 15C
   (no transmitter certification) are often amateur radios.  The system
   will detect them and scrape the manufacturer's website for specs.
   Ensure the radio has a `website` URL set and click "Scrape Website"
   on the edit page.

4. **Manual assignment** — Edit the radio and check the Service Types
   checkboxes manually if automated assignment doesn't find a match.

## Change-in-Identification (CID) Radios Missing Specs

CID radios are re-labeled versions of existing certified devices.  The
FCC database stores technical data under the original FCC ID, not the
new re-label ID.  Symptoms:

- Service types are empty or show only Part 15B/C
- No power output or emission designator data
- `is_a_whitelabel` is False but should be True
- Notes mention "Change in Identification"

To fix CID radios across the database:
```bash
# Preview what would change
python manage.py backfill_cid_radios

# Apply all CID backfills
python manage.py backfill_cid_radios --apply

# Re-sync a specific radio and backfill
python manage.py backfill_cid_radios --apply --sync-fcc --fcc-id 2ASNSRB48P
```
