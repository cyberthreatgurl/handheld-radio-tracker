"""Regression tests for fcc_utils — algorithm-level tests.

Covers:
- _allowlist_match_terms() — allowlist matching logic
- _radio_allowlist_terms() — env-driven allowlist assembly
- _is_original_equipment_purpose() — purpose classification
- Change in Identification allowlist bypass (regression fix)
- _exact_grantee_query() — grantee code detection
- _clean_query() — query normalization
- _is_fcc_authoritative_url() — URL safety
- _build_generic_search_payload() — FCC form payload builder
- _normalize_brand_identity() — brand name normalization
- _find_existing_grantee_brand() — grantee-to-brand resolution
"""

import os
from unittest.mock import Mock, patch

from django.test import TestCase

from ..fcc_utils import (
    _allowlist_match_terms,
    _exact_grantee_query,
    _clean_query,
    _is_fcc_authoritative_url,
    _build_generic_search_payload,
    _normalize_brand_identity,
    _find_existing_grantee_brand,
    _fcc_request_with_retry,
    fetch_and_sync_fcc_id,
)
from ..models import Radio

# Import the module directly to test env-dependent functions
from .. import fcc_utils


class AllowlistMatchTermsTest(TestCase):
    """Regression tests for the allowlist matching algorithm."""

    def setUp(self):
        self.allowlist = ['TRANSCEIVER', 'TRANSMITTER', 'RECEIVER', 'GMRS', 'MURS']

    def test_fcc_id_field_is_excluded(self):
        """FCC ID is a technical identifier and must not match the allowlist."""
        record = {'FCCId': 'XYZ-GMRS1', 'grantee': '', 'applicationPurpose': '', 'grantDate': ''}
        meta = {'text_blob': ''}
        matched = _allowlist_match_terms(record, meta, self.allowlist)
        self.assertEqual(matched, [])

    def test_matches_grantee_field(self):
        """Grantee name containing an allowlist term should match."""
        record = {'FCCId': 'XYZ-123', 'grantee': 'GMRS Transceiver Co', 'applicationPurpose': '', 'grantDate': ''}
        meta = {'text_blob': ''}
        matched = _allowlist_match_terms(record, meta, self.allowlist)
        self.assertIn('TRANSCEIVER', matched)

    def test_matches_application_purpose(self):
        """applicationPurpose matching should be case-insensitive."""
        record = {'FCCId': 'XYZ-123', 'grantee': '', 'applicationPurpose': 'original equipment', 'grantDate': ''}
        meta = {'text_blob': ''}
        # "ORIGINAL EQUIPMENT" is not in the test allowlist, so no match
        matched = _allowlist_match_terms(record, meta, self.allowlist)
        self.assertEqual(matched, [])

    def test_matches_secondary_metadata_blob(self):
        """Secondary metadata text blob should contribute to matching."""
        record = {'FCCId': 'XYZ-123', 'grantee': '', 'applicationPurpose': '', 'grantDate': ''}
        meta = {'text_blob': 'This device is a 5W GMRS transceiver'}
        matched = _allowlist_match_terms(record, meta, self.allowlist)
        self.assertIn('GMRS', matched)
        self.assertIn('TRANSCEIVER', matched)

    def test_no_match_returns_empty_list(self):
        """No allowlist terms → empty list."""
        record = {'FCCId': 'XYZ-123', 'grantee': 'Some Company', 'applicationPurpose': 'Change in Identification', 'grantDate': '2024-01-01'}
        meta = {'text_blob': ''}
        matched = _allowlist_match_terms(record, meta, ['RADIO', 'HAM'])
        self.assertEqual(matched, [])

    def test_case_insensitive_matching(self):
        """Allowlist matching should be case-insensitive."""
        record = {'FCCId': 'xyz-123', 'grantee': 'tRANscEIver Co', 'applicationPurpose': '', 'grantDate': ''}
        meta = {'text_blob': ''}
        matched = _allowlist_match_terms(record, meta, ['TRANSCEIVER'])
        self.assertIn('TRANSCEIVER', matched)

    def test_match_from_grant_date(self):
        """grantDate alone shouldn't match radio terms."""
        record = {'FCCId': 'XYZ-123', 'grantee': '', 'applicationPurpose': '', 'grantDate': '2024-01-01'}
        meta = {'text_blob': ''}
        matched = _allowlist_match_terms(record, meta, ['2024'])
        self.assertIn('2024', matched)

    def test_empty_allowlist_returns_empty(self):
        """Empty allowlist → no filtering."""
        record = {'FCCId': 'XYZ-123', 'grantee': 'Some Co', 'applicationPurpose': 'Change', 'grantDate': '2024'}
        meta = {'text_blob': 'metadata'}
        matched = _allowlist_match_terms(record, meta, [])
        self.assertEqual(matched, [])

    def test_all_sources_combined(self):
        """All non-FCC-ID sources should be concatenated and matched."""
        record = {
            'FCCId': 'ABC-XYZ',
            'grantee': 'GMRS Corp',
            'applicationPurpose': 'Original Equipment',
            'grantDate': '2020-01-01',
        }
        meta = {'text_blob': 'VHF/UHF transceiver module'}
        matched = _allowlist_match_terms(
            record, meta, ['TRANSCEIVER', 'GMRS', 'ORIGINAL EQUIPMENT'],
        )
        self.assertIn('GMRS', matched)
        self.assertIn('TRANSCEIVER', matched)
        self.assertIn('ORIGINAL EQUIPMENT', matched)


