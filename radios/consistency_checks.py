"""
Database consistency checks for the radio-tracker application.

Each public function returns a list of issue dicts with at minimum:
    {
        "level":   "ERROR" | "WARNING" | "INFO",
        "check":   <check-name string>,
        "message": <human-readable description>,
    }

Functions are intentionally side-effect-free (read-only).
"""

import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Corporate designator patterns
# These are stripped when comparing "significant" name words.
# ---------------------------------------------------------------------------
CORPORATE_DESIGNATORS = re.compile(
    r'\b('
    r'ltd|limited|corp|corporation|inc|incorporated|llc|llp|lp|plc'
    r'|co|company|companies|group|holdings|holding|international|internatonal'
    r'|enterprises|enterprise|technologies|technology|tech|electronics'
    r'|electronic|industries|industry|manufacturing|manufact'
    r'|gmbh|ag|bv|srl|sarl|oy|ab|as|nv|pvt|pte'
    r'|hk|usa|us|china|shenzhen|guangzhou|quanzhou|fujian|beijing'
    r')\b',
    flags=re.IGNORECASE,
)

# FCC grantee-code validation pattern (matches _is_valid_grantee_code logic)
_VALID_3_CHAR = re.compile(r'^[A-Z][A-Z0-9]{2}$')
_VALID_5_CHAR = re.compile(r'^[2-9][A-Z0-9]{4}$')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_text(value: str) -> str:
    """Lowercase, strip punctuation, return cleaned string."""
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _significant_words(value: str) -> List[str]:
    """Split a name into meaningful words after removing corporate designators."""
    cleaned = CORPORATE_DESIGNATORS.sub(' ', value or '')
    cleaned = re.sub(r'[^a-z0-9 ]+', ' ', cleaned.lower())
    return [w for w in cleaned.split() if len(w) > 1]


def _is_valid_grantee_code(code: str) -> bool:
    c = (code or '').strip().upper()
    if not c:
        return False
    if _VALID_3_CHAR.match(c):
        return True
    if _VALID_5_CHAR.match(c):
        return True
    return False


def _names_match(db_name: str, fcc_name: str) -> bool:
    """
    Return True when the significant words of db_name and fcc_name overlap
    meaningfully.  A single shared word is enough to consider them the same
    entity (handles abbreviations, reorderings, etc.).
    """
    db_words = set(_significant_words(db_name))
    fcc_words = set(_significant_words(fcc_name))
    if not db_words or not fcc_words:
        # If one side is empty after stripping, fall back to full-normalized compare
        return _normalize_text(db_name) == _normalize_text(fcc_name)
    return bool(db_words & fcc_words)


def _issue(level: str, check: str, message: str, **extra) -> dict:
    entry = {"level": level, "check": check, "message": message}
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Phase 0 — Build a grantee→FCC-name cache from local XML files
# ---------------------------------------------------------------------------

def build_grantee_name_map(xml_dir: str) -> Dict[str, str]:
    """
    Walk *xml_dir* and build {grantee_code_upper: applicant_name} from all
    FCC XML files found there.

    Two XML schemas are supported:
      1. results.xml / *results*.xml  — <grantee_code> + <grantee_name>
      2. *authorization_search_results.xml — <fcc_id> + <applicant_name>

    When both sources cover the same grantee the results.xml entry wins
    (it comes directly from the FCC grantee registry).
    """
    from radios.fcc_id_utils import split_fcc_id

    grantee_map: Dict[str, str] = {}     # from authorization XMLs (lower priority)
    registry_map: Dict[str, str] = {}    # from results.xml files (higher priority)

    if not os.path.isdir(xml_dir):
        logger.warning("consistency_checks: xml_dir not found: %s", xml_dir)
        return grantee_map

    xml_files = []
    for root_dir, _, files in os.walk(xml_dir):
        for fname in files:
            if fname.lower().endswith('.xml'):
                xml_files.append(os.path.join(root_dir, fname))

    for path in xml_files:
        try:
            # FCC XML files sometimes use ISO-8859-1 encoding
            with open(path, 'rb') as fh:
                raw = fh.read()
            # Replace bare & that would break the parser
            raw = re.sub(rb'&(?!amp;|lt;|gt;|quot;|apos;|#)', b'&amp;', raw)
            root = ET.fromstring(raw)
        except Exception as exc:
            logger.debug("consistency_checks: skipping unparseable XML %s: %s", path, exc)
            continue

        for row in root.findall('.//Row'):
            grantee_code = (row.findtext('grantee_code') or '').strip().upper()
            grantee_name = (row.findtext('grantee_name') or '').strip()
            if grantee_code and grantee_name:
                registry_map[grantee_code] = grantee_name
                continue

            fcc_id = (row.findtext('fcc_id') or '').strip()
            applicant_name = (row.findtext('applicant_name') or '').strip()
            if fcc_id and applicant_name:
                grantee_code_from_fcc, _ = split_fcc_id(fcc_id)
                if grantee_code_from_fcc:
                    key = grantee_code_from_fcc.upper()
                    # Only store the first occurrence (most records share the same applicant)
                    if key not in grantee_map:
                        grantee_map[key] = applicant_name

    # Merge: registry wins
    grantee_map.update(registry_map)
    return grantee_map


