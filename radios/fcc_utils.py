import logging
import os
import re
import threading as _threading
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from html import unescape
from datetime import datetime, time as datetime_time, timezone as datetime_timezone, date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import xmltodict
from curl_cffi import requests
from bs4 import BeautifulSoup
from django.db.models import Q
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from radios.models import (
    Brand, IgnoredGrantee, Manufacturer, Radio,
    RadioFCCTestReport, RadioManual, RadioOETDocument, SyncSkippedGrantee,
    normalize_grantee_code,
)
from radios.fcc_id_utils import (
    normalize_fcc_id_for_lookup, split_fcc_id, _validate_grantee_code,
)
from radios.manual_extraction import extract_specs_from_text, extract_text_from_pdf_with_metadata
from radios.fcc_validation import validate_fcc_brand_assignment

# Mapping from normalized FCC rule part strings to RadioServiceType names.
# Keys are the canonical rule_part values stored in RadioServiceType.
FCC_PART_TO_SERVICE_TYPE = {
    'Part 9': 'EMS',
    'Part 15B': 'Part 15 Subpart B',
    'Part 15C': 'Part 15 Subpart C',
    'Part 15': 'Part 15 Subpart B',
    'Part 15 Subpart B': 'Part 15 Subpart B',
    'Part 15 Subpart C': 'Part 15 Subpart C',
    'Part 80': 'Marine',
    'Part 87': 'Aviation',
    'Part 90': 'Commercial',
    'Part 95B': 'FRS',
    'Part 95D': 'CB',
    'Part 95E': 'GMRS',
    'Part 95J': 'MURS',
    'Part 97': 'Amateur',
}

URL = "https://apps.fcc.gov/OETLabServices/getFCCIDList?"
GENERIC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm"
GENERIC_SEARCH_FORM_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
OET_EXHIBITS_URL = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
TCB_REPORT_URL = "https://apps.fcc.gov/tcb/GetTcb731Report.do"

# Amateur radio detection: amateur devices (Part 97) do not require FCC
# certification for their transmitter.  Manufacturers often file them under
# Part 15B or 15C (unintentional/intentional radiator) with blank TX fields.
# These frequency windows cover the two most common amateur bands.
AMATEUR_BAND_2M = (144.0, 148.0)    # 2-Meter VHF band
AMATEUR_BAND_70CM = (420.0, 450.0)  # 70-Centimeter UHF band
# Rule parts that indicate a device was NOT certified for transmission.
# Devices with ONLY these rule parts (and blank TX fields) may be amateur radios.
_AMATEUR_SUSPECT_RULE_PARTS = {'15B', '15C', 'Part 15B', 'Part 15C'}
# Keywords that suggest a device is a two-way radio (not a pure receiver/scanner).
AMATEUR_PRODUCT_KEYWORDS = [
        # --- Standard & Legacy Base Terms ---
        'TWO WAY RADIO', 'TWO-WAY RADIO', 'DIGITAL TWO WAY RADIO',
        'TRANSCEIVER', 'SCANNING RECEIVER', 'DMR RADIO',
    
        # --- Transceiver Variations (Extremely Common in FCC Filings) ---
        'FM TRANSCEIVER', 'VHF/UHF TRANSCEIVER', 'VHF UHF TRANSCEIVER',
        'DUAL BAND TRANSCEIVER', 'DUAL-BAND TRANSCEIVER', 'TRI-BAND TRANSCEIVER',
        'PORTABLE TRANSCEIVER', 'MOBILE TRANSCEIVER', 'HANDHELD TRANSCEIVER',
        'RADIO TRANSCEIVER', 'AMATEUR RADIO TRANSCEIVER', 'BASE STATION TRANSCEIVER',
        'REPEATER TRANSCEIVER', 'RF TRANSCEIVER', 'WIRELESS TRANSCEIVER',
        'LMR TRANSCEIVER', 'PMR TRANSCEIVER',
    
        # --- Receiver / Scanner Terms (Crucial for Ham Radios) ---
        'COMMUNICATION RECEIVER', 'COMMUNICATIONS RECEIVER',
        'SCANNER RECEIVER', 'MULTI-BAND RECEIVER', 'WIDEBAND RECEIVER',
    
        # --- Handheld / Mobile / Consumer Descriptions ---
        'WALKIE TALKIE', 'WALKIE-TALKIE', 'WALKIE TALKIES',
        'PORTABLE RADIO', 'HANDHELD RADIO', 'HANDHELD TWO WAY RADIO',
        'PORTABLE TWO WAY RADIO', 'PORTABLE TWO-WAY RADIO',
        'MOBILE RADIO', 'MOBILE TWO WAY RADIO', 'PERSONAL RADIO',
        'HANDHELD TERMINAL', 'PORTABLE TERMINAL',
    
        # --- Digital Protocols & PoC Terms ---
        'DMR TRANSCEIVER', 'DMR PORTABLE RADIO', 'DMR MOBILE RADIO',
        'DIGITAL PORTABLE RADIO', 'DIGITAL MOBILE RADIO', 'DIGITAL TRANSCEIVER',
        'P25 TRANSCEIVER', 'NXDN TRANSCEIVER', 'dPMR TRANSCEIVER',
        'POC RADIO', 'POC TERMINAL', 'IP RADIO', 'LTE TWO WAY RADIO',
    
        # --- Service-Specific Terms (GMRS, FRS, MURS, CB, Marine, Aviation) ---
        'GMRS TRANSCEIVER', 'GMRS RADIO', 'GENERAL MOBILE RADIO SERVICE',
        'FRS TRANSCEIVER', 'FRS RADIO', 'FAMILY RADIO SERVICE',
        'CB TRANSCEIVER', 'CB RADIO', 'CITIZENS BAND',
        'MURS TRANSCEIVER', 'MURS RADIO',
        'MARINE TRANSCEIVER', 'VHF MARINE RADIO',
        'AIRBAND TRANSCEIVER', 'AVIATION TRANSCEIVER',

        # --- Mesh / LoRa / Unlicensed ISM Transceivers ---
        'LoRa TRANSCEIVER', 'LORA MODULE', 'MESH RADIO',
        '900MHz TRANSCEIVER', 'DIGITAL DATA TRANSCEIVER'
    
    ]

logger = logging.getLogger(__name__)
# pylint: disable=no-member, broad-except, global-statement
# pylint: disable=too-many-locals, too-many-branches, too-many-statements
# pylint: disable=too-many-return-statements, too-many-nested-blocks
# pylint: disable=import-outside-toplevel
# pylint: disable=missing-module-docstring, invalid-name, too-many-lines
# pylint: disable=line-too-long
# pyright: reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
# pyright: reportOptionalMemberAccess=false

# Set to True within a process when the FCC site is completely unreachable
# (curl error 28 — 0 bytes received). Avoids burning retries for every
# subsequent FCC ID in the same sync run; resets to False on any success.
_fcc_connection_down = False

# Set to True when Playwright fails to reach the FCC site at all (initial
# page.goto times out). All subsequent Playwright calls in the same sync
# are skipped immediately — no browser is launched, no 30s waits burned.
# Resets to False at the start of each new sync.
_fcc_playwright_down = False

# Cache for Playwright-collected exhibit/TCB links from the last browser
# page scrape.  Keyed by FCC ID.  Populated by
# _submit_generic_search_form_via_playwright and consumed by
# _fetch_secondary_metadata_from_html_fallback so TCB application IDs
# (from GetTcb731Report.do links) are not lost.
_pw_exhibit_links_cache: dict[str, list[str]] = {}

# Module-level cache for FCC secondary metadata, avoiding redundant
# GenericSearchResult.cfm fetches for the same FCC ID within a sync run.
_sync_metadata_cache = {}

# Module-level cache for brand/manufacturer lookups, keyed by
# (grantee_code, brand_name).
_sync_brand_cache = {}

# Module-level set of FCC IDs that have already had OET documents synced
# during the current run — used for de-duplication across sibling radios.
_synced_oet_fcc_ids = set()

# Per-thread Playwright browser pool.  Playwright's sync API is built on
# greenlets, which are thread-affine — sharing a browser across threads
# causes greenlet.error ("cannot switch to a different thread").  Each
# thread gets its own _(browser, playwright) tuple on first use.
_playwright_local = _threading.local()


def reset_sync_metadata_cache():
    """Clear the per-sync metadata cache at the start of a new sync run."""
    global _sync_metadata_cache, _sync_brand_cache, _synced_oet_fcc_ids
    _sync_metadata_cache = {}
    _sync_brand_cache = {}
    _synced_oet_fcc_ids = set()


class _FCCConnectionDownError(Exception):
    """Raised instead of TimeoutError when the connection-down fast-fail fires.
    Lets callers distinguish an expected skip from a genuine unexpected error
    and suppress the noisy full-traceback ERROR log.
    """

# Allowlist: keywords that identify a device as a two-way radio.  Matched
# case-insensitively against the combined text of FCC ID, grantee name,
# application purpose, and secondary metadata.
#
# Set FCC_RADIO_ALLOWLIST_TERMS in the environment to override.
DEFAULT_RADIO_ALLOWLIST_TERMS = (
    "TWO WAY RADIO,TWO-WAY RADIO,DIGITAL TWO WAY RADIO,"
    "TRANSCEIVER,SCANNING RECEIVER,DMR RADIO,"
    "FM TRANSCEIVER,VHF/UHF TRANSCEIVER,VHF UHF TRANSCEIVER,"
    "DUAL BAND TRANSCEIVER,DUAL-BAND TRANSCEIVER,TRI-BAND TRANSCEIVER,"
    "PORTABLE TRANSCEIVER,MOBILE TRANSCEIVER,HANDHELD TRANSCEIVER,"
    "RADIO TRANSCEIVER,AMATEUR RADIO TRANSCEIVER,"
    "AMATEUR RADIO,AMATEUR,"
    "HAM,"
    "BASE STATION TRANSCEIVER,REPEATER TRANSCEIVER,"
    "RF TRANSCEIVER,WIRELESS TRANSCEIVER,"
    "LMR TRANSCEIVER,PMR TRANSCEIVER,"
    "COMMUNICATION RECEIVER,COMMUNICATIONS RECEIVER,"
    "SCANNER RECEIVER,MULTI-BAND RECEIVER,WIDEBAND RECEIVER,"
    "WALKIE TALKIE,WALKIE-TALKIE,WALKIE TALKIES,"
    "PORTABLE RADIO,HANDHELD RADIO,HANDHELD TWO WAY RADIO,"
    "PORTABLE TWO WAY RADIO,PORTABLE TWO-WAY RADIO,"
    "MOBILE RADIO,MOBILE TWO WAY RADIO,PERSONAL RADIO,"
    "HANDHELD TERMINAL,PORTABLE TERMINAL,"
    "DMR TRANSCEIVER,DMR PORTABLE RADIO,DMR MOBILE RADIO,"
    "DIGITAL PORTABLE RADIO,DIGITAL MOBILE RADIO,DIGITAL TRANSCEIVER,"
    "P25 TRANSCEIVER,NXDN TRANSCEIVER,dPMR TRANSCEIVER,"
    "POC RADIO,POC TERMINAL,IP RADIO,LTE TWO WAY RADIO,"
    "GMRS TRANSCEIVER,GMRS RADIO,GENERAL MOBILE RADIO SERVICE,GMRS,"
    "FRS TRANSCEIVER,FRS RADIO,FAMILY RADIO SERVICE,FRS,"
    "CB TRANSCEIVER,CB RADIO,CITIZENS BAND,CB,"
    "MURS TRANSCEIVER,MURS RADIO,MURS,"
    "MARINE TRANSCEIVER,VHF MARINE RADIO,"
    "AIRBAND TRANSCEIVER,AVIATION TRANSCEIVER,"
    "LoRa TRANSCEIVER,LORA MODULE,MESH RADIO,"
    "900MHz TRANSCEIVER,DIGITAL DATA TRANSCEIVER"
)
RADIO_ALLOWLIST_ENV_NAME = "FCC_RADIO_ALLOWLIST_TERMS"

# Allowlist terms that require word-boundary matching (\\b) to prevent
# false positives from technical identifiers.  For example, "CB" should
# match "CB RADIO" but NOT "2ASCB" or "FSCBT9104DI".
_STRICT_ALLOWLIST_TERMS = frozenset({'CB', 'FRS', 'GMRS', 'MURS', 'HAM'})


def _term_matches_text(term, text):
    """Check if a term matches text, using word boundaries for strict terms.

    Short standalone terms like CB, FRS, GMRS, MURS, HAM use regex
    ``\\b`` word-boundary matching to prevent false positives from
    technical identifiers like grantee codes (e.g. "2ASCB" should
    NOT match "CB").

    All other terms use simple substring matching.
    """
    if term in _STRICT_ALLOWLIST_TERMS:
        pattern = r'\b' + re.escape(term) + r'\b'
        return bool(re.search(pattern, text, re.IGNORECASE))
    return term in text

# Denylist: accessories and non-radio devices that should NOT be tracked
# even if they match an allowlist term like "RECEIVER".  Terms are
# case-insensitive substring matches against the same combined text blob
# used by the allowlist (FCC ID, grantee name, application purpose,
# grant date, and secondary metadata text_blob).
#
# Set FCC_RADIO_DENYLIST_TERMS in the environment to override these.
# Set it to an empty string to disable denylist filtering entirely.
DEFAULT_RADIO_DENYLIST_TERMS = (
    # ── Accessories (microphones, chargers, cables, etc.) ──
    "SPEAKER MICROPHONE,REMOTE SPEAKER MIC,REMOTE MICROPHONE,"
    "HAND MICROPHONE,DESK MICROPHONE,PTT MICROPHONE,"
    "BLUETOOTH HEADSET,WIRELESS HEADSET,EARPIECE,"
    "BATTERY CHARGER,CHARGING CRADLE,DESKTOP CHARGER,"
    "RAPID CHARGER,CHARGER CUP,"
    "PROGRAMMING CABLE,CLONING CABLE,DATA CABLE,USB CABLE,"
    "MOUNTING BRACKET,MOUNTING KIT,CARRYING CASE,"
    "POWER SUPPLY,AC ADAPTER,DC ADAPTER,"
    "ANTENNA REPLACEMENT,WHIP ANTENNA,STUBBY ANTENNA,"
    "WATERPROOF BAG,SOFT CASE,HARD CASE,"
    # ── Consumer electronics (TVs, displays, monitors) ──
    "TELEVISION,QLED TV,OLED TV,LCD TV,LED TV,PLASMA TV,"
    "SMART TV,DIGITAL TV,FLAT PANEL TV,"
    "LED MONITOR,LCD MONITOR,COMPUTER MONITOR,DISPLAY MONITOR,"
    "DIGITAL SIGNAGE,VIDEO DISPLAY,FLAT PANEL DISPLAY,"
    "UHD TV,4K TV,8K TV,HDR TV,"
    "SET-TOP BOX,STREAMING DEVICE,STREAMING BOX,"
    "SOUNDBAR,HOME THEATER,AUDIO SYSTEM,"
    "WASHING MACHINE,REFRIGERATOR,DISHWASHER,MICROWAVE OVEN,"
    "VACUUM CLEANER,AIR CONDITIONER,AIR PURIFIER"
)
RADIO_DENYLIST_ENV_NAME = "FCC_RADIO_DENYLIST_TERMS"

# Rule parts to exclude from import (e.g. 15.231 covers low-power
# periodic transmitters like garage door openers, car key fobs,
# remote sensors — these are not handheld radios we care about).
_DEFAULT_IGNORED_RULE_PARTS = frozenset({
    '15.231',
    '15.231(e)',
})
_IGNORED_RULE_PARTS_ENV = 'FCC_IGNORED_RULE_PARTS'

# Rule parts that indicate a licensed or type-accepted radio transmitter
# (not just an unintentional radiator under Part 15).
# Override via FCC_RADIO_RULE_PARTS env var (comma-separated).
# Set to 'none' to disable this classifier check.
_DEFAULT_RADIO_RULE_PARTS: frozenset[str] = frozenset({
    '9', 'Part 9',         # Part 9 — Unlicensed / some DMR radios
    '90', 'Part 90',       # LMR / Commercial
    '95', 'Part 95',       # Personal Radio Services (GMRS/FRS/CB/MURS)
    '95A', 'Part 95A',     # GMRS (legacy)
    '95B', 'Part 95B',     # FRS
    '95D', 'Part 95D',     # CB
    '95E', 'Part 95E',     # GMRS
    '95J', 'Part 95J',     # MURS
    '80', 'Part 80',       # Marine
    '87', 'Part 87',       # Aviation
    '97', 'Part 97',       # Amateur
    '22', 'Part 22',       # Cellular / Wide-area (some PoC radios)
    '24', 'Part 24',       # PCS (some PoC radios)
    '74', 'Part 74',       # Broadcast Auxiliary
})
_RADIO_RULE_PARTS_ENV = 'FCC_RADIO_RULE_PARTS'

# Frequency bands (MHz) that are characteristic of two-way radios.
# Format per env entry: lower-upper:label (e.g. "136-174:VHF").
# Override via FCC_RADIO_FREQ_BANDS env var (comma-separated).
# Set to 'none' to disable this classifier check.
_DEFAULT_RADIO_FREQ_BANDS: tuple[tuple[float, float, str], ...] = (
    (26.0, 28.0, 'CB'),           # HF CB band
    (118.0, 136.975, 'Airband'),    # Aviation VHF
    (136.0, 174.0, 'VHF'),        # VHF LMR / Marine / MURS
    (150.0, 155.0, 'MURS'),       # MURS specific
    (156.0, 174.0, 'Marine'),     # Marine VHF
    (400.0, 520.0, 'UHF'),        # UHF LMR / GMRS / FRS
    (462.0, 468.0, 'GMRS/FRS'),   # GMRS/FRS specific
    (28.000, 29.700, 'Amateur'),  # 10m amateur
    (420.0, 450.0, 'Amateur'),    # 70cm amateur
    (144.0, 148.0, 'Amateur'),    # 2m amateur
    (902.0, 928.0, 'LoRa/ISM'),   # 900 MHz ISM / LoRa
    (700.0, 800.0, 'LTE/PoC'),    # LTE / PoC radios
)
_RADIO_FREQ_BANDS_ENV = 'FCC_RADIO_FREQ_BANDS'

# Emission designator prefixes that indicate a voice or data radio
# transmitter (not WiFi, Bluetooth, or telemetry-only devices).
# Override via FCC_RADIO_EMISSION_PREFIXES env var (comma-separated).
# Set to 'none' to disable this classifier check.
_DEFAULT_RADIO_EMISSION_PREFIXES: frozenset[str] = frozenset({
    # Analog voice
    '11K0F3E', '16K0F3E', '6K00F3E', '8K00F3E', '20K0F3E',
    '11K0F3D', '16K0F3D', '8K00F1D',
    'A3E',  # AM voice (airband)
    # Digital voice (DMR)
    '7K60FXE', '7K60FXD', '7K60FXW',
    '7K60F1E', '7K60F1D', '7K60F1W',
    # Digital voice (NXDN / P25 Phase 1)
    '8K10F1E', '8K10F1D', '8K30F1E', '8K30F1D',
    '8K10FXE', '8K10FXD', '8K30FXE', '8K30FXD',
    '4K00F1E', '4K00F1D', '4K00F7W',
    # P25 Phase 2
    '8K70D1W', '8K70D1E',
    # Digital voice (dPMR / NXDN narrow)
    '4K00F7W', '4K00F7E',
    # Data/telemetry (but still a transmitter)
    'G7D', 'F1D', 'F2D', 'G1D', 'G7W',
})
_RADIO_EMISSION_PREFIXES_ENV = 'FCC_RADIO_EMISSION_PREFIXES'

# Application purposes that always pass the allowlist pre-filter.
# Override via FCC_ALWAYS_PASS_PURPOSES env var (comma-separated).
# Set to 'none' to disable (no purposes auto-pass).
_DEFAULT_ALWAYS_PASS_PURPOSES: frozenset[str] = frozenset({
    'original equipment',
    'change in identification',
    'class ii permissive change',
    'class i permissive change',
    'grant of equipment authorization',
})
_ALWAYS_PASS_PURPOSES_ENV = 'FCC_ALWAYS_PASS_PURPOSES'


# ── Env-var accessor helpers ───────────────────────────────────────────


def _parse_env_frozenset(env_name, default):
    """Parse a comma-separated env var into a frozenset.

    Returns *default* when the env var is unset or empty.
    Set to 'none' to return an empty frozenset (disable check).
    """
    raw = os.environ.get(env_name, '').strip()
    if not raw:
        return default
    if raw.lower() == 'none':
        return frozenset()
    return frozenset(p.strip() for p in raw.split(',') if p.strip())


def _parse_env_freq_bands(env_name, default):
    """Parse comma-separated 'lo-hi:label' env var into freq band tuples.

    Returns *default* when the env var is unset or empty.
    Set to 'none' to return an empty tuple (disable check).
    """
    raw = os.environ.get(env_name, '').strip()
    if not raw:
        return default
    if raw.lower() == 'none':
        return ()
    result = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        label = ''
        if ':' in entry:
            range_part, label = entry.split(':', 1)
            label = label.strip()
        else:
            range_part = entry
        parts = range_part.split('-')
        if len(parts) != 2:
            continue
        try:
            lo = float(parts[0].strip())
            hi = float(parts[1].strip())
        except ValueError:
            continue
        result.append((lo, hi, label or f'{lo}-{hi}'))
    return tuple(result) if result else default


def _get_radio_rule_parts():
    """Return the effective radio rule parts set (env-overridable)."""
    return _parse_env_frozenset(_RADIO_RULE_PARTS_ENV, _DEFAULT_RADIO_RULE_PARTS)


def _get_radio_freq_bands():
    """Return the effective radio frequency band list (env-overridable)."""
    return _parse_env_freq_bands(_RADIO_FREQ_BANDS_ENV, _DEFAULT_RADIO_FREQ_BANDS)


def _get_radio_emission_prefixes():
    """Return the effective emission designator prefixes (env-overridable)."""
    return _parse_env_frozenset(
        _RADIO_EMISSION_PREFIXES_ENV, _DEFAULT_RADIO_EMISSION_PREFIXES,
    )


def _get_always_pass_purposes():
    """Return the effective always-pass purposes set (env-overridable)."""
    return _parse_env_frozenset(
        _ALWAYS_PASS_PURPOSES_ENV, _DEFAULT_ALWAYS_PASS_PURPOSES,
 )