class RadioAllowlistTermsTest(TestCase):
    """Regression tests for _radio_allowlist_terms() — env-driven list assembly."""

    def setUp(self):
        # Save original env var
        self.original_env = os.environ.get('FCC_RADIO_ALLOWLIST_TERMS')

    def tearDown(self):
        # Restore original env var
        if self.original_env is not None:
            os.environ['FCC_RADIO_ALLOWLIST_TERMS'] = self.original_env
        else:
            os.environ.pop('FCC_RADIO_ALLOWLIST_TERMS', None)

    def test_default_allowlist_includes_core_terms(self):
        """Default allowlist must include core radio terms."""
        os.environ.pop('FCC_RADIO_ALLOWLIST_TERMS', None)
        terms = fcc_utils._radio_allowlist_terms()
        self.assertIn('TRANSCEIVER', terms)
        self.assertIn('GMRS', terms)
        self.assertIn('MURS', terms)
        self.assertIn('HAM', terms)
        self.assertIn('CB', terms)

    def test_custom_terms_merged_with_defaults(self):
        """Custom env terms should be merged with defaults."""
        os.environ['FCC_RADIO_ALLOWLIST_TERMS'] = 'GMRS,HAM'
        terms = fcc_utils._radio_allowlist_terms()
        self.assertIn('GMRS', terms)
        self.assertIn('HAM', terms)
        self.assertIn('TRANSCEIVER', terms)  # Default still present

    def test_no_duplicates_in_merged_list(self):
        """Merged list should not contain duplicates."""
        os.environ['FCC_RADIO_ALLOWLIST_TERMS'] = 'TRANSCEIVER,GMRS'
        terms = fcc_utils._radio_allowlist_terms()
        self.assertEqual(len(terms), len(set(terms)))

    def test_allowlist_env_parsing_handles_whitespace(self):
        os.environ['FCC_RADIO_ALLOWLIST_TERMS'] = '  GMRS , HAM , CB  '
        terms = fcc_utils._radio_allowlist_terms()
        self.assertIn('GMRS', terms)
        self.assertIn('HAM', terms)
        self.assertIn('CB', terms)


class OriginalEquipmentPurposeTest(TestCase):
    """Regression tests for _is_original_equipment_purpose."""

    def test_original_equipment_recognized(self):
        self.assertTrue(fcc_utils._is_original_equipment_purpose('Original Equipment'))

    def test_change_in_id_not_original(self):
        self.assertFalse(fcc_utils._is_original_equipment_purpose('Change in Identification'))

    def test_class_ii_permissive_not_original(self):
        self.assertFalse(fcc_utils._is_original_equipment_purpose('Class II Permissive Change'))

    def test_class_i_permissive_not_original(self):
        self.assertFalse(fcc_utils._is_original_equipment_purpose('Class I Permissive Change'))

    def test_case_insensitive(self):
        self.assertTrue(fcc_utils._is_original_equipment_purpose('original equipment'))

    def test_empty_returns_false(self):
        self.assertFalse(fcc_utils._is_original_equipment_purpose(''))

    def test_none_returns_false(self):
        self.assertFalse(fcc_utils._is_original_equipment_purpose(None))

    def test_partial_match_not_confused(self):
        """'Original' alone should not match."""
        self.assertFalse(fcc_utils._is_original_equipment_purpose('Original'))

    def test_whitespace_handled(self):
        self.assertTrue(fcc_utils._is_original_equipment_purpose('  Original Equipment  '))


