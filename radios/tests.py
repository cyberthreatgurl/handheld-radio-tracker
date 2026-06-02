import tempfile
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from urllib.parse import quote_plus

from .models import Radio
from .models import Brand
from .models import RadioManual
from .models import RadioOETDocument
from .fcc_id_utils import split_fcc_id, normalize_fcc_id_for_lookup
from .fcc_utils import (
    _fcc_lookup_variants,
    _build_generic_search_payload,
    _apply_extracted_specs_to_radio,
    _build_oet_document_filename,
    _download_oet_document_bytes,
    _extract_oet_documents_from_attachment_html,
    _extract_oet_documents_from_html,
    _is_fcc_authoritative_url,
    _extract_original_equipment_summary,
    _extract_secondary_metadata_from_generic_search_html,
    _sync_oet_documents_for_radio,
)
from .manual_extraction import extract_specs_from_text


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

    def test_lookup_variants_include_normalized_form_for_hyphen_in_product_code(self):
        variants = _fcc_lookup_variants('2A4FBTDBL-1')
        self.assertEqual(variants[0], '2A4FBTDBL-1')
        self.assertIn('2A4FB-TDBL-1', variants)
        self.assertIn('2A4FBTDBL1', variants)

    def test_generic_search_payload_uses_live_form_field_names(self):
        payload = _build_generic_search_payload('2A4FBTDBL-1')
        self.assertEqual(payload['grantee_code'], '2A4FB')
        self.assertEqual(payload['product_code'], 'TDBL-1')
        self.assertEqual(payload['product_exact_match'], 'on')
        self.assertEqual(payload['application_status_description'], '')
        self.assertEqual(payload['eas_apps_only'], 'Y')
        self.assertNotIn('application_status', payload)
        self.assertNotIn('RequestTimeout', payload)


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
        self.assertTrue(
            any('application_id=ABC' in url for url in parsed['candidate_exhibit_urls'])
        )


