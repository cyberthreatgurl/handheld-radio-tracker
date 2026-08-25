"""Regression tests for bulk brand deletion."""

from django.test import TestCase
from django.urls import reverse

from ..models import Brand, IgnoredGrantee, Radio


class BrandBulkDeleteTest(TestCase):
    """Deleting multiple brands removes them, their radios, and ignores grantees."""

    def setUp(self):
        self.brand_a = Brand.objects.create(name='BrandA', grantee_code='2AAA')
        self.brand_b = Brand.objects.create(name='BrandB', grantee_code='2BBB')
        Radio.objects.create(brand='BrandA', model='A1', fcc_id='2AAA-A1')
        Radio.objects.create(brand='BrandB', model='B1', fcc_id='2BBB-B1')

    def test_bulk_delete_removes_brands_radios_and_ignores_grantees(self):
        """Selected brands, their radios, and grantee codes are handled."""
        response = self.client.post(
            reverse('brand_bulk_delete'),
            {
                'brand_ids': [self.brand_a.pk, self.brand_b.pk],
                'confirm_delete': 'yes',
            },
        )
        self.assertRedirects(response, reverse('brand_list'))
        self.assertFalse(
            Brand.objects.filter(
                pk__in=[self.brand_a.pk, self.brand_b.pk],
            ).exists(),
        )
        self.assertFalse(
            Radio.objects.filter(brand__in=['BrandA', 'BrandB']).exists(),
        )
        self.assertTrue(IgnoredGrantee.is_ignored('2AAA'))
        self.assertTrue(IgnoredGrantee.is_ignored('2BBB'))

    def test_bulk_delete_requires_confirmation(self):
        """Missing confirmation leaves brands untouched."""
        response = self.client.post(
            reverse('brand_bulk_delete'),
            {'brand_ids': [self.brand_a.pk]},
        )
        self.assertRedirects(response, reverse('brand_list'))
        self.assertTrue(Brand.objects.filter(pk=self.brand_a.pk).exists())

    def test_bulk_delete_empty_selection(self):
        """An empty selection leaves brands untouched."""
        response = self.client.post(
            reverse('brand_bulk_delete'),
            {'brand_ids': [], 'confirm_delete': 'yes'},
        )
        self.assertRedirects(response, reverse('brand_list'))
        self.assertTrue(Brand.objects.filter(pk=self.brand_a.pk).exists())

    def test_brand_list_renders_bulk_delete_controls(self):
        """The brand list page exposes the selection and delete controls."""
        response = self.client.get(reverse('brand_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Delete All Selected')
        self.assertContains(response, 'name="brand_ids"')
        self.assertContains(response, 'select-all-brands')
