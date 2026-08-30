import tempfile
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.test.client import RequestFactory

from ..admin import BrandAdmin
from ..models import Radio, Brand, IgnoredGrantee, Manufacturer
from ..models import RadioManual, RadioFCCTestReport, RadioOETDocument, RadioFirmware
from ..fcc_id_utils import split_fcc_id, normalize_fcc_id_for_lookup
from ..fcc_utils import (
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
    _ensure_grantee_brand_and_manufacturer,
    _resolve_authoritative_radio_brand_name,
    fetch_and_sync_fcc_id,
    reset_sync_metadata_cache,
    _sync_oet_documents_for_radio,
)
from ..manual_extraction import extract_specs_from_text, extract_text_from_pdf_with_metadata


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


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class BrandDeletionCleanupTest(TestCase):
    def _build_brand_fixture(self, brand_name='ScreenCo', manufacturer_name='ScreenCo OEM', extra_brand=None):
        brand = Brand.objects.create(name=brand_name, grantee_code='XH8')
        manufacturer = Manufacturer.objects.create(full_name=manufacturer_name)
        manufacturer.brands.add(brand)
        if extra_brand is not None:
            manufacturer.brands.add(extra_brand)

        radio = Radio.objects.create(
            brand=brand.name,
            model='Projector Screen 100',
            manufacturer=manufacturer,
            fcc_id='XH8-SCREEN100',
        )
        RadioManual.objects.create(
            radio=radio,
            manual_pdf=SimpleUploadedFile('manual.pdf', b'manual-bytes', content_type='application/pdf'),
        )
        RadioFCCTestReport.objects.create(
            radio=radio,
            fcc_id=radio.fcc_id,
            report_pdf=SimpleUploadedFile('report.pdf', b'report-bytes', content_type='application/pdf'),
        )
        RadioOETDocument.objects.create(
            radio=radio,
            fcc_id=radio.fcc_id,
            view_attachment='Attachment',
            document_url='https://example.com/oet.pdf',
            document_file=SimpleUploadedFile('oet.pdf', b'oet-bytes', content_type='application/pdf'),
        )
        RadioFirmware.objects.create(
            radio=radio,
            label='Main',
            version='1.0',
        )
        return brand, manufacturer, radio

    def test_brand_delete_removes_brand_radios_documents_and_single_brand_manufacturer(self):
        brand, manufacturer, radio = self._build_brand_fixture()

        brand.delete()

        self.assertFalse(Brand.objects.filter(pk=brand.pk).exists())
        self.assertFalse(Radio.objects.filter(pk=radio.pk).exists())
        self.assertFalse(RadioManual.objects.filter(radio=radio).exists())
        self.assertFalse(RadioFCCTestReport.objects.filter(radio=radio).exists())
        self.assertFalse(RadioOETDocument.objects.filter(radio=radio).exists())
        self.assertFalse(RadioFirmware.objects.filter(radio=radio).exists())
        self.assertFalse(Manufacturer.objects.filter(pk=manufacturer.pk).exists())

    def test_brand_delete_keeps_manufacturer_when_other_brand_links_remain(self):
        extra_brand = Brand.objects.create(name='Other Brand', grantee_code='OTHR1')
        brand, manufacturer, radio = self._build_brand_fixture(extra_brand=extra_brand)

        brand.delete()

        self.assertFalse(Brand.objects.filter(pk=brand.pk).exists())
        self.assertFalse(Radio.objects.filter(pk=radio.pk).exists())
        self.assertTrue(Manufacturer.objects.filter(pk=manufacturer.pk).exists())
        self.assertEqual(list(manufacturer.brands.values_list('name', flat=True)), ['Other Brand'])

    def test_admin_bulk_delete_uses_same_brand_cleanup(self):
        brand, manufacturer, radio = self._build_brand_fixture()
        admin_instance = BrandAdmin(Brand, AdminSite())
        request = RequestFactory().post('/admin/radios/brand/')

        admin_instance.delete_queryset(request, Brand.objects.filter(pk=brand.pk))

        self.assertFalse(Brand.objects.filter(pk=brand.pk).exists())
        self.assertFalse(Radio.objects.filter(pk=radio.pk).exists())
        self.assertFalse(Manufacturer.objects.filter(pk=manufacturer.pk).exists())


