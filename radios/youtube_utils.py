"""YouTube Data API v3 helpers for pre-filling radio video URLs."""
# pylint: disable=broad-exception-caught
# broad-exception-caught: intentional when parsing external API error bodies

import logging
import re
import time

from curl_cffi import requests as curl_requests
from django.conf import settings

logger = logging.getLogger(__name__)

_SEARCH_URL = 'https://www.googleapis.com/youtube/v3/search'
_FETCH_COUNT = 50  # fetch extra so the model-title filter can fill the top N
_MAX_RETRIES = 4
_BACKOFF_BASE = 1.0  # seconds


class YouTubeQuotaExceeded(Exception):
    """Raised when the YouTube Data API daily quota is exhausted."""


def _normalize_terms(value):
    """Lowercase a string and strip every non-alphanumeric character."""
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def _error_reasons(payload):
    """Return the ``reason`` codes embedded in a YouTube API error body."""
    error = payload.get('error') or {}
    return [item.get('reason') for item in error.get('errors', [])]


def _backoff_delay(attempt):
    """Exponential backoff in seconds for a given retry attempt."""
    return _BACKOFF_BASE * (2 ** attempt)


def _search_request(params):
    """GET the search endpoint with retry/backoff; return the JSON body."""
    attempt = 0
    while True:
        resp = curl_requests.get(_SEARCH_URL, params=params, timeout=20)
        if resp.status_code == 200:
            return resp.json()

        try:
            body = resp.json()
        except Exception:
            body = {}

        if 'quotaExceeded' in _error_reasons(body):
            raise YouTubeQuotaExceeded(
                'YouTube Data API daily quota exceeded; try again tomorrow.')

        retryable = resp.status_code in (429, 500, 502, 503)
        if retryable and attempt < _MAX_RETRIES:
            attempt += 1
            logger.warning(
                'YouTube API transient error status=%s retry=%s',
                resp.status_code, attempt,
            )
            time.sleep(_backoff_delay(attempt))
            continue

        resp.raise_for_status()


def search_radio_videos(brand, model, max_results=5):
    """Return up to ``max_results`` YouTube watch URLs for a radio model.

    Queries the YouTube Data API v3 for ``"<brand> <model>"`` ordered by
    view count, keeps only results whose title contains the model, then
    returns the top ``max_results`` watch URLs. Raises ``ValueError`` if
    ``YOUTUBE_API_KEY`` is not configured, and ``YouTubeQuotaExceeded``
    once the daily quota is exhausted.
    """
    api_key = (settings.YOUTUBE_API_KEY or '').strip()
    if not api_key:
        raise ValueError('YOUTUBE_API_KEY is not configured')

    query = f'{brand} {model}'.strip()
    params = {
        'part': 'snippet',
        'q': query,
        'type': 'video',
        'order': 'viewCount',
        'maxResults': _FETCH_COUNT,
        'key': api_key,
    }
    data = _search_request(params)

    model_norm = _normalize_terms(model)
    urls = []
    for item in data.get('items', []):
        snippet = item.get('snippet') or {}
        title = snippet.get('title') or ''
        if model_norm and model_norm not in _normalize_terms(title):
            continue
        video_id = (item.get('id') or {}).get('videoId')
        if not video_id:
            continue
        urls.append(f'https://www.youtube.com/watch?v={video_id}')
        if len(urls) >= max_results:
            break
    return urls