class FCCOETHtmlParsingTest(TestCase):
    def test_extracts_only_real_attachment_rows_from_live_style_table(self):
        html = """
        <table>
            <tr><td><a href="http://www.fcc.gov/oet">OET Home Page</a></td></tr>
            <tr>
                <th>View Attachment</th><th>Exhibit Type</th><th>Date Submitted to FCC</th><th>Display Type</th><th>Date Available</th>
            </tr>
            <tr>
                <td><a target="_new" href="/eas/GetApplicationAttachment.html?id=5776823">User manual</a></td>
                <td>Users Manual</td>
                <td>03/24/2022</td>
                <td>pdf</td>
                <td>03/24/2022</td>
            </tr>
        </table>
        """

        docs = _extract_oet_documents_from_html(
            html,
            base_url='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm?application_id=abc',
        )

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['view_attachment'], 'User manual')
        self.assertEqual(docs[0]['exhibit_type'], 'Users Manual')
        self.assertIn('GetApplicationAttachment.html?id=5776823', docs[0]['document_url'])
        self.assertIn('ViewExhibitReport.cfm?application_id=abc', docs[0]['referer_url'])

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
                <a href="https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=5776823">FCC User Manual</a>
            </body>
        </html>
        """

        docs = _extract_oet_documents_from_attachment_html(
            html,
            base_url='https://apps.fcc.gov/oetcf/eas/reports/ViewAttachment.cfm?foo=1',
        )

        self.assertEqual(len(docs), 3)
        self.assertTrue(any('GetAttachment.cfm' in doc['document_url'] for doc in docs))
        self.assertTrue(any('GenericExhibit.cfm' in doc['document_url'] for doc in docs))
        self.assertTrue(any('GetApplicationAttachment.html' in doc['document_url'] for doc in docs))

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

    def test_download_oet_document_bytes_uses_referer_header(self):
        response = type('Response', (), {'status_code': 200, 'content': b'%PDF-1.4 test'})()

        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=response) as mocked_request:
            content = _download_oet_document_bytes(
                'https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=5776823',
                referer_url='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm?application_id=abc',
            )

        self.assertEqual(content, b'%PDF-1.4 test')
        self.assertEqual(
            mocked_request.call_args.kwargs['headers']['Referer'],
            'https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm?application_id=abc',
        )


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

        with tempfile.TemporaryDirectory(prefix='radio-tracker-test-media-') as tempdir:
            with self.settings(MEDIA_ROOT=tempdir):
                with patch('radios.fcc_utils._download_oet_document_bytes', return_value=b'%PDF-1.4 fallback'):
                    synced = _sync_oet_documents_for_radio(target_radio, '2AJGM-UV82', {'oet_documents': []})

        self.assertEqual(synced, 1)
        self.assertTrue(
            RadioOETDocument.objects.filter(
                radio=target_radio,
                fcc_id__iexact='2AJGM-UV82',
                view_attachment='User Manual',
            ).exists()
        )
        self.assertTrue(
            RadioManual.objects.filter(
                radio=target_radio,
                doc_type=RadioManual.DocType.MANUAL,
                source_url__contains='doc=abc',
            ).exists()
        )


class FCCOETDocumentLibrarySyncTest(TestCase):
    def test_sync_promotes_classified_oet_documents_into_manual_library(self):
        radio = Radio.objects.create(brand='BrandA', model='M1', fcc_id='2AJGM-UV82')
        secondary_metadata = {
            'oet_documents': [
                {
                    'view_attachment': 'User Manual',
                    'exhibit_type': 'Users Manual',
                    'date_submitted_to_fcc': '12/01/2025',
                    'display_type': 'pdf',
                    'date_available': '12/02/2025',
                    'document_url': 'https://apps.fcc.gov/oetcf/eas/reports/GenericExhibit.cfm?doc=manual',
                },
                {
                    'view_attachment': 'Change in Identification Letter',
                    'exhibit_type': 'Change in Identification',
                    'date_submitted_to_fcc': '12/01/2025',
                    'display_type': 'pdf',
                    'date_available': '12/02/2025',
                    'document_url': 'https://apps.fcc.gov/oetcf/eas/reports/GenericExhibit.cfm?doc=change',
                },
                {
                    'view_attachment': 'RF Exposure Test Report',
                    'exhibit_type': 'Test Report',
                    'date_submitted_to_fcc': '12/01/2025',
                    'display_type': 'pdf',
                    'date_available': '12/02/2025',
                    'document_url': 'https://apps.fcc.gov/oetcf/eas/reports/GenericExhibit.cfm?doc=test',
                },
            ],
        }

        with tempfile.TemporaryDirectory(prefix='radio-tracker-test-media-') as tempdir:
            with self.settings(MEDIA_ROOT=tempdir):
                with patch('radios.fcc_utils._download_oet_document_bytes', return_value=b'%PDF-1.4 test'):
                    synced = _sync_oet_documents_for_radio(radio, '2AJGM-UV82', secondary_metadata)

        self.assertEqual(synced, 3)
        self.assertEqual(RadioOETDocument.objects.filter(radio=radio).count(), 3)
        self.assertEqual(RadioManual.objects.filter(radio=radio).count(), 3)
        self.assertTrue(
            RadioManual.objects.filter(
                radio=radio,
                doc_type=RadioManual.DocType.MANUAL,
                source_url__contains='doc=manual',
            ).exists()
        )
        self.assertTrue(
            RadioManual.objects.filter(
                radio=radio,
                doc_type=RadioManual.DocType.CHANGE_IN_ID,
                source_url__contains='doc=change',
            ).exists()
        )
        self.assertTrue(
            RadioManual.objects.filter(
                radio=radio,
                doc_type=RadioManual.DocType.TEST_REPORT,
                source_url__contains='doc=test',
            ).exists()
        )


class FCCPdfSpecExtractionTest(TestCase):
    def test_extract_specs_from_text_detects_fcc_features(self):
        sample_text = (
            'Model: UV-Example\n'
            'FCC ID: 2AJGM-UVEX\n'
            '5W output power\n'
            'Built-in GPS and APRS support\n'
            'DMR digital radio\n'
            'Air Band receive\n'
            'Battery: 2500 mAh\n'
            '136-174 MHz and 400-480 MHz\n'
        )

        extracted = extract_specs_from_text(sample_text, source_name='FCC Test Report')

        self.assertEqual(extracted['power_watts'], '5W')
        self.assertEqual(extracted['gps'], 'Yes')
        self.assertEqual(extracted['aprs'], 'Yes')
        self.assertEqual(extracted['dmr'], 'Yes')
        self.assertEqual(extracted['air_band'], 'Yes')
        self.assertEqual(extracted['battery_mah'], 2500)
        self.assertIn('VHF', extracted['freq_bands_tx'])
        self.assertIn('UHF', extracted['freq_bands_tx'])

    def test_apply_extracted_specs_only_backfills_blank_fields(self):
        radio = Radio.objects.create(
            brand='BrandA',
            model='M1',
            fcc_id='2AJGM-UV82',
            power_watts='10W',
            gps='',
            aprs='',
        )

        changes = _apply_extracted_specs_to_radio(
            radio,
            {
                'power_watts': '5W',
                'gps': 'Yes',
                'aprs': 'Yes',
                'dmr': 'Yes',
                'battery_mah': 2500,
            },
            'unit-test',
        )

        radio.refresh_from_db()
        self.assertEqual(radio.power_watts, '10W')
        self.assertEqual(radio.gps, 'Yes')
        self.assertEqual(radio.aprs, 'Yes')
        self.assertEqual(radio.dmr, 'Yes')
        self.assertEqual(radio.battery_mah, 2500)
        self.assertNotIn('power_watts', changes)
        self.assertIn('gps', changes)