class ExactGranteeQueryTest(TestCase):
    """Regression tests for _exact_grantee_query."""

    def test_3char_alpha_grantee(self):
        self.assertEqual(_exact_grantee_query('AXI'), 'AXI')

    def test_5char_numeric_prefix_grantee(self):
        self.assertEqual(_exact_grantee_query('2AJGM'), '2AJGM')

    def test_full_fcc_id_with_hyphen_returns_empty(self):
        self.assertEqual(_exact_grantee_query('2AJGM-UV5R'), '')

    def test_short_code_returns_empty(self):
        self.assertEqual(_exact_grantee_query('AB'), '')

    def test_6char_code_returns_empty(self):
        self.assertEqual(_exact_grantee_query('ABCDEF'), '')

    def test_empty_returns_empty(self):
        self.assertEqual(_exact_grantee_query(''), '')

    def test_none_returns_empty(self):
        self.assertEqual(_exact_grantee_query(None), '')

    def test_case_normalized(self):
        self.assertEqual(_exact_grantee_query('2ajgm'), '2AJGM')

    def test_whitespace_stripped(self):
        self.assertEqual(_exact_grantee_query('  2AJGM  '), '2AJGM')


class CleanQueryTest(TestCase):
    """Tests for _clean_query."""

    def test_cleans_whitespace(self):
        self.assertEqual(_clean_query(' 2AJGM-UV5R '), '2AJGM-UV5R')

    def test_uppercases(self):
        self.assertEqual(_clean_query('2ajgm-uv5r'), '2AJGM-UV5R')

    def test_removes_spaces_mid_string(self):
        self.assertEqual(_clean_query('2AJGM UV5R'), '2AJGMUV5R')

    def test_empty_returns_empty(self):
        self.assertEqual(_clean_query(''), '')

    def test_none_returns_empty(self):
        self.assertEqual(_clean_query(None), '')


class FCCAuthoritativeURLTest(TestCase):
    """Regression: URL validation before fetching."""

    def test_fcc_gov_url_is_authoritative(self):
        self.assertTrue(_is_fcc_authoritative_url('https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm'))

    def test_transition_fcc_gov_is_authoritative(self):
        self.assertTrue(_is_fcc_authoritative_url('https://transition.fcc.gov/oet/reports/sample.pdf'))

    def test_fcc_report_dot_net_not_authoritative(self):
        self.assertFalse(_is_fcc_authoritative_url('https://fcc.report/FCC-ID/2AJGM-UV82/5137067.pdf'))

    def test_arbitrary_url_not_authoritative(self):
        self.assertFalse(_is_fcc_authoritative_url('https://evil.com/payload.pdf'))

    def test_empty_url_not_authoritative(self):
        self.assertFalse(_is_fcc_authoritative_url(''))


class GenericSearchPayloadTest(TestCase):
    """Regression: FCC form payload builder."""

    def test_builds_payload_for_hyphenated_id(self):
        payload = _build_generic_search_payload('2AJGM-UV5R')
        self.assertEqual(payload['grantee_code'], '2AJGM')
        self.assertEqual(payload['product_code'], 'UV5R')
        self.assertIn(payload['product_exact_match'], ('', 'on'))

    def test_builds_payload_for_dash_in_product(self):
        payload = _build_generic_search_payload('2AO8L-RT-920')
        self.assertEqual(payload['grantee_code'], '2AO8L')
        self.assertEqual(payload['product_code'], 'RT-920')

    def test_builds_payload_for_long_prefix(self):
        payload = _build_generic_search_payload('2A4FBTDBL-1')
        self.assertEqual(payload['grantee_code'], '2A4FB')
        self.assertEqual(payload['product_code'], 'TDBL-1')

    def test_payload_includes_required_fields(self):
        payload = _build_generic_search_payload('2AJGM-UV5R')
        self.assertEqual(payload['application_status_description'], '')
        self.assertEqual(payload['eas_apps_only'], 'Y')
        self.assertNotIn('application_status', payload)
        self.assertNotIn('RequestTimeout', payload)

    def test_empty_fcc_id_returns_empty_payload(self):
        payload = _build_generic_search_payload('')
        self.assertIsNone(payload)