# ── End of classifier constants ────────────────────────────────────────


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_OET_APP_ID_RE = re.compile(r'application_id=([A-Za-z0-9%+=/]+)', re.IGNORECASE)
# TCB Form 731 links use applicationId= (camelCase, no underscore) —
# e.g. /tcb/GetTcb731Report.do?applicationId=KRN6OLNepnfjuhLB5CIO0Q%3D%3D&fcc_id=DBZT20A
_TCB_APP_ID_RE = re.compile(r'applicationId=([A-Za-z0-9%+=/]+)', re.IGNORECASE)
FCC_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def _normalize_brand_identity(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _find_existing_grantee_brand(grantee_code, grantee_name):
    brand = Brand.objects.filter(grantee_code__iexact=grantee_code).first()
    if brand is not None:
        return brand

    for field_name in ('name', 'alias', 'full_name'):
        brand = Brand.objects.filter(**{f'{field_name}__iexact': grantee_name}).first()
        if brand is not None:
            return brand

    name_key = _normalize_brand_identity(grantee_name)
    if not name_key:
        return None

    blank_code_brands = Brand.objects.filter(
        Q(grantee_code__isnull=True) | Q(grantee_code__exact=''),
    ).only(
        'id', 'name', 'alias', 'full_name', 'grantee_code',
    )
    for candidate in blank_code_brands:
        for value in (candidate.name, candidate.alias, candidate.full_name):
            if _normalize_brand_identity(value) == name_key:
                return candidate

    return None


def _find_matching_blank_code_brand(grantee_name, exclude_brand_id=None):
    name_key = _normalize_brand_identity(grantee_name)
    if not name_key:
        return None

    blank_code_brands = Brand.objects.filter(
        Q(grantee_code__isnull=True) | Q(grantee_code__exact=''),
    ).only(
        'id', 'name', 'alias', 'full_name', 'grantee_code',
    )
    if exclude_brand_id is not None:
        blank_code_brands = blank_code_brands.exclude(pk=exclude_brand_id)

    for candidate in blank_code_brands:
        for value in (candidate.name, candidate.alias, candidate.full_name):
            if _normalize_brand_identity(value) == name_key:
                return candidate

    return None


def _resolve_authoritative_radio_brand_name(auth_brand, grantee_code, grantee_name):
    norm_name = (grantee_name or '').strip()
    if auth_brand is None or not norm_name:
        return norm_name

    norm_brand = _normalize_brand_identity(auth_brand.name)
    grantee_key = _normalize_brand_identity(norm_name)
    if norm_brand == grantee_key:
        return auth_brand.name

    auth_code = normalize_grantee_code(getattr(auth_brand, 'grantee_code', ''))
    if auth_code != normalize_grantee_code(grantee_code):
        return norm_name

    blank_brand = _find_matching_blank_code_brand(
        norm_name,
        exclude_brand_id=auth_brand.id,
    )
    if blank_brand is None:
        return norm_name

    return auth_brand.name


def _is_connection_timeout_error(exc):
    """Return True when the exception is a network-level connection timeout
    (curl error 28: 0 bytes received).  These should NOT be retried — the
    host is unreachable and retrying burns time with no benefit."""
    msg = str(exc).lower()
    return 'curl: (28)' in msg or '0 bytes received' in msg


def _is_playwright_timeout_error(exc):
    """Return True for any Playwright or network timeout.

    Playwright raises two distinct error types:
    - ``playwright.TimeoutError`` (class name contains 'TimeoutError') for
      explicit wait/timeout calls.
    - ``playwright.Error`` (class name is just 'Error') for network-level
      failures such as ``net::ERR_TIMED_OUT``, whose message contains
      'timed_out' but NOT 'timeout'.
    """
    type_name = type(exc).__name__
    msg = str(exc).lower()
    return (
        'timeouterror' in type_name.lower()
        or 'timeout' in msg
        or 'timed_out' in msg
        or 'err_timed_out' in msg
    )


def _fcc_request_with_retry(method, url, *, session=None, retries=2, retry_delay=0.6, **kwargs):
    global _fcc_connection_down
    requester = session if session is not None else requests
    last_response = None

    # If a prior request in this process already hit a connection timeout,
    # skip the HTTP call entirely and raise immediately so callers fall
    # through to the Playwright path without wasting additional retries.
    if _fcc_connection_down:
        raise _FCCConnectionDownError(
            'FCC site unreachable (connection-down flag set); '
            'skipping HTTP attempt',
        )

    # Env-driven 503 fast-fail: skip retries for 503 since it's an FCC overload
    # signal that won't resolve within our retry window.
    _skip_503_retry = os.environ.get('FCC_SKIP_RETRY_ON_503', 'true').strip().lower() in \
        ('1', 'true', 'yes', 'on')

    for attempt in range(retries + 1):
        try:
            response = getattr(requester, method)(url, **kwargs)
        except Exception as exc:
            if _is_connection_timeout_error(exc):
                _fcc_connection_down = True
                logger.warning(
                    'FCC request connection timeout (no bytes received) method=%s url=%s '
                    '— skipping retries and marking FCC site as unreachable',
                    method.upper(), url,
                )
                raise
            if attempt >= retries:
                raise
            logger.warning(
                "FCC request retry after exception method=%s url=%s attempt=%s retries=%s",
                method.upper(), url, attempt + 1, retries,
            )
            time.sleep(retry_delay * (attempt + 1))
            continue

        last_response = response
        _fcc_connection_down = False

        # 503 fast-fail: skip retries, fall through to next handler immediately
        if response.status_code == 503 and _skip_503_retry and attempt < retries:
            logger.info(
                "FCC request 503 fast-fail method=%s url=%s — skipping remaining retries",
                method.upper(), url,
            )
            return response

        if response.status_code not in FCC_RETRY_STATUS_CODES or attempt >= retries:
            return response

        logger.info(
            "FCC request retrying method=%s url=%s status=%s attempt=%s retries=%s",
            method.upper(), url, response.status_code, attempt + 1, retries,
        )
        time.sleep(retry_delay * (attempt + 1))

    return last_response


def _parse_allowlist_terms(value):
    terms = []
    for raw in (value or '').split(','):
        term = raw.strip().upper()
        if term and term not in terms:
            terms.append(term)
    return terms


def _radio_allowlist_terms():
    raw = os.environ.get(RADIO_ALLOWLIST_ENV_NAME, DEFAULT_RADIO_ALLOWLIST_TERMS)
    terms = _parse_allowlist_terms(raw)
    for required in _parse_allowlist_terms(DEFAULT_RADIO_ALLOWLIST_TERMS):
        if required not in terms:
            terms.append(required)
    return terms


def _radio_denylist_terms():
    """Return the combined denylist of non-radio accessory terms.

    Unlike the allowlist where defaults are always appended, the denylist
    allows the ``FCC_RADIO_DENYLIST_TERMS`` env var to fully replace the
    defaults.  Set it to an empty string to disable all denylist filtering.
    If the env var is not set, the built-in defaults are used.
    """
    raw = os.environ.get(RADIO_DENYLIST_ENV_NAME)
    if raw is not None:
        # Explicit override: caller controls the full denylist.
        return _parse_allowlist_terms(raw)
    # No env var: use built-in defaults.
    return _parse_allowlist_terms(DEFAULT_RADIO_DENYLIST_TERMS)


def _ignored_rule_parts():
    """Return the set of rule parts to exclude from import.

    Override via the ``FCC_IGNORED_RULE_PARTS`` env var (comma-separated).
    Set to an empty string to disable all rule-part filtering.
    """
    raw = os.environ.get(_IGNORED_RULE_PARTS_ENV, '').strip()
    if raw == '':
        return set(_DEFAULT_IGNORED_RULE_PARTS)
    if raw.lower() == 'none':
        return set()
    return {part.strip() for part in raw.split(',') if part.strip()}


def _rule_parts_match_ignored(rule_parts):
    """Return True if any rule part is in the ignored set."""
    if not rule_parts:
        return False
    ignored = _ignored_rule_parts()
    if not ignored:
        return False
    norm_ignored = {p.strip().upper() for p in ignored}
    for rp in rule_parts:
        if (rp or '').strip().upper() in norm_ignored:
            return True
    return False


def _iter_dict_nodes(payload):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_dict_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_dict_nodes(item)


def _extract_fcc_key(value):
    return _clean_query((value or '').replace('-', ''))


def _fcc_lookup_variants(fcc_id):
    variants = []
    for candidate in (
        (fcc_id or '').strip(),
        normalize_fcc_id_for_lookup(fcc_id),
        _extract_fcc_key(fcc_id),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _dict_text_blob(payload):
    values = []
    for value in payload.values():
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                values.append(text)
    return ' | '.join(values).upper()


def _extract_product_designation(payload):
    if not isinstance(payload, dict):
        return ''

    for key in (
        'product_designation', 'productDesignation', 'product_description',
        'productDescription', 'equipment_description',
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    blob = _dict_text_blob(payload)
    match = re.search(r'PRODUCT\s+DESIGNATION\s*[:\-]\s*([^|]{2,120})', blob, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ''


def _extract_urls_from_payload(payload):
    urls = []
    if not isinstance(payload, dict):
        return urls

    for key, value in payload.items():
        if isinstance(value, str):
            lower_key = str(key).lower()
            if lower_key.endswith('url') or lower_key.endswith('link') or 'href' in lower_key:
                trimmed = value.strip()
                if trimmed.lower().startswith('http'):
                    urls.append(trimmed)
            for found in URL_PATTERN.findall(value):
                urls.append(found)
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _is_original_equipment_purpose(value):
    return (value or '').strip().lower() == 'original equipment'


def _parse_year_from_grant_date(value):
    text = (value or '').strip()
    if not text:
        return None

    parsed = _parse_datetime_value(text)
    if parsed:
        return parsed.year

    match = re.search(r'(19|20)\d{2}', text)
    if match:
        return int(match.group(0))
    return None


def _parse_decimal(value):
    text = (value or '').strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, TypeError):
        return None


def _format_decimal_8(value):
    return f"{value.quantize(Decimal('0.00000000'))}"


def _extract_grant_date(primary_record, secondary_metadata):
    """Extract the earliest grant date from primary record or secondary metadata.

    Returns a datetime.date or None.
    """
    dates = []

    # Primary API record grantDate
    raw_primary = (primary_record or {}).get('grantDate', '')
    if raw_primary:
        dt = _parse_datetime_value(raw_primary)
        if dt:
            dates.append(dt.date() if hasattr(dt, 'date') else dt)

    # Secondary metadata OEM rows
    for node in (secondary_metadata or {}).get('original_equipment_rows', []):
        raw = node.get('grant_date', '')
        if raw:
            dt = _parse_datetime_value(raw)
            if dt:
                dates.append(dt.date() if hasattr(dt, 'date') else dt)

    return min(dates) if dates else None


def _extract_original_equipment_summary(primary_record, sec_metadata):
    years = []
    frequency_ranges = []

    # Primary API record can contribute grant date + purpose.
    primary_purpose = (
        primary_record.get('applicationPurpose', '')
        if isinstance(primary_record, dict) else ''
    )
    if _is_original_equipment_purpose(primary_purpose):
        year = _parse_year_from_grant_date(primary_record.get('grantDate', ''))
        if year:
            years.append(year)

    # Secondary metadata can include grant date + purpose + frequency rows.
    for node in (sec_metadata or {}).get('original_equipment_rows', []):
        year = _parse_year_from_grant_date(node.get('grant_date', ''))
        if year:
            years.append(year)

        lower = _parse_decimal(node.get('lower_freq_mhz', ''))
        upper = _parse_decimal(node.get('upper_freq_mhz', ''))
        if lower is None or upper is None:
            continue
        if lower > upper:
            lower, upper = upper, lower
        frequency_ranges.append((lower, upper))

    intro_year = min(years) if years else None

    # Choose the narrowest original-equipment frequency range as the representative TX band.
    # This favors the most specific emission band when FCC metadata contains broad + narrow entries.
    freq_bands_tx = ''
    if frequency_ranges:
        lower, upper = min(frequency_ranges, key=lambda pair: (pair[1] - pair[0], pair[0]))
        freq_bands_tx = f"{_format_decimal_8(lower)}-{_format_decimal_8(upper)} MHz"

    grant_date = _extract_grant_date(primary_record, sec_metadata)

    return {
        'intro_year': intro_year,
        'freq_bands_tx': freq_bands_tx,
        'grant_date': grant_date,
    }


# NOAA Weather Radio band: 162.400 – 162.550 MHz (7 channels, receive-only).
_NOAA_WX_LOWER = 162.40
_NOAA_WX_UPPER = 162.55

# Bluetooth (2.4 GHz ISM band): 2402.0 – 2480.0 MHz.
_BLUETOOTH_LOWER = 2402.0
_BLUETOOTH_UPPER = 2480.0


def _detect_noaa_and_bluetooth(oe_summary, sec_metadata):
    """Check whether the FCC data indicates NOAA WX and/or Bluetooth support.

    Examines the original-equipment frequency rows from both the primary
    FCC grant and the secondary metadata search.  If any row's frequency
    range overlaps the NOAA WX band (162.40–162.55 MHz) or Bluetooth band
    (2402–2480 MHz), the corresponding flag is returned as True.

    Args:
        oe_summary: Dict from ``_extract_original_equipment_summary``.
        sec_metadata: Dict from ``fetch_fcc_secondary_metadata``.

    Returns:
        (noaa_wx, bluetooth) tuple of bools.
    """
    noaa_wx = False
    bluetooth = False

    rows = (sec_metadata or {}).get('original_equipment_rows', [])
    if not rows:
        return noaa_wx, bluetooth

    for row in rows:
        try:
            lower = float(row.get('lower_freq_mhz', 0) or 0)
            upper = float(row.get('upper_freq_mhz', 0) or 0)
        except (ValueError, TypeError):
            continue
        if lower <= 0 or upper <= 0:
            continue
        if lower > upper:
            lower, upper = upper, lower

        # NOAA WX: any overlap with 162.40–162.55 MHz
        if (
            not noaa_wx
            and lower <= _NOAA_WX_UPPER
            and upper >= _NOAA_WX_LOWER
        ):
            noaa_wx = True

        # Bluetooth: any overlap with 2402–2480 MHz
        if (
            not bluetooth
            and lower <= _BLUETOOTH_UPPER
            and upper >= _BLUETOOTH_LOWER
        ):
            bluetooth = True

        if noaa_wx and bluetooth:
            break

    return noaa_wx, bluetooth


def _is_test_report_candidate(payload):
    blob = _dict_text_blob(payload)
    return any(
        marker in blob
        for marker in (
            'TEST REPORT',
            'TEST-REPORT',
            'EXHIBIT TYPE: TEST REPORT',
            'EXHIBIT DESCRIPTION: TEST REPORT',
        )
    )


def _extract_test_report_candidates(data, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    candidates = []
    seen = set()

    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )

        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        if not _is_test_report_candidate(node):
            continue

        urls = _extract_urls_from_payload(node)
        designation = _extract_product_designation(node)
        title = (
            node.get('exhibit_description')
            or node.get('exhibitDescription')
            or node.get('document_description')
            or node.get('documentDescription')
            or node.get('description')
            or 'FCC Test Report'
        )

        for url in urls:
            key = (url, title, designation)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    'url': url,
                    'title': str(title).strip(),
                    'product_designation': designation,
                }
            )

    return candidates


def _strip_html_tags(value):
    text = re.sub(r'<[^>]+>', ' ', value or '', flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', unescape(text)).strip()


def _parse_date_only(value):
    text = (value or '').strip()
    if not text:
        return None

    parsed = parse_date(text)
    if parsed:
        return parsed

    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_oet_documents_from_xml(data, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    documents = []
    seen = set()

    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )
        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        view_attachment = (
            node.get('view_attachment')
            or node.get('viewAttachment')
            or node.get('exhibit_description')
            or node.get('exhibitDescription')
            or node.get('description')
            or ''
        ).strip()
        exhibit_type = (
            node.get('exhibit_type')
            or node.get('exhibitType')
            or node.get('doc_type')
            or node.get('document_type')
            or ''
        ).strip()
        date_submitted = (
            node.get('date_submitted_to_fcc')
            or node.get('dateSubmittedToFcc')
            or node.get('date_submitted')
            or node.get('dateSubmitted')
            or ''
        ).strip()
        display_type = (
            node.get('display_type')
            or node.get('displayType')
            or node.get('file_type')
            or ''
        ).strip()
        date_available = (
            node.get('date_available')
            or node.get('dateAvailable')
            or ''
        ).strip()

        document_url = ''
        for extracted_url in _extract_urls_from_payload(node):
            lower_url = extracted_url.lower()
            if (
                'viewexhibitreport' in lower_url
                or 'report=' in lower_url
                or lower_url.endswith('.pdf')
            ):
                document_url = extracted_url

        if not any((
            view_attachment, exhibit_type, date_submitted,
            display_type, date_available, document_url,
        )):
            continue

        key = (
            document_url, view_attachment, exhibit_type,
            date_submitted, display_type, date_available,
        )
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': exhibit_type,
                'date_submitted_to_fcc': date_submitted,
                'display_type': display_type,
                'date_available': date_available,
                'document_url': document_url,
            }
        )

    return documents


def _extract_oet_documents_from_html(html_text, base_url):
    documents = []
    seen = set()

    soup = BeautifulSoup(html_text or '', 'html.parser')
    for row in soup.find_all('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) < 5:
            continue

        attachment_index = None
        document_url = ''
        for index, cell in enumerate(cells):
            link = cell.find(
                'a',
                href=lambda href: (
                    isinstance(href, str)
                    and _is_fcc_attachment_document_url(href)
                ),
            )
            if link:
                attachment_index = index
                document_url = unescape(urljoin(base_url, link.get('href').strip()))
                break

            cell_html = str(cell)
            link_match = re.search(
                (
                    r'(?:["\'])(/oetcf/eas/reports/'
                    r'(?:GenericExhibit\.cfm|GetAttachment\.cfm|ViewAttachment\.cfm)'
                    r'[^"\'\s)]*|/eas/GetApplicationAttachment\.html[^"\'\s)]*)(?:["\'])'
                ),
                cell_html,
                flags=re.IGNORECASE,
            )
            if link_match:
                attachment_index = index
                document_url = unescape(urljoin(base_url, link_match.group(1).strip()))
                break

        if attachment_index is None or not document_url:
            continue

        row_cells = cells[attachment_index:attachment_index + 5]
        if len(row_cells) < 5:
            continue

        row_text = row_cells[0].get_text(' ', strip=True)
        view_attachment = _strip_html_tags(row_text or '')
        row_text = row_cells[1].get_text(' ', strip=True)
        exhibit_type = _strip_html_tags(row_text or '')
        row_text = row_cells[2].get_text(' ', strip=True)
        date_submitted = _strip_html_tags(row_text or '')
        row_text = row_cells[3].get_text(' ', strip=True)
        display_type = _strip_html_tags(row_text or '')
        row_text = row_cells[4].get_text(' ', strip=True)
        date_available = _strip_html_tags(row_text or '')

        if not any((
            view_attachment, exhibit_type, date_submitted,
            display_type, date_available, document_url,
        )):
            continue

        if not _is_fcc_attachment_document_url(document_url):
            continue

        if view_attachment.lower() == 'view attachment' and exhibit_type.lower() == 'exhibit type':
            continue

        key = (
            document_url, view_attachment, exhibit_type,
            date_submitted, display_type, date_available,
        )
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': exhibit_type,
                'date_submitted_to_fcc': date_submitted,
                'display_type': display_type,
                'date_available': date_available,
                'document_url': document_url,
                'referer_url': base_url,
            }
        )

    return documents


def _extract_oet_documents_from_attachment_html(html_text, base_url):
    documents = []
    seen = set()

    soup = BeautifulSoup(html_text or '', 'html.parser')
    for link in soup.find_all('a', href=True):
        href = str(link.get('href') or '').strip()
        if not href:
            continue

        lower_href = href.lower()
        if (
            'getattachment.cfm' not in lower_href
            and 'genericexhibit.cfm' not in lower_href
            and 'getapplicationattachment.html' not in lower_href
        ):
            continue

        document_url = unescape(urljoin(base_url, href))
        view_attachment = _strip_html_tags(link.get_text(' ', strip=True) or '') or 'FCC Attachment'
        key = (document_url, view_attachment)
        if key in seen:
            continue
        seen.add(key)

        documents.append(
            {
                'view_attachment': view_attachment,
                'exhibit_type': '',
                'date_submitted_to_fcc': '',
                'display_type': 'pdf',
                'date_available': '',
                'document_url': document_url,
                'referer_url': base_url,
            }
        )

    return documents


def _is_fcc_attachment_document_url(url):
    lower_url = (url or '').strip().lower()
    return any(
        token in lower_url
        for token in (
            'getattachment.cfm',
            'genericexhibit.cfm',
            'getapplicationattachment.html',
            'viewattachment.cfm',
        )
    )