def fetch_live_grantee_name(grantee_code: str) -> Optional[str]:
    """
    Query the FCC OETLabServices API for a single grantee code and return
    the first applicant_name found, or None if unavailable.
    Only called when --fetch-live is passed.
    """
    try:
        import xmltodict
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("consistency_checks: curl_cffi or xmltodict not installed; skipping live fetch")
        return None

    url = f"https://apps.fcc.gov/OETLabServices/getFCCIDList?fccId={grantee_code}"
    try:
        response = cffi_requests.get(url, impersonate="chrome124", timeout=15)
        if response.status_code != 200:
            return None
        data = xmltodict.parse(response.text)
        wrapper = data.get("fCCIDInfoes", {})
        result = wrapper.get("fccidInfo", [])
        records = [result] if isinstance(result, dict) else (result if result else [])
        for rec in records:
            name = (rec.get("grantee") or '').strip()
            if name:
                return name
    except Exception as exc:
        logger.debug("consistency_checks: live fetch failed for %s: %s", grantee_code, exc)
    return None


# ---------------------------------------------------------------------------
# Phase 1 — Brand Grantee Name Verification
# ---------------------------------------------------------------------------

def check_brand_grantee_names(
    grantee_name_map: Dict[str, str],
    fetch_live: bool = False,
    verbose: bool = False,
) -> List[dict]:
    """
    For every Brand that has a grantee_code, verify the DB name (or full_name)
    shares significant words with the FCC registry name.

    Issues emitted:
      ERROR   — no word overlap at all between DB and FCC names
      WARNING — grantee_code is present but not found in any XML or live source
      INFO    — match is OK (only when verbose=True)
    """
    from radios.models import Brand

    issues = []
    live_cache: Dict[str, Optional[str]] = {}

    brands_with_grantee = Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code__exact='')

    for brand in brands_with_grantee.iterator():
        code = brand.grantee_code.strip().upper()

        if not _is_valid_grantee_code(code):
            issues.append(_issue(
                "ERROR", "brand_grantee_names",
                f"Brand pk={brand.pk} name=\"{brand.name}\" has invalid grantee_code format: \"{code}\"",
                pk=brand.pk, grantee_code=code,
            ))
            continue

        fcc_name = grantee_name_map.get(code)

        if fcc_name is None and fetch_live:
            if code not in live_cache:
                live_cache[code] = fetch_live_grantee_name(code)
            fcc_name = live_cache[code]

        if fcc_name is None:
            issues.append(_issue(
                "WARNING", "brand_grantee_names",
                f"[BRAND UNKNOWN] Grantee {code}: DB=\"{brand.name}\" — not found in XML cache or FCC registry",
                pk=brand.pk, grantee_code=code, db_name=brand.name,
            ))
            continue

        # Check against name, alias, and full_name
        db_candidates = [v for v in (brand.name, brand.alias, brand.full_name) if v]
        matched = any(_names_match(c, fcc_name) for c in db_candidates)

        if matched:
            if verbose:
                issues.append(_issue(
                    "INFO", "brand_grantee_names",
                    f"[BRAND OK] Grantee {code}: DB=\"{brand.name}\" | FCC=\"{fcc_name}\"",
                    pk=brand.pk, grantee_code=code, db_name=brand.name, fcc_name=fcc_name,
                ))
        else:
            full_note = f" / full=\"{brand.full_name}\"" if brand.full_name else ""
            issues.append(_issue(
                "ERROR", "brand_grantee_names",
                f"[BRAND MISMATCH] Grantee {code}: DB=\"{brand.name}\"{full_note} | FCC=\"{fcc_name}\"",
                pk=brand.pk, grantee_code=code, db_name=brand.name,
                db_full_name=brand.full_name, fcc_name=fcc_name,
            ))

    return issues


# ---------------------------------------------------------------------------
# Phase 2 — Radio FCC ID → Brand Consistency
# ---------------------------------------------------------------------------