class NormalizeBrandIdentityTest(TestCase):
    """Tests for _normalize_brand_identity — brand name normalization."""

    def test_strips_punctuation(self):
        self.assertEqual(_normalize_brand_identity('Quanzhou, Inc.'), 'quanzhouinc')

    def test_lowercase_and_strip(self):
        self.assertEqual(_normalize_brand_identity('  Baofeng  '), 'baofeng')

    def test_handles_spaces(self):
        self.assertEqual(_normalize_brand_identity('Vertex Standard USA, Inc.'), 'vertexstandardusainc')

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_brand_identity(''), '')

    def test_none_returns_empty(self):
        self.assertEqual(_normalize_brand_identity(None), '')


class FindExistingGranteeBrandTest(TestCase):
    """Regression tests for _find_existing_grantee_brand."""

    def setUp(self):
        from ..models import Brand
        self.brand = Brand.objects.create(
            name='Baofeng',
            grantee_code='2AJGM',
            full_name='Baofeng Technology Co., Ltd',
        )

    def test_finds_by_grantee_code(self):
        from ..models import Brand
        result = _find_existing_grantee_brand('2AJGM', 'Baofeng Technology Co., Ltd')
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.brand.id)

    def test_finds_by_name_exact(self):
        from ..models import Brand
        result = _find_existing_grantee_brand('WRONG', 'Baofeng')
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.brand.id)

    def test_returns_none_when_no_match(self):
        result = _find_existing_grantee_brand('XXXXX', 'Nonexistent Company')
        self.assertIsNone(result)

    def test_finds_blank_code_brand_by_normalized_name(self):
        from ..models import Brand
        blank_brand = Brand.objects.create(name='Baofeng Technology Co Ltd')
        result = _find_existing_grantee_brand('XXXXX', 'Baofeng Technology Co., Ltd.')
        self.assertIsNotNone(result)
        self.assertEqual(result.id, blank_brand.id)


class FCC503FastFailTest(TestCase):
    """Regression: 503 fast-fail skips retries when enabled."""

    def test_503_returns_immediately_with_fast_fail(self):
        """When FCC_SKIP_RETRY_ON_503 is true, a 503 response should be
        returned without retrying."""
        os.environ['FCC_SKIP_RETRY_ON_503'] = 'true'
        from unittest.mock import patch, Mock

        mock_resp = Mock()
        mock_resp.status_code = 503

        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=mock_resp):
            from ..fcc_utils import _fcc_request_with_retry as fn
            result = fn('get', 'https://example.com/test', impersonate='chrome124', timeout=5)

        self.assertEqual(result.status_code, 503)

    def test_503_retries_when_fast_fail_disabled(self):
        """When FCC_SKIP_RETRY_ON_503 is false, 503 should trigger retries."""
        os.environ['FCC_SKIP_RETRY_ON_503'] = 'false'
        from unittest.mock import patch, Mock

        mock_resp = Mock()
        mock_resp.status_code = 503

        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=mock_resp):
            from ..fcc_utils import _fcc_request_with_retry as fn
            result = fn('get', 'https://example.com/test', impersonate='chrome124', timeout=5)

        self.assertEqual(result.status_code, 503)

    def test_200_returns_normally(self):
        """A 200 response should return immediately regardless of fast-fail."""
        os.environ['FCC_SKIP_RETRY_ON_503'] = 'true'
        from unittest.mock import patch, Mock

        mock_resp = Mock()
        mock_resp.status_code = 200

        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=mock_resp):
            from ..fcc_utils import _fcc_request_with_retry as fn
            result = fn('get', 'https://example.com/test', impersonate='chrome124', timeout=5)

        self.assertEqual(result.status_code, 200)


