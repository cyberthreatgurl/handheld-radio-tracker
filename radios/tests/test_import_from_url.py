"""Regression tests for the import-from-URL view redirect behavior."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class ImportFromUrlRedirectTest(TestCase):
    """After a successful import the user lands on the radio edit page."""

    def setUp(self):
        staff = User.objects.create_user(
            username='staff', password='testpass123', is_staff=True,
        )
        self.client.force_login(staff)

    def test_successful_import_redirects_to_radio_edit(self):
        """A created/updated radio redirects to its edit page."""
        report = {
            'url': 'https://example.com/product',
            'brand': 'Acme',
            'model': 'X1',
            'part_number': '',
            'radio_id': 99,
            'radio_created': True,
            'updated_fields': [],
            'service_types_added': [],
            'manuals': [],
            'confidence': 1.0,
            'errors': [],
        }
        with mock.patch(
            'radios.site_import.upsert_radio_from_url',
            return_value=report,
        ):
            response = self.client.post(
                reverse('import_radio_from_url'),
                {'url': 'https://example.com/product'},
            )
        self.assertRedirects(
            response,
            reverse('radio_edit', args=[99]),
            fetch_redirect_response=False,
        )

    def test_failed_import_redirects_to_radio_list(self):
        """An import with no identifiable brand/model returns to the list."""
        report = {
            'url': 'https://example.com/product',
            'brand': '',
            'model': '',
            'part_number': '',
            'radio_id': None,
            'radio_created': False,
            'updated_fields': [],
            'service_types_added': [],
            'manuals': [],
            'confidence': None,
            'errors': ['missing_identity'],
        }
        with mock.patch(
            'radios.site_import.upsert_radio_from_url',
            return_value=report,
        ):
            response = self.client.post(
                reverse('import_radio_from_url'),
                {'url': 'https://example.com/product'},
            )
        self.assertRedirects(
            response,
            reverse('radio_list'),
            fetch_redirect_response=False,
        )