class RadioDetailFCCLinkTest(TestCase):
    def test_detail_renders_fcc_id_text_without_oet_url(self):
        radio = Radio.objects.create(
            brand='Xiamen Radtel Electronics Co., Ltd',
            model='RT-920',
            fcc_id='2AO8L-RT-920',
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        # No OET page URL stored → the FCC ID renders as plain text.
        self.assertContains(response, '2AO8L-RT-920')
        self.assertNotContains(response, 'ViewExhibitReport.cfm')

    def test_detail_links_fcc_id_to_stored_oet_url(self):
        radio = Radio.objects.create(
            brand='AnyBrand',
            model='AlphaModel',
            fcc_id='LKD1',
            oet_page_url=(
                'https://apps.fcc.gov/oetcf/eas/reports/'
                'ViewExhibitReport.cfm?application_id=abc123'
            ),
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        self.assertContains(response, 'ViewExhibitReport.cfm')
        self.assertContains(response, 'application_id=abc123')


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

    def test_detail_renders_compact_fcc_id_with_brand_grantee(self):
        Brand.objects.create(name='Kydera', grantee_code='VO6')
        radio = Radio.objects.create(
            brand='Kydera',
            model='200UV',
            fcc_id='VO6200UV',
        )

        response = self.client.get(reverse('radio_detail', kwargs={'pk': radio.pk}))

        self.assertContains(response, 'VO6200UV')

    def test_lookup_variants_include_normalized_form_for_hyphen_in_product_code(self):
        variants = _fcc_lookup_variants('2A4FBTDBL-1')
        self.assertEqual(variants[0], '2A4FBTDBL-1')
        self.assertIn('2A4FB-TDBL-1', variants)
        self.assertIn('2A4FBTDBL1', variants)

    def test_generic_search_payload_uses_live_form_field_names(self):
        payload = _build_generic_search_payload('2A4FBTDBL-1')
        self.assertEqual(payload['grantee_code'], '2A4FB')
        self.assertEqual(payload['product_code'], 'TDBL-1')
        self.assertEqual(payload['product_exact_match'], '')
        self.assertEqual(payload['application_status_description'], '')
        self.assertEqual(payload['eas_apps_only'], 'Y')
        self.assertNotIn('application_status', payload)
        self.assertNotIn('RequestTimeout', payload)


class IgnoredGranteeSyncTest(TestCase):
    def test_fetch_and_sync_skips_specific_fcc_id_for_ignored_grantee(self):
        IgnoredGrantee.objects.create(grantee_code='XH8', reason='Out of scope AV devices')

        with patch('radios.fcc_utils._fcc_request_with_retry') as mocked_request:
            added, updated, messages = fetch_and_sync_fcc_id('XH8-SCREEN100')

        self.assertEqual(added, 0)
        self.assertEqual(updated, 0)
        self.assertTrue(any('ignore list' in message.lower() for message in messages))
        mocked_request.assert_not_called()
        self.assertFalse(Radio.objects.filter(fcc_id='XH8-SCREEN100').exists())


class FCCGranteeBrandManufacturerSyncTest(TestCase):
    def test_ensure_grantee_brand_and_manufacturer_backfills_existing_brand(self):
        brand = Brand.objects.create(name='Quanzhou Buxun Electronic Technology Co., Ltd.')

        ensured_brand, ensured_manufacturer = _ensure_grantee_brand_and_manufacturer(
            '2AYFM',
            'Quanzhou Buxun Electronic Technology Co., Ltd.',
        )

        brand.refresh_from_db()
        self.assertEqual(ensured_brand.id, brand.id)
        self.assertEqual(brand.grantee_code, '2AYFM')
        self.assertEqual(brand.full_name, 'Quanzhou Buxun Electronic Technology Co., Ltd.')
        self.assertIsNotNone(ensured_manufacturer)
        self.assertEqual(ensured_manufacturer.full_name, 'Quanzhou Buxun Electronic Technology Co., Ltd.')
        self.assertTrue(ensured_manufacturer.brands.filter(pk=brand.pk).exists())

    def test_ensure_grantee_brand_and_manufacturer_backfills_normalized_blank_code_brand(self):
        brand = Brand.objects.create(name='Vertex Standard USA, Inc.')

        ensured_brand, ensured_manufacturer = _ensure_grantee_brand_and_manufacturer(
            'AXI',
            'Vertex Standard USA Inc',
        )

        brand.refresh_from_db()
        self.assertEqual(ensured_brand.id, brand.id)
        self.assertEqual(brand.grantee_code, 'AXI')
        self.assertEqual(brand.full_name, 'Vertex Standard USA Inc')
        self.assertIsNotNone(ensured_manufacturer)
        self.assertEqual(ensured_manufacturer.full_name, 'Vertex Standard USA Inc')
        self.assertTrue(ensured_manufacturer.brands.filter(pk=brand.pk).exists())

    def test_ensure_grantee_brand_and_manufacturer_absorbs_blank_code_variant_into_coded_brand(self):
        coded_brand = Brand.objects.create(
            name='Tidradio',
            grantee_code='2AWL3',
            full_name='Quanzhou longtuo electronic technology co. ,Lt',
        )
        blank_variant = Brand.objects.create(
            name='Quanzhou longtuo electronic technology co. ,Ltd',
        )

        ensured_brand, ensured_manufacturer = _ensure_grantee_brand_and_manufacturer(
            '2AWL3',
            'Quanzhou longtuo electronic technology co. ,Ltd',
        )

        coded_brand.refresh_from_db()
        blank_variant.refresh_from_db()
        self.assertEqual(ensured_brand.id, coded_brand.id)
        self.assertEqual(coded_brand.grantee_code, '2AWL3')
        self.assertEqual(coded_brand.full_name, 'Quanzhou longtuo electronic technology co. ,Ltd')
        self.assertIsNone(blank_variant.grantee_code)
        self.assertIsNotNone(ensured_manufacturer)
        self.assertEqual(ensured_manufacturer.full_name, 'Quanzhou longtuo electronic technology co. ,Ltd')
        self.assertTrue(ensured_manufacturer.brands.filter(pk=coded_brand.pk).exists())

    def test_resolve_authoritative_radio_brand_name_prefers_coded_brand_for_duplicate_variant(self):
        coded_brand = Brand.objects.create(
            name='Tidradio',
            grantee_code='2AWL3',
            full_name='Quanzhou longtuo electronic technology co. ,Lt',
        )
        Brand.objects.create(name='Quanzhou longtuo electronic technology co. ,Ltd')

        resolved = _resolve_authoritative_radio_brand_name(
            coded_brand,
            '2AWL3',
            'Quanzhou longtuo electronic technology co. ,Ltd',
        )

        self.assertEqual(resolved, 'Tidradio')

    def test_resolve_authoritative_radio_brand_name_leaves_reseller_case_alone_without_duplicate(self):
        coded_brand = Brand.objects.create(
            name='Some Canonical Brand',
            grantee_code='VO6',
            full_name='FUJIAN NEW CENTURY COMMUNICATIONS CO., LTD',
        )

        resolved = _resolve_authoritative_radio_brand_name(
            coded_brand,
            'VO6',
            'Kydera',
        )

        self.assertEqual(resolved, 'Kydera')


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
    def setUp(self):
        # Clear module-level OET sync caches so a prior test in the same run
        # (e.g. FCCOETDocumentLibrarySyncTest) cannot short-circuit this test
        # via the _synced_oet_fcc_ids de-duplication set.
        reset_sync_metadata_cache()

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


class ManualExtractionFallbackTest(TestCase):
    def test_pdf_crypto_dependency_error_falls_back_to_ocr(self):
        class FakeDependencyError(Exception):
            pass

        class FakePages:
            def __len__(self):
                raise FakeDependencyError('cryptography required')

            def __iter__(self):
                return iter(())

        class FakeReader:
            def __init__(self, _file_path):
                self.pages = FakePages()

        fake_errors_module = type('FakeErrorsModule', (), {'DependencyError': FakeDependencyError})

        with patch.dict('sys.modules', {'pypdf': type('FakePdfModule', (), {'PdfReader': FakeReader}), 'pypdf.errors': fake_errors_module}):
            with patch('radios.manual_extraction._extract_text_via_ocr', return_value='Recovered OCR text ' * 10):
                text, meta = extract_text_from_pdf_with_metadata('/tmp/encrypted.pdf')

        self.assertIn('Recovered OCR text', text)
        self.assertEqual(meta['method'], 'ocr')
        self.assertEqual(meta['direct_parse_reason'], 'pdf_crypto_dependency_missing')