def _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id):
    target_key = _extract_fcc_key(fcc_id)
    matched_records = []
    matched_keys = set()
    oe_rows = []
    exhibit_urls = []
    rule_parts_set = set()

    body_match = re.search(
        r'<tbody[^>]*id=["\']offTblBdy["\'][^>]*>(.*?)</tbody>',
        html_text or '',
        flags=re.IGNORECASE | re.DOTALL,
    )
    row_source = body_match.group(1) if body_match else (html_text or '')

    tcb_application_ids = []
    seen_tcb_ids = set()

    for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', row_source, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 16:
            continue

        row_fcc_id = _strip_html_tags(cells[11])
        if not row_fcc_id:
            continue
        if target_key and _extract_fcc_key(row_fcc_id) != target_key:
            continue

        application_purpose = _strip_html_tags(cells[12])
        grant_date = _strip_html_tags(cells[13])
        lower_freq = _strip_html_tags(cells[14])
        upper_freq = _strip_html_tags(cells[15])

        matched_records.append(
            ' | '.join(
                part for part in (
                    row_fcc_id,
                    application_purpose,
                    grant_date,
                    lower_freq,
                    upper_freq,
                )
                if part
            )
        )
        matched_keys.update(
            {
                'fcc_id',
                'application_purpose',
                'grant_date',
                'lower_freq_mhz',
                'upper_freq_mhz',
            }
        )

        # Extract rule parts from cell 16 (if available)
        if len(cells) > 16:
            raw_rule_parts = _strip_html_tags(cells[16])
            if raw_rule_parts:
                matched_keys.add('rule_parts')
                for part in raw_rule_parts.replace(';', ',').split(','):
                    part = part.strip()
                    if part:
                        rule_parts_set.add(part)

        # Extract applicationId from the TCB Form 731 link (cell 1, "View Form").
        # This is the most direct source — the link format is:
        #   /tcb/GetTcb731Report.do?applicationId=XXX&fcc_id=YYY
        for href in re.findall(r'href=["\']([^"\']+)["\']', cells[1], flags=re.IGNORECASE):
            tcb_match = _TCB_APP_ID_RE.search(unescape(href))
            if tcb_match:
                app_id = tcb_match.group(1)
                if app_id not in seen_tcb_ids:
                    seen_tcb_ids.add(app_id)
                    tcb_application_ids.append(app_id)

        for href in re.findall(r'href=["\']([^"\']+)["\']', cells[2], flags=re.IGNORECASE):
            url = unescape(urljoin(base_url, href.strip()))
            if 'ViewExhibitReport.cfm' in url:
                exhibit_urls.append(url)

        if _is_original_equipment_purpose(application_purpose):
            power_output = _strip_html_tags(cells[18]) if len(cells) > 18 else ''
            emission_designator = _strip_html_tags(cells[17]) if len(cells) > 17 else ''
            oe_rows.append(
                {
                    'grant_date': grant_date,
                    'application_purpose': application_purpose,
                    'lower_freq_mhz': lower_freq,
                    'upper_freq_mhz': upper_freq,
                    'power_output': power_output,
                    'emission_designator': emission_designator,
                }
            )

    # NOTE: Intentionally no unconstrained application_id regex fallback here.
    # Grabbing the first application_id found anywhere in the HTML when no row
    # matched the target FCC ID caused cross-contamination (e.g. a grantee-wide
    # search showing 2AZSA-RT490's row alongside 2AZSA-RT950 would hand RT490's
    # application_id to the RT950 document fetch, attaching the wrong exhibits).
    # Callers receive an empty candidate_exhibit_urls list and should treat that
    # as "no OET exhibits found" rather than falling back to an unverified ID.

    # Fallback: when the HTML table doesn't have the expected 16-cell structure
    # (e.g. Playwright-rendered pages with different column layout), try to
    # extract rule parts from the raw HTML text using regex.
    if not rule_parts_set and html_text:
        rule_part_matches = re.findall(
            r'(?:rule\s*parts?|47\s*CFR)\s*[:.]?\s*'
            r'((?:Part\s*\d+[A-Za-z]*(?:\s*Subpart\s+[A-Za-z])?'
            r'(?:\s*[,;/]\s*Part\s*\d+[A-Za-z]*)*))',
            html_text or '',
            flags=re.IGNORECASE,
        )
        for match in rule_part_matches:
            for segment in re.split(r'[,;/]', match):
                segment = segment.strip()
                if segment:
                    rule_parts_set.add(segment)

    # Fallback: also extract applicationId from any TCB 731 link in cells[1]
    # for rows that didn't match the target FCC ID (useful for grantee-wide
    # searches where the first matching row is the right one).
    if not tcb_application_ids:
        for row_html in re.findall(
            r'<tr[^>]*>(.*?)</tr>', row_source, flags=re.IGNORECASE | re.DOTALL
        ):
            cells = re.findall(
                r'<t[dh][^>]*>(.*?)</t[dh]>', row_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if len(cells) < 12:
                continue
            for href in re.findall(
                r'href=["\']([^"\']+)["\']', cells[1], flags=re.IGNORECASE
            ):
                tcb_match = _TCB_APP_ID_RE.search(unescape(href))
                if tcb_match:
                    app_id = tcb_match.group(1)
                    if app_id not in seen_tcb_ids:
                        seen_tcb_ids.add(app_id)
                        tcb_application_ids.append(app_id)

    return {
        'record_count': len(matched_records),
        'text_blob': ' || '.join(matched_records),
        'matched_keys': sorted(matched_keys),
        'original_equipment_rows': oe_rows,
        'rule_parts': sorted(rule_parts_set),
        'candidate_exhibit_urls': exhibit_urls,
        'tcb_application_ids': tcb_application_ids,
    }


def _build_generic_search_payload(fcc_id):
    grantee_code, product_code = split_fcc_id(fcc_id)
    if not grantee_code or not product_code:
        return None

    return {
        'grantee_code': grantee_code,
        'product_code': product_code,
        'product_exact_match': '',
        'applicant_name': '',
        'grant_date_from': '',
        'grant_date_to': '',
        'comments': '',
        'application_purpose_description': '',
        'sdr_filings_only': '',
        'eas_apps_only': 'Y',
        'tcb_apps_only': '',
        'composite_apps_only': '',
        'test_firm': '',
        'application_status_description': '',
        'equipment_class_description': '',
        'lower_frequency': '',
        'upper_frequency': '',
        'freq_exact_match': '',
        'bandwidth_from': '',
        'emission_designator': '',
        'tolerance_from': '',
        'tolerance_to': '',
        'tolerance_exact_match': '',
        'power_output_from': '',
        'power_output_to': '',
        'power_exact_match': '',
        'rule_part_exact_match': '',
        'product_description': '',
        'modular_type_description': '',
        'modular_type_two': 'A',
    }


def _generic_search_headers():
    return {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': GENERIC_SEARCH_FORM_URL,
    }


def _submit_generic_search_form_via_playwright(fcc_id):
    global _fcc_playwright_down
    if _fcc_playwright_down:
        logger.info(
            'FCC browser fallback skipped fcc_id=%s reason=playwright_down',
            fcc_id,
        )
        return '', GENERIC_SEARCH_FORM_URL

    payload = _build_generic_search_payload(fcc_id)
    if payload is None:
        return '', GENERIC_SEARCH_FORM_URL

    browser, _pw = _get_playwright_instance()
    if browser is None:
        logger.info('FCC browser fallback unavailable fcc_id=%s reason=playwright_missing', fcc_id)
        return '', GENERIC_SEARCH_FORM_URL

    try:
        page = _new_fcc_page(browser)
        page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
        page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
        page.locator('input[name="product_code"]').fill(payload['product_code'])
        exact_match = page.locator('input[name="product_exact_match"]')
        if exact_match.is_checked():
            exact_match.uncheck()
        page.locator('input[type="submit"][value="Start Search"]').click()
        page.wait_for_load_state('networkidle', timeout=30000)

        # Wait for results table to render
        try:
            page.wait_for_selector('tbody#offTblBdy', timeout=15000)
        except Exception:
            logger.info(
                'FCC browser fallback search table wait timeout fcc_id=%s', fcc_id,
            )

        html_text = page.content()
        current_url = page.url

        # Strategy 1: Look for links in the DOM — use evaluate() for a single
        # RPC round-trip instead of N get_attribute() calls (one per link).
        exhibit_links = []
        try:
            raw_hrefs = page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    'a[href*="ViewExhibitReport.cfm"],'
                    + 'a[href*="GetTcb731Report.do"]'
                )).filter(a =>
                    a.href.includes('application_id=')
                    || a.href.includes('applicationId=')
                ).map(a => a.href).slice(0, 50)"""
            )
            for href in (raw_hrefs or []):
                if href:
                    absolute_url = urljoin(current_url, href)
                    if absolute_url not in exhibit_links:
                        exhibit_links.append(absolute_url)
                        logger.info('FCC browser found exhibit link in DOM fcc_id=%s url=%s', fcc_id, absolute_url)
        except Exception:
            logger.exception('FCC browser fallback link extraction failed fcc_id=%s', fcc_id)

        # Strategy 2: Extract application_id from page HTML using regex
        if not exhibit_links:
            app_id_pattern = r'application_id=([A-Za-z0-9%+/=]+)'
            matches = re.findall(app_id_pattern, html_text)
            if matches:
                # Use the first application_id found
                app_id = matches[0]
                constructed_url = (
                    f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500"
                    f"&calledFromFrame=N&application_id={app_id}&fcc_id={fcc_id}"
                )
                exhibit_links.append(constructed_url)
                logger.info(
                    'FCC browser extracted application_id from HTML fcc_id=%s app_id=%s',
                    fcc_id, app_id[:20],
                )

        # Strategy 3: Try clicking on the first row to trigger navigation
        if not exhibit_links:
            try:
                first_link = page.query_selector('tbody#offTblBdy tr td a')
                if first_link:
                    href = first_link.get_attribute('href')
                    if href:
                        absolute_url = urljoin(current_url, href)
                        if 'ViewExhibitReport.cfm' in absolute_url:
                            exhibit_links.append(absolute_url)
                            logger.info('FCC browser found exhibit link via first row fcc_id=%s', fcc_id)
            except Exception:
                logger.info(
                    'FCC browser first row click attempt failed fcc_id=%s', fcc_id,
                )

        # --- Background: FCC grantee/product code rules -------------------------
        # FCC IDs beginning with a digit have a 5-character grantee code;
        # those beginning with a letter have a 3-character grantee code.
        # split_fcc_id() applies these rules and always returns the product code
        # WITHOUT the separator dash (e.g. grantee='2AJGM', product='UV5RPRO').
        # However the FCC's own search database sometimes stores the product code
        # WITH a leading dash (e.g. product_code='-UV5RPRO').  Searching without
        # the dash returns "no applications on file" while searching with the dash
        # returns the correct record.  Strategy 4a and Strategy 5 both cover this
        # by retrying with f"-{product_code}" whenever the target FCC ID is not
        # found among the collected exhibit links.
        # -----------------------------------------------------------------------

        # Strategy 4a: Hyphen-prefixed product code — fast targeted retry.
        # When the initial search found no exhibit links, try the same grantee
        # with the product code prefixed by '-' before falling back to the much
        # more expensive grantee-wide search.  This covers radios like
        # 2AJGM-UV5RPRO where the FCC stores the product code as '-UV5RPRO'.
        if not exhibit_links and not payload['product_code'].startswith('-'):
            hyphen_product_code_4a = f"-{payload['product_code']}"
            logger.info(
                'FCC browser fallback hyphen-product early retry fcc_id=%s product_code=%s',
                fcc_id, hyphen_product_code_4a,
            )
            try:
                page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                page.locator('input[name="product_code"]').fill(hyphen_product_code_4a)
                hp_exact_4a = page.locator('input[name="product_exact_match"]')
                if hp_exact_4a.is_checked():
                    hp_exact_4a.uncheck()
                page.locator('input[type="submit"][value="Start Search"]').click()
                page.wait_for_load_state('networkidle', timeout=30000)
                try:
                    page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                except Exception:
                    pass
                hp_html_4a = page.content()
                hp_url_4a = page.url
                fcc_id_upper_4a = (fcc_id or '').upper()
                if fcc_id_upper_4a in (hp_html_4a or '').upper():
                    logger.info(
                        'FCC browser hyphen-product early retry found target fcc_id=%s',
                        fcc_id,
                    )
                    html_text = hp_html_4a
                    current_url = hp_url_4a
                    raw_hrefs = page.evaluate(
                        """() => Array.from(document.querySelectorAll(
                            'a[href*="ViewExhibitReport.cfm"],'
                            + 'a[href*="GetTcb731Report.do"]'
                        )).filter(a =>
                            a.href.includes('application_id=')
                            || a.href.includes('applicationId=')
                        ).map(a => a.href).slice(0, 20)"""
                    )
                    for href in (raw_hrefs or []):
                        if href:
                            absolute_url = urljoin(hp_url_4a, href)
                            if absolute_url not in exhibit_links:
                                exhibit_links.append(absolute_url)
                else:
                    logger.info(
                        'FCC browser hyphen-product early retry no match '
                        'fcc_id=%s — falling back to grantee-only',
                        fcc_id,
                    )
            except Exception:
                logger.exception('FCC browser hyphen-product early retry failed fcc_id=%s', fcc_id)

        # Strategy 4b: Grantee-only retry — when the product_code search (both
        # normal and hyphen-prefix) returns no matching exhibit links, search by
        # grantee code alone so the FCC returns ALL records for this grantee.
        if not exhibit_links and 'no applications on file' in html_text.lower():
            logger.info(
                'FCC browser fallback grantee-only retry fcc_id=%s grantee=%s',
                fcc_id, payload['grantee_code'],
            )
            try:
                page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                grantee_exact = page.locator('input[name="product_exact_match"]')
                if grantee_exact.is_checked():
                    grantee_exact.uncheck()
                page.locator('input[type="submit"][value="Start Search"]').click()
                page.wait_for_load_state('networkidle', timeout=30000)
                try:
                    page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                except Exception:
                    logger.info('FCC browser grantee-only table wait timeout fcc_id=%s', fcc_id)
                html_text = page.content()
                current_url = page.url
                logger.info(
                    'FCC browser grantee-only results fcc_id=%s url=%s has_table=%s',
                    fcc_id, current_url, 'offTblBdy' in html_text,
                )
                try:
                    raw_hrefs = page.evaluate(
                        """() => Array.from(document.querySelectorAll(
                            'a[href*="ViewExhibitReport.cfm"],'
                            + 'a[href*="GetTcb731Report.do"]'
                        )).filter(a =>
                            a.href.includes('application_id=')
                            || a.href.includes('applicationId=')
                        ).map(a => a.href).slice(0, 50)"""
                    )
                    for href in (raw_hrefs or []):
                        if href:
                            absolute_url = urljoin(current_url, href)
                            if absolute_url not in exhibit_links:
                                exhibit_links.append(absolute_url)
                except Exception:
                    logger.exception('FCC browser grantee-only link extraction failed fcc_id=%s', fcc_id)
            except Exception:
                logger.exception('FCC browser grantee-only retry failed fcc_id=%s', fcc_id)

        # Strategy 5: Hyphen-prefixed product code retry after grantee-only
        fcc_id_upper = (fcc_id or '').upper()
        target_link_found = any(fcc_id_upper in (lnk or '').upper() for lnk in exhibit_links)
        if not target_link_found and not payload['product_code'].startswith('-'):
            hyphen_product_code = f"-{payload['product_code']}"
            logger.info(
                'FCC browser fallback hyphen-product retry fcc_id=%s product_code=%s',
                fcc_id, hyphen_product_code,
            )
            try:
                page.goto(GENERIC_SEARCH_FORM_URL, wait_until='domcontentloaded', timeout=30000)
                page.locator('input[name="grantee_code"]').fill(payload['grantee_code'])
                page.locator('input[name="product_code"]').fill(hyphen_product_code)
                hyphen_exact = page.locator('input[name="product_exact_match"]')
                if hyphen_exact.is_checked():
                    hyphen_exact.uncheck()
                page.locator('input[type="submit"][value="Start Search"]').click()
                page.wait_for_load_state('networkidle', timeout=30000)
                try:
                    page.wait_for_selector('tbody#offTblBdy', timeout=15000)
                except Exception:
                    logger.info(
                        'FCC browser hyphen-product table wait timeout fcc_id=%s', fcc_id,
                    )
                hyphen_html = page.content()
                hyphen_url = page.url
                fcc_id_upper = (fcc_id or '').upper()
                logger.info(
                    'FCC browser hyphen-product results fcc_id=%s has_target=%s',
                    fcc_id, fcc_id_upper in (hyphen_html or '').upper(),
                )
                if fcc_id_upper in (hyphen_html or '').upper():
                    html_text = hyphen_html
                    current_url = hyphen_url
                    try:
                        raw_hrefs = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'a[href*="ViewExhibitReport.cfm"],'
                                + 'a[href*="GetTcb731Report.do"]'
                            )).filter(a =>
                                a.href.includes('application_id=')
                                || a.href.includes('applicationId=')
                            ).map(a => a.href).slice(0, 20)"""
                        )
                        for href in (raw_hrefs or []):
                            if href:
                                absolute_url = urljoin(hyphen_url, href)
                                if absolute_url not in exhibit_links:
                                    exhibit_links.insert(0, absolute_url)
                                    logger.info(
                                        'FCC browser hyphen-product exhibit link fcc_id=%s url=%s',
                                        fcc_id, absolute_url,
                                    )
                    except Exception:
                        logger.exception('FCC browser hyphen-product link extraction failed fcc_id=%s', fcc_id)
            except Exception:
                logger.exception('FCC browser hyphen-product retry failed fcc_id=%s', fcc_id)

        page.close()
        # Stash the Playwright-collected links so the TCB app ID extraction
        # code can also use them (they include GetTcb731Report.do links with
        # applicationId= that exhibit URLs alone don't capture).
        _pw_exhibit_links_cache[fcc_id] = list(exhibit_links)
        logger.info(
            'FCC browser fallback search success fcc_id=%s url=%s has_detail=%s exhibit_links=%s',
            fcc_id, current_url, 'ViewExhibitReport.cfm' in html_text, len(exhibit_links),
        )
        return html_text, current_url
    except Exception as exc:
        if _is_playwright_timeout_error(exc):
            _fcc_playwright_down = True
            logger.warning(
                'FCC browser fallback search timeout fcc_id=%s — setting playwright_down flag',
                fcc_id,
            )
        else:
            logger.exception('FCC browser fallback search failed fcc_id=%s', fcc_id)
        return '', GENERIC_SEARCH_FORM_URL


def discover_new_grantees_from_fcc(start_date, end_date):
    """Find grantee codes not yet in the Brand table.

    Phase 1: scans the local Radio table for FCC IDs with unknown
    grantee prefixes (instant, reliable).

    Phase 2: submits the FCC GenericSearch form via Playwright with
    only a date range (no other filters), then scrapes the results
    for entirely new grantee codes that have never appeared in any
    radio record.

    Args:
        start_date: datetime or date for the start of the search window.
        end_date:   datetime or date for the end of the search window.

    Returns:
        Set of grantee code strings (uppercase) discovered.
    """
    known_codes = set(
        Brand.objects.exclude(grantee_code__isnull=True)
        .exclude(grantee_code='')
        .values_list('grantee_code', flat=True)
    )
    known_codes = {c.strip().upper() for c in known_codes}
    ignored_codes = set(IgnoredGrantee.ignored_codes())
    skipped_codes = set(SyncSkippedGrantee.skipped_codes())
    excluded = known_codes | ignored_codes | skipped_codes

    sd = start_date if hasattr(start_date, 'strftime') else start_date
    ed = end_date if hasattr(end_date, 'strftime') else end_date
    date_from = sd.strftime('%m/%d/%Y') if hasattr(sd, 'strftime') else str(sd)
    date_to = ed.strftime('%m/%d/%Y') if hasattr(ed, 'strftime') else str(ed)

    logger.info(
        "Grantee discovery start known=%d date_from=%s date_to=%s",
        len(known_codes), date_from, date_to,
    )

    discovered = set()

    # ── Phase 1: scan local radio FCC IDs for unknown prefixes ──
    candidates = Counter()
    for radio in Radio.objects.exclude(fcc_id='').exclude(
        fcc_id__isnull=True,
    ).iterator():
        try:
            grantee, _product = split_fcc_id(
                radio.fcc_id.strip().upper(),
            )
            if (
                grantee
                and grantee not in excluded
                and _validate_grantee_code(grantee)
            ):
                candidates[grantee] += 1
        except (ValueError, IndexError):
            continue

    if candidates:
        local = set(candidates.keys())
        discovered.update(local)
        logger.info(
            "Grantee discovery local found=%d codes=%s",
            len(local), sorted(local)[:20],
        )
        excluded.update(local)

    # ── Phase 2: HTTP POST to FCC GenericSearch, fetch XML export ──
    # Uses curl_cffi with Chrome impersonation to POST the date range
    # to the FCC GenericSearch form, then fetches the XML export.
    #
    # NOTE: The FCC server has a hardcoded ~500ms RequestTimeout that
    # frequently blocks programmatic date-range queries.  When this
    # happens, Phase 2 silently returns no results.  Use the
    # discover_grantees --xml-file command to import from a manually
    # saved FCC XML export as a reliable alternative.
    if start_date is not None and end_date is not None:
        try:
            session = requests.Session()
            # Step 1: GET the form page to obtain session cookies
            _fcc_request_with_retry(
                'get', GENERIC_SEARCH_FORM_URL,
                session=session,
                impersonate='chrome124',
                timeout=15,
            )
            # Step 2: POST the date range to trigger the search
            post_data = {
                'grant_date_from': date_from,
                'grant_date_to': date_to,
                'submit': 'Start Search',
            }
            search_resp = _fcc_request_with_retry(
                'post', GENERIC_SEARCH_FORM_URL,
                session=session,
                data=post_data,
                impersonate='chrome124',
                timeout=30,
                headers={
                    'Referer': GENERIC_SEARCH_FORM_URL,
                },
            )
            # Step 3: Fetch XML export from the results page
            xml_url = GENERIC_SEARCH_URL + '?export=xml'
            xml_resp = _fcc_request_with_retry(
                'get', xml_url,
                session=session,
                impersonate='chrome124',
                timeout=30,
                headers={
                    'Referer': GENERIC_SEARCH_URL,
                },
            )
            xml_text = xml_resp.text
            html_text = search_resp.text
            timed_out = 'RequestTimeout' in (html_text or '')

            if xml_text and '<Row>' in xml_text:
                fcc_grantees = _parse_grantees_from_xml(
                    xml_text, excluded,
                )
                if fcc_grantees:
                    logger.info(
                        "Grantee discovery HTTP XML found=%d",
                        len(fcc_grantees),
                    )
                    discovered.update(fcc_grantees)
            elif html_text and 'offTblBdy' in html_text:
                fcc_grantees = _parse_grantees_from_html(
                    html_text, excluded,
                )
                if fcc_grantees:
                    logger.info(
                        "Grantee discovery HTTP HTML found=%d",
                        len(fcc_grantees),
                    )
                    discovered.update(fcc_grantees)
            elif timed_out:
                logger.info(
                    "Grantee discovery HTTP timed out by FCC server "
                    "(RequestTimeout) date_from=%s date_to=%s",
                    date_from, date_to,
                )
            else:
                logger.info(
                    "Grantee discovery HTTP no results "
                    "date_from=%s date_to=%s "
                    "xml_status=%s html_len=%s",
                    date_from, date_to,
                    xml_resp.status_code, len(html_text or ''),
                )
        except _FCCConnectionDownError:
            logger.warning(
                "Grantee discovery HTTP skipped — FCC connection down",
            )
        except Exception:
            logger.warning(
                "Grantee discovery HTTP failed", exc_info=True,
            )

    return discovered


def _parse_grantees_from_xml(xml_text, excluded_codes):
    """Extract unique grantee codes from the FCC GenericSearch XML export.

    The XML uses ``<Row>`` elements with ``<fcc_id>`` containing the
    full FCC ID from which we extract the grantee code prefix.
    """
    if not xml_text:
        return set()

    discovered = set()
    try:
        soup = BeautifulSoup(xml_text, 'xml')
        rows = soup.find_all('Row')
        if not rows:
            rows = soup.find_all('row')
        for row in rows:
            fcc_elem = row.find('fcc_id')
            if not fcc_elem or not fcc_elem.string:
                continue
            fcc_id = fcc_elem.string.strip().upper()
            grantee_code, _product_code = split_fcc_id(fcc_id)
            if (
                grantee_code
                and _validate_grantee_code(grantee_code)
                and grantee_code not in excluded_codes
            ):
                discovered.add(grantee_code)
    except Exception:
        logger.warning("Grantee discovery XML parse failed", exc_info=True)

    return discovered


def _parse_grantees_from_html(html_text, excluded_codes):
    """Extract unique grantee codes from the GenericSearch results table.

    The FCC GenericSearch results table has ``tbody#offTblBdy`` with
    rows containing the grantee code in the first column.
    """
    if not html_text:
        return set()

    discovered = set()
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        table = soup.find('tbody', id='offTblBdy')
        if not table:
            return set()
        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if not cells:
                continue
            cell_text = cells[0].get_text(' ', strip=True)
            grantee_code, _product_code = split_fcc_id(cell_text)
            if (
                grantee_code
                and _validate_grantee_code(grantee_code)
                and grantee_code not in excluded_codes
            ):
                discovered.add(grantee_code)
    except Exception:
        logger.warning("Grantee discovery HTML parse failed", exc_info=True)

    return discovered


def _get_playwright_instance():
    """Return a per-thread Playwright browser, reusing across calls in the same thread.

    Playwright's sync API uses greenlets which are thread-affine — sharing
    a browser across threads causes greenlet.error.  Each calling thread gets
    its own browser on first use, reused for subsequent calls.  Must be paired
    with a call to _close_playwright_instance() when the thread is done.
    """
    browser = getattr(_playwright_local, 'browser', None)
    pw = getattr(_playwright_local, 'playwright', None)
    if browser is not None:
        return browser, pw

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None

    pw = sync_playwright().start()
    browser = _launch_fcc_playwright_browser(pw)
    _playwright_local.playwright = pw
    _playwright_local.browser = browser
    logger.info('FCC Playwright browser pool created (thread=%s)', _threading.current_thread().name)
    return browser, pw


