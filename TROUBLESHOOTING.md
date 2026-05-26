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