def check_radio_fcc_brand_consistency(verbose: bool = False) -> List[dict]:
    """
    For each Radio with an fcc_id, parse the grantee code and verify it
    matches the radio's brand (or the radio is explicitly flagged as a white label).

    Issues emitted:
      ERROR   — brand doesn't match grantee brand and is_a_whitelabel=False
      WARNING — grantee is valid but not found in the Brand table
      INFO    — match is OK (only when verbose=True) or white-label is expected
    """
    from radios.models import Brand, Radio
    from radios.fcc_id_utils import split_fcc_id

    issues = []

    radios_with_fcc = Radio.objects.exclude(fcc_id__isnull=True).exclude(fcc_id__exact='')

    # Pre-build grantee → Brand lookup
    grantee_brand_map: Dict[str, object] = {
        b.grantee_code.strip().upper(): b
        for b in Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code__exact='')
    }

    for radio in radios_with_fcc.iterator():
        grantee_code, product_code = split_fcc_id(radio.fcc_id)
        grantee_upper = grantee_code.upper() if grantee_code else ''

        if not _is_valid_grantee_code(grantee_upper):
            # Reported separately in Phase 5 — skip here to avoid double-reporting
            continue

        grantee_brand = grantee_brand_map.get(grantee_upper)

        if grantee_brand is None:
            issues.append(_issue(
                "WARNING", "radio_fcc_brand",
                f"[RADIO UNKNOWN GRANTEE] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                f"fcc_id=\"{radio.fcc_id}\" → grantee={grantee_upper} not in Brand table",
                pk=radio.pk, brand=radio.brand, fcc_id=radio.fcc_id, grantee_code=grantee_upper,
            ))
            continue

        # Check if radio.brand resolves to the grantee brand (name or alias)
        db_candidates = [v for v in (grantee_brand.name, grantee_brand.alias, grantee_brand.full_name) if v]
        brand_matches = any(_names_match(radio.brand, c) for c in db_candidates)

        if brand_matches:
            if verbose:
                issues.append(_issue(
                    "INFO", "radio_fcc_brand",
                    f"[RADIO OK] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                    f"fcc_id=\"{radio.fcc_id}\" → grantee={grantee_upper} (\"{grantee_brand.name}\")",
                    pk=radio.pk, brand=radio.brand, fcc_id=radio.fcc_id, grantee_code=grantee_upper,
                ))
            continue

        if radio.is_a_whitelabel:
            if verbose:
                issues.append(_issue(
                    "INFO", "radio_fcc_brand",
                    f"[RADIO WHITE-LABEL] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                    f"fcc_id=\"{radio.fcc_id}\" → grantee={grantee_upper} (\"{grantee_brand.name}\") — white-label, expected",
                    pk=radio.pk, brand=radio.brand, fcc_id=radio.fcc_id, grantee_code=grantee_upper,
                ))
            continue

        # Mismatch and not flagged as white-label
        issues.append(_issue(
            "ERROR", "radio_fcc_brand",
            f"[RADIO MISMATCH] Radio pk={radio.pk} brand=\"{radio.brand}\" "
            f"fcc_id=\"{radio.fcc_id}\" → grantee={grantee_upper} maps to \"{grantee_brand.name}\" "
            f"(is_a_whitelabel=False)",
            pk=radio.pk, brand=radio.brand, fcc_id=radio.fcc_id,
            grantee_code=grantee_upper, grantee_brand=grantee_brand.name,
        ))

    return issues


# ---------------------------------------------------------------------------
# Phase 3 — Manufacturer Name Format
# ---------------------------------------------------------------------------