def _close_playwright_instance():
    """Close this thread's Playwright browser and stop the instance."""
    browser = getattr(_playwright_local, 'browser', None)
    pw = getattr(_playwright_local, 'playwright', None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
        _playwright_local.browser = None
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
        _playwright_local.playwright = None


def _launch_fcc_playwright_browser(playwright):
    headless_env = os.environ.get('FCC_PLAYWRIGHT_HEADLESS')
    # Default to headless so browser windows never steal focus during background syncs.
    # Set FCC_PLAYWRIGHT_HEADLESS=0 in the environment to show the browser for debugging.
    preferred_headless = True
    if headless_env is not None:
        preferred_headless = headless_env.strip().lower() not in {'0', 'false', 'no', 'off'}

    # Anti-bot-detection args: remove the AutomationControlled flag that FCC's server
    # uses to detect and stall headless browsers, causing networkidle timeouts.
    stealth_args = [
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
    ]
    launch_attempts = [
        {'channel': 'chrome', 'headless': preferred_headless, 'args': stealth_args + ['--disable-http2']},
        {'channel': 'chrome', 'headless': preferred_headless, 'args': stealth_args},
        {'headless': preferred_headless, 'args': stealth_args + ['--disable-http2']},
        {'headless': preferred_headless, 'args': stealth_args},
        {'headless': preferred_headless},
    ]
    last_error = None
    for launch_kwargs in launch_attempts:
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            last_error = exc
            logger.info('FCC browser launch attempt failed kwargs=%s error=%s', launch_kwargs, exc)
    if last_error is not None:
        raise last_error
    raise RuntimeError('Unable to launch FCC browser fallback')


def _new_fcc_page(browser):
    """Create a new browser page with anti-bot-detection context settings.

    FCC's CFML server detects headless Chrome via navigator.webdriver and stalls
    the connection, causing networkidle timeouts.  Overriding the property in every
    new document (add_init_script) and using a realistic viewport/user-agent reduces
    the chance of being fingerprinted as an automated browser.
    """
    context = browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        viewport={'width': 1280, 'height': 800},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context.new_page()


def _submit_generic_search_form(fcc_id):
    payload = _build_generic_search_payload(fcc_id)
    if payload is None:
        return '', GENERIC_SEARCH_FORM_URL

    headers = _generic_search_headers()
    session = requests.Session()

    try:
        _fcc_request_with_retry(
            'get',
            GENERIC_SEARCH_FORM_URL,
            session=session,
            headers=headers,
            impersonate='chrome124',
            timeout=20,
        )
    except _FCCConnectionDownError:
        logger.info('FCC generic search form seed skipped fcc_id=%s reason=connection_down', fcc_id)
    except Exception:
        logger.exception('FCC generic search form seed failed fcc_id=%s', fcc_id)

    try:
        response = _fcc_request_with_retry(
            'post',
            GENERIC_SEARCH_URL,
            session=session,
            data=payload,
            headers=headers,
            impersonate='chrome124',
            timeout=20,
        )
    except _FCCConnectionDownError:
        logger.info('FCC generic search form submit skipped fcc_id=%s reason=connection_down — falling back to Playwright', fcc_id)
        return _submit_generic_search_form_via_playwright(fcc_id)
    except Exception:
        logger.exception('FCC generic search form submit failed fcc_id=%s', fcc_id)
        return _submit_generic_search_form_via_playwright(fcc_id)

    if response.status_code != 200:
        logger.info(
            'FCC generic search form non-200 fcc_id=%s status=%s',
            fcc_id,
            response.status_code,
        )
        return _submit_generic_search_form_via_playwright(fcc_id)

    if 'ViewExhibitReport.cfm' not in (response.text or ''):
        body_text = ' '.join((response.text or '').split())[:220]
        logger.info(
            'FCC generic search form returned no detail link fcc_id=%s body_snippet=%s',
            fcc_id,
            body_text,
        )
        # Before falling back to the browser, try the hyphen-prefixed product code.
        # The FCC database stores some product codes with a leading '-' (e.g.
        # '-UV5RPRO'), so a search with 'UV5RPRO' returns nothing while '-UV5RPRO'
        # returns the correct record.  split_fcc_id() always strips the separator
        # dash from the product, so we must explicitly retry with the dash here.
        if not payload['product_code'].startswith('-'):
            hyphen_payload = dict(payload, product_code=f"-{payload['product_code']}")
            try:
                hyphen_response = _fcc_request_with_retry(
                    'post',
                    GENERIC_SEARCH_URL,
                    session=session,
                    data=hyphen_payload,
                    headers=headers,
                    impersonate='chrome124',
                    timeout=20,
                )
                if hyphen_response.status_code == 200 and 'ViewExhibitReport.cfm' in (hyphen_response.text or ''):
                    logger.info(
                        'FCC generic search form hyphen-product found detail link fcc_id=%s',
                        fcc_id,
                    )
                    return hyphen_response.text, GENERIC_SEARCH_FORM_URL
            except Exception:
                pass  # fall through to browser
        browser_html, browser_url = _submit_generic_search_form_via_playwright(fcc_id)
        if browser_html:
            return browser_html, browser_url

    return response.text or '', GENERIC_SEARCH_FORM_URL


def _fetch_oet_documents_via_playwright(fcc_id, candidate_urls=None, _allow_grantee_fallback=True):
    if _fcc_playwright_down:
        logger.info(
            'FCC browser OET skipped fcc_id=%s reason=playwright_down',
            fcc_id,
        )
        return []

    browser, _pw = _get_playwright_instance()
    if browser is None:
        logger.info('FCC browser fallback unavailable for OET docs fcc_id=%s reason=playwright_missing', fcc_id)
        return []

    detail_urls = []
    seen = set()
    for url in candidate_urls or []:
        if isinstance(url, str) and url.strip() and url not in seen:
            seen.add(url)
            detail_urls.append(url)

    if not detail_urls:
        html_text, base_url = _submit_generic_search_form_via_playwright(fcc_id)
        if html_text:
            parsed = _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id)
            for url in parsed.get('candidate_exhibit_urls', []):
                if url not in seen:
                    seen.add(url)
                    detail_urls.append(url)

        if not detail_urls:
            # Generic search form could not produce exhibit URLs.
            # Fall back to navigating the browser directly to the ViewExhibitReport
            # URL variants — the browser bypasses the 503 that blocks curl_cffi.
            for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
                direct_url = (
                    f"{OET_EXHIBITS_URL}?mode=Exhibits"
                    f"&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
                )
                if direct_url not in seen:
                    seen.add(direct_url)
                    detail_urls.append(direct_url)
            logger.info(
                'FCC browser OET falling back to direct ViewExhibitReport navigation fcc_id=%s url_count=%s',
                fcc_id, len(detail_urls),
            )
        else:
            logger.info('FCC browser fallback extracted exhibit URLs fcc_id=%s url_count=%s', fcc_id, len(detail_urls))

    if not detail_urls:
        return []

    try:
        page = _new_fcc_page(browser)
        consecutive_timeouts = 0
        attachment_only_fallback = []
        for url in detail_urls[:10]:
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                consecutive_timeouts = 0
                html_text = page.content()
                current_url = page.url

                # If the page redirected to a ViewExhibitReport with application_id, capture it
                if 'application_id=' in current_url and current_url not in seen:
                    logger.info(
                        'FCC browser OET redirect captured fcc_id=%s redirect_url=%s',
                        fcc_id, current_url,
                    )

                documents = _extract_oet_documents_from_html(html_text, base_url=current_url)
                documents += _extract_oet_documents_from_attachment_html(html_text, base_url=current_url)

                # If no documents found yet, give JS a short window to finish rendering
                # before reading content a second time (avoids full 10s networkidle wait).
                if not documents:
                    try:
                        page.wait_for_load_state('networkidle', timeout=3000)
                    except Exception:
                        pass
                    html_text = page.content()
                    current_url = page.url
                    documents = _extract_oet_documents_from_html(html_text, base_url=current_url)
                    documents += _extract_oet_documents_from_attachment_html(html_text, base_url=current_url)

                if documents:
                    # Verify the exhibit page actually references the target FCC ID.
                    html_upper = (html_text or '').upper()
                    fcc_id_upper = (fcc_id or '').upper()
                    target_fcc_key = _extract_fcc_key(fcc_id)
                    fcc_id_in_page = (
                        fcc_id_upper in html_upper or
                        (target_fcc_key and target_fcc_key in html_upper)
                    )
                    if not fcc_id_in_page:
                        logger.warning(
                            'FCC exhibit page fcc_id_mismatch: target=%s not found in page HTML '
                            'page_url=%s — rejecting %s docs to prevent cross-contamination',
                            fcc_id, current_url, len(documents),
                        )
                        documents = []

                if documents:
                    has_metadata = any(d.get('exhibit_type') for d in documents)
                    if has_metadata:
                        page.close()
                        logger.info(
                            'FCC browser fallback OET success fcc_id=%s url=%s document_count=%s',
                            fcc_id, url, len(documents),
                        )
                        return documents
                    attachment_only_fallback = list(documents)
                    logger.info(
                        'FCC browser OET attachment-only docs found fcc_id=%s url=%s count=%s — '
                        'trying application_id link for full metadata',
                        fcc_id, url, len(attachment_only_fallback),
                    )
                else:
                    attachment_only_fallback = []

                # If page itself contains an application_id link but no documents table yet,
                # try following that link within the same browser session
                app_links = page.query_selector_all(
                    'a[href*="ViewExhibitReport.cfm"][href*="application_id="]',
                )
                for link in app_links[:5]:
                    href = link.get_attribute('href')
                    if not href:
                        continue
                    abs_href = urljoin(current_url, href)
                    if abs_href in seen:
                        continue
                    seen.add(abs_href)
                    logger.info(
                        'FCC browser OET following application_id link fcc_id=%s url=%s',
                        fcc_id, abs_href,
                    )
                    page.goto(abs_href, wait_until='domcontentloaded', timeout=30000)
                    inner_html = page.content()
                    inner_url = page.url
                    docs = _extract_oet_documents_from_html(inner_html, base_url=inner_url)
                    docs += _extract_oet_documents_from_attachment_html(inner_html, base_url=inner_url)
                    if not docs:
                        try:
                            page.wait_for_load_state('networkidle', timeout=3000)
                        except Exception:
                            pass
                        inner_html = page.content()
                        inner_url = page.url
                        docs = _extract_oet_documents_from_html(inner_html, base_url=inner_url)
                        docs += _extract_oet_documents_from_attachment_html(inner_html, base_url=inner_url)
                    if docs:
                        inner_upper = (inner_html or '').upper()
                        fcc_id_upper = (fcc_id or '').upper()
                        target_fcc_key = _extract_fcc_key(fcc_id)
                        fcc_id_in_inner = (
                            fcc_id_upper in inner_upper or
                            (target_fcc_key and target_fcc_key in inner_upper)
                        )
                        if not fcc_id_in_inner:
                            logger.warning(
                                'FCC OET inner link fcc_id_mismatch: target=%s not in page HTML '
                                'inner_url=%s — rejecting %s docs to prevent cross-contamination',
                                fcc_id, inner_url, len(docs),
                            )
                            docs = []
                    if docs:
                        page.close()
                        logger.info(
                            'FCC browser OET application_id link success fcc_id=%s url=%s document_count=%s',
                            fcc_id, abs_href, len(docs),
                        )
                        return docs

                # app_links exhausted — if we had attachment-only docs, use as last resort
                if attachment_only_fallback:
                    page.close()
                    logger.info(
                        'FCC browser OET using attachment-only fallback fcc_id=%s url=%s count=%s',
                        fcc_id, url, len(attachment_only_fallback),
                    )
                    return attachment_only_fallback

            except Exception as exc:
                if _is_playwright_timeout_error(exc):
                    consecutive_timeouts += 1
                    logger.warning(
                        'FCC browser OET page load timeout fcc_id=%s url=%s consecutive=%s',
                        fcc_id, url, consecutive_timeouts,
                    )
                else:
                    consecutive_timeouts = 0
                    logger.exception(
                        'FCC browser OET page load failed fcc_id=%s url=%s',
                        fcc_id, url,
                    )
                if consecutive_timeouts >= 2:
                    logger.warning(
                        'FCC browser OET circuit breaker triggered fcc_id=%s '
                        'consecutive_timeouts=%s — skipping remaining direct URLs',
                        fcc_id, consecutive_timeouts,
                    )
                    break
                continue

        page.close()
    except Exception:
        logger.exception('FCC browser fallback OET failed fcc_id=%s', fcc_id)

    # Nothing found via direct URL navigation.  Fall back to the grantee-only
    # browser search so we can discover the application_id through the FCC's
    # search results table and then navigate to the correct exhibit page.
    if not _allow_grantee_fallback:
        # Already inside a grantee-fallback attempt; don't recurse again.
        return []

    logger.info('FCC browser OET direct URLs exhausted, trying grantee search fcc_id=%s', fcc_id)
    grantee_html, grantee_base = _submit_generic_search_form_via_playwright(fcc_id)
    if grantee_html:
        parsed = _extract_secondary_metadata_from_generic_search_html(grantee_html, grantee_base, fcc_id)
        # Row-filtered candidates (highest confidence — from the target FCC ID row)
        filtered_urls = parsed.get('candidate_exhibit_urls', [])

        # Broaden to ALL ViewExhibitReport.cfm links on the grantee page.
        # For Change-in-ID grants the target FCC ID row in the search table may
        # point to a page with no parseable documents; the correct exhibit page
        # (with the real application_id) exists elsewhere on the same page.
        # FCC ID verification at navigation time (below) guards against
        # cross-contamination from other grantees' exhibit links.
        all_exhibit_urls = list(filtered_urls)
        for href_raw in re.findall(
            r'href=["\']([^"\']*ViewExhibitReport\.cfm[^"\']*application_id=[^"\']+)["\']',
            grantee_html, re.IGNORECASE,
        ):
            abs_url = unescape(urljoin(grantee_base, href_raw.strip()))
            if abs_url not in all_exhibit_urls:
                all_exhibit_urls.append(abs_url)

        # Last-resort: direct fcc_id navigation URLs (no application_id required).
        # For Change-in-ID applications the exhibit page application_id may not
        # appear anywhere on the grantee search results page, but the FCC server
        # will resolve ?fcc_id=<target> to the correct exhibit list.  These are
        # added at the end so they are only reached when no application_id link
        # in the search table matched.
        for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
            direct_url = (
                f"{OET_EXHIBITS_URL}?mode=Exhibits"
                f"&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
            )
            if direct_url not in all_exhibit_urls:
                all_exhibit_urls.append(direct_url)

        if all_exhibit_urls:
            logger.info(
                'FCC OET grantee fallback candidate_count=%s (filtered=%s all=%s direct=%s) fcc_id=%s',
                len(all_exhibit_urls), len(filtered_urls), len(all_exhibit_urls),
                sum(1 for u in all_exhibit_urls if 'application_id' not in u),
                fcc_id,
            )
            return _fetch_oet_documents_via_playwright(
                fcc_id,
                candidate_urls=all_exhibit_urls,
                _allow_grantee_fallback=False,  # prevent re-entrant grantee search
            )

    return []


def _fetch_secondary_metadata_from_html_fallback(fcc_id, _params):
    # Skip all network calls if both HTTP and Playwright are known-down.
    if _fcc_connection_down and _fcc_playwright_down:
        logger.info(
            'FCC secondary metadata html fallback skipped fcc_id=%s reason=connection_and_playwright_down',
            fcc_id,
        )
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
            'rule_parts': [],
        }

    html_text, base_url = _submit_generic_search_form(fcc_id)
    if not html_text:
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
            'rule_parts': [],
        }

    parsed = _extract_secondary_metadata_from_generic_search_html(html_text, base_url, fcc_id)
    oet_documents = _fetch_oet_documents_from_html(
        fcc_id,
        candidate_urls=parsed.get('candidate_exhibit_urls', []),
    )

    # Try the TCB Form 731 Report for additional rule parts.
    # Merge with the HTML-parsed rule parts — do NOT replace them.
    # A single FCC ID can have multiple grants, each with different
    # rule parts (e.g. 15B for receiver, 95E for transmitter).
    rule_parts = list(parsed.get('rule_parts', []))
    candidate_urls = parsed.get('candidate_exhibit_urls', [])

    # Collect application IDs from two sources:
    # 1. TCB Form 731 links (cells[1]) — most direct, uses applicationId=
    # 2. ViewExhibitReport.cfm links (cells[2]) — uses application_id=
    # 3. Playwright-collected links (cached from DOM scrape) — includes both
    tcb_app_ids = list(parsed.get('tcb_application_ids', []))
    url_app_ids = _extract_application_id_from_urls(candidate_urls)
    for app_id in url_app_ids:
        if app_id not in tcb_app_ids:
            tcb_app_ids.append(app_id)
    # Also pull in application IDs from Playwright-collected links (TCB
    # 731 + exhibit links scraped directly from the DOM).
    pw_links = _pw_exhibit_links_cache.pop(fcc_id, [])
    if pw_links:
        pw_app_ids = _extract_application_id_from_urls(pw_links)
        for app_id in pw_app_ids:
            if app_id not in tcb_app_ids:
                tcb_app_ids.append(app_id)
        logger.info(
            "FCC TCB pw link cache consumed fcc_id=%s pw_link_count=%s "
            "pw_app_ids=%s merged_total=%s",
            fcc_id, len(pw_links),
            [aid[:20] for aid in pw_app_ids],
            len(tcb_app_ids),
        )

    logger.info(
        "FCC TCB collecting app IDs (html fallback) fcc_id=%s "
        "direct_tcb=%s url_extracted=%s total=%s ids=%s",
        fcc_id,
        len(parsed.get('tcb_application_ids', [])),
        len(url_app_ids),
        len(tcb_app_ids),
        [aid[:20] for aid in tcb_app_ids],
    )
    orig_fcc_id_from_tcb = ''
    tcb_oe_rows = []
    for tcb_app_id in tcb_app_ids:
        tcb_result = _fetch_rule_parts_from_tcb_report(fcc_id, tcb_app_id)
        tcb_rule_parts = tcb_result.get('rule_parts', [])
        tcb_orig_fcc_id = tcb_result.get('original_fcc_id', '')
        tcb_freq_rows = tcb_result.get('oe_rows', [])
        if tcb_rule_parts:
            for part in tcb_rule_parts:
                if part not in rule_parts:
                    rule_parts.append(part)
            logger.info(
                "FCC TCB rule parts found (html fallback) fcc_id=%s "
                "app_id=%s rule_parts=%s freq_rows=%s",
                fcc_id, tcb_app_id[:20], tcb_rule_parts, len(tcb_freq_rows),
            )
        else:
            logger.info(
                "FCC TCB empty result (html fallback) fcc_id=%s app_id=%s",
                fcc_id, tcb_app_id[:20],
            )
        if tcb_orig_fcc_id:
            orig_fcc_id_from_tcb = tcb_orig_fcc_id
        if tcb_freq_rows:
            tcb_oe_rows.extend(tcb_freq_rows)

    # Merge TCB frequency rows into OE rows
    parsed_oe = list(parsed.get('original_equipment_rows', []))
    if tcb_oe_rows:
        parsed_oe.extend(tcb_oe_rows)
        logger.info(
            "FCC TCB frequency rows merged fcc_id=%s tcb_row_count=%s",
            fcc_id, len(tcb_oe_rows),
        )

    return {
        'record_count': parsed.get('record_count', 0),
        'text_blob': parsed.get('text_blob', ''),
        'matched_keys': parsed.get('matched_keys', []),
        'test_report_candidates': [],
        'original_equipment_rows': parsed_oe,
        'oet_documents': oet_documents,
        'rule_parts': rule_parts,
        'application_id': (
            tcb_app_ids[0] if tcb_app_ids
            else ''
        ),
    }


def _fetch_oet_documents_from_html(fcc_id, candidate_urls=None):
    # Collect candidate URLs (from metadata parse) plus direct fcc_id variants.
    # curl_cffi gets 503 from all CFML endpoints (ViewExhibitReport.cfm,
    # GenericSearchResult.cfm) so we skip the HTTP loop entirely and hand
    # everything straight to the Playwright path, which uses a real browser.
    seen = set()
    deduped_urls = []
    for url in list(candidate_urls or []):
        if isinstance(url, str) and url.strip() and url not in seen:
            seen.add(url)
            deduped_urls.append(url.strip())

    for lookup_fcc_id in _fcc_lookup_variants(fcc_id):
        url = f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500&calledFromFrame=N&fcc_id={lookup_fcc_id}"
        if url not in seen:
            seen.add(url)
            deduped_urls.append(url)

    logger.info(
        "FCC OET fetch start fcc_id=%s candidate_url_count=%s",
        fcc_id,
        len(deduped_urls),
    )

    browser_docs = _fetch_oet_documents_via_playwright(fcc_id, candidate_urls=deduped_urls)
    if browser_docs:
        logger.info(
            'FCC OET browser fallback success fcc_id=%s document_count=%s',
            fcc_id,
            len(browser_docs),
        )
        return browser_docs

    return []


def _fetch_oet_documents_via_generic_search_form(fcc_id):
    html_text, base_url = _submit_generic_search_form(fcc_id)
    if not html_text:
        return []

    soup = BeautifulSoup(html_text, 'html.parser')

    detail_urls = []
    direct_docs = []
    seen_detail = set()
    seen_direct = set()
    for link in soup.find_all('a', href=True):
        href = str(link.get('href') or '').strip()
        if not href:
            continue

        lower_href = href.lower()
        absolute = urljoin(base_url, href)
        if 'viewattachment.cfm' in lower_href or 'viewexhibitreport.cfm' in lower_href:
            if absolute not in seen_detail:
                seen_detail.add(absolute)
                detail_urls.append(absolute)
        elif 'getattachment.cfm' in lower_href or 'genericexhibit.cfm' in lower_href:
            if absolute not in seen_direct:
                seen_direct.add(absolute)
                direct_docs.append(
                    {
                        'view_attachment': _strip_html_tags(link.get_text(' ', strip=True) or '') or 'FCC Attachment',
                        'exhibit_type': '',
                        'date_submitted_to_fcc': '',
                        'display_type': 'pdf',
                        'date_available': '',
                        'document_url': absolute,
                    }
                )

    documents = []
    seen_docs = set()
    for doc in direct_docs:
        key = (doc.get('document_url', ''), doc.get('view_attachment', ''))
        if key in seen_docs:
            continue
        seen_docs.add(key)
        documents.append(doc)

    for detail_url in detail_urls[:20]:
        try:
            detail_response = _fcc_request_with_retry('get', detail_url, impersonate='chrome124', timeout=20)
        except Exception:
            logger.exception("FCC OET form fallback detail fetch failed fcc_id=%s url=%s", fcc_id, detail_url)
            continue

        if detail_response.status_code != 200:
            logger.info(
                "FCC OET form fallback detail non-200 fcc_id=%s status=%s url=%s",
                fcc_id,
                detail_response.status_code,
                detail_url,
            )
            continue

        extracted = _extract_oet_documents_from_html(detail_response.text or '', base_url=detail_url)
        extracted += _extract_oet_documents_from_attachment_html(detail_response.text or '', base_url=detail_url)
        for doc in extracted:
            key = (doc.get('document_url', ''), doc.get('view_attachment', ''))
            if key in seen_docs:
                continue
            seen_docs.add(key)
            documents.append(doc)

    return documents


def _is_fcc_authoritative_url(url):
    host = (urlparse(url).hostname or '').lower()
    return host.endswith('fcc.gov')


def _build_oet_document_filename(fcc_id, view_attachment, document_url, display_type):
    parsed = urlparse(document_url or '')
    path_name = Path(parsed.path).name
    stem = path_name or _safe_filename(view_attachment or '') or 'oet_document'
    stem = _safe_filename(stem) or 'oet_document'

    display = (display_type or '').strip().lower()
    display_ext = f'.{display}' if display in {'pdf', 'doc', 'docx', 'txt'} else '.pdf'

    suffix = Path(stem).suffix.lower()
    if not suffix:
        candidate = f"{stem}{display_ext}"
    elif suffix in {'.cfm', '.php', '.asp', '.aspx', '.html', '.htm'}:
        candidate = f"{Path(stem).stem}{display_ext}"
    else:
        candidate = stem

    prefix = _safe_filename(fcc_id or 'fccid') or 'fccid'
    final_name = f"{prefix}_{candidate}"
    return final_name[:220]


def _download_oet_document_bytes(url, referer_url=''):
    if not _is_fcc_authoritative_url(url):
        logger.warning("FCC OET download skipped non-authoritative url=%s", url)
        return b''

    headers = None
    if referer_url:
        headers = {
            'Referer': referer_url,
            'User-Agent': _generic_search_headers()['User-Agent'],
        }

    try:
        response = _fcc_request_with_retry('get', url, impersonate='chrome124', timeout=25, headers=headers)
    except Exception:
        logger.exception("FCC OET document download failed url=%s", url)
        return b''

    if response.status_code != 200:
        logger.info("FCC OET document download non-200 url=%s status=%s", url, response.status_code)
        return b''

    content = response.content or b''
    if not content:
        return b''

    content_prefix = content[:200].lstrip().lower()
    if (
        b'you are not authorized to access this page' in content_prefix
        or content_prefix.startswith(b'<!doctype html')
        or content_prefix.startswith(b'<html')
    ):
        logger.info('FCC OET document download rejected url=%s referer=%s', url, referer_url)
        return b''

    return content


def _classify_oet_document_type(exhibit_type, view_attachment):
    blob = ' '.join(
        part.strip().lower()
        for part in (exhibit_type or '', view_attachment or '')
        if part and part.strip()
    )
    if not blob:
        return ''

    if 'test report' in blob:
        return RadioManual.DocType.TEST_REPORT
    if 'change in identification' in blob or 'change letter' in blob:
        return RadioManual.DocType.CHANGE_IN_ID
    if 'authorization' in blob or 'grant of equipment authorization' in blob:
        return RadioManual.DocType.AUTHORIZATION
    if 'firmware' in blob:
        return RadioManual.DocType.FIRMWARE
    if 'cps' in blob or 'programming software' in blob:
        return RadioManual.DocType.CPS
    if 'manual' in blob or 'user guide' in blob or 'instruction' in blob:
        return RadioManual.DocType.MANUAL
    return ''


def _sync_manual_record_for_oet_document(radio, fcc_id, oet_doc):
    doc_type = _classify_oet_document_type(oet_doc.exhibit_type, oet_doc.view_attachment)
    if not doc_type:
        return False

    source_url = (oet_doc.document_url or '').strip()
    if not source_url and not oet_doc.document_file:
        return False

    manual_doc = RadioManual.objects.filter(radio=radio, source_url=source_url).first()
    created = False
    if manual_doc is None:
        manual_doc = RadioManual(radio=radio, source_url=source_url)
        created = True

    # When the manual already has a PDF, all the expensive work (download,
    # parse, spec backfill) was done on the first pass.  Exit early so that
    # duplicate OET document entries in the exhibit list don't trigger a
    # second PDF parse for the same record.
    if not created and manual_doc.manual_pdf:
        return False

    manual_doc.doc_type = doc_type
    manual_doc.status = RadioManual.ProcessingStatus.LINKED
    manual_doc.extraction_confidence = 1.0
    # Merge with existing extracted_data so that spec_extraction (written by
    # _backfill_radio_specs_from_manual_doc on the first pass) is preserved.
    manual_doc.extracted_data = {
        **(manual_doc.extracted_data or {}),
        'source': 'fcc_oet_document',
        'fcc_id': fcc_id,
        'oet_document_id': oet_doc.id,
        'view_attachment': oet_doc.view_attachment,
        'exhibit_type': oet_doc.exhibit_type,
    }

    if not manual_doc.manual_pdf:
        content = b''
        filename = ''

        if oet_doc.document_file:
            filename = Path(oet_doc.document_file.name).name
            oet_doc.document_file.open('rb')
            try:
                content = oet_doc.document_file.read()
            finally:
                oet_doc.document_file.close()
        elif source_url and _is_fcc_authoritative_url(source_url):
            content = _download_oet_document_bytes(source_url)
            if content:
                filename = _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=oet_doc.view_attachment,
                    document_url=source_url,
                    display_type=oet_doc.display_type,
                )

        if not (content and filename):
            return False

        manual_doc.manual_pdf.save(filename, ContentFile(content), save=False)

    manual_doc.save()
    _backfill_radio_specs_from_manual_doc(radio, manual_doc)

    logger.info(
        'FCC manual library sync radio_id=%s fcc_id=%s manual_id=%s oet_doc_id=%s doc_type=%s created=%s source_url=%s',
        getattr(radio, 'id', None),
        fcc_id,
        manual_doc.id,
        oet_doc.id,
        manual_doc.doc_type,
        created,
        source_url,
    )
    return created


def _normalize_fcc_rule_part(raw_part):
    """Normalize a raw FCC rule part string to canonical 'Part XX' format.

    Handles inputs from both the FCC API (e.g. '15B', '90', '95E') and
    parsed PDF text (e.g. 'Part 15 Subpart B', 'FCC Part 90').
    """
    part = raw_part.strip()

    # Already canonical: 'Part 15B', 'Part 90'
    if re.match(r'^Part\s+\d+[A-Za-z]*(?:\s+Subpart\s+[A-Za-z])?$', part, re.IGNORECASE):
        return part

    # Strip FCC prefix: 'FCC Part 90' → 'Part 90'
    part = re.sub(r'^FCC\s+', '', part, flags=re.IGNORECASE).strip()

    # Already has Part prefix after stripping FCC
    if re.match(r'^Part\s+\d+[A-Za-z]*(?:\s+Subpart\s+[A-Za-z])?$', part, re.IGNORECASE):
        return part

    # Raw part number: '15B', '90', '95E' → 'Part 15B', 'Part 90'
    if re.match(r'^\d+[A-Za-z]*(?:\s+Subpart\s+[A-Za-z])?$', part, re.IGNORECASE):
        return f'Part {part}'

    # Last resort: try to extract a part number from the string
    match = re.search(r'(\d+[A-Za-z]*(?:\s+Subpart\s+[A-Za-z])?)', part, re.IGNORECASE)
    if match:
        return f'Part {match.group(1).strip()}'

    return part


def _assign_service_types_from_rule_parts(radio, rule_parts):
    """Auto-assign RadioServiceTypes based on FCC rule parts.

    Args:
        radio: Radio model instance.
        rule_parts: List of FCC rule part strings from either the FCC API
                    (e.g. ['15B', '90']) or parsed from test report PDFs
                    (e.g. ['Part 15B', 'Part 90']).

    Returns:
        List of RadioServiceType names that were newly assigned.
    """
    if not rule_parts:
        return []

    RadioServiceType = None
    try:
        from radios.models import RadioServiceType  # pylint: disable=redefined-outer-name,reimported
    except ImportError:
        return []

    assigned = []
    for rule_part in rule_parts:
        normalized = _normalize_fcc_rule_part(rule_part)
        service_name = FCC_PART_TO_SERVICE_TYPE.get(normalized)
        if not service_name:
            # Try matching without "Subpart" suffix for broader lookup
            base_part = re.sub(r'\s*Subpart\s+[A-Za-z]', '', normalized).strip()
            service_name = FCC_PART_TO_SERVICE_TYPE.get(base_part)
        if not service_name:
            logger.debug(
                "FCC rule part has no mapped service type "
                "rule_part=%s normalized=%s radio_id=%s",
                rule_part,
                normalized,
                getattr(radio, 'id', None),
            )
            continue

        service_type = RadioServiceType.objects.filter(name=service_name).first()
        if not service_type:
            logger.warning(
                "Service type not found in DB name=%s rule_part=%s",
                service_name,
                normalized,
            )
            continue

        if radio.service_types.filter(pk=service_type.pk).exists():
            continue

        radio.service_types.add(service_type)
        assigned.append(service_name)
        logger.info(
            "FCC service type auto-assigned radio_id=%s fcc_id=%s "
            "rule_part=%s service_type=%s",
            getattr(radio, 'id', None),
            getattr(radio, 'fcc_id', '') or '',
            normalized,
            service_name,
        )

    return assigned


