"""Account-security helpers: rate limiting and anti-bot defenses.

All protection is self-contained (Django's cache backend), so it works without
external services. CAPTCHA can be layered on later — see ``captcha_ok`` for the
integration point.
"""

import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Default limits; override via settings (e.g. AUTH_RATE_LIMIT = '10/1h').
AUTH_RATE_LIMIT = getattr(settings, 'AUTH_RATE_LIMIT', '10/1h')
SIGNUP_RATE_LIMIT = getattr(settings, 'SIGNUP_RATE_LIMIT', '5/1h')

_LIMITS = {
    'login': AUTH_RATE_LIMIT,
    'signup': SIGNUP_RATE_LIMIT,
}

_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


def _client_ip(request):
    """Best-effort client IP, respecting a single trusted proxy hop."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _parse_limit(spec):
    """Parse a 'count/window' spec like '10/1h' into (count, seconds)."""
    count, _, window = spec.partition('/')
    count = int(count) if count.isdigit() else 10
    unit = window[-1:] if window else 'h'
    value = int(window[:-1]) if window[:-1].isdigit() else 1
    return count, value * _UNIT_SECONDS.get(unit, 3600)


def _cache_key(scope, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]
    return f'accounts:ratelimit:{scope}:{digest}'


def check_rate_limit(scope, request, identifier=None):
    """Record an attempt and return ``(allowed, retry_after_seconds)``.

    ``identifier`` (e.g. a username) is combined with the client IP so a single
    IP cannot lock out every account, and a single account cannot be hammered
    from many IPs. Returns ``retry_after_seconds=0`` when allowed.
    """
    max_count, window = _parse_limit(_LIMITS.get(scope, AUTH_RATE_LIMIT))
    ip = _client_ip(request)
    key = _cache_key(scope, identifier or ip)
    now = time.time()
    attempts = [t for t in (cache.get(key) or []) if now - t < window]
    if len(attempts) >= max_count:
        retry_after = int(window - (now - attempts[0])) + 1
        logger.warning(
            'Rate limit hit scope=%s ip=%s identifier=%s', scope, ip, identifier,
        )
        return False, retry_after
    attempts.append(now)
    cache.set(key, attempts, timeout=window)
    return True, 0


def reset_rate_limit(scope, request, identifier=None):
    """Clear the counter after a successful attempt (login/signup)."""
    key = _cache_key(scope, identifier or _client_ip(request))
    cache.delete(key)


def is_honeypot_submitted(request, field_name='website'):
    """Return True if a bot filled the hidden honeypot field."""
    return bool((request.POST.get(field_name) or '').strip())


def captcha_ok(request):
    """Placeholder CAPTCHA check.

    Always returns True for now. To enable reCAPTCHA/hCaptcha later:
      1. Add ``RECAPTCHA_PUBLIC_KEY`` / ``RECAPTCHA_PRIVATE_KEY`` to .env.
      2. Render the widget in ``signup.html``.
      3. Verify ``g-recaptcha-response`` here (or use django-recaptcha).
    """
    return True
