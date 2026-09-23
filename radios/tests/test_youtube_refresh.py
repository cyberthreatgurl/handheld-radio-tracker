"""Regression tests for the YouTube video pre-fill and refresh flow."""

# pylint: disable=no-member, missing-function-docstring
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# missing-function-docstring: test methods are self-documenting by name

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Radio, YouTubeRefreshLog
from ..youtube_utils import (
    YouTubeQuotaExceeded, search_radio_videos,
)


def _search_items(titles):
    """Build a YouTube search.list response payload from titles."""
    return {
        'items': [
            {
                'id': {'videoId': f'video{i:03d}'},
                'snippet': {'title': title},
            }
            for i, title in enumerate(titles)
        ],
    }


class SearchRadioVideosTest(TestCase):
    """youtube_utils.search_radio_videos selects model-matching top videos."""

    def _mock_response(self, payload, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    @patch('radios.youtube_utils.curl_requests.get')
    def test_returns_top_five_model_matching_watch_urls(self, mock_get):
        mock_get.return_value = self._mock_response(_search_items([
            'Baofeng UV-5R full review',
            'Baofeng UV-5R programming guide',
            'Baofeng UV-5R range test',
            'Baofeng UV-5R unboxing',
            'Baofeng UV-5R vs GT-5R',
            'Baofeng UV-82 setup',  # wrong model -> filtered out
        ]))
        with self.settings(YOUTUBE_API_KEY='test-key'):
            urls = search_radio_videos('Baofeng', 'UV-5R')

        self.assertEqual(len(urls), 5)
        self.assertIn('video000', urls[0])
        self.assertIn('video004', urls[-1])
        self.assertFalse(any('video005' in url for url in urls))

    @patch('radios.youtube_utils.curl_requests.get')
    def test_raises_without_api_key(self, _mock_get):
        with self.settings(YOUTUBE_API_KEY=''):
            with self.assertRaises(ValueError):
                search_radio_videos('Baofeng', 'UV-5R')

    @patch('radios.youtube_utils.curl_requests.get')
    def test_model_filter_is_case_and_punctuation_insensitive(self, mock_get):
        mock_get.return_value = self._mock_response(_search_items([
            'Baofeng uv5r teardown',
            'Unrelated video',
        ]))
        with self.settings(YOUTUBE_API_KEY='test-key'):
            urls = search_radio_videos('Baofeng', 'UV-5R')

        self.assertEqual(len(urls), 1)
        self.assertIn('video000', urls[0])

    @patch('radios.youtube_utils.time.sleep')
    @patch('radios.youtube_utils.curl_requests.get')
    def test_retries_after_429_then_succeeds(self, mock_get, _mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.json.return_value = {
            'error': {'errors': [{'reason': 'rateLimitExceeded'}]},
        }
        success = self._mock_response(_search_items(['Baofeng UV-5R review']))
        mock_get.side_effect = [rate_limited, success]

        with self.settings(YOUTUBE_API_KEY='test-key'):
            urls = search_radio_videos('Baofeng', 'UV-5R')

        self.assertEqual(len(urls), 1)
        self.assertEqual(mock_get.call_count, 2)

    @patch('radios.youtube_utils.curl_requests.get')
    def test_raises_quota_exceeded_on_403_quota_body(self, mock_get):
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {
            'error': {
                'errors': [{'reason': 'quotaExceeded'}],
                'message': 'quota exceeded',
            },
        }
        mock_get.return_value = resp

        with self.settings(YOUTUBE_API_KEY='test-key'):
            with self.assertRaises(YouTubeQuotaExceeded):
                search_radio_videos('Baofeng', 'UV-5R')


class RefreshRadioYoutubeViewTest(TestCase):
    """The manual refresh endpoint is admin-only and globally capped daily."""

    def setUp(self):
        self.radio = Radio.objects.create(brand='Baofeng', model='UV-5R')
        self.url = reverse('refresh_radio_youtube',
                           kwargs={'pk': self.radio.pk})
        self.edit_url = reverse('radio_edit', kwargs={'pk': self.radio.pk})

    def _superuser(self):
        return User.objects.create_superuser(
            username='admin', password='testpass123', email='admin@example.com')

    def _staff(self):
        return User.objects.create_user(
            username='staff', password='testpass123', is_staff=True)

    @patch('radios.youtube_utils.search_radio_videos')
    def test_superuser_refresh_populates_field(self, mock_search):
        mock_search.return_value = [
            'https://www.youtube.com/watch?v=video000',
            'https://www.youtube.com/watch?v=video001',
        ]
        self.client.force_login(self._superuser())
        response = self.client.post(self.url)

        self.assertRedirects(response, self.edit_url)
        self.radio.refresh_from_db()
        self.assertEqual(
            self.radio.youtube_video_urla,
            'https://www.youtube.com/watch?v=video000\n'
            'https://www.youtube.com/watch?v=video001',
        )
        self.assertIsNotNone(self.radio.youtube_videos_refreshed_at)
        self.assertTrue(
            YouTubeRefreshLog.objects.filter(radio=self.radio).exists())

    @patch('radios.youtube_utils.search_radio_videos')
    def test_second_refresh_within_24h_is_blocked(self, mock_search):
        mock_search.return_value = ['https://www.youtube.com/watch?v=video000']
        self.client.force_login(self._superuser())
        self.client.post(self.url)
        self.radio.refresh_from_db()
        first_at = self.radio.youtube_videos_refreshed_at

        response = self.client.post(self.url)

        self.assertRedirects(response, self.edit_url)
        self.radio.refresh_from_db()
        self.assertEqual(self.radio.youtube_videos_refreshed_at, first_at)
        self.assertEqual(
            YouTubeRefreshLog.objects.filter(radio=self.radio).count(), 1)

    @patch('radios.youtube_utils.search_radio_videos')
    def test_non_superuser_is_redirected_to_login(self, mock_search):
        mock_search.return_value = ['https://www.youtube.com/watch?v=video000']
        self.client.force_login(self._staff())
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)
        self.radio.refresh_from_db()
        self.assertEqual(self.radio.youtube_video_urla, '')

    def test_refresh_allowed_again_after_24h(self):
        user = self._superuser()
        old_log = YouTubeRefreshLog.objects.create(user=user, radio=self.radio)
        old_log.created_at = timezone.now() - timedelta(days=2)
        old_log.save(update_fields=['created_at'])

        with patch('radios.youtube_utils.search_radio_videos') as mock_search:
            mock_search.return_value = [
                'https://www.youtube.com/watch?v=video000']
            self.client.force_login(user)
            response = self.client.post(self.url)

        self.assertRedirects(response, self.edit_url)
        self.radio.refresh_from_db()
        self.assertTrue(self.radio.youtube_video_urla)