def _apply_extracted_specs_to_radio(radio, extracted_specs, source_label):
    changes = []

    field_map = {
        'freq_bands_tx': 'freq_bands_tx',
        'power_watts': 'power_watts',
        'gps': 'gps',
        'aprs': 'aprs',
        'dmr': 'dmr',
        'air_band': 'air_band',
        'cost_approx': 'cost_approx',
    }
    for extracted_key, radio_field in field_map.items():
        value = extracted_specs.get(extracted_key)
        if value and not getattr(radio, radio_field):
            setattr(radio, radio_field, value)
            changes.append(radio_field)

    battery_mah = extracted_specs.get('battery_mah')
    if battery_mah and radio.battery_mah is None:
        radio.battery_mah = battery_mah
        changes.append('battery_mah')

    if changes:
        radio.save(update_fields=changes)
        logger.info(
            'FCC spec backfill applied radio_id=%s fcc_id=%s source=%s fields=%s',
            getattr(radio, 'id', None),
            getattr(radio, 'fcc_id', '') or '',
            source_label,
            ','.join(changes),
        )

    return changes


def _extract_specs_from_saved_pdf(file_field, source_name):
    file_path = getattr(file_field, 'path', '') if file_field else ''
    if not file_path:
        return '', {}, {}

    extracted_text, extraction_meta = extract_text_from_pdf_with_metadata(file_path)
    if not extracted_text:
        return '', {}, extraction_meta

    # Strip NUL bytes; PostgreSQL rejects string literals containing 0x00.
    extracted_text = extracted_text.replace('\x00', '')
    # Strip lone surrogate code points (U+D800–U+DFFF).  PDF text extraction
    # can emit unpaired surrogates when the font encoding is corrupt; PostgreSQL
    # utf-8 rejects them with "surrogates not allowed".
    extracted_text = extracted_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
    extracted_specs = extract_specs_from_text(extracted_text, source_name=source_name)
    return extracted_text, extracted_specs, extraction_meta


def _ensure_fcc_rule_parts_in_specs(extracted_data, extracted_text):
    """Backfill fcc_rule_parts into existing spec_extraction when missing.

    Test reports processed before the fcc_rule_parts feature was added will
    have spec_extraction without fcc_rule_parts.  If extracted_text is
    available, re-parse it to extract the FCC Part and update the stored data.
    """
    specs = (extracted_data or {}).get('spec_extraction', {})
    if 'fcc_rule_parts' in specs:
        return specs.get('fcc_rule_parts', []), False

    if not extracted_text:
        return [], False

    from radios.manual_extraction import _extract_fcc_part_from_standards
    rule_parts = sorted(_extract_fcc_part_from_standards(extracted_text))
    if rule_parts:
        specs['fcc_rule_parts'] = rule_parts
        extracted_data['spec_extraction'] = specs
        return rule_parts, True

    # Mark as processed so we don't retry every time
    specs['fcc_rule_parts'] = []
    extracted_data['spec_extraction'] = specs
    return [], True


def _backfill_radio_specs_from_manual_doc(radio, manual_doc):
    if not manual_doc.manual_pdf:
        return []

    existing_specs = (manual_doc.extracted_data or {}).get('spec_extraction', {})
    if existing_specs:
        source_label = manual_doc.source_url or manual_doc.manual_pdf.name or str(manual_doc.pk)
        changes = _apply_extracted_specs_to_radio(radio, existing_specs, source_label)

        # Handle stale data: fcc_rule_parts may be missing from specs processed
        # before the feature was added.  Re-parse from stored extracted_text.
        rule_parts, updated = _ensure_fcc_rule_parts_in_specs(
            manual_doc.extracted_data, manual_doc.extracted_text,
        )
        if updated:
            manual_doc.save(update_fields=['extracted_data'])

        assigned = _assign_service_types_from_rule_parts(radio, rule_parts)
        if assigned:
            changes = changes + [f'service_type:{name}' for name in assigned]
        return changes

    source_name = manual_doc.manual_pdf.name or manual_doc.source_url or ''
    extracted_text, extracted_specs, extraction_meta = _extract_specs_from_saved_pdf(manual_doc.manual_pdf, source_name)
    if not extracted_text:
        return []

    manual_doc.extracted_text = extracted_text
    manual_doc.extracted_data = {
        **(manual_doc.extracted_data or {}),
        'spec_extraction': extracted_specs,
        'pdf_extraction': extraction_meta,
    }
    manual_doc.extraction_confidence = max(manual_doc.extraction_confidence or 0.0, 0.85)
    manual_doc.save(update_fields=['extracted_text', 'extracted_data', 'extraction_confidence'])

    changes = _apply_extracted_specs_to_radio(radio, extracted_specs, source_name)
    rule_parts = extracted_specs.get('fcc_rule_parts', [])
    assigned = _assign_service_types_from_rule_parts(radio, rule_parts)
    if assigned:
        changes = changes + [f'service_type:{name}' for name in assigned]
    return changes


def _backfill_radio_specs_from_test_report(radio, report):
    if not report.report_pdf:
        return []

    existing_specs = (report.extracted_data or {}).get('spec_extraction', {})
    if existing_specs:
        source_label = report.source_url or report.report_title or report.report_pdf.name or str(report.pk)
        changes = _apply_extracted_specs_to_radio(radio, existing_specs, source_label)

        # Handle stale data: fcc_rule_parts may be missing from specs processed
        # before the feature was added.  RadioFCCTestReport doesn't store
        # extracted_text separately, so we'd need to re-extract the PDF.
        # For now, just use what's available.
        rule_parts, _updated = _ensure_fcc_rule_parts_in_specs(
            report.extracted_data, '',
        )
        assigned = _assign_service_types_from_rule_parts(radio, rule_parts)
        if assigned:
            changes = changes + [f'service_type:{name}' for name in assigned]
        return changes

    source_name = report.report_title or report.report_pdf.name or report.source_url or ''
    _, extracted_specs, extraction_meta = _extract_specs_from_saved_pdf(report.report_pdf, source_name)
    if not extracted_specs:
        return []

    report.extracted_data = {
        **(report.extracted_data or {}),
        'spec_extraction': extracted_specs,
        'pdf_extraction': extraction_meta,
    }
    report.save(update_fields=['extracted_data'])

    changes = _apply_extracted_specs_to_radio(radio, extracted_specs, source_name)
    rule_parts = extracted_specs.get('fcc_rule_parts', [])
    assigned = _assign_service_types_from_rule_parts(radio, rule_parts)
    if assigned:
        changes = changes + [f'service_type:{name}' for name in assigned]
    return changes


def _sync_oet_documents_for_radio(radio, fcc_id, secondary_metadata, force_reload=False):
    # De-duplicate: if this FCC ID was already fully synced in the current run,
    # just copy documents from sibling radios instead of re-processing.
    fcc_id_norm = (fcc_id or '').strip().upper()
    if fcc_id_norm and fcc_id_norm in _synced_oet_fcc_ids:
        return _copy_oet_docs_between_radios(radio, fcc_id)

    documents = secondary_metadata.get('oet_documents', []) if secondary_metadata else []
    logger.info(
        "FCC OET sync start radio_id=%s radio_fcc_id=%s requested_fcc_id=%s metadata_doc_count=%s",
        getattr(radio, 'id', None),
        (getattr(radio, 'fcc_id', '') or '').strip(),
        fcc_id,
        len(documents),
    )

    if not documents:
        synced = 0
        manual_synced = 0
        copied_names = []
        existing_docs = RadioOETDocument.objects.filter(fcc_id__iexact=fcc_id).exclude(radio=radio)
        for existing in existing_docs:
            copied_doc, created = RadioOETDocument.objects.update_or_create(
                radio=radio,
                fcc_id=fcc_id,
                document_url=existing.document_url,
                view_attachment=existing.view_attachment,
                defaults={
                    'exhibit_type': existing.exhibit_type,
                    'date_submitted_to_fcc': existing.date_submitted_to_fcc,
                    'display_type': existing.display_type,
                    'date_available': existing.date_available,
                    'document_file': existing.document_file.name if existing.document_file else '',
                },
            )

            if (
                not copied_doc.document_file
                and existing.document_url
                and _is_fcc_authoritative_url(existing.document_url)
            ):
                content = _download_oet_document_bytes(existing.document_url)
                if content:
                    filename = _build_oet_document_filename(
                        fcc_id=fcc_id,
                        view_attachment=existing.view_attachment,
                        document_url=existing.document_url,
                        display_type=existing.display_type,
                    )
                    copied_doc.document_file.save(filename, ContentFile(content), save=True)
                    logger.info(
                        "FCC OET fallback document downloaded radio_id=%s fcc_id=%s oet_doc_id=%s filename=%s source_url=%s",
                        getattr(radio, 'id', None),
                        fcc_id,
                        copied_doc.id,
                        filename,
                        existing.document_url,
                    )

            if _sync_manual_record_for_oet_document(radio, fcc_id, copied_doc):
                manual_synced += 1

            if created:
                synced += 1
                copied_names.append((existing.view_attachment or existing.document_url or '').strip())
        logger.info(
            "FCC OET sync fallback copy radio_id=%s fcc_id=%s copied_count=%s manual_docs_synced=%s copied_docs=%s",
            getattr(radio, 'id', None),
            fcc_id,
            synced,
            manual_synced,
            copied_names[:10],
        )
        _update_radio_oet_page_url(radio, fcc_id)
        return synced

    synced = 0
    manual_synced = 0
    synced_names = []

    stale_docs = RadioOETDocument.objects.filter(radio=radio, fcc_id__iexact=fcc_id)
    stale_deleted = 0
    for stale_doc in stale_docs:
        if _is_fcc_attachment_document_url(stale_doc.document_url):
            continue
        stale_doc.delete()
        stale_deleted += 1
    if stale_deleted:
        logger.info(
            'FCC OET sync pruned invalid existing docs radio_id=%s fcc_id=%s deleted_count=%s',
            getattr(radio, 'id', None),
            fcc_id,
            stale_deleted,
        )

    # Deduplicate documents: the HTML-table parser and the bare-attachment-link parser
    # both run on the same page and can produce entries for the same attachment URL.
    # The HTML parser may populate exhibit_type/dates; the attachment parser always leaves
    # them empty.  When both match the same (document_url, view_attachment) key the
    # update_or_create in the loop below would overwrite the good metadata with blanks on
    # the second pass.  Resolve by keeping the entry with more metadata for each key.
    _dedup: dict = {}
    for _doc in documents:
        _key = (
            (_doc.get('document_url') or '').strip(),
            (_doc.get('view_attachment') or '').strip(),
        )
        if _key not in _dedup:
            _dedup[_key] = _doc
        elif _doc.get('exhibit_type') and not _dedup[_key].get('exhibit_type'):
            _dedup[_key] = _doc
    documents = list(_dedup.values())

    for document in documents:
        view_attachment = (document.get('view_attachment') or '').strip()
        document_url = (document.get('document_url') or '').strip()
        if not view_attachment and not document_url:
            continue

        defaults = {
            'exhibit_type': (document.get('exhibit_type') or '').strip(),
            'date_submitted_to_fcc': _parse_date_only(document.get('date_submitted_to_fcc')),
            'display_type': (document.get('display_type') or '').strip(),
            'date_available': _parse_date_only(document.get('date_available')),
        }
        oet_doc, _ = RadioOETDocument.objects.update_or_create(
            radio=radio,
            fcc_id=fcc_id,
            document_url=document_url,
            view_attachment=view_attachment,
            defaults=defaults,
        )

        needs_download = force_reload or not oet_doc.document_file
        if not needs_download and oet_doc.document_file:
            existing_suffix = Path(oet_doc.document_file.name or '').suffix.lower()
            expected_suffix = Path(
                _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=view_attachment,
                    document_url=document_url,
                    display_type=defaults.get('display_type', ''),
                )
            ).suffix.lower()
            if existing_suffix != expected_suffix:
                needs_download = True

        if document_url and needs_download and _is_fcc_authoritative_url(document_url):
            content = _download_oet_document_bytes(
                document_url,
                referer_url=(document.get('referer_url') or '').strip(),
            )
            if content:
                filename = _build_oet_document_filename(
                    fcc_id=fcc_id,
                    view_attachment=view_attachment,
                    document_url=document_url,
                    display_type=defaults.get('display_type', ''),
                )
                oet_doc.document_file.save(filename, ContentFile(content), save=True)
                logger.info(
                    "FCC OET document downloaded radio_id=%s fcc_id=%s oet_doc_id=%s filename=%s source_url=%s",
                    getattr(radio, 'id', None),
                    fcc_id,
                    oet_doc.id,
                    filename,
                    document_url,
                )

        if _sync_manual_record_for_oet_document(radio, fcc_id, oet_doc):
            manual_synced += 1

        synced += 1
        synced_names.append((view_attachment or document_url or '').strip())

    logger.info(
        "FCC OET sync complete radio_id=%s fcc_id=%s synced_count=%s manual_docs_synced=%s synced_docs=%s",
        getattr(radio, 'id', None),
        fcc_id,
        synced,
        manual_synced,
        synced_names[:10],
    )
    # Store application_id from secondary_metadata so the TCB report can be
    # fetched later (the stored document URLs use numeric ids, not application_id).
    _update_radio_oet_page_url(radio, fcc_id)
    app_id = (secondary_metadata or {}).get('application_id', '')
    if app_id and not getattr(radio, 'oet_page_url', ''):
        url = (
            f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500"
            f"&calledFromFrame=N&application_id={app_id}"
            f"&fcc_id={fcc_id}"
        )
        Radio.objects.filter(pk=radio.pk).update(oet_page_url=url)
        radio.oet_page_url = url
        logger.info(
            'FCC OET page URL stored from metadata radio_id=%s fcc_id=%s',
            radio.pk, fcc_id,
        )
    if fcc_id_norm and synced:
        _synced_oet_fcc_ids.add(fcc_id_norm)
    return synced


def _update_radio_oet_page_url(radio, fcc_id):
    """Derive and save the FCC EAS exhibits listing URL from stored OET document URLs.

    Only updates if radio.oet_page_url is not already set.  Uses the application_id
    embedded in any document_url on a RadioOETDocument record to reconstruct the
    exhibits listing page (mode=Exhibits) which is the stable, shareable URL.
    """
    if not radio or not getattr(radio, 'pk', None):
        return
    if getattr(radio, 'oet_page_url', ''):
        return  # Already stored from a previous sync
    for doc in RadioOETDocument.objects.filter(radio=radio, fcc_id__iexact=fcc_id).exclude(document_url='')[:20]:
        m = _OET_APP_ID_RE.search(doc.document_url)
        if m:
            app_id = m.group(1)
            url = (
                f"{OET_EXHIBITS_URL}?mode=Exhibits&RequestTimeout=500"
                f"&calledFromFrame=N&application_id={app_id}"
                f"&fcc_id={fcc_id}"
            )
            Radio.objects.filter(pk=radio.pk).update(oet_page_url=url)
            radio.oet_page_url = url
            logger.info(
                'FCC OET page URL stored radio_id=%s fcc_id=%s oet_url=%s',
                radio.pk, fcc_id, url,
            )
            return


def _copy_oet_docs_between_radios(target_radio, fcc_id):
    """Copy OET documents from any existing radio with the same FCC ID.

    Used when this FCC ID has already been synced in the current run — avoids
    re-fetching the same secondary metadata and re-downloading files.
    Returns the count of documents copied.
    """
    copied = 0
    existing_docs = RadioOETDocument.objects.filter(
        fcc_id__iexact=fcc_id,
    ).exclude(radio=target_radio)

    for existing in existing_docs:
        _, created = RadioOETDocument.objects.update_or_create(
            radio=target_radio,
            fcc_id=fcc_id,
            document_url=existing.document_url,
            view_attachment=existing.view_attachment,
            defaults={
                'exhibit_type': existing.exhibit_type,
                'date_submitted_to_fcc': existing.date_submitted_to_fcc,
                'display_type': existing.display_type,
                'date_available': existing.date_available,
                'document_file': (
                    existing.document_file.name if existing.document_file else ''
                ),
            },
        )
        if created:
            copied += 1
    if copied:
        _update_radio_oet_page_url(target_radio, fcc_id)
        logger.info(
            "FCC OET sync copy from sibling radio_id=%s fcc_id=%s "
            "copied_count=%s",
            getattr(target_radio, 'id', None),
            fcc_id,
            copied,
        )
    return copied


def _safe_filename(value):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('_')


def _build_test_report_filename(fcc_id, title, url):
    url_name = Path((url or '').split('?', 1)[0]).name
    ext = '.pdf' if not url_name.lower().endswith('.pdf') else ''
    base = _safe_filename(f"{fcc_id}_{title or 'fcc_test_report'}")
    if not base:
        base = _safe_filename(f"{fcc_id}_fcc_test_report")
    if not base.lower().endswith('.pdf'):
        base = f"{base}{ext or '.pdf'}"
    return base[:190]


def _download_report_bytes(url):
    try:
        response = _fcc_request_with_retry('get', url, impersonate='chrome124', timeout=20)
    except Exception:
        logger.exception("FCC test report download failed url=%s", url)
        return b''

    if response.status_code != 200:
        logger.info("FCC test report download non-200 url=%s status=%s", url, response.status_code)
        return b''

    content = response.content or b''
    if not content:
        return b''
    return content


def _attach_test_reports_to_radio(radio, fcc_id, secondary_metadata, force_reload=False):
    candidates = secondary_metadata.get('test_report_candidates', []) if secondary_metadata else []
    if not candidates:
        return 0

    attached = 0
    attached_files = []
    for candidate in candidates:
        source_url = (candidate.get('url') or '').strip()
        if not source_url:
            continue

        if not force_reload and RadioFCCTestReport.objects.filter(radio=radio, fcc_id__iexact=fcc_id, source_url=source_url).exists():
            continue

        content = _download_report_bytes(source_url)
        if not content:
            continue

        report = RadioFCCTestReport(
            radio=radio,
            fcc_id=fcc_id,
            source_url=source_url,
            report_title=(candidate.get('title') or '').strip(),
            product_designation=(candidate.get('product_designation') or '').strip(),
            extracted_data={
                'source': 'fcc_secondary_metadata',
                'candidate': candidate,
            },
        )
        filename = _build_test_report_filename(fcc_id, report.report_title, source_url)
        report.report_pdf.save(filename, ContentFile(content), save=False)
        report.save()
        _backfill_radio_specs_from_test_report(radio, report)
        attached += 1
        attached_files.append(filename)
        logger.info(
            "FCC test report attached radio_id=%s fcc_id=%s report_id=%s url=%s title=%s designation=%s",
            radio.id,
            fcc_id,
            report.id,
            source_url,
            report.report_title,
            report.product_designation,
        )

    logger.info(
        "FCC test report sync complete radio_id=%s fcc_id=%s attached_count=%s files=%s",
        getattr(radio, 'id', None),
        fcc_id,
        attached,
        attached_files[:10],
    )

    return attached


def _sanitize_fcc_xml(content):
    """Sanitize FCC XML to fix common malformation issues before parsing.

    FCC XML responses often contain unescaped ampersands in company
    names and invalid XML 1.0 control characters.
    """
    if not content:
        return content

    # Fix unescaped & — replace with &amp; but preserve already-escaped
    # entities (&amp; &lt; &gt; &quot; &apos; &#NN;)
    content = re.sub(r'&(?!(amp|lt|gt|quot|apos|#)\b)', '&amp;', content)

    # Strip control characters invalid in XML 1.0
    # (anything below 0x20 except tab, newline, carriage return)
    content = re.sub(
        r'[\x00-\x08\x0b\x0c\x0e-\x1f]',
        '',
        content,
    )

    return content


def _fetch_rule_parts_from_tcb_report(fcc_id, application_id):
    """Fetch the TCB Form 731 Report and extract Rule Parts from the
    Equipment Specifications table.

    The TCB report is the authoritative source for rule parts (e.g. '15B',
    '90', '95E').  The table has a 'Rule Parts' column under the 'Equipment
    Specifications' fieldset.

    Args:
        fcc_id: FCC ID string.
        application_id: The application ID from the FCC exhibit URL.

    Returns:
        Dict with 'rule_parts', 'original_fcc_id', 'oe_rows'.
        All lists are empty on failure.
    """
    empty_result = {'rule_parts': [], 'original_fcc_id': '', 'oe_rows': []}
    if not application_id:
        return empty_result

    url = f"{TCB_REPORT_URL}?applicationId={application_id}&fcc_id={fcc_id}"
    try:
        response = _fcc_request_with_retry(
            'get', url, impersonate='chrome124', timeout=15,
        )
    except Exception:
        logger.info(
            "FCC TCB report fetch failed fcc_id=%s application_id=%s",
            fcc_id, application_id,
        )
        return empty_result

    if response.status_code != 200:
        logger.info(
            "FCC TCB report non-200 fcc_id=%s application_id=%s status=%s",
            fcc_id, application_id, response.status_code,
        )
        return empty_result

    html = response.text or ''
    tcb_data = _extract_data_from_tcb_html(html)
    rule_parts = tcb_data.get('rule_parts', [])
    oe_rows = tcb_data.get('oe_rows', [])
    # Also extract original FCC ID for Change-in-Identification filings.
    # The TCB 731 report is the authoritative source — it contains the full
    # application purpose text including "Original FCC ID: XXX" that the
    # XML API and HTML table often truncate to just "Change in Identification".
    orig_fcc_id = _CID_ORIGINAL_FCC_ID_RE.search(html)
    orig_fcc_id = (orig_fcc_id.group(1) or '').strip().upper() if orig_fcc_id else ''
    return {
        'rule_parts': rule_parts,
        'original_fcc_id': orig_fcc_id,
        'oe_rows': oe_rows,
    }


def _extract_data_from_tcb_html(html):
    """Parse the TCB Form 731 HTML and extract rule parts and frequency
    data from the Equipment Specifications table.

    Returns a dict with:
        rule_parts: list of rule part strings (e.g. ['15B', '90'])
        oe_rows: list of dicts with freq/power/emission data
    """
    rule_parts = set()
    oe_rows = []
    soup = BeautifulSoup(html, 'html.parser')

    # Find the Equipment Specifications fieldset
    for fieldset in soup.find_all('fieldset'):
        legend = fieldset.find('legend')
        if not legend or 'equipment spec' not in (legend.get_text() or '').lower():
            continue

        # Find the table within this fieldset
        table = fieldset.find('table')
        if not table:
            continue

        rows = table.find_all('tr')
        if len(rows) < 2:
            continue

        # Find column indices from header row
        header_row = rows[0]
        headers = header_row.find_all('th')
        col_map = {}
        for idx, th in enumerate(headers):
            text = th.get_text(' ', strip=True).lower()
            if 'lower' in text and 'freq' in text:
                col_map['lower_freq'] = idx
            elif 'upper' in text and 'freq' in text:
                col_map['upper_freq'] = idx
            elif 'rule' in text and 'part' in text:
                col_map['rule_parts'] = idx
            elif 'power' in text and 'output' in text:
                col_map['power_output'] = idx
            elif 'emission' in text and 'design' in text:
                col_map['emission_designator'] = idx

        # Collect rule parts and frequency data from data rows
        for row in rows[1:]:
            cells = row.find_all('td')
            if len(cells) < 2:
                continue

            # Rule parts
            rp_idx = col_map.get('rule_parts')
            if rp_idx is not None and len(cells) > rp_idx:
                value = cells[rp_idx].get_text(' ', strip=True)
                if value:
                    for part in re.split(r'[,;/\s]+', value):
                        part = part.strip()
                        if part:
                            rule_parts.add(part)

            # Frequency data
            lo_idx = col_map.get('lower_freq')
            hi_idx = col_map.get('upper_freq')
            if lo_idx is not None and hi_idx is not None:
                lo_val = cells[lo_idx].get_text(' ', strip=True) if len(cells) > lo_idx else ''
                hi_val = cells[hi_idx].get_text(' ', strip=True) if len(cells) > hi_idx else ''
                if lo_val or hi_val:
                    pw_idx = col_map.get('power_output')
                    em_idx = col_map.get('emission_designator')
                    # Capture this row's specific rule part(s) so each
                    # frequency range gets its own rule part assignment
                    # (critical for PoC radios where LTE Band 2=24E,
                    # Band 12=27, Band 5=22H, etc.)
                    row_rule_parts = []
                    if rp_idx is not None and len(cells) > rp_idx:
                        rp_val = cells[rp_idx].get_text(' ', strip=True)
                        if rp_val:
                            for part in re.split(r'[,;/\s]+', rp_val):
                                part = part.strip()
                                if part:
                                    row_rule_parts.append(part)
                    oe_rows.append({
                        'lower_freq_mhz': lo_val,
                        'upper_freq_mhz': hi_val,
                        'power_output': (
                            cells[pw_idx].get_text(' ', strip=True)
                            if pw_idx is not None and len(cells) > pw_idx else ''
                        ),
                        'emission_designator': (
                            cells[em_idx].get_text(' ', strip=True)
                            if em_idx is not None and len(cells) > em_idx else ''
                        ),
                        'rule_parts': row_rule_parts,
                    })

    return {
        'rule_parts': sorted(rule_parts),
        'oe_rows': oe_rows,
    }