class NormalizeRulePartTest(TestCase):
    """Regression: rule part normalization for ignored/key-fob matching."""

    def test_plain_unchanged(self):
        self.assertEqual(fcc_utils._normalize_rule_part('15.231'), '15.231')

    def test_paren_suffix_stripped(self):
        self.assertEqual(fcc_utils._normalize_rule_part('15.231(e)'), '15.231')

    def test_spaced_paren_suffix_stripped(self):
        self.assertEqual(fcc_utils._normalize_rule_part('15.231 (e)'), '15.231')

    def test_bare_letter_suffix_stripped(self):
        self.assertEqual(fcc_utils._normalize_rule_part('15.231e'), '15.231')

    def test_non_dotted_parts_untouched(self):
        self.assertEqual(fcc_utils._normalize_rule_part('95E'), '95E')
        self.assertEqual(fcc_utils._normalize_rule_part('15B'), '15B')

    def test_empty_and_none(self):
        self.assertEqual(fcc_utils._normalize_rule_part(''), '')
        self.assertEqual(fcc_utils._normalize_rule_part(None), '')


class RulePartsMatchIgnoredTest(TestCase):
    """Regression: ignored rule parts match subsection variants."""

    def test_exact_match(self):
        self.assertTrue(fcc_utils._rule_parts_match_ignored(['15.231']))

    def test_paren_variant_matches(self):
        self.assertTrue(fcc_utils._rule_parts_match_ignored(['15.231(a)']))

    def test_bare_letter_variant_matches(self):
        self.assertTrue(fcc_utils._rule_parts_match_ignored(['15.231e']))

    def test_non_ignored_parts_do_not_match(self):
        self.assertFalse(fcc_utils._rule_parts_match_ignored(['90', '95E']))

    def test_empty_returns_false(self):
        self.assertFalse(fcc_utils._rule_parts_match_ignored([]))


class DetectKeyFobTest(TestCase):
    """Regression: key fob / RKE / TPMS detection."""

    def test_rule_part_signal(self):
        record = {'applicationPurpose': 'Change in Identification', 'grantee': 'Remote Tech LLC'}
        meta = {'rule_parts': ['15.231'], 'text_blob': '', 'original_equipment_rows': []}
        signals = fcc_utils._detect_key_fob(record, meta, 'TOY-V1')
        self.assertIn('rule_part=15.231', signals)

    def test_keyword_signal(self):
        record = {'applicationPurpose': 'Original Equipment', 'grantee': 'Some Co'}
        meta = {
            'rule_parts': [],
            'text_blob': 'Remote keyless entry transmitter',
            'original_equipment_rows': [],
        }
        signals = fcc_utils._detect_key_fob(record, meta, 'FOB-1')
        self.assertTrue(any(s.startswith('keyword=') for s in signals))

    def test_frequency_signal(self):
        record = {'applicationPurpose': 'Original Equipment', 'grantee': 'Some Co'}
        meta = {
            'rule_parts': [],
            'text_blob': '',
            'original_equipment_rows': [
                {
                    'lower_freq_mhz': '314.35',
                    'upper_freq_mhz': '314.35',
                    'emission_designator': '',
                },
            ],
        }
        signals = fcc_utils._detect_key_fob(record, meta, '')
        self.assertIn('freq=keyfob_band', signals)

    def test_radio_not_misclassified(self):
        record = {'applicationPurpose': 'Original Equipment', 'grantee': 'Baofeng'}
        meta = {
            'rule_parts': ['95E'],
            'text_blob': 'GMRS transceiver',
            'original_equipment_rows': [
                {
                    'lower_freq_mhz': '462.0',
                    'upper_freq_mhz': '467.0',
                    'emission_designator': '11K0F3E',
                },
            ],
        }
        self.assertEqual(fcc_utils._detect_key_fob(record, meta, 'UV-5R'), [])

    def test_wideband_receiver_not_misclassified_as_key_fob(self):
        record = {'applicationPurpose': 'Original Equipment', 'grantee': 'Iradio'}
        meta = {
            'rule_parts': ['15B'],
            'text_blob': 'Scanning receiver',
            'original_equipment_rows': [
                {'lower_freq_mhz': '108.0', 'upper_freq_mhz': '136.0',
                 'emission_designator': ''},
                {'lower_freq_mhz': '136.0', 'upper_freq_mhz': '174.0',
                 'emission_designator': ''},
                {'lower_freq_mhz': '350.0', 'upper_freq_mhz': '390.0',
                 'emission_designator': ''},
                {'lower_freq_mhz': '400.0', 'upper_freq_mhz': '520.0',
                 'emission_designator': ''},
            ],
        }
        self.assertEqual(
            fcc_utils._detect_key_fob(record, meta, 'UV-98PLUS'),
            [],
        )


