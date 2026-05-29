from django.test import TestCase
from django.urls import reverse
from urllib.parse import quote_plus

from .models import Radio
from .models import Brand
from .models import RadioOETDocument
from .fcc_id_utils import split_fcc_id, normalize_fcc_id_for_lookup
from .fcc_utils import (
    _build_oet_document_filename,
    _extract_oet_documents_from_attachment_html,
    _extract_oet_documents_from_html,
    _is_fcc_authoritative_url,
    _extract_original_equipment_summary,
    _extract_secondary_metadata_from_generic_search_html,
    _sync_oet_documents_for_radio,
)


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


class FCCOETHtmlParsingTest(TestCase):
    def test_extracts_oet_documents_from_standard_href_rows(self):
        html = """
        <table>
            <tr>
                <th>View Attachment</th><th>Exhibit Type</th><th>Date Submitted to FCC</th><th>Display Type</th><th>Date Available</th>
            </tr>
            <tr>
                <td><a href="/oetcf/eas/reports/GenericExhibit.cfm?foo=1">Cover Letter</a></td>
                <td>Cover Letter</td>
                <td>12/01/2025</td>
                <td>pdf</td>
                <td>12/02/2025</td>
            </tr>
        </table>
        """

        docs = _extract_oet_documents_from_html(
            html,
            base_url='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm',
        )

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['view_attachment'], 'Cover Letter')
        self.assertIn('/oetcf/eas/reports/GenericExhibit.cfm', docs[0]['document_url'])

    def test_extracts_oet_documents_when_link_is_embedded_in_js(self):
        html = """
        <table>
            <tr>
                <td onclick="window.open('/oetcf/eas/reports/GenericExhibit.cfm?mode=doc&x=1')">External Photos</td>
                <td>External Photos</td>
                <td>01/03/2024</td>
                <td>pdf</td>
                <td>01/04/2024</td>
            </tr>
        </table>
        """

        docs = _extract_oet_documents_from_html(
            html,
            base_url='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm',
        )

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['view_attachment'], 'External Photos')
        self.assertIn('/oetcf/eas/reports/GenericExhibit.cfm', docs[0]['document_url'])

    def test_extracts_oet_documents_from_attachment_page_links(self):
        html = """
        <html>
            <body>
                <a href="GetAttachment.cfm?id_file_num=12345">Test Report</a>
                <a href="/oetcf/eas/reports/GenericExhibit.cfm?foo=bar">User Manual</a>
            </body>
        </html>
        """

        docs = _extract_oet_documents_from_attachment_html(
            html,
            base_url='https://apps.fcc.gov/oetcf/eas/reports/ViewAttachment.cfm?foo=1',
        )

        self.assertEqual(len(docs), 2)
        self.assertTrue(any('GetAttachment.cfm' in doc['document_url'] for doc in docs))
        self.assertTrue(any('GenericExhibit.cfm' in doc['document_url'] for doc in docs))

    def test_is_fcc_authoritative_url(self):
        self.assertTrue(_is_fcc_authoritative_url('https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm'))
        self.assertTrue(_is_fcc_authoritative_url('https://transition.fcc.gov/oet/reports/sample.pdf'))
        self.assertFalse(_is_fcc_authoritative_url('https://fcc.report/FCC-ID/2AJGM-UV82/5137067.pdf'))

    def test_build_oet_document_filename_prefers_safe_name(self):
        filename = _build_oet_document_filename(
            fcc_id='2AJGM-UV82',
            view_attachment='User Manual',
            document_url='https://apps.fcc.gov/oetcf/eas/reports/GenericExhibit.cfm?foo=1',
            display_type='pdf',
        )
        self.assertTrue(filename.startswith('2AJGM-UV82_'))
        self.assertTrue(filename.endswith('.pdf'))


class FCCOETSyncFallbackTest(TestCase):
    def test_sync_uses_existing_fcc_docs_when_secondary_metadata_empty(self):
        source_radio = Radio.objects.create(brand='BrandA', model='M1', fcc_id='2AJGM-UV82')
        target_radio = Radio.objects.create(brand='BrandB', model='M2', fcc_id='2AJGM-UV82')

        RadioOETDocument.objects.create(
            radio=source_radio,
            fcc_id='2AJGM-UV82',
            view_attachment='User Manual',
            exhibit_type='Users Manual',
            document_url='https://apps.fcc.gov/oetcf/eas/reports/GenericExhibit.cfm?doc=abc',
        )

        synced = _sync_oet_documents_for_radio(target_radio, '2AJGM-UV82', {'oet_documents': []})

        self.assertEqual(synced, 1)
        self.assertTrue(
            RadioOETDocument.objects.filter(
                radio=target_radio,
                fcc_id__iexact='2AJGM-UV82',
                view_attachment='User Manual',
            ).exists()
        )
