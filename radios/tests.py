from django.test import TestCase
from django.urls import reverse
from urllib.parse import quote_plus

from .models import Radio
from .models import Brand
from .fcc_id_utils import split_fcc_id, normalize_fcc_id_for_lookup
from .fcc_utils import _extract_original_equipment_summary, _extract_secondary_metadata_from_generic_search_html


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


class FCCOriginalEquipmentSummaryTest(TestCase):
    def test_summary_derives_intro_year_and_uses_narrowest_frequency_range(self):
        primary_record = {
            'applicationPurpose': 'Original Equipment',
            'grantDate': '12/06/2020',
        }
        secondary_metadata = {
            'original_equipment_rows': [
                {
                    'grant_date': '12/06/2020',
                    'application_purpose': 'Original Equipment',
                    'lower_freq_mhz': '150.00000000',
                    'upper_freq_mhz': '174.00000000',
                },
                {
                    'grant_date': '12/06/2020',
                    'application_purpose': 'Original Equipment',
                    'lower_freq_mhz': '400.00000000',
                    'upper_freq_mhz': '480.00000000',
                },
                {
                    'grant_date': '12/06/2020',
                    'application_purpose': 'Original Equipment',
                    'lower_freq_mhz': '136.00000000',
                    'upper_freq_mhz': '174.00000000',
                },
                {
                    'grant_date': '12/06/2020',
                    'application_purpose': 'Original Equipment',
                    'lower_freq_mhz': '400.00000000',
                    'upper_freq_mhz': '520.00000000',
                },
            ],
        }

        summary = _extract_original_equipment_summary(primary_record, secondary_metadata)

        self.assertEqual(summary['intro_year'], 2020)
        self.assertEqual(summary['freq_bands_tx'], '150.00000000-174.00000000 MHz')

    def test_summary_ignores_non_original_equipment_data(self):
        primary_record = {
            'applicationPurpose': 'Change in Identification',
            'grantDate': '12/06/2020',
        }
        secondary_metadata = {
            'original_equipment_rows': [],
        }

        summary = _extract_original_equipment_summary(primary_record, secondary_metadata)

        self.assertIsNone(summary['intro_year'])
        self.assertEqual(summary['freq_bands_tx'], '')


class FCCGenericSearchHtmlParsingTest(TestCase):
        def test_parses_rendered_generic_search_rows_for_target_fcc_id(self):
                html = """
                <table>
                    <tbody id=\"offTblBdy\">
                        <tr class=\"rowalternate\">
                            <td></td><td></td>
                            <td>
                                <a href=\"/oetcf/eas/reports/ViewExhibitReport.cfm?mode=Exhibits&application_id=ABC&fcc_id=2AJTBD500\">Detail</a>
                            </td>
                            <td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>
                            <td>2AJTBD500</td>
                            <td>Original Equipment</td>
                            <td>11/19/2025</td>
                            <td>136.0</td>
                            <td>174.0</td>
                        </tr>
                        <tr class=\"rowprimary\">
                            <td></td><td></td>
                            <td>
                                <a href=\"/oetcf/eas/reports/ViewExhibitReport.cfm?mode=Exhibits&application_id=XYZ&fcc_id=2AJTB-MINI9\">Detail</a>
                            </td>
                            <td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>
                            <td>2AJTB-MINI9</td>
                            <td>Change in Identification</td>
                            <td>07/03/2018</td>
                            <td>400.0</td>
                            <td>406.0</td>
                        </tr>
                    </tbody>
                </table>
                """

                parsed = _extract_secondary_metadata_from_generic_search_html(
                        html,
                        base_url='https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm',
                        fcc_id='2AJTBD500',
                )

                self.assertEqual(parsed['record_count'], 1)
                self.assertIn('2AJTBD500', parsed['text_blob'])
                self.assertIn('application_purpose', parsed['matched_keys'])
                self.assertEqual(len(parsed['original_equipment_rows']), 1)
                self.assertEqual(parsed['original_equipment_rows'][0]['grant_date'], '11/19/2025')
                self.assertEqual(parsed['original_equipment_rows'][0]['lower_freq_mhz'], '136.0')
                self.assertEqual(parsed['original_equipment_rows'][0]['upper_freq_mhz'], '174.0')
                self.assertTrue(
                        any('ViewExhibitReport.cfm' in url for url in parsed['candidate_exhibit_urls'])
                )