def _extract_rule_parts_from_tcb_html(html):
    """Legacy wrapper: extract only rule parts from TCB HTML."""
    return _extract_data_from_tcb_html(html).get('rule_parts', [])


def fetch_fcc_secondary_metadata(fcc_id):
    """Fetch additional FCC search metadata for a specific FCC ID."""
    # If both HTTP and Playwright are known-down for this sync run, skip all
    # network calls immediately — nothing will succeed and each attempt burns
    # 30–90 seconds of Playwright timeouts.
    if _fcc_connection_down and _fcc_playwright_down:
        logger.info(
            'FCC metadata fetch skipped fcc_id=%s reason=connection_and_playwright_down',
            fcc_id,
        )
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
            'rule_parts': [],
            'original_fcc_id_from_tcb': '',
        }

    grantee_code, product_code = split_fcc_id(fcc_id)
    if not grantee_code or not product_code:
        return {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': _fetch_oet_documents_from_html(fcc_id),
            'rule_parts': [],
            'original_fcc_id_from_tcb': '',
        }

    matched_records = []
    orig_fcc_id_from_tcb = ''
    params = {
        'grantee_code': grantee_code,
        'product_code': product_code,
        'product_exact_match': '',
        'RequestTimeout': '500',
        'outputformat': 'XML',
        'show_records': '25',
        'fetchfrom': '0',
        'calledFromFrame': 'N',
        'eas_apps_only': 'Y',
    }
    html_fallback_params = dict(params)
    html_fallback_params.pop('outputformat', None)

    try:
        response = _fcc_request_with_retry(
            'get',
            GENERIC_SEARCH_URL,
            params=params,
            impersonate="chrome124",
            timeout=15,
        )
    except _FCCConnectionDownError:
        logger.info('FCC metadata fetch skipped fcc_id=%s reason=connection_down', fcc_id)
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)
    except Exception:
        logger.exception("FCC metadata fetch failed fcc_id=%s", fcc_id)
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    if response.status_code != 200:
        logger.info(
            "FCC metadata fetch non-200 fcc_id=%s status=%s",
            fcc_id,
            response.status_code,
        )
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    try:
        data = xmltodict.parse(_sanitize_fcc_xml(response.text))
    except Exception:
        logger.warning(
            "FCC metadata XML parse failed fcc_id=%s — falling back to HTML",
            fcc_id,
        )
        return _fetch_secondary_metadata_from_html_fallback(fcc_id, html_fallback_params)

    target_key = _extract_fcc_key(fcc_id)
    matched_records = []
    matched_keys = set()
    oe_rows = []
    exhibit_urls = []
    rule_parts_set = set()
    for node in _iter_dict_nodes(data):
        if not isinstance(node, dict):
            continue

        fcc_value = (
            node.get('fcc_id')
            or node.get('fccid')
            or node.get('FCCId')
            or node.get('fccId')
            or ''
        )

        if fcc_value and _extract_fcc_key(fcc_value) != target_key:
            continue

        blob = _dict_text_blob(node)
        if not blob:
            continue

        for key in node.keys():
            matched_keys.add(str(key))
        matched_records.append(blob)

        for url in _extract_urls_from_payload(node):
            clean_url = unescape(url)
            if 'ViewExhibitReport.cfm' in clean_url:
                exhibit_urls.append(clean_url)

        application_purpose = (node.get('application_purpose') or node.get('applicationPurpose') or '').strip()
        if _is_original_equipment_purpose(application_purpose):
            oe_rows.append(
                {
                    'grant_date': (node.get('grant_date') or node.get('grantDate') or '').strip(),
                    'application_purpose': application_purpose,
                    'lower_freq_mhz': (node.get('lower_freq_mhz') or node.get('lowerFreqMHz') or '').strip(),
                    'upper_freq_mhz': (node.get('upper_freq_mhz') or node.get('upperFreqMHz') or '').strip(),
                    'power_output': (node.get('power_output') or node.get('powerOutput') or '').strip(),
                    'emission_designator': (node.get('emission_designator') or node.get('emissionDesignator') or '').strip(),
                }
            )

        # Collect rule parts from the XML response.
        # The Generic Search XML uses 'rule_parts' (snake_case) as the field name.
        raw_rule_parts = (node.get('rule_parts') or node.get('ruleParts') or '').strip()
        if raw_rule_parts:
            for part in raw_rule_parts.replace(';', ',').split(','):
                part = part.strip()
                if part:
                    rule_parts_set.add(part)

    oet_documents = _extract_oet_documents_from_xml(data, fcc_id)
    if not oet_documents:
        oet_documents = _fetch_oet_documents_from_html(fcc_id, candidate_urls=exhibit_urls)

    # Extract application_id from exhibit URLs and fetch rule parts from the
    # TCB Form 731 Report.  A single FCC ID can have multiple grants (e.g.
    # one for receiver certification under Part 15B and another for
    # transmitter certification under Part 95E), so we fetch the TCB report
    # for *every* application_id and merge — not replace — the rule parts.
    tcb_app_ids = _extract_application_id_from_urls(exhibit_urls)
    logger.info(
        "FCC TCB collecting app IDs fcc_id=%s exhibit_url_count=%s "
        "tcb_app_id_count=%s ids=%s",
        fcc_id, len(exhibit_urls), len(tcb_app_ids),
        [aid[:20] for aid in tcb_app_ids],
    )
    tcb_oe_rows = []
    for tcb_app_id in tcb_app_ids:
        tcb_result = _fetch_rule_parts_from_tcb_report(fcc_id, tcb_app_id)
        tcb_rule_parts = tcb_result.get('rule_parts', [])
        tcb_orig_fcc_id = tcb_result.get('original_fcc_id', '')
        tcb_freq_rows = tcb_result.get('oe_rows', [])
        if tcb_rule_parts:
            rule_parts_set.update(tcb_rule_parts)
            logger.info(
                "FCC TCB rule parts found fcc_id=%s app_id=%s rule_parts=%s "
                "freq_rows=%s",
                fcc_id, tcb_app_id[:20], tcb_rule_parts, len(tcb_freq_rows),
            )
        else:
            logger.info(
                "FCC TCB empty result fcc_id=%s app_id=%s",
                fcc_id, tcb_app_id[:20],
            )
        if tcb_orig_fcc_id:
            orig_fcc_id_from_tcb = tcb_orig_fcc_id
        if tcb_freq_rows:
            tcb_oe_rows.extend(tcb_freq_rows)

    # The FCC GenericSearch XML format (outputformat=XML) omits critical
    # technical fields: rule_parts, emission_designator, and power_output.
    # These fields ARE present in the HTML table (cells 16-18).  When the
    # XML parse succeeded but left these fields empty, supplement with the
    # HTML fallback so _classify_fcc_device has the data it needs.
    xml_has_emission = any(
        (row.get('emission_designator') or '').strip()
        for row in oe_rows
    )
    if not rule_parts_set or not xml_has_emission:
        logger.info(
            "FCC metadata XML missing technical fields fcc_id=%s "
            "rule_parts=%s has_emission=%s — supplementing with HTML fallback",
            fcc_id,
            sorted(rule_parts_set),
            xml_has_emission,
        )
        html_result = _fetch_secondary_metadata_from_html_fallback(
            fcc_id, html_fallback_params,
        )
        # Merge HTML rule_parts (already includes TCB report results)
        for part in html_result.get('rule_parts', []):
            rule_parts_set.add(part)
        # Merge HTML oe_rows — prefer HTML rows (they have emission data)
        html_oe = html_result.get('original_equipment_rows', [])
        if html_oe:
            if not xml_has_emission:
                oe_rows = html_oe
            else:
                # Merge: deduplicate by grant_date + freq range
                seen_oe = set()
                merged_oe = list(html_oe)
                for row in html_oe:
                    key = (
                        row.get('grant_date', ''),
                        row.get('lower_freq_mhz', ''),
                        row.get('upper_freq_mhz', ''),
                    )
                    seen_oe.add(key)
                for row in oe_rows:
                    key = (
                        row.get('grant_date', ''),
                        row.get('lower_freq_mhz', ''),
                        row.get('upper_freq_mhz', ''),
                    )
                    if key not in seen_oe:
                        seen_oe.add(key)
                        merged_oe.append(row)
                oe_rows = merged_oe
        # Merge text_blob from HTML
        html_blob = html_result.get('text_blob', '')
        if html_blob:
            matched_records.append(html_blob)
        # Merge original_fcc_id_from_tcb from HTML fallback (CID chain)
        html_orig_fcc = html_result.get('original_fcc_id_from_tcb', '')
        if html_orig_fcc and not orig_fcc_id_from_tcb:
            orig_fcc_id_from_tcb = html_orig_fcc

    # Merge TCB frequency rows into OE rows (supplements XML/HTML data
    # when the FCC GenericSearch returns 503 but TCB reports succeed).
    if tcb_oe_rows:
        oe_rows = list(oe_rows) + tcb_oe_rows
        logger.info(
            "FCC TCB frequency rows merged into OE rows fcc_id=%s "
            "tcb_row_count=%s total_oe_rows=%s",
            fcc_id, len(tcb_oe_rows), len(oe_rows),
        )

    return {
        'record_count': len(matched_records),
        'text_blob': ' || '.join(matched_records),
        'matched_keys': sorted(matched_keys),
        'test_report_candidates': _extract_test_report_candidates(data, fcc_id),
        'original_equipment_rows': oe_rows,
        'oet_documents': oet_documents,
        'rule_parts': sorted(rule_parts_set),
        'application_id': tcb_app_ids[0] if tcb_app_ids else '',
        'original_fcc_id_from_tcb': orig_fcc_id_from_tcb,
    }


def _extract_application_id_from_urls(urls):
    """Extract every unique application_id from a list of FCC exhibit URLs.

    A single FCC ID can have multiple grants (e.g. one for Part 15B receiver
    certification and another for Part 95E transmitter certification), each
    with its own application_id.  Returning all of them lets the caller fetch
    the TCB Form 731 for every grant and merge the rule parts together.
    """
    app_ids = []
    seen = set()
    for url in urls:
        # Try both patterns: application_id= (exhibit links) and
        # applicationId= (TCB 731 links collected by Playwright)
        match = _OET_APP_ID_RE.search(url) or _TCB_APP_ID_RE.search(url)
        if match:
            app_id = match.group(1)
            if app_id not in seen:
                seen.add(app_id)
                app_ids.append(app_id)
    return app_ids


_CID_ORIGINAL_FCC_ID_RE = re.compile(
    r'Original\s+FCC\s+ID\s*[:#]?\s*([A-Za-z0-9\-]{3,30})',
    re.IGNORECASE,
)


def _extract_original_fcc_id_from_cid(application_purpose):
    """Extract the original FCC ID from a Change-in-Identification filing.

    CID filings mention the original grant in the application purpose text:
        "Change in identification of presently authorized equipment.
         Original FCC ID: 2A3OORB48P Grant Date: 11/20/2025"

    Args:
        application_purpose: The full application purpose text.

    Returns:
        Original FCC ID string (e.g. '2A3OORB48P'), or empty string.
    """
    if not application_purpose:
        return ''
    match = _CID_ORIGINAL_FCC_ID_RE.search(application_purpose)
    return (match.group(1) or '').strip().upper() if match else ''


def _detect_amateur_radio(rule_parts, oe_rows, application_purpose=''):
    """Detect whether an FCC filing likely represents an amateur radio.

    Amateur radios (Part 97) do not require FCC transmitter certification.
    Manufacturers often file them under Part 15B/15C with blank TX fields.
    This function checks for the telltale combination of:
      1. Rule parts limited to 15B/15C (no TX-certified rule parts like 90/95E)
      2. Power output blank/zero
      3. Emission designator blank
      4. Frequencies overlapping 2m (144-148 MHz) or 70cm (420-450 MHz) bands

    Args:
        rule_parts: List of normalized rule part strings (e.g. ['15B']).
        oe_rows: List of OE row dicts with lower_freq_mhz, upper_freq_mhz,
                 power_output, emission_designator.
        application_purpose: The application purpose string.

    Returns:
        True if the filing is likely an amateur radio.
    """
    if not rule_parts or not oe_rows:
        return False

    # Step 1: Rule parts must be limited to suspect non-TX parts
    normalized_parts = {p.strip().upper() for p in rule_parts}
    has_tx_cert = bool(normalized_parts - _AMATEUR_SUSPECT_RULE_PARTS)
    if has_tx_cert:
        return False

    # Step 2: Check for blank/zero power and emission across all OE rows
    has_tx_specs = False
    for row in oe_rows:
        power = (row.get('power_output') or '').strip()
        emission = (row.get('emission_designator') or '').strip()
        if power and power not in ('0', '0.0', 'NaN', 'N/A'):
            has_tx_specs = True
        if emission and emission.lower() not in ('nan', 'n/a'):
            has_tx_specs = True
    if has_tx_specs:
        return False  # Has TX specs — likely a certified transmitter, not amateur

    # Step 3: Check if frequencies overlap amateur bands
    in_amateur_band = False
    for row in oe_rows:
        try:
            lower = float(row.get('lower_freq_mhz', 0) or 0)
            upper = float(row.get('upper_freq_mhz', 0) or 0)
        except (ValueError, TypeError):
            continue
        if lower <= 0 or upper <= 0:
            continue

        # Check overlap with 2m band (144-148)
        if lower <= AMATEUR_BAND_2M[1] and upper >= AMATEUR_BAND_2M[0]:
            in_amateur_band = True
            break
        # Check overlap with 70cm band (420-450)
        if lower <= AMATEUR_BAND_70CM[1] and upper >= AMATEUR_BAND_70CM[0]:
            in_amateur_band = True
            break

    if not in_amateur_band:
        return False

    # Step 4: Confirm with product description keywords
    purpose_upper = (application_purpose or '').upper()
    has_radio_keyword = any(
        kw in purpose_upper for kw in AMATEUR_PRODUCT_KEYWORDS
    )
    if not has_radio_keyword:
        # Still flag it — frequencies + rule parts are strong indicators
        logger.debug(
            "Amateur radio candidate missing product keyword "
            "purpose=%s",
            application_purpose,
        )

    return True


def _scrape_website_for_tx_specs(radio):
    """Scrape a radio's website for transmitter specifications.

    Called when a radio is identified as a likely amateur device.  Uses the
    existing website enrichment pipeline to extract TX-related specs (power,
    frequency bands, GPS, APRS, DMR, etc.) from the manufacturer's product page.
    Also parses YouTube video transcripts if available.

    Every scraped field is logged at INFO level so the operator can diagnose
    extraction problems from the logfile.

    Args:
        radio: Radio model instance.

    Returns:
        Dict of extracted specs, or empty dict on failure.
    """
    specs = {}

    # Scrape manufacturer website
    website = (radio.website or '').strip()
    if website:
        from radios.manual_extraction import enrich_specs_from_product_url

        logger.info(
            "Amateur radio website scrape start radio_id=%s "
            "fcc_id=%s url=%s",
            getattr(radio, 'id', None),
            getattr(radio, 'fcc_id', '') or '',
            website,
        )

        try:
            web_specs = enrich_specs_from_product_url(website)
            if web_specs:
                specs.update(web_specs)
                # Log all extracted fields so the operator can see
                # exactly what the parser found (or missed).
                populated = {
                    k: v for k, v in sorted(specs.items())
                    if v and k not in {'website', 'source_domain'}
                }
                logger.info(
                    "Amateur radio website scrape done radio_id=%s "
                    "fcc_id=%s fields=%s extracted=%s",
                    getattr(radio, 'id', None),
                    getattr(radio, 'fcc_id', '') or '',
                    len(populated),
                    populated,
                )
            else:
                logger.info(
                    "Amateur radio website scrape empty radio_id=%s "
                    "fcc_id=%s url=%s — no specs extracted",
                    getattr(radio, 'id', None),
                    getattr(radio, 'fcc_id', '') or '',
                    website,
                )
        except Exception:
            logger.exception(
                "Amateur radio website scrape failed radio_id=%s "
                "fcc_id=%s url=%s",
                getattr(radio, 'id', None),
                getattr(radio, 'fcc_id', '') or '',
                website,
            )

    # Parse YouTube video transcripts
    try:
        yt_specs = _parse_youtube_videos_for_specs(radio)
        if yt_specs:
            # Merge: website specs take priority over YouTube
            for k, v in yt_specs.items():
                if v and not specs.get(k):
                    specs[k] = v
            logger.info(
                "Amateur radio YouTube parse done radio_id=%s "
                "fcc_id=%s youtube_fields=%s",
                getattr(radio, 'id', None),
                getattr(radio, 'fcc_id', '') or '',
                ','.join(k for k, v in yt_specs.items() if v),
            )
    except Exception:
        logger.exception(
            "Amateur radio YouTube parse failed radio_id=%s "
            "fcc_id=%s",
            getattr(radio, 'id', None),
            getattr(radio, 'fcc_id', '') or '',
        )

    return specs


def _parse_youtube_videos_for_specs(radio):
    """Parse YouTube video transcripts linked to a radio for TX specs.

    Iterates the radio's youtube_video_urla field and fetches auto-generated
    captions/transcripts.  Parses the combined text with extract_specs_from_text.

    Args:
        radio: Radio model instance.

    Returns:
        Dict of extracted specs merged across all videos, or empty dict.
    """
    youtube_urls = (radio.youtube_video_urla or '').strip()
    if not youtube_urls:
        return {}

    all_text = []
    for line in youtube_urls.splitlines():
        url = line.strip()
        if not url:
            continue
        video_id = _extract_youtube_video_id(url)
        if not video_id:
            continue

        transcript = _fetch_youtube_transcript(video_id)
        if transcript:
            all_text.append(transcript)

    if not all_text:
        return {}

    combined = '\n'.join(all_text)
    extracted = extract_specs_from_text(combined, source_name='youtube_transcripts')
    extracted['source_hint'] = 'youtube_transcripts'

    logger.info(
        "YouTube transcript parse result radio_id=%s "
        "freq_bands=%s power=%s gps=%s aprs=%s dmr=%s",
        getattr(radio, 'id', None),
        extracted.get('freq_bands_tx', ''),
        extracted.get('power_watts', ''),
        extracted.get('gps', ''),
        extracted.get('aprs', ''),
        extracted.get('dmr', ''),
    )
    return extracted


def _extract_youtube_video_id(url):
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|'
        r'youtube\.com/v/)([A-Za-z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ''


def _fetch_youtube_transcript(video_id):
    """Fetch YouTube video transcript/captions.

    Tries multiple strategies:
    1. youtube-transcript-api (pip package)
    2. yt-dlp with auto-generated subs
    3. Direct timedtext API call

    Returns combined transcript text, or empty string on failure.
    """
    # Strategy 1: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        # Prefer manually created English captions, then auto-generated
        try:
            transcript = transcript_list.find_transcript(['en'])
        except Exception:
            try:
                transcript = transcript_list.find_generated_transcript(['en'])
            except Exception:
                # Try any available language and auto-translate to English
                for t in transcript_list:
                    if t.is_translatable:
                        transcript = t.translate('en')
                        break
                else:
                    transcript = None

        if transcript:
            pieces = transcript.fetch()
            return ' '.join(p.get('text', '') for p in pieces)
    except ImportError:
        logger.debug("youtube-transcript-api not installed, trying fallback")
    except Exception:
        logger.debug(
            "YouTube transcript API failed video_id=%s, trying fallback",
            video_id,
        )

    # Strategy 2: Direct timedtext API
    try:
        url = (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        response = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; RadioTrackerBot/1.0)',
        })
        # Extract player_response JSON for captions
        match = re.search(
            r'ytInitialPlayerResponse\s*=\s*({.+?});',
            response.text,
            re.DOTALL,
        )
        if match:
            import json as _json
            player_data = _json.loads(match.group(1))
            captions = (
                player_data.get('captions', {})
                .get('playerCaptionsTracklistRenderer', {})
                .get('captionTracks', [])
            )
            if captions:
                # Get the first English track
                for track in captions:
                    if track.get('languageCode') == 'en':
                        base_url = track.get('baseUrl', '')
                        if base_url:
                            caption_resp = requests.get(base_url, timeout=10)
                            if caption_resp.status_code == 200:
                                # Parse XML/SRT captions to plain text
                                text = re.sub(
                                    r'<[^>]+>', '',
                                    caption_resp.text,
                                )
                                text = re.sub(
                                    r'\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->.*?\n',
                                    ' ',
                                    text,
                                )
                                text = re.sub(r'\n\s*\d+\s*\n', ' ', text)
                                return text.strip()
                        break
    except Exception:
        logger.debug(
            "YouTube timedtext fallback failed video_id=%s",
            video_id,
        )

    return ''


def _apply_website_specs_to_radio(radio, extracted):
    """Apply website-scraped specs to a radio model.

    Called only for amateur radios (detected via FCC metadata) where the
    FCC certification data is unreliable — amateur devices are often
    filed under Part 15B with placeholder or maximum-rated values that
    do not reflect the actual product specs.

    Website-scraped values take precedence over existing FCC-supplied
    values because the manufacturer's own product page is the better
    source for a device that was never transmitter-certified.
    """
    field_map = {
        'freq_bands_tx': 'freq_bands_tx',
        'power_watts': 'power_watts',
        'gps': 'gps',
        'aprs': 'aprs',
        'dmr': 'dmr',
        'air_band': 'air_band',
        'satellite_tracking': 'satellite_tracking',
        'cost_approx': 'cost_approx',
    }
    changes = []
    for extracted_key, radio_field in field_map.items():
        value = extracted.get(extracted_key)
        if not value:
            continue
        str_value = str(value)[:200]
        current = getattr(radio, radio_field)
        # Update when the field is empty OR when the website value
        # differs from the stored value (FCC data for amateur radios
        # is often incorrect).
        if str_value and str_value != (current or ''):
            setattr(radio, radio_field, str_value)
            changes.append(radio_field)

    # Numeric fields
    battery_mah = extracted.get('battery_mah')
    if battery_mah:
        try:
            battery_int = int(battery_mah)
        except (ValueError, TypeError):
            battery_int = None
        if battery_int is not None and radio.battery_mah != battery_int:
            radio.battery_mah = battery_int
            changes.append('battery_mah')

    # USB-C: prefer the direct key from the extractor, fall back to
    # text-pattern matching on other fields.
    usb_c_from_scrape = extracted.get('usb_c_charging')
    if usb_c_from_scrape and not radio.usb_c_charging:
        radio.usb_c_charging = True
        changes.append('usb_c_charging')
    elif not radio.usb_c_charging:
        text_blob = (
            (extracted.get('freq_bands_tx', '') or '') + ' ' +
            (extracted.get('notes', '') or '')
        ).lower()
        if 'usb-c' in text_blob or 'usb c' in text_blob:
            radio.usb_c_charging = True
            changes.append('usb_c_charging')

    if changes:
        radio.save(update_fields=changes)
        logger.info(
            "Amateur radio website specs applied radio_id=%s "
            "fields=%s",
            getattr(radio, 'id', None),
            ','.join(changes),
        )

    return changes


def _sync_radio_certifications(radio, fcc_id, sec_metadata):
    """Create or update RadioCertification records from FCC metadata.

    Each original_equipment_row in sec_metadata represents one FCC grant
    with its own frequency range, rule parts, emission designators, and
    power output.  This function creates a RadioCertification record per
    unique grant (deduplicated by frequency range).

    Args:
        radio: The Radio model instance.
        fcc_id: The FCC ID string (may differ from radio.fcc_id for CID).
        sec_metadata: The dictionary from fetch_fcc_secondary_metadata.
    """
    from radios.models import RadioCertification

    if not sec_metadata or not radio:
        return

    oe_rows = sec_metadata.get('original_equipment_rows', [])
    rule_parts = sec_metadata.get('rule_parts', [])

    if not oe_rows:
        # No OE rows — still create a single cert with rule parts if available
        if rule_parts:
            _upsert_certification(
                radio, fcc_id,
                lower_mhz=None, upper_mhz=None,
                rule_parts_str=', '.join(rule_parts),
                emission_designators='',
                power_watts=None,
                grant_date=radio.grant_date,
                authorization_type='certification',
            )
        return

    # Deduplicate by frequency range — same FCC ID can have multiple
    # rows with identical frequencies (e.g. different modulation modes).
    seen_ranges = set()
    for row in oe_rows:
        lower_str = (row.get('lower_freq_mhz') or '').strip()
        upper_str = (row.get('upper_freq_mhz') or '').strip()
        if not lower_str or not upper_str:
            continue

        try:
            lower = float(lower_str)
            upper = float(upper_str)
        except (ValueError, TypeError):
            continue

        range_key = (lower, upper)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)

        emission = (row.get('emission_designator') or '').strip()
        power_str = (row.get('power_output') or '').strip()
        try:
            power = float(power_str) if power_str else None
        except (ValueError, TypeError):
            power = None

        row_grant_date = (row.get('grant_date') or '').strip()
        try:
            grant_date = datetime.strptime(row_grant_date, '%m/%d/%Y').date() if row_grant_date else radio.grant_date
        except (ValueError, TypeError):
            grant_date = radio.grant_date

        # Use per-row rule parts when available (from TCB extraction),
        # falling back to the global rule_parts list (from XML/HTML).
        row_rule_parts = row.get('rule_parts', [])
        if row_rule_parts:
            cert_rule_parts_str = ', '.join(row_rule_parts)
        else:
            cert_rule_parts_str = ', '.join(rule_parts) if rule_parts else ''

        _upsert_certification(
            radio, fcc_id,
            lower_mhz=lower, upper_mhz=upper,
            rule_parts_str=cert_rule_parts_str,
            emission_designators=emission,
            power_watts=power,
            grant_date=grant_date,
            authorization_type='certification',
        )


