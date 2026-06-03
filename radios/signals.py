import logging
import time
import urllib.parse

import requests
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Brand, Manufacturer
from .oem_relationships import apply_oem_mapping_for_brand


logger = logging.getLogger(__name__)

# Nominatim requires an identifying User-Agent per the usage policy.
_NOMINATIM_USER_AGENT = getattr(
    settings,
    'NOMINATIM_USER_AGENT',
    'handheld-radio-tracker/1.0 (https://github.com/cyberthreatgurl/handheld-radio-tracker)',
)
_NOMINATIM_ENDPOINT = 'https://nominatim.openstreetmap.org/search'
# Nominatim usage policy: max 1 request per second.
_NOMINATIM_RATE_LIMIT_SECONDS = 1.1


def _nominatim_query(query: str) -> tuple[float | None, float | None]:
    """Send a single free-text query to Nominatim. Returns (lat, lon) or (None, None)."""
    params = {
        'q': query.strip(),
        'format': 'json',
        'limit': 1,
        'addressdetails': 0,
    }
    headers = {'User-Agent': _NOMINATIM_USER_AGENT}
    try:
        response = requests.get(
            _NOMINATIM_ENDPOINT,
            params=params,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
        return None, None
    except Exception:
        logger.exception("Nominatim query failed q=%r", query[:200])
        return None, None


def geocode_address(address: str) -> tuple[float | None, float | None, str]:
    """
    Resolve a free-text address to (latitude, longitude, precision) using Nominatim,
    with automatic fallback to progressively coarser geographic levels.

    Precision labels (coarsest-last):
      'full'    — exact address matched
      'city'    — street component dropped; city/locality matched
      'state'   — resolved to state / province level
      'country' — only the country could be resolved

    Returns (None, None, '') when nothing resolves.
    Each Nominatim call is preceded by a rate-limit sleep.
    """
    if not address or not address.strip():
        return None, None, ''

    # Split into comma-separated parts; strip each.
    parts = [p.strip() for p in address.split(',') if p.strip()]

    # Build candidate queries from most-specific to least-specific.
    # The precision label is paired with the slice of parts to attempt.
    #
    # For "123 Factory Rd, Shenzhen, Guangdong, China":
    #   full    → "123 Factory Rd, Shenzhen, Guangdong, China"  (all 4 parts)
    #   city    → "Shenzhen, Guangdong, China"                   (drop part[0])
    #   state   → "Guangdong, China"                             (drop parts[0:2])
    #   country → "China"                                        (last part only)
    #
    # If there is only one or two comma parts we still try all defined levels
    # that produce a non-empty, distinct query.

    candidates: list[tuple[str, str]] = []
    seen_queries: set[str] = set()

    def _add(label: str, fragment_parts: list[str]) -> None:
        q = ', '.join(fragment_parts)
        if q and q not in seen_queries:
            seen_queries.add(q)
            candidates.append((label, q))

    _add('full',    parts)
    _add('city',    parts[1:] if len(parts) > 1 else [])
    _add('state',   parts[2:] if len(parts) > 2 else [])
    _add('country', [parts[-1]] if parts else [])

    for precision, query in candidates:
        time.sleep(_NOMINATIM_RATE_LIMIT_SECONDS)
        lat, lon = _nominatim_query(query)
        if lat is not None:
            logger.debug(
                "Nominatim resolved precision=%s query=%r lat=%s lon=%s",
                precision, query[:200], lat, lon,
            )
            return lat, lon, precision

    return None, None, ''


@receiver(post_save, sender=Manufacturer)
def geocode_manufacturer_on_save(sender, instance, created, update_fields, **kwargs):
    """
    Geocode the manufacturer's address via Nominatim whenever the address field
    is created or updated.  Skips the signal if only unrelated fields changed.
    Stores the result in latitude/longitude and sets geocode_failed accordingly.
    """
    # Only re-geocode when the address field was explicitly updated, or on first save.
    if update_fields is not None and 'address' not in update_fields:
        return

    address = (instance.address or '').strip()
    if not address:
        # Address cleared — wipe stored coords without calling the API.
        if instance.latitude is not None or instance.longitude is not None or instance.geocode_failed:
            Manufacturer.objects.filter(pk=instance.pk).update(
                latitude=None,
                longitude=None,
                geocode_failed=False,
                geocode_precision='',
            )
        return

    lat, lon, precision = geocode_address(address)
    failed = lat is None

    Manufacturer.objects.filter(pk=instance.pk).update(
        latitude=lat,
        longitude=lon,
        geocode_failed=failed,
        geocode_precision=precision,
    )

    if failed:
        logger.warning(
            "Geocode failed manufacturer_id=%s name=%r address=%r",
            instance.pk,
            instance.full_name,
            address[:200],
        )
    else:
        logger.info(
            "Geocode success manufacturer_id=%s name=%r precision=%s lat=%s lon=%s",
            instance.pk,
            instance.full_name,
            precision,
            lat,
            lon,
        )


@receiver(post_save, sender=Brand)
def apply_oem_mapping_on_brand_save(sender, instance, created, update_fields, **kwargs):
    """
    When a user manually associates a grantee brand to a parent OEM brand,
    automatically propagate that relationship to existing radios.
    """
    if not instance.parent_brand_id:
        return

    # Run when record is created, when update fields are unknown, or when relevant fields changed.
    if update_fields is not None:
        relevant = {'parent_brand', 'grantee_code'}
        if not (set(update_fields) & relevant):
            return

    try:
        result = apply_oem_mapping_for_brand(instance)
        logger.info(
            "OEM mapping trigger source=brand_save brand_id=%s brand=%s matched=%s updated=%s created=%s",
            instance.pk,
            instance.name,
            result.get('matched', 0),
            result.get('updated', 0),
            created,
        )
    except Exception:
        logger.exception(
            "OEM mapping trigger failed source=brand_save brand_id=%s brand=%s",
            instance.pk,
            instance.name,
        )