def check_manufacturer_names(verbose: bool = False) -> List[dict]:
    """
    Verify that each Manufacturer.full_name:
      - Has at least two words
      - Contains at least one corporate designator (Ltd., Corp., LLC, etc.)

    Issues emitted:
      WARNING — full_name is present but looks like a single-word brand name
      ERROR   — full_name is blank
      INFO    — OK (only when verbose=True)
    """
    from radios.models import Manufacturer

    # Designators we expect to appear in a legal entity name
    LEGAL_DESIGNATOR_RE = re.compile(
        r'\b(ltd|limited|corp|corporation|inc|incorporated|llc|llp|lp|plc|co\.|company|companies|group|holdings|holding|gmbh|ag|bv|srl|sarl|pvt|pte)\b',
        flags=re.IGNORECASE,
    )

    issues = []

    for mfr in Manufacturer.objects.all().iterator():
        if not mfr.full_name or not mfr.full_name.strip():
            issues.append(_issue(
                "ERROR", "manufacturer_names",
                f"[MFR BLANK] Manufacturer pk={mfr.pk} alias=\"{mfr.alias}\" — full_name is blank",
                pk=mfr.pk, alias=mfr.alias,
            ))
            continue

        words = mfr.full_name.split()
        has_designator = bool(LEGAL_DESIGNATOR_RE.search(mfr.full_name))

        if len(words) < 2:
            issues.append(_issue(
                "WARNING", "manufacturer_names",
                f"[MFR FORMAT] Manufacturer pk={mfr.pk} full_name=\"{mfr.full_name}\" — single word, likely a brand not an OEM",
                pk=mfr.pk, full_name=mfr.full_name,
            ))
        elif not has_designator:
            issues.append(_issue(
                "WARNING", "manufacturer_names",
                f"[MFR FORMAT] Manufacturer pk={mfr.pk} full_name=\"{mfr.full_name}\" — no corporate designator (Ltd., Corp., LLC, etc.)",
                pk=mfr.pk, full_name=mfr.full_name,
            ))
        else:
            if verbose:
                issues.append(_issue(
                    "INFO", "manufacturer_names",
                    f"[MFR OK] Manufacturer pk={mfr.pk} full_name=\"{mfr.full_name}\"",
                    pk=mfr.pk, full_name=mfr.full_name,
                ))

    return issues


# ---------------------------------------------------------------------------
# Phase 4 — OEM / Brand Hierarchy Integrity
# ---------------------------------------------------------------------------

def check_hierarchy_integrity(verbose: bool = False) -> List[dict]:
    """
    Check for structural problems in the Brand/Manufacturer/Radio hierarchy:

      - Circular parent_brand chains
      - Brand.parent_brand pointing to itself
      - Radio with is_a_whitelabel=True but manufacturer=None
      - Manufacturer with zero brands linked
      - Multiple Brands sharing the same grantee_code (duplicates)

    Issues emitted: ERROR / WARNING / INFO (verbose)
    """
    from radios.models import Brand, Manufacturer, Radio

    issues = []

    # --- Circular / self-referential parent_brand chains ---
    all_brands = {b.pk: b for b in Brand.objects.all()}

    for brand in all_brands.values():
        if brand.parent_brand_id is None:
            continue

        if brand.parent_brand_id == brand.pk:
            issues.append(_issue(
                "ERROR", "hierarchy",
                f"[HIERARCHY SELF-REF] Brand pk={brand.pk} name=\"{brand.name}\" — parent_brand points to itself",
                pk=brand.pk, brand=brand.name,
            ))
            continue

        # Walk the chain; detect cycle
        visited = {brand.pk}
        cursor_id = brand.parent_brand_id
        cycle_detected = False
        while cursor_id is not None:
            if cursor_id in visited:
                cycle_detected = True
                break
            visited.add(cursor_id)
            parent_obj = all_brands.get(cursor_id)
            if parent_obj is None:
                break
            cursor_id = parent_obj.parent_brand_id

        if cycle_detected:
            issues.append(_issue(
                "ERROR", "hierarchy",
                f"[HIERARCHY CYCLE] Brand pk={brand.pk} name=\"{brand.name}\" — circular parent_brand chain detected",
                pk=brand.pk, brand=brand.name,
            ))
        elif verbose:
            parent = all_brands.get(brand.parent_brand_id)
            parent_name = parent.name if parent else f"pk={brand.parent_brand_id}"
            issues.append(_issue(
                "INFO", "hierarchy",
                f"[HIERARCHY OK] Brand pk={brand.pk} name=\"{brand.name}\" → parent=\"{parent_name}\"",
                pk=brand.pk, brand=brand.name,
            ))

    # --- White-label radios with no manufacturer ---
    wl_no_mfr = Radio.objects.filter(is_a_whitelabel=True, manufacturer__isnull=True)
    for radio in wl_no_mfr.iterator():
        issues.append(_issue(
            "WARNING", "hierarchy",
            f"[HIERARCHY WHITE-LABEL NO MFR] Radio pk={radio.pk} brand=\"{radio.brand}\" "
            f"fcc_id=\"{radio.fcc_id}\" — is_a_whitelabel=True but manufacturer is null",
            pk=radio.pk, brand=radio.brand, fcc_id=radio.fcc_id,
        ))

    # --- Manufacturers with zero brands ---
    for mfr in Manufacturer.objects.all().prefetch_related('brands').iterator(chunk_size=500):
        if mfr.brands.count() == 0:
            issues.append(_issue(
                "WARNING", "hierarchy",
                f"[HIERARCHY MFR NO BRANDS] Manufacturer pk={mfr.pk} full_name=\"{mfr.full_name}\" — no brands linked",
                pk=mfr.pk, full_name=mfr.full_name,
            ))

    # --- Duplicate grantee codes across Brand records ---
    from django.db.models import Count
    dupes = (
        Brand.objects
        .exclude(grantee_code__isnull=True)
        .exclude(grantee_code__exact='')
        .values('grantee_code')
        .annotate(cnt=Count('pk'))
        .filter(cnt__gt=1)
    )
    for row in dupes:
        code = row['grantee_code']
        pks = list(Brand.objects.filter(grantee_code__iexact=code).values_list('pk', 'name'))
        detail = ', '.join(f"pk={pk} \"{name}\"" for pk, name in pks)
        issues.append(_issue(
            "ERROR", "hierarchy",
            f"[HIERARCHY DUPE GRANTEE] Grantee code \"{code}\" shared by {row['cnt']} brands: {detail}",
            grantee_code=code, brands=pks,
        ))

    return issues


