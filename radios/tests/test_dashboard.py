"""Regression tests for dashboard brand/model validity checks."""

from django.test import TestCase
from django.urls import reverse

from ..models import Brand, Radio


class DashboardDeletedBrandTest(TestCase):
    """The dashboard excludes brands/models whose Brand record was deleted."""

    def test_excludes_orphaned_radios_of_deleted_brand(self):
        """A radio whose Brand row no longer exists is hidden from the dashboard."""
        brand = Brand.objects.create(name='GhostBrand', grantee_code='2GHO')
        Radio.objects.create(brand='GhostBrand', model='M1', fcc_id='2GHO-M1')

        # Delete only the Brand row (bulk delete bypasses the cascade override),
        # leaving the radio behind with a now-invalid brand name.
        Brand.objects.filter(pk=brand.pk).delete()

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'GhostBrand')

    def test_full_deletion_no_longer_stale_after_refresh(self):
        """After deleting a brand and its radios, a fresh GET hides them."""
        brand = Brand.objects.create(name='FullDelete', grantee_code='2FUL')
        Radio.objects.create(brand='FullDelete', model='M1', fcc_id='2FUL-M1')

        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'FullDelete')

        brand.delete()  # cascade override: deletes brand + radios

        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'FullDelete')
