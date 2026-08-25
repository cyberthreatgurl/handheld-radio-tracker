"""Regression tests for brand deletion and FCC ignore-list behavior.

When a brand with an FCC grantee code is deleted, the grantee code should be
added to ``IgnoredGrantee`` so future sync/import workflows skip it.
"""

from django.test import TestCase

from ..models import Brand, IgnoredGrantee


class BrandDeletionIgnoreGranteeTest(TestCase):
    """Deleting a brand with a grantee code populates the ignore list."""

    def test_delete_brand_with_grantee_adds_to_ignored(self):
        """Grantee code is added to IgnoredGrantee with a reason."""
        brand = Brand.objects.create(name='TestBrand', grantee_code='2TEST')
        brand.delete()

        self.assertFalse(Brand.objects.filter(pk=brand.pk).exists())
        self.assertTrue(IgnoredGrantee.is_ignored('2TEST'))

        ignored = IgnoredGrantee.objects.get(grantee_code='2TEST')
        self.assertEqual(ignored.reason, 'Brand deleted from database')

    def test_delete_brand_normalizes_grantee_code(self):
        """Grantee code is normalized (trimmed, uppercased) before saving."""
        brand = Brand.objects.create(name='TestBrandTwo', grantee_code='  2test ')
        brand.delete()

        self.assertTrue(IgnoredGrantee.is_ignored('2TEST'))
        self.assertFalse(IgnoredGrantee.objects.filter(grantee_code='2test').exists())

    def test_delete_brand_without_grantee_adds_nothing(self):
        """A brand without a grantee code leaves the ignore list untouched."""
        brand = Brand.objects.create(name='TestBrandThree', grantee_code='')
        brand.delete()

        self.assertEqual(IgnoredGrantee.objects.count(), 0)
