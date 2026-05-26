from django.test import TestCase
from django.urls import reverse
from urllib.parse import quote_plus

from .models import Radio
from .models import Brand
from .fcc_id_utils import split_fcc_id, normalize_fcc_id_for_lookup


class RadioModelTest(TestCase):
    def setUp(self):
        Radio.objects.create(
            brand='Baofeng',
            model='UV-5R',
            fcc_id='2AJGM-UV5R',
        )
    
    def test_radio_string_representation(self):
        radio = Radio.objects.get(model='UV-5R')
        self.assertEqual(str(radio), 'Baofeng UV-5R')
    
    def test_radio_creation(self):
        radio = Radio.objects.get(model='UV-5R')
        self.assertEqual(radio.brand, 'Baofeng')
        self.assertEqual(radio.fcc_id, '2AJGM-UV5R')


class RadioDetailFCCLinkTest(TestCase):
    def test_detail_fcc_link_keeps_existing_hyphenated_id(self):
        radio = Radio.objects.create(
            brand='Xiamen Radtel Electronics Co., Ltd',
            model='RT-920',
            fcc_id='2AO8L-RT-920',
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        expected_lookup = quote_plus('2AO8L-RT-920')
        self.assertContains(
            response,
            f'https://www.fcc.gov/oet/ea/fccid?id={expected_lookup}',
        )

    def test_detail_fcc_link_normalizes_alpha_prefix_compact_id_to_3_char_grantee(self):
        radio = Radio.objects.create(
            brand='AnyBrand',
            model='AlphaModel',
            fcc_id='LKD1',
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        expected_lookup = quote_plus('LKD-1')
        self.assertContains(
            response,
            f'https://www.fcc.gov/oet/ea/fccid?id={expected_lookup}',
        )


class FCCIDUtilsTest(TestCase):
    def test_split_compact_numeric_prefix_uses_five_char_grantee(self):
        grantee, product = split_fcc_id('2AJGMBF1904')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, 'BF1904')

    def test_split_compact_alpha_prefix_uses_three_char_grantee(self):
        grantee, product = split_fcc_id('LKD1')
        self.assertEqual(grantee, 'LKD')
        self.assertEqual(product, '1')

    def test_split_prefers_known_grantee_code_when_supplied(self):
        grantee, product = split_fcc_id('VO6200UV', preferred_grantee_code='VO6')
        self.assertEqual(grantee, 'VO6')
        self.assertEqual(product, '200UV')

    def test_normalize_preserves_hyphenated_product_with_dashes(self):
        lookup = normalize_fcc_id_for_lookup('2AJGM-BF-1904')
        self.assertEqual(lookup, '2AJGM-BF-1904')

    def test_detail_fcc_link_normalizes_compact_id_using_brand_grantee(self):
        Brand.objects.create(name='Kydera', grantee_code='VO6')
        radio = Radio.objects.create(
            brand='Kydera',
            model='200UV',
            fcc_id='VO6200UV',
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        expected_lookup = quote_plus('VO6-200UV')
        self.assertContains(
            response,
            f'https://www.fcc.gov/oet/ea/fccid?id={expected_lookup}',
        )