# ---------------------------------------------------------------------------
# Phase 5 — FCC ID Parse Validity
# ---------------------------------------------------------------------------

def check_fcc_id_validity(verbose: bool = False) -> List[dict]:
    """
    For every Radio.fcc_id, verify:
      - The inferred grantee code passes the FCC length rule
        (digit-start → 5 chars; letter-start → 3 chars)
      - The product code portion is non-empty (grantee-only IDs are incomplete)

    Issues emitted:
      ERROR   — invalid grantee length or empty product code
      INFO    — OK (only when verbose=True)
    """
    from radios.models import Radio
    from radios.fcc_id_utils import split_fcc_id

    issues = []

    radios_with_fcc = Radio.objects.exclude(fcc_id__isnull=True).exclude(fcc_id__exact='')

    for radio in radios_with_fcc.iterator():
        raw = (radio.fcc_id or '').strip()
        grantee_code, product_code = split_fcc_id(raw)
        grantee_upper = grantee_code.upper() if grantee_code else ''

        valid_grantee = _is_valid_grantee_code(grantee_upper)
        has_product = bool(product_code and product_code.strip())

        if not valid_grantee:
            issues.append(_issue(
                "ERROR", "fcc_id_validity",
                f"[FCC ID INVALID GRANTEE] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                f"fcc_id=\"{raw}\" → grantee=\"{grantee_upper}\" fails FCC length rule "
                f"(digit-start=5 chars, letter-start=3 chars)",
                pk=radio.pk, brand=radio.brand, fcc_id=raw, grantee_code=grantee_upper,
            ))
        elif not has_product:
            issues.append(_issue(
                "ERROR", "fcc_id_validity",
                f"[FCC ID NO PRODUCT CODE] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                f"fcc_id=\"{raw}\" → grantee=\"{grantee_upper}\" but product code is empty",
                pk=radio.pk, brand=radio.brand, fcc_id=raw, grantee_code=grantee_upper,
            ))
        else:
            if verbose:
                issues.append(_issue(
                    "INFO", "fcc_id_validity",
                    f"[FCC ID OK] Radio pk={radio.pk} brand=\"{radio.brand}\" "
                    f"fcc_id=\"{raw}\" → grantee={grantee_upper} product={product_code}",
                    pk=radio.pk, brand=radio.brand, fcc_id=raw,
                    grantee_code=grantee_upper, product_code=product_code,
                ))

    return issues


# ---------------------------------------------------------------------------
# Aggregated runner (used by the management command)
# ---------------------------------------------------------------------------

ALL_CHECKS = ("brands", "radios", "manufacturers", "hierarchy", "fcc-ids")


def run_all_checks(
    checks=ALL_CHECKS,
    xml_dir: str = "data",
    fetch_live: bool = False,
    verbose: bool = False,
) -> List[dict]:
    """
    Run the requested subset of checks and return a flat list of issue dicts.
    *checks* is an iterable of check names from ALL_CHECKS.
    """
    checks_set = set(checks)
    issues: List[dict] = []

    grantee_name_map: Dict[str, str] = {}
    if "brands" in checks_set:
        grantee_name_map = build_grantee_name_map(xml_dir)
        issues += check_brand_grantee_names(grantee_name_map, fetch_live=fetch_live, verbose=verbose)

    if "radios" in checks_set:
        issues += check_radio_fcc_brand_consistency(verbose=verbose)

    if "manufacturers" in checks_set:
        issues += check_manufacturer_names(verbose=verbose)

    if "hierarchy" in checks_set:
        issues += check_hierarchy_integrity(verbose=verbose)

    if "fcc-ids" in checks_set:
        issues += check_fcc_id_validity(verbose=verbose)

    return issues