def _upsert_certification(radio, fcc_id, lower_mhz, upper_mhz, rule_parts_str,
                          emission_designators, power_watts, grant_date,
                          authorization_type):
    """Create or update a single RadioCertification record."""
    from radios.models import RadioCertification

    existing = RadioCertification.objects.filter(
        radio=radio,
        fcc_id__iexact=fcc_id,
        freq_range_lower_mhz=lower_mhz,
        freq_range_upper_mhz=upper_mhz,
    ).first()

    if existing:
        changed = False
        if rule_parts_str and existing.rule_parts != rule_parts_str:
            existing.rule_parts = rule_parts_str
            changed = True
        if emission_designators and existing.emission_designators != emission_designators:
            existing.emission_designators = emission_designators
            changed = True
        if power_watts is not None and existing.power_output_watts != power_watts:
            existing.power_output_watts = power_watts
            changed = True
        if grant_date and existing.grant_date != grant_date:
            existing.grant_date = grant_date
            changed = True
        if authorization_type and existing.authorization_type != authorization_type:
            existing.authorization_type = authorization_type
            changed = True
        if changed:
            existing.save()
            logger.info(
                "FCC cert updated radio_id=%s fcc_id=%s "
                "lower=%s upper=%s rule_parts=%s",
                radio.pk, fcc_id, lower_mhz, upper_mhz, rule_parts_str,
            )
    else:
        RadioCertification.objects.create(
            radio=radio,
            fcc_id=fcc_id,
            freq_range_lower_mhz=lower_mhz,
            freq_range_upper_mhz=upper_mhz,
            rule_parts=rule_parts_str,
            emission_designators=emission_designators,
            power_output_watts=power_watts,
            grant_date=grant_date,
            authorization_type=authorization_type,
        )
        logger.info(
            "FCC cert created radio_id=%s fcc_id=%s "
            "lower=%s upper=%s rule_parts=%s",
            radio.pk, fcc_id, lower_mhz, upper_mhz, rule_parts_str,
        )


def _detect_and_scrape_amateur_radio(radio, sec_metadata):

    from radios.models import RadioCertification

    rule_parts = sec_metadata.get('rule_parts', [])
    oe_rows = sec_metadata.get('original_equipment_rows', [])
    application_purpose = sec_metadata.get('text_blob', '')

    # Skip if the radio already has TX specs or certifications
    if radio.power_watts or RadioCertification.objects.filter(radio=radio).exists():
        return

    if not _detect_amateur_radio(rule_parts, oe_rows, application_purpose):
        return

    logger.info(
        "Amateur radio detected radio_id=%s fcc_id=%s rule_parts=%s",
        getattr(radio, 'id', None),
        getattr(radio, 'fcc_id', '') or '',
        rule_parts,
    )

    if not radio.website:
        logger.info(
            "Amateur radio no website to scrape radio_id=%s",
            getattr(radio, 'id', None),
        )
        return

    extracted = _scrape_website_for_tx_specs(radio)
    if extracted:
        _apply_website_specs_to_radio(radio, extracted)


# ── FCC Technical Field Classifier ─────────────────────────────────────
# Maps FCC rule parts, frequency ranges, and emission designators to
# radio service types.  This is the authoritative classification — FCC
# certification data (rule parts, frequencies, emission codes) tells us
# what the device IS, not marketing keywords in user manuals.

def _classify_fcc_device(primary_record, sec_metadata):
    """Classify a device as a two-way radio using FCC technical fields.

    Checks rule parts, frequency ranges, and emission designators from
    the FCC secondary metadata.  These are the authoritative technical
    fields — they tell us what the device IS, not what marketing calls it.

    Args:
        primary_record: Dict from the FCC primary API (getFCCIDList).
        sec_metadata: Dict from ``fetch_fcc_secondary_metadata``.

    Returns:
        Tuple of (is_radio: bool, matched_tags: list[str]).
        ``is_radio`` is True if the FCC technical fields indicate a
        two-way radio transmitter.  ``matched_tags`` lists which
        service types were matched (e.g. ['GMRS', 'VHF']).
    """
    matched_tags: list[str] = []
    reasons: list[str] = []

    # ── Rule Parts check (highest weight) ──
    rule_parts = set(sec_metadata.get('rule_parts', []))
    radio_parts = rule_parts & _get_radio_rule_parts()
    if radio_parts:
        matched_tags.extend(sorted(radio_parts))
        reasons.append(f'rule_parts={sorted(radio_parts)}')

    # ── Frequency Range check ──
    oe_rows = sec_metadata.get('original_equipment_rows', [])
    freq_matched: set[str] = set()
    for row in oe_rows:
        try:
            lower = float(row.get('lower_freq_mhz', 0) or 0)
            upper = float(row.get('upper_freq_mhz', 0) or 0)
        except (ValueError, TypeError):
            continue
        if lower <= 0 or upper <= 0:
            continue
        if lower > upper:
            lower, upper = upper, lower

        for band_low, band_high, label in _get_radio_freq_bands():
            if lower <= band_high and upper >= band_low:
                freq_matched.add(label)

    if freq_matched:
        matched_tags.extend(sorted(freq_matched))
        reasons.append(f'freq_bands={sorted(freq_matched)}')

    # ── Emission Designator check ──
    has_radio_emission = False
    for row in oe_rows:
        emission = (row.get('emission_designator') or '').strip().upper()
        if not emission:
            continue
        # Check if any known radio emission prefix is in this designator
        for prefix in _get_radio_emission_prefixes():
            if emission.startswith(prefix):
                has_radio_emission = True
                break
        if has_radio_emission:
            break

    if has_radio_emission:
        reasons.append('emission=radio_modulation')
        matched_tags.append('RADIO_EMISSION')

    # ── Text blob (grant notes) supplementary check ──
    text_blob = (sec_metadata.get('text_blob', '') or '').upper()
    if text_blob:
        # Look for equipment-specific descriptors in the text_blob
        # These are the FCC's own descriptors, not marketing terms
        if 'TRANSCEIVER' in text_blob or 'TWO WAY RADIO' in text_blob:
            reasons.append('text_blob=transceiver')
            matched_tags.append('TRANSCEIVER')

    # ── Decision ──
    # A device is a two-way radio if:
    # 1. It has a radio rule part (90, 95, 80, 87, 97, etc.) OR
    # 2. It has a radio frequency band AND a radio emission designator OR
    # 3. It has both radio frequency AND emission (strong signal even
    #    without explicit radio rule parts — covers Part 15C amateur
    #    radios filed as intentional radiators)
    is_radio = (
        bool(radio_parts)
        or (bool(freq_matched) and has_radio_emission)
        or ('TRANSCEIVER' in matched_tags)
    )

    # Log classification for debugging
    if is_radio:
        logger.info(
            "FCC device classifier: radio detected "
            "fcc_id=%s reasons=%s",
            primary_record.get('FCCId', ''),
            '; '.join(reasons),
        )
    else:
        logger.info(
            "FCC device classifier: non-radio "
            "fcc_id=%s reasons=none "
            "rule_parts=%s freq_bands=%s emission=%s",
            primary_record.get('FCCId', ''),
            sorted(rule_parts), sorted(freq_matched),
            has_radio_emission,
        )

    return is_radio, matched_tags


def _primary_record_matches_allowlist(primary_record, allowlist_terms):
    """Quick check: does the primary FCC API record alone match any allowlist term?

    Used as a pre-filter before the expensive secondary metadata fetch
    (curl_cffi + Playwright).  Only checks the grantee name and
    application purpose — the fields available without an extra
    FCC round-trip.  The FCC ID is intentionally excluded: it is a
    technical identifier, not a product description, and product codes
    can accidentally match short allowlist terms (e.g. FSCBT9104DI
    matching 'CB').

    Records with common FCC equipment authorization purposes always
    pass: grantee names are company names ("Kenwood", "Hytera") that
    never contain radio keywords, so the allowlist-terms check on
    grantee+name alone would reject every legitimate radio filing.
    The expensive secondary metadata fetch is still required for the
    definitive allowlist check on text_blob.

    Returns True if at least one term matches or the purpose is a
    known equipment authorization type.
    """
    app_purpose = (primary_record.get('applicationPurpose', '') or '').strip().lower()
    if app_purpose in _get_always_pass_purposes():
        return True

    # Secondary check: the purpose may contain radio keywords directly
    # (e.g. "Original Equipment - TWO WAY RADIO" from some FCC records).
    sources = [
        (primary_record.get('grantee', '') or ''),
        app_purpose,
    ]
    text = ' | '.join(str(v) for v in sources if v).upper()
    return any(_term_matches_text(term, text) for term in allowlist_terms)


def _allowlist_match_terms(primary_record, secondary_metadata, allowlist_terms):
    """Return which allowlist terms appear in the FCC record metadata.

    Searches the combined text of grantee name, application purpose,
    grant date, and secondary metadata for case-insensitive substring
    matches.  The FCC ID is intentionally excluded: it is a technical
    identifier, not a product description, and product codes can
    accidentally match short allowlist terms.
    """
    sources = [
        (primary_record.get('grantee', '') or ''),
        (primary_record.get('applicationPurpose', '') or ''),
        (primary_record.get('grantDate', '') or ''),
        _strip_fcc_id_from_blob(
            secondary_metadata.get('text_blob', ''),
            primary_record.get('FCCId', ''),
        ),
    ]
    text = ' | '.join(str(v) for v in sources if v).upper()
    return [term for term in allowlist_terms if _term_matches_text(term, text)]


def _strip_fcc_id_from_blob(text_blob, fcc_id):
    """Remove the target FCC ID prefix from each row in the text_blob.

    The text_blob rows have the format:
    ``2ASCB-DF11DUAL | Original Equipment | 03/05/2024 | 2402.0 | 2480.0``

    Stripping the leading FCC ID prevents false substring matches where
    a grantee code like '2ASCB' accidentally matches the 'CB' allowlist
    term.  The FCC ID is a label, not product description text.
    """
    if not text_blob or not fcc_id:
        return text_blob or ''
    fcc_id_upper = fcc_id.strip().upper()
    cleaned_rows = []
    for row in text_blob.split(' || '):
        stripped = row.strip()
        if stripped.upper().startswith(fcc_id_upper):
            # Remove "FCCID | " prefix from the row
            stripped = stripped[len(fcc_id_upper):].lstrip().lstrip('|').strip()
        cleaned_rows.append(stripped)
    return ' || '.join(cleaned_rows)


def _denylist_match_terms(primary_record, secondary_metadata, denylist_terms):
    """Return which denylist terms appear in the FCC record metadata.

    Uses the same source fields and case-insensitive substring matching
    as ``_allowlist_match_terms``.  A non-empty return means the device
    should be excluded from the radio database (it is an accessory like
    a speaker microphone, charger, cable, or mounting bracket, not a
    two-way radio).
    """
    if not denylist_terms:
        return []
    sources = [
        (primary_record.get('grantee', '') or ''),
        (primary_record.get('applicationPurpose', '') or ''),
        (primary_record.get('grantDate', '') or ''),
        _strip_fcc_id_from_blob(
            secondary_metadata.get('text_blob', ''),
            primary_record.get('FCCId', ''),
        ),
    ]
    text = ' | '.join(str(v) for v in sources if v).upper()
    return [term for term in denylist_terms if _term_matches_text(term, text)]


def _clean_query(value):
    return (value or '').strip().upper().replace(' ', '')


def _exact_grantee_query(value):
    """Return an exact grantee code when query is a standalone valid grantee code."""
    cleaned = _clean_query(value)
    if not cleaned or '-' in cleaned:
        return ''
    if 'A' <= cleaned[0] <= 'Z' and len(cleaned) == 3:
        return cleaned
    if '2' <= cleaned[0] <= '9' and len(cleaned) == 5:
        return cleaned
    return ''


def _parse_datetime_value(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)):
        try:
            numeric_value = float(value)
            if numeric_value <= 0:
                return None
            if numeric_value > 1_000_000_000_000:
                numeric_value = numeric_value / 1000.0
            return datetime.fromtimestamp(numeric_value, tz=datetime_timezone.utc)
        except Exception:
            return None

    text = str(value).strip()
    if not text:
        return None

    dt = parse_datetime(text)
    if dt:
        return dt

    if text.endswith('Z'):
        dt = parse_datetime(f"{text[:-1]}+00:00")
        if dt:
            return dt

    parsed_date = parse_date(text)
    if parsed_date:
        return datetime.combine(parsed_date, datetime_time.min, tzinfo=datetime_timezone.utc)

    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            candidate = datetime.strptime(text, fmt)
            if fmt.endswith("%S"):
                return candidate
            return datetime.combine(candidate.date(), datetime_time.min)
        except ValueError:
            continue

    return None


def _extract_record_last_modified_datetime(primary_record):
    if not isinstance(primary_record, dict):
        return None

    canonical = {
        str(key).lower(): value
        for key, value in primary_record.items()
    }
    preferred_keys = (
        'lastmodifieddate',
        'lastmodified',
        'modifieddate',
        'datemodified',
        'lastupdatedate',
        'lastupdate',
        'modificationdate',
    )

    candidate_values = []
    for key in preferred_keys:
        if key in canonical:
            candidate_values.append(canonical[key])

    if not candidate_values:
        for key, value in canonical.items():
            if 'modif' in key or 'lastupdate' in key or 'updated' in key:
                candidate_values.append(value)

    for value in candidate_values:
        parsed = _parse_datetime_value(value)
        if not parsed:
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        else:
            parsed = parsed.astimezone(datetime_timezone.utc)
        return parsed

    return None


def _should_skip_supporting_lookup(primary_record, last_lookup_at, start_date=None):
    """Return (should_skip, record_last_modified).

    Primary path: skip if the FCC record's last-modified date is on or before the
    radio's last lookup timestamp (nothing new to pull).

    Fallback path (FCC returns 503 so no last-modified date is available): if a
    start_date window is active and the radio was already looked up *within* that
    window, there is nothing new to process — skip to avoid redundant re-processing
    across consecutive daily syncs.
    """
    if not last_lookup_at:
        return False, None

    record_last_modified = _extract_record_last_modified_datetime(primary_record)

    lookup_dt = last_lookup_at
    if timezone.is_naive(lookup_dt):
        lookup_dt = timezone.make_aware(lookup_dt, datetime_timezone.utc)
    else:
        lookup_dt = lookup_dt.astimezone(datetime_timezone.utc)

    if record_last_modified is not None:
        # Normal path: compare FCC record age to last lookup.
        return record_last_modified <= lookup_dt, record_last_modified

    # No last-modified date (FCC returned 503 / Playwright fallback with no metadata).
    # Fall back to the date-window heuristic: if the radio was already processed
    # on or after start_date, no new data could have arrived within this sync window.
    if start_date is not None:
        start_dt = start_date
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt, datetime_timezone.utc)
        else:
            start_dt = start_dt.astimezone(datetime_timezone.utc)
        if lookup_dt >= start_dt:
            return True, None

    return False, None


def _stamp_lookup_timestamp(radio, looked_up_at):
    if radio.last_fccid_lookup_at == looked_up_at:
        return
    radio.last_fccid_lookup_at = looked_up_at
    radio.save(update_fields=['last_fccid_lookup_at'])


def _ensure_grantee_brand_and_manufacturer(grantee_code, grantee_name):
    norm_code = normalize_grantee_code(grantee_code)
    norm_name = (grantee_name or '').strip()
    if not norm_code or not norm_name:
        return None, None

    brand = _find_existing_grantee_brand(norm_code, norm_name)
    blank_brand = _find_matching_blank_code_brand(
        norm_name,
        exclude_brand_id=brand.id if brand is not None else None,
    )

    if brand is None:
        if blank_brand is not None:
            brand = blank_brand
            blank_brand = None

    if brand is None:
        brand = Brand.objects.create(
            name=norm_name,
            grantee_code=norm_code,
            full_name=norm_name,
        )
    else:
        update_fields = []
        if not brand.grantee_code:
            brand.grantee_code = norm_code
            update_fields.append('grantee_code')

        brand_full_name_key = _normalize_brand_identity(brand.full_name)
        grantee_name_key = _normalize_brand_identity(norm_name)
        if not brand.full_name or (blank_brand is not None and brand_full_name_key != grantee_name_key):
            brand.full_name = norm_name
            update_fields.append('full_name')
        if not brand.alias and blank_brand is not None and blank_brand.alias:
            brand.alias = blank_brand.alias
            update_fields.append('alias')
        if update_fields:
            brand.save(update_fields=update_fields)

    manufacturer = Manufacturer.objects.filter(full_name__iexact=norm_name).first()
    if manufacturer is None:
        manufacturer = Manufacturer.objects.create(
            full_name=norm_name,
            alias=brand.alias or brand.name,
        )
    elif not manufacturer.alias and (brand.alias or brand.name):
        manufacturer.alias = brand.alias or brand.name
        manufacturer.save(update_fields=['alias'])

    manufacturer.brands.add(brand)
    return brand, manufacturer

