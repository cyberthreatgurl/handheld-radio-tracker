"""Regression tests for the consolidated brand detail/edit page.

The brand detail page (``brand_detail``) is the canonical brand URL and also
hosts inline editing. The legacy ``/edit/`` URL renders the same page with the
edit form open, and successful edits redirect back to the clean detail URL.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Brand, Radio


class BrandDetailEditTest(TestCase):
    """The brand detail page also hosts editing and is the canonical URL."""

    def setUp(self):
        self.brand = Brand.objects.create(name='TestBrand', grantee_code='2TEST')
        staff = User.objects.create_user(
            username='staff', password='testpass123', is_staff=True,
        )
        self.client.force_login(staff)

    def _post_data(self, **overrides):
        data = {
            'name': 'RenamedBrand',
            'alias': '',
            'grantee_code': '2TEST',
            'country': '',
            'website': '',
            'parent_brand': '',
            'manufacturer_oem': '',
            'white_label_vendors': [],
            'notes': '',
        }
        data.update(overrides)
        return data

    def test_detail_page_renders(self):
        """The canonical detail URL renders with the edit affordance."""
        response = self.client.get(reverse('brand_detail', args=[self.brand.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Edit Brand')

    def test_edit_url_renders_detail_with_form(self):
        """The legacy /edit/ URL renders the detail page in edit mode."""
        response = self.client.get(reverse('brand_edit', args=[self.brand.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Edit Brand')
        self.assertContains(response, 'Save Brand')

    def test_edit_query_param_shows_form(self):
        """?edit=1 opens the inline edit form on the detail page."""
        response = self.client.get(
            reverse('brand_detail', args=[self.brand.pk]) + '?edit=1'
        )
        self.assertContains(response, 'Save Brand')

    def test_post_updates_brand_and_redirects_to_detail(self):
        """Submitting the inline form saves and redirects to the detail URL."""
        response = self.client.post(
            reverse('brand_detail', args=[self.brand.pk]),
            self._post_data(),
        )
        self.assertRedirects(
            response,
            reverse('brand_detail', args=[self.brand.pk]),
        )
        self.brand.refresh_from_db()
        self.assertEqual(self.brand.name, 'RenamedBrand')

    def test_post_renames_radios(self):
        """Renaming a brand cascades to its associated radios."""
        Radio.objects.create(brand='TestBrand', model='M1', fcc_id='2TEST-M1')
        response = self.client.post(
            reverse('brand_detail', args=[self.brand.pk]),
            self._post_data(),
        )
        self.assertRedirects(
            response,
            reverse('brand_detail', args=[self.brand.pk]),
        )
        radio = Radio.objects.get(fcc_id='2TEST-M1')
        self.assertEqual(radio.brand, 'RenamedBrand')
