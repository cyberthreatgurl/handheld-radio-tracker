"""Regression tests for the maintenance page storage gauge."""

# pylint: disable=no-member, missing-function-docstring
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# missing-function-docstring: test methods are self-documenting by name

from collections import namedtuple
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

DiskUsage = namedtuple('DiskUsage', 'total used free')


class MaintenanceStorageGaugeTest(TestCase):
    """The maintenance page must surface artifacts storage usage."""

    def _login_staff(self):
        user = User.objects.create_user(
            username='staff',
            password='testpass123',
            is_staff=True,
        )
        self.client.force_login(user)

    def test_gauge_renders_with_storage_context(self):
        self._login_staff()
        with patch(
            'radios.views.shutil.disk_usage',
            return_value=DiskUsage(total=100_000_000_000, used=37_500_000_000, free=62_500_000_000),
        ):
            response = self.client.get(reverse('maintenance'))

        self.assertEqual(response.status_code, 200)
        storage = response.context['storage']
        self.assertEqual(storage['percent_used'], 37.5)
        self.assertEqual(storage['needle_angle'], -22.5)
        self.assertEqual(storage['needle_color'], '#10b981')
        self.assertContains(response, 'Storage Used by Artifacts')
        self.assertContains(response, '37.5%')

    def test_gauge_shows_fallback_when_storage_unavailable(self):
        self._login_staff()
        with patch(
            'radios.views.shutil.disk_usage',
            side_effect=OSError('unavailable'),
        ):
            response = self.client.get(reverse('maintenance'))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['storage'])
        self.assertContains(response, 'Storage information unavailable.')