def fetch_and_sync_fcc_id(fcc_id_query, start_date=None, end_date=None, force_reload=False, honor_skip_lists=True, preserve_existing=False):
    """
    Fetches FCC ID data using curl_cffi and saves it to the database.

    Args:
        fcc_id_query:  FCC ID or grantee code to query.
        start_date:    Optional date/datetime — only grants on or after this date.
        end_date:      Optional date/datetime — only grants on or before this date.
        force_reload:  When True, skip stale-lookup checks and re-download all docs.
        honor_skip_lists: When False, ignore SyncSkippedGrantee (manual syncs).
        preserve_existing: When True, do not overwrite non-empty radio fields
                       (freq_bands_tx, power_watts, grant_date, etc.) — only
                       fill in fields that are currently blank.

    Returns (count_added, count_updated, messages)
    """
    messages = []
    ignored_codes = set(IgnoredGrantee.ignored_codes())
    skipped_codes = set(SyncSkippedGrantee.skipped_codes())
    q_grantee = _exact_grantee_query(fcc_id_query)
    if not q_grantee:
        q_grantee, _ = split_fcc_id(_clean_query(fcc_id_query))
    q_grantee = normalize_grantee_code(q_grantee)
    if q_grantee and q_grantee in ignored_codes and honor_skip_lists:
        message = f"Skipped FCC query '{fcc_id_query}' because grantee {q_grantee} is on the ignore list."
        messages.append(message)
        logger.info(
            "FCC sync skipped ignored query query=%s ignored_grantee=%s",
            fcc_id_query,
            q_grantee,
        )
        return 0, 0, messages
    if q_grantee and q_grantee in skipped_codes and honor_skip_lists:
        message = (
            f"Skipped FCC query '{fcc_id_query}' because grantee "
            f"{q_grantee} is on the sync-skipped list."
        )
        messages.append(message)
        logger.info(
            "FCC sync skipped query query=%s skipped_grantee=%s",
            fcc_id_query,
            q_grantee,
        )
        return 0, 0, messages
    request_url = f"{URL}fccId={fcc_id_query}"
    if start_date is not None:
        # Accept either a date or datetime object.
        sd = start_date.date() if hasattr(start_date, 'date') else start_date
        request_url += f"&startDate={sd.strftime('%m/%d/%Y')}"
    if end_date is not None:
        ed = end_date.date() if hasattr(end_date, 'date') else end_date
        request_url += f"&endDate={ed.strftime('%m/%d/%Y')}"
    messages.append(f"Querying FCC API: {request_url}")
    logger.info("FCC sync request started query=%s start_date=%s end_date=%s", fcc_id_query, start_date, end_date)

    # Reset the connection-down flags at the start of each sync so that a
    # temporary FCC outage from a prior sync doesn't block a new one.
    global _fcc_connection_down, _fcc_playwright_down
    _fcc_connection_down = False
    _fcc_playwright_down = False

    try:
        response = _fcc_request_with_retry('get', request_url, impersonate="chrome124", timeout=15)
    except Exception as e:
        messages.append(f"Request failed with error: {e}")
        logger.exception("FCC sync request failed query=%s", fcc_id_query)
        return 0, 0, messages
    
    if response.status_code != 200:
        messages.append(f"Error: Received status code {response.status_code}")
        logger.warning("FCC sync non-200 status query=%s status=%s", fcc_id_query, response.status_code)
        return 0, 0, messages

    try:
        data = xmltodict.parse(response.text)
        wrapper = data.get("fCCIDInfoes", {})
        if not wrapper:
            logger.info("FCC sync empty wrapper query=%s", fcc_id_query)
            return 0, 0, messages
        result = wrapper.get("fccidInfo", [])
    except Exception as e:
        messages.append(f"Failed to parse XML response: {e}")
        logger.exception("FCC sync parse failed query=%s", fcc_id_query)
        return 0, 0, messages

    records = [result] if isinstance(result, dict) else (result if result else [])
    if not records:
        logger.info(
            "FCC sync no records returned query=%s start_date=%s end_date=%s — skipping",
            fcc_id_query,
            start_date,
            end_date,
        )
        messages.append(
            f"No grant applications returned from FCC for '{fcc_id_query}' "
            + (f"since {start_date.strftime('%m/%d/%Y')}" if start_date else "(full history)")
            + ". Nothing to process."
        )
        return 0, 0, messages
    exact_grantee = _exact_grantee_query(fcc_id_query)
    # When the user submits a specific full FCC ID (e.g. "2AN62-GC5"), the allowlist
    # is too aggressive: filings like "Change in Identification" contain no radio
    # keywords but are legitimate updates to an existing device grant.
    # Only enforce the allowlist for bulk grantee-code scans.
    is_specific_fcc_id = '-' in _clean_query(fcc_id_query)
    allowlist_terms = _radio_allowlist_terms()
    denylist_terms = _radio_denylist_terms()
    skipped_non_exact = 0
    skipped_ignored = 0
    skipped_non_radio = 0
    skipped_denylist = 0
    skipped_ignored_rule_part = 0
    skipped_stale = 0
    attached_reports = 0
    synced_oet_docs = 0
    brand_cache = {}
    lookup_started_at = timezone.now()

    count_added = 0
    count_updated = 0
    logger.info("FCC sync parsing records query=%s record_count=%s", fcc_id_query, len(records))
    for idx, res in enumerate(records, start=1):
        fcc_id = res.get('FCCId', '')
        if not fcc_id:
            continue

        grantee_code, product_code = split_fcc_id(fcc_id)
        grantee_code = normalize_grantee_code(grantee_code)
        if not product_code:
            product_code = fcc_id

        if grantee_code and grantee_code in ignored_codes:
            skipped_ignored += 1
            logger.info(
                "FCC ingest skipped ignored grantee source=fcc_api query=%s ignored_grantee=%s fcc_id=%s",
                fcc_id_query,
                grantee_code,
                fcc_id,
            )
            continue

        # FCC endpoint may return prefix-like results for grantee searches.
        # Enforce exact grantee code matching when query is a standalone grantee code.
        if exact_grantee and grantee_code != exact_grantee:
            skipped_non_exact += 1
            logger.info(
                "FCC sync skipped non-exact grantee query=%s expected_grantee=%s got_grantee=%s fcc_id=%s",
                fcc_id_query,
                exact_grantee,
                grantee_code,
                fcc_id,
            )
            continue

        raw_brand_name = (res.get("grantee", '') or '').strip()
        if not raw_brand_name:
            raw_brand_name = grantee_code

        brand_cache_key = (grantee_code, raw_brand_name)
        if brand_cache_key in brand_cache:
            auth_brand, auth_mfr = brand_cache[brand_cache_key]
        else:
            # Lookup only — don't create Brand/Manufacturer until we
            # confirm at least one record matches the allowlist.
            auth_brand = _find_existing_grantee_brand(grantee_code, raw_brand_name)
            auth_mfr = None
            brand_cache[brand_cache_key] = (auth_brand, auth_mfr)
        auth_brand_name = _resolve_authoritative_radio_brand_name(
            auth_brand,
            grantee_code,
            raw_brand_name,
        )

        validation = validate_fcc_brand_assignment(fcc_id, raw_brand_name)
        brand_val = validation.get('resolved_brand_name') or raw_brand_name or grantee_code
        if brand_val == raw_brand_name and auth_brand_name:
            brand_val = auth_brand_name

        if validation.get('status') == 'white_label_possible':
            logger.info(
                "FCC validation white-label candidate source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s grantee_brand=%s provided_brand=%s resolved_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('grantee_brand_name', ''),
                validation.get('provided_brand_name', ''),
                brand_val,
            )
        elif validation.get('status') == 'unknown_grantee':
            logger.warning(
                "FCC validation unknown grantee source=fcc_api query=%s record_index=%s fcc_id=%s inferred_grantee=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('inferred_grantee_code', ''),
                validation.get('provided_brand_name', ''),
            )
        elif validation.get('status') == 'invalid_fcc_id':
            logger.warning(
                "FCC validation invalid id source=fcc_api query=%s record_index=%s fcc_id=%s provided_brand=%s",
                fcc_id_query,
                idx,
                fcc_id,
                validation.get('provided_brand_name', ''),
            )

        radios_with_fcc = list(Radio.objects.filter(fcc_id__iexact=fcc_id))
        existing_radio = None
        if not radios_with_fcc:
            existing_radio = Radio.objects.filter(brand=brand_val, model=product_code).first()

        stale_radios = {}
        has_radio = False

        for radio in radios_with_fcc:
            should_skip, rec_modified = _should_skip_supporting_lookup(
                res, radio.last_fccid_lookup_at, start_date=start_date,
            )
            if force_reload:
                should_skip = False
            stale_radios[radio.id] = (should_skip, rec_modified)
            if not should_skip:
                has_radio = True

        if existing_radio:
            should_skip, rec_modified = _should_skip_supporting_lookup(
                res,
                existing_radio.last_fccid_lookup_at,
                start_date=start_date,
            )
            if force_reload:
                should_skip = False
            stale_radios[existing_radio.id] = (should_skip, rec_modified)
            if not should_skip:
                has_radio = True

        if not radios_with_fcc and not existing_radio:
            has_radio = True

        if not has_radio:
            # When a date filter is active and the FCC record hasn't changed since
            # the last lookup, there is genuinely nothing new for this grant — skip
            # all secondary processing and move on to the next record.
            if start_date is not None:
                stale_count = len(radios_with_fcc) + (1 if existing_radio else 0)
                skipped_stale += stale_count
                if stale_count:
                    last_modified = max(
                        (stale_radios[r.id][1] for r in radios_with_fcc
                         if stale_radios.get(r.id, (False, None))[1]),
                        default=None,
                    )
                    if existing_radio:
                        _, ex_mod = stale_radios.get(existing_radio.id, (False, None))
                        if ex_mod and (last_modified is None or ex_mod > last_modified):
                            last_modified = ex_mod
                    logger.info(
                        "FCC ingest skipped stale lookups source=fcc_api query=%s "
                        "fcc_id=%s stale_count=%s latest_record_modified=%s",
                        fcc_id_query,
                        fcc_id,
                        stale_count,
                        last_modified.isoformat() if last_modified else '',
                    )
                continue

            # No date filter (full-history scan): still retry OET doc sync so missing
            # exhibit links can be backfilled on previously processed radios.
            sec_metadata = _sync_metadata_cache.get(fcc_id)
            if sec_metadata is None:
                sec_metadata = fetch_fcc_secondary_metadata(fcc_id)
                _sync_metadata_cache[fcc_id] = sec_metadata

            stale_count = len(radios_with_fcc) + (1 if existing_radio else 0)
            for radio in radios_with_fcc:
                synced_oet_docs += _sync_oet_documents_for_radio(radio, fcc_id, sec_metadata, force_reload=force_reload)
                _assign_service_types_from_rule_parts(
                    radio, sec_metadata.get('rule_parts', []),
                )
                _stamp_lookup_timestamp(radio, lookup_started_at)
            if existing_radio:
                synced_oet_docs += _sync_oet_documents_for_radio(existing_radio, fcc_id, sec_metadata, force_reload=force_reload)
                _assign_service_types_from_rule_parts(
                    existing_radio, sec_metadata.get('rule_parts', []),
                )
                _stamp_lookup_timestamp(existing_radio, lookup_started_at)
            skipped_stale += stale_count
            if stale_count:
                logger.info(
                    "FCC ingest skipped stale lookups (full-history OET backfill) "
                    "source=fcc_api query=%s fcc_id=%s stale_count=%s",
                    fcc_id_query,
                    fcc_id,
                    stale_count,
                )
            continue

        sec_metadata = _sync_metadata_cache.get(fcc_id)
        if sec_metadata is None:
            # Quick pre-check: if the primary FCC API record has no radio
            # keywords, skip the expensive secondary metadata fetch
            # (curl_cffi + Playwright fallback = 15-30s per ID).
            # Only applies to bulk grantee scans — specific FCC ID and
            # Change-in-ID queries always proceed.
            if (
                allowlist_terms
                and not is_specific_fcc_id
                and not _primary_record_matches_allowlist(
                    res, allowlist_terms,
                )
            ):
                skipped_non_radio += 1
                logger.info(
                    "FCC ingest quick-skip no radio keywords in primary "
                    "record query=%s fcc_id=%s primary_purpose=%s",
                    fcc_id_query, fcc_id,
                    res.get('applicationPurpose', ''),
                )
                continue

            sec_metadata = fetch_fcc_secondary_metadata(fcc_id)
            _sync_metadata_cache[fcc_id] = sec_metadata

        app_purpose = (res.get('applicationPurpose', '') or '')
        is_change_in_id = 'change in identification' in app_purpose.lower()

        # For Change-in-Identification filings, the technical specs (rule parts,
        # power, emission designators) live under the original FCC ID, not the
        # re-label ID.  Follow the chain to get the actual certification data.
        if is_change_in_id:
            orig_fcc_id = _extract_original_fcc_id_from_cid(app_purpose)
            # Fallback 1: the XML API often truncates application_purpose to
            # just "Change in Identification".  The full text with the
            # original FCC ID may be available in sec_metadata's text_blob
            # (from the HTML GenericSearch results).
            if not orig_fcc_id:
                text_blob = sec_metadata.get('text_blob', '')
                if text_blob:
                    orig_fcc_id = _extract_original_fcc_id_from_cid(text_blob)
                    if orig_fcc_id:
                        logger.info(
                            "FCC CID chain found original FCC ID in text_blob "
                            "fcc_id=%s orig_fcc_id=%s",
                            fcc_id, orig_fcc_id,
                        )
            # Fallback 2: the TCB Form 731 report is the authoritative source
            # for CID filing details.  _fetch_rule_parts_from_tcb_report
            # extracts the original FCC ID from the report HTML.
            if not orig_fcc_id:
                orig_fcc_id = sec_metadata.get('original_fcc_id_from_tcb', '')
                if orig_fcc_id:
                    logger.info(
                        "FCC CID chain found original FCC ID in TCB report "
                        "fcc_id=%s orig_fcc_id=%s",
                        fcc_id, orig_fcc_id,
                    )
            # Fallback 3: scan the XML batch records for an Original
            # Equipment filing with matching product code.  CID filings
            # (e.g. 2AU74UV-S9) often share a product code prefix with
            # the original device (e.g. 2AU74UV-S9PLUS) returned in the
            # same FCC API response.
            if not orig_fcc_id:
                cid_product = (product_code or '').upper()
                for batch_res in records:
                    batch_fcc = batch_res.get('FCCId', '')
                    if not batch_fcc or batch_fcc == fcc_id:
                        continue
                    batch_purpose = (
                        batch_res.get('applicationPurpose', '') or ''
                    ).lower()
                    if 'original equipment' not in batch_purpose:
                        continue
                    _, batch_product = split_fcc_id(batch_fcc)
                    batch_product = (batch_product or '').upper()
                    # Match if one product code is a prefix of the other
                    # (e.g. 'UV-S9' matches 'UV-S9PLUS')
                    if (
                        batch_product
                        and (
                            batch_product.startswith(cid_product)
                            or cid_product.startswith(batch_product)
                        )
                    ):
                        orig_fcc_id = batch_fcc.upper()
                        logger.info(
                            "FCC CID chain found original FCC ID in batch "
                            "records fcc_id=%s orig_fcc_id=%s",
                            fcc_id, orig_fcc_id,
                        )
                        break
            if orig_fcc_id:
                orig_fcc_id = orig_fcc_id.upper()
                if orig_fcc_id != (fcc_id or '').upper():
                    orig_metadata = _sync_metadata_cache.get(orig_fcc_id)
                    if orig_metadata is None:
                        orig_metadata = fetch_fcc_secondary_metadata(orig_fcc_id)
                        _sync_metadata_cache[orig_fcc_id] = orig_metadata
                    if orig_metadata:
                        orig_rule_parts = orig_metadata.get('rule_parts', [])
                        if orig_rule_parts:
                            current_parts = set(sec_metadata.get('rule_parts', []))
                            current_parts.update(orig_rule_parts)
                            sec_metadata['rule_parts'] = sorted(current_parts)
                            logger.info(
                                "FCC CID chain original rule_parts merged "
                                "fcc_id=%s orig_fcc_id=%s orig_rule_parts=%s "
                                "merged_rule_parts=%s",
                                fcc_id, orig_fcc_id, orig_rule_parts,
                                sec_metadata['rule_parts'],
                            )
                        # Also merge original OE rows so frequency/power data
                        # from the original grant is available for detection
                        orig_oe_rows = orig_metadata.get('original_equipment_rows', [])
                        if orig_oe_rows:
                            current_oe = list(sec_metadata.get('original_equipment_rows', []))
                            current_oe.extend(orig_oe_rows)
                            sec_metadata['original_equipment_rows'] = current_oe

        # ── Device classification using FCC technical fields ──
        # The classifier checks rule parts, frequency ranges, and emission
        # designators — the authoritative FCC certification fields that
        # tell us what the device actually IS.  This replaces the old
        # keyword-matching allowlist which was matching marketing terms
        # against technical field names (company names like "Kenwood"
        # never contain "TRANSCEIVER").
        is_radio_device, classifier_tags = _classify_fcc_device(res, sec_metadata)

        # Supplementary: still run the keyword allowlist against grant notes
        # and text_blob for tagging purposes (stored on the radio model).
        kw_matched = _allowlist_match_terms(res, sec_metadata, allowlist_terms)

        if not is_radio_device and not is_specific_fcc_id and not is_change_in_id:
            # Even for non-radio classifications, ingest OET exhibits for existing FCC-linked radios.
            for radio in radios_with_fcc:
                should_skip, _ = stale_radios.get(radio.id, (False, None))
                if should_skip:
                    continue
                synced_oet_docs += _sync_oet_documents_for_radio(radio, fcc_id, sec_metadata, force_reload=force_reload)
                _assign_service_types_from_rule_parts(
                    radio, sec_metadata.get('rule_parts', []),
                )
                _stamp_lookup_timestamp(radio, lookup_started_at)
            skipped_non_radio += 1
            logger.info(
                "FCC ingest skipped record source=fcc_api query=%s "
                "fcc_id=%s reason=not_a_radio_device "
                "rule_parts=%s primary_purpose=%s "
                "metadata_record_count=%s",
                fcc_id_query,
                fcc_id,
                sec_metadata.get('rule_parts', []),
                res.get('applicationPurpose', ''),
                sec_metadata.get('record_count', 0),
            )
            continue

        # Combine classifier tags with keyword matches for storage.
        # Classifier tags are authoritative (GMRS, FRS, CB, VHF, UHF, etc.).
        # Keyword matches are descriptive (TRANSCEIVER, PORTABLE RADIO, etc.).
        stored_terms = sorted(set(classifier_tags + kw_matched))

        # After the allowlist passes, check the denylist.  A device that
        # matched "RECEIVER" (allowlist) but also matches "SPEAKER
        # MICROPHONE" (denylist) is an accessory, not a radio — skip it.
        # Denylist is also skipped for specific FCC ID lookups and
        # Change-in-ID filings, matching the allowlist exception policy.
        if denylist_terms and not is_specific_fcc_id and not is_change_in_id:
            denied_terms = _denylist_match_terms(res, sec_metadata, denylist_terms)
            if denied_terms:
                # Still backfill OET docs for existing FCC-linked radios.
                for radio in radios_with_fcc:
                    should_skip, _ = stale_radios.get(radio.id, (False, None))
                    if should_skip:
                        continue
                    synced_oet_docs += _sync_oet_documents_for_radio(
                        radio, fcc_id, sec_metadata, force_reload=force_reload,
                    )
                    _assign_service_types_from_rule_parts(
                        radio, sec_metadata.get('rule_parts', []),
                    )
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                skipped_denylist += 1
                logger.info(
                    "FCC ingest skipped record source=fcc_api query=%s "
                    "fcc_id=%s reason=denylist_match "
                    "denied_terms=%s allow_terms=%s "
                    "primary_purpose=%s",
                    fcc_id_query,
                    fcc_id,
                    ','.join(denied_terms),
                    ','.join(kw_matched),
                    res.get('applicationPurpose', ''),
                )
                continue

        # Check for ignored rule parts (e.g. 15.231 — low-power periodic
        # transmitters like garage door openers and car key fobs).
        rule_parts_for_check = sec_metadata.get('rule_parts', []) or []
        if _rule_parts_match_ignored(rule_parts_for_check):
            for radio in radios_with_fcc:
                should_skip, _ = stale_radios.get(radio.id, (False, None))
                if should_skip:
                    continue
                _stamp_lookup_timestamp(radio, lookup_started_at)
            skipped_ignored_rule_part += 1
            logger.info(
                "FCC ingest skipped record source=fcc_api query=%s "
                "fcc_id=%s reason=ignored_rule_part "
                "rule_parts=%s",
                fcc_id_query,
                fcc_id,
                rule_parts_for_check,
            )
            continue

        # Allowlist passed for this record — now ensure brand and
        # manufacturer exist in the database.  Deferred until here
        # to avoid creating orphaned Brand/Manufacturer rows for
        # grantees whose FCC records never match the allowlist.
        if auth_mfr is None:
            auth_brand, auth_mfr = _ensure_grantee_brand_and_manufacturer(
                grantee_code,
                raw_brand_name,
            )
            brand_cache[brand_cache_key] = (auth_brand, auth_mfr)

        oe_summary = _extract_original_equipment_summary(res, sec_metadata)

        # Detect NOAA WX and Bluetooth from original-equipment frequency rows
        _noaa_wx, _bt = _detect_noaa_and_bluetooth(oe_summary, sec_metadata)

        # Format new details for notes
        grant_date = res.get("grantDate", "N/A")
        app_purpose_str = app_purpose or "N/A"
        new_notes = f"FCC Grant Date: {grant_date} | Purpose: {app_purpose_str}"
        if is_change_in_id:
            orig_fcc = _extract_original_fcc_id_from_cid(app_purpose)
            if orig_fcc:
                new_notes += f" | Original FCC ID: {orig_fcc}"

        # "Change in Identification" means the grantee name changed after initial filing —
        # the device was built under one company and is now sold under another, i.e. white label.
        # Note: is_change_in_id is already computed above for the allowlist check.

        # Check if Radio already exists
        if radios_with_fcc:
            for radio in radios_with_fcc:
                should_skip, rec_modified = stale_radios.get(radio.id, (False, None))
                if should_skip:
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                    skipped_stale += 1
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        radio.id,
                        radio.brand,
                        radio.model,
                        fcc_id,
                        rec_modified.isoformat() if rec_modified else '',
                        radio.last_fccid_lookup_at.isoformat() if radio.last_fccid_lookup_at else '',
                    )
                    continue

                has_changes = False
                if new_notes not in radio.notes:
                    radio.notes = f"{new_notes}\n{radio.notes}".strip()
                    has_changes = True

                if is_change_in_id and not radio.is_a_whitelabel:
                    radio.is_a_whitelabel = True
                    has_changes = True
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, radio.id, radio.brand, radio.model, fcc_id,
                    )

                derived_grant_date = oe_summary.get('grant_date')
                if derived_grant_date and radio.grant_date != derived_grant_date:
                    if not preserve_existing or not radio.grant_date:
                        radio.grant_date = derived_grant_date
                        has_changes = True

                derived_intro_year = oe_summary.get('intro_year')
                if derived_intro_year and not radio.grant_date:
                    if not preserve_existing or not radio.grant_date:
                        radio.grant_date = date(derived_intro_year, 1, 1)
                        has_changes = True

                if _noaa_wx and not radio.noaa_wx:
                    radio.noaa_wx = True
                    has_changes = True
                if _bt and not radio.bluetooth:
                    radio.bluetooth = True
                    has_changes = True

                if brand_val and radio.brand != brand_val:
                    if not preserve_existing:
                        radio_brand_key = _normalize_brand_identity(radio.brand)
                        raw_brand_key = _normalize_brand_identity(raw_brand_name)
                        if not radio_brand_key or radio_brand_key == raw_brand_key:
                            radio.brand = brand_val
                            has_changes = True

                if auth_mfr and radio.manufacturer_id != auth_mfr.id:
                    if not preserve_existing or not radio.manufacturer_id:
                        radio.manufacturer = auth_mfr
                        has_changes = True

                if has_changes:
                    radio.last_fccid_lookup_at = lookup_started_at
                    radio.allowlist_terms = sorted(set(
                        (radio.allowlist_terms or []) + stored_terms,
                    ))
                    radio.save()
                    count_updated += 1
                    logger.info(
                        (
                            'FCC ingest update source=fcc_api query=%s action=update_by_fcc_id'
                            ' radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s'
                            ' grant_date=%s is_whitelabel=%s'
                            ' allowlist_terms=%s',
                            fcc_id_query, radio.id, radio.brand, radio.model,
                            fcc_id, validation.get('status', ''),
                            radio.grant_date, radio.is_a_whitelabel,
                            radio.allowlist_terms,
                        )
                    )
                else:
                    _stamp_lookup_timestamp(radio, lookup_started_at)
                    if stored_terms:
                        Radio.objects.filter(pk=radio.pk).update(
                            allowlist_terms=sorted(set(
                                (radio.allowlist_terms or []) + stored_terms,
                            )),
                        )
                attached_reports += _attach_test_reports_to_radio(radio, fcc_id, sec_metadata, force_reload=force_reload)
                synced_oet_docs += _sync_oet_documents_for_radio(radio, fcc_id, sec_metadata, force_reload=force_reload)
                # Sync RadioCertification records with per-grant detail
                _sync_radio_certifications(radio, fcc_id, sec_metadata)
                # Auto-assign service types from FCC API rule_parts
                api_rule_parts = sec_metadata.get('rule_parts', [])
                if api_rule_parts:
                    logger.info(
                        "FCC API rule_parts found fcc_id=%s rule_parts=%s",
                        fcc_id, api_rule_parts,
                    )
                _assign_service_types_from_rule_parts(radio, api_rule_parts)
                # Detect amateur radios and scrape website for TX specs
                _detect_and_scrape_amateur_radio(radio, sec_metadata)
        else:
            if existing_radio:
                should_skip, rec_modified = stale_radios.get(existing_radio.id, (False, None))
                if should_skip:
                    _stamp_lookup_timestamp(existing_radio, lookup_started_at)
                    skipped_stale += 1
                    logger.info(
                        "FCC ingest skipped stale lookup source=fcc_api query=%s radio_id=%s brand=%s model=%s fcc_id=%s record_last_modified=%s last_lookup_at=%s",
                        fcc_id_query,
                        existing_radio.id,
                        existing_radio.brand,
                        existing_radio.model,
                        fcc_id,
                        rec_modified.isoformat() if rec_modified else '',
                        existing_radio.last_fccid_lookup_at.isoformat() if existing_radio.last_fccid_lookup_at else '',
                    )
                    continue

                # Update the existing radio instead of creating a duplicate
                if fcc_id and existing_radio.fcc_id != fcc_id:
                    old_fcc = existing_radio.fcc_id
                    existing_radio.fcc_id = fcc_id
                    logger.info(
                        "FCC ingest normalized fcc_id source=fcc_api "
                        "radio_id=%s old=%s new=%s",
                        existing_radio.id, old_fcc, fcc_id,
                    )
                if new_notes not in existing_radio.notes:
                    existing_radio.notes = f"{new_notes}\n{existing_radio.notes}".strip()

                if is_change_in_id and not existing_radio.is_a_whitelabel:
                    existing_radio.is_a_whitelabel = True
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, existing_radio.id,
                        existing_radio.brand, existing_radio.model, fcc_id,
                    )

                derived_grant_date = oe_summary.get('grant_date')
                if derived_grant_date and existing_radio.grant_date != derived_grant_date:
                    if not preserve_existing or not existing_radio.grant_date:
                        existing_radio.grant_date = derived_grant_date

                derived_intro_year = oe_summary.get('intro_year')
                if derived_intro_year and not existing_radio.grant_date:
                    if not preserve_existing or not existing_radio.grant_date:
                        existing_radio.grant_date = date(derived_intro_year, 1, 1)

                if _noaa_wx and not existing_radio.noaa_wx:
                    existing_radio.noaa_wx = True
                if _bt and not existing_radio.bluetooth:
                    existing_radio.bluetooth = True

                if brand_val and existing_radio.brand != brand_val:
                    if not preserve_existing:
                        radio_brand_key = _normalize_brand_identity(existing_radio.brand)
                        raw_brand_key = _normalize_brand_identity(raw_brand_name)
                        if not radio_brand_key or radio_brand_key == raw_brand_key:
                            existing_radio.brand = brand_val

                if auth_mfr and existing_radio.manufacturer_id != auth_mfr.id:
                    if not preserve_existing or not existing_radio.manufacturer_id:
                        existing_radio.manufacturer = auth_mfr

                existing_radio.last_fccid_lookup_at = lookup_started_at
                existing_radio.allowlist_terms = sorted(set(
                    (existing_radio.allowlist_terms or []) + stored_terms,
                ))
                existing_radio.save()
                count_updated += 1
                logger.info(
                    "FCC ingest update source=fcc_api query=%s action=update_by_brand_model"
                    " radio_id=%s brand=%s model=%s fcc_id=%s validation=%s"
                    " grant_date=%s",
                    fcc_id_query, existing_radio.id, brand_val, product_code,
                    fcc_id, validation.get('status', ''),
                    existing_radio.grant_date,
                )
                attached_reports += _attach_test_reports_to_radio(existing_radio, fcc_id, sec_metadata, force_reload=force_reload)
                synced_oet_docs += _sync_oet_documents_for_radio(existing_radio, fcc_id, sec_metadata, force_reload=force_reload)
                _sync_radio_certifications(existing_radio, fcc_id, sec_metadata)
                _assign_service_types_from_rule_parts(
                    existing_radio, sec_metadata.get('rule_parts', []),
                )
                _detect_and_scrape_amateur_radio(existing_radio, sec_metadata)
            else:
                created_radio = Radio.objects.create(
                    brand=brand_val,
                    model=product_code,
                    manufacturer=auth_mfr,
                    fcc_id=fcc_id,
                    notes=new_notes,
                    is_a_whitelabel=is_change_in_id,
                    grant_date=oe_summary.get('grant_date'),
                    freq_bands_tx=oe_summary.get('freq_bands_tx', ''),
                    noaa_wx=_noaa_wx,
                    bluetooth=_bt,
                    last_fccid_lookup_at=lookup_started_at,
                    allowlist_terms=stored_terms,
                )
                if is_change_in_id:
                    logger.info(
                        "FCC ingest white_label_flagged source=change_in_id query=%s radio_id=%s brand=%s model=%s fcc_id=%s",
                        fcc_id_query, created_radio.id, brand_val, product_code, fcc_id,
                    )
                count_added += 1
                logger.info(
                    (
                        'FCC ingest create source=fcc_api query=%s action=create'
                        ' radio_id=%s brand=%s model=%s fcc_id=%s validation_status=%s'
                        ' inferred_grantee=%s grant_date=%s',
                        fcc_id_query, created_radio.id, brand_val, product_code,
                        fcc_id, validation.get('status', ''),
                        validation.get('inferred_grantee_code', ''),
                        created_radio.grant_date,
                    )
                )
                attached_reports += _attach_test_reports_to_radio(created_radio, fcc_id, sec_metadata, force_reload=force_reload)
                synced_oet_docs += _sync_oet_documents_for_radio(created_radio, fcc_id, sec_metadata, force_reload=force_reload)
                _sync_radio_certifications(created_radio, fcc_id, sec_metadata)
                _assign_service_types_from_rule_parts(
                    created_radio, sec_metadata.get('rule_parts', []),
                )
                _detect_and_scrape_amateur_radio(created_radio, sec_metadata)

    if exact_grantee and skipped_non_exact:
        messages.append(
            f"Filtered {skipped_non_exact} non-exact grantee matches while enforcing exact grantee code {exact_grantee}."
        )
    if skipped_ignored:
        messages.append(
            f"Skipped {skipped_ignored} FCC record(s) because their grantee code is on the ignore list."
        )
    if skipped_non_radio:
        messages.append(
            f"Skipped {skipped_non_radio} records that did not match FCC_RADIO_ALLOWLIST_TERMS ({','.join(allowlist_terms)})."
        )
    if skipped_denylist:
        messages.append(
            f"Skipped {skipped_denylist} records that matched FCC_RADIO_DENYLIST_TERMS "
            f"({','.join(denylist_terms[:10])}{'...' if len(denylist_terms) > 10 else ''})."
        )
    if skipped_stale:
        messages.append(
            f"Skipped {skipped_stale} radio records because FCC last-modified data was not newer than the prior lookup timestamp."
        )
    if attached_reports:
        messages.append(f"Attached {attached_reports} FCC test report files.")
    if synced_oet_docs:
        messages.append(f"Synced {synced_oet_docs} OET exhibit documents.")
    messages.append(f"Successfully processed {len(records)} records for {fcc_id_query}.")
    logger.info(
        "FCC sync completed query=%s added=%s updated=%s exact_grantee=%s skipped_non_exact=%s skipped_non_radio=%s skipped_denylist=%s skipped_ignored_rule_part=%s skipped_stale_lookup=%s attached_test_reports=%s synced_oet_documents=%s",
        fcc_id_query,
        count_added,
        count_updated,
        exact_grantee,
        skipped_non_exact,
        skipped_non_radio,
        skipped_denylist,
        skipped_ignored_rule_part,
        skipped_stale,
        attached_reports,
        synced_oet_docs,
    )
    return count_added, count_updated, messages