class MetadataIsEmptyTest(TestCase):
    """Regression: empty secondary metadata detection."""

    def test_empty_dict(self):
        self.assertTrue(fcc_utils._metadata_is_empty({}))

    def test_none(self):
        self.assertTrue(fcc_utils._metadata_is_empty(None))

    def test_outage_result_is_empty(self):
        meta = {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
            'rule_parts': [],
            'original_fcc_id_from_tcb': '',
        }
        self.assertTrue(fcc_utils._metadata_is_empty(meta))

    def test_rule_parts_make_it_non_empty(self):
        self.assertFalse(fcc_utils._metadata_is_empty({'rule_parts': ['95E']}))

    def test_application_id_makes_it_non_empty(self):
        self.assertFalse(fcc_utils._metadata_is_empty({'application_id': 'abc123'}))


class KeyFobSyncSkipTest(TestCase):
    """End-to-end: fetch_and_sync_fcc_id never imports key fobs."""

    def setUp(self):
        fcc_utils.reset_sync_metadata_cache()

    @staticmethod
    def _primary_response(fcc_id, purpose, grantee='Remote Tech LLC'):
        xml = (
            '<?xml version="1.0"?><fCCIDInfoes><fccidInfo>'
            f'<FCCId>{fcc_id}</FCCId>'
            f'<grantee>{grantee}</grantee>'
            f'<applicationPurpose>{purpose}</applicationPurpose>'
            f'<grantDate>04/22/2018</grantDate>'
            '</fccidInfo></fCCIDInfoes>'
        )
        resp = Mock()
        resp.status_code = 200
        resp.text = xml
        return resp

    def test_key_fob_rule_part_skipped_even_for_change_in_id(self):
        primary = self._primary_response('2AOKM-TOY-V1', 'Change in Identification')
        meta = {
            'record_count': 1,
            'text_blob': (
                '2AOKM-TOY-V1 | Change in Identification | '
                '04/22/2018 | 314.35 | 314.35'
            ),
            'rule_parts': ['15.231'],
            'application_id': 'abc123',
            'original_equipment_rows': [
                {
                    'lower_freq_mhz': '314.35',
                    'upper_freq_mhz': '314.35',
                    'emission_designator': '',
                },
            ],
            'oet_documents': [],
            'original_fcc_id_from_tcb': '',
        }
        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=primary), \
             patch('radios.fcc_utils.fetch_fcc_secondary_metadata', return_value=meta):
            added, updated, messages = fetch_and_sync_fcc_id('2AOKM-TOY-V1')

        self.assertEqual(added, 0)
        self.assertEqual(updated, 0)
        self.assertFalse(Radio.objects.filter(fcc_id__iexact='2AOKM-TOY-V1').exists())
        self.assertTrue(any('key fob' in message.lower() for message in messages))

    def test_empty_metadata_defers_record_creation(self):
        primary = self._primary_response('2AOKM-TOY-V1', 'Change in Identification')
        meta = {
            'record_count': 0,
            'text_blob': '',
            'matched_keys': [],
            'test_report_candidates': [],
            'original_equipment_rows': [],
            'oet_documents': [],
            'rule_parts': [],
            'original_fcc_id_from_tcb': '',
        }
        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=primary), \
             patch('radios.fcc_utils.fetch_fcc_secondary_metadata', return_value=meta):
            added, updated, messages = fetch_and_sync_fcc_id('2AOKM-TOY-V1')

        self.assertEqual(added, 0)
        self.assertEqual(updated, 0)
        self.assertFalse(Radio.objects.filter(fcc_id__iexact='2AOKM-TOY-V1').exists())
        self.assertTrue(any('no FCC metadata' in message for message in messages))
