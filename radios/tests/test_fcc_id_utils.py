"""Regression tests for fcc_id_utils — FCC ID parsing.

Covers:
- The dash hypothesis: every valid FCC ID contains a hyphen
- split_fcc_id() with hyphenated and compact IDs
- normalize_fcc_id_for_lookup()
- _extract_fcc_key() edge cases
- _fcc_lookup_variants()
"""

from django.test import TestCase
from ..fcc_id_utils import (
    canonical_fcc_id,
    split_fcc_id,
    normalize_fcc_id_for_lookup,
)
from ..fcc_utils import _extract_fcc_key, _fcc_lookup_variants


class SplitFCCIDHyphenTest(TestCase):
    """Regression: FCC product codes always contain a hyphen."""

    def test_split_hyphenated_numeric_prefix_5char_grantee(self):
        """2AJGM-UV5R → grantee=2AJGM, product=UV5R"""
        grantee, product = split_fcc_id('2AJGM-UV5R')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, 'UV5R')

    def test_split_hyphenated_numeric_prefix_5char_grantee_compact(self):
        """2AJGMUV5R → grantee=2AJGM, product=UV5R (no hyphen inferred)"""
        grantee, product = split_fcc_id('2AJGMUV5R')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, 'UV5R')

    def test_split_hyphenated_alpha_prefix_3char_grantee(self):
        """LKD-1 → grantee=LKD, product=1"""
        grantee, product = split_fcc_id('LKD-1')
        self.assertEqual(grantee, 'LKD')
        self.assertEqual(product, '1')

    def test_split_hyphenated_alpha_prefix_3char_grantee_compact(self):
        """LKD1 → grantee=LKD, product=1 (no hyphen inferred)"""
        grantee, product = split_fcc_id('LKD1')
        self.assertEqual(grantee, 'LKD')
        self.assertEqual(product, '1')

    def test_split_dash_in_product_code_preserved(self):
        """2AO8L-RT-920 → grantee=2AO8L, product=RT-920 (dash in product preserved)"""
        grantee, product = split_fcc_id('2AO8L-RT-920')
        self.assertEqual(grantee, '2AO8L')
        self.assertEqual(product, 'RT-920')

    def test_split_dash_in_product_code_with_compact_prefix(self):
        """2A4FBTDBL-1 → grantee=2A4FB, product=TDBL-1 (long prefix before dash)"""
        grantee, product = split_fcc_id('2A4FBTDBL-1')
        self.assertEqual(grantee, '2A4FB')
        self.assertEqual(product, 'TDBL-1')

    def test_split_preferred_grantee_overrides_inference(self):
        """VO6200UV with preferred=VO6 → grantee=VO6, product=200UV"""
        grantee, product = split_fcc_id('VO6200UV', preferred_grantee_code='VO6')
        self.assertEqual(grantee, 'VO6')
        self.assertEqual(product, '200UV')

    def test_split_empty_id_returns_empty(self):
        grantee, product = split_fcc_id('')
        self.assertEqual(grantee, '')
        self.assertEqual(product, '')

    def test_split_none_id_returns_empty(self):
        grantee, product = split_fcc_id(None)
        self.assertEqual(grantee, '')
        self.assertEqual(product, '')

    def test_split_whitespace_id_returns_empty(self):
        grantee, product = split_fcc_id('   ')
        self.assertEqual(grantee, '')
        self.assertEqual(product, '')

    def test_split_lowercase_input_normalized(self):
        """Input is case-normalized to uppercase."""
        grantee, product = split_fcc_id('2ajgm-uv5r')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, 'UV5R')

    def test_split_with_spaces_normalized(self):
        grantee, product = split_fcc_id(' 2AJGM-UV5R ')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, 'UV5R')

    def test_split_grantee_only_no_product_returns_grantee(self):
        grantee, product = split_fcc_id('2AJGM')
        self.assertEqual(grantee, '2AJGM')
        self.assertEqual(product, '')

    def test_split_invalid_short_id_returns_raw(self):
        """A 2-char ID with no valid grantee length → returns empty product."""
        grantee, product = split_fcc_id('AB')
        self.assertEqual(grantee, 'AB')
        self.assertEqual(product, '')


class NormalizeFCCIDTest(TestCase):
    """Tests for normalize_fcc_id_for_lookup."""

    def test_normalize_hyphenated_id(self):
        result = normalize_fcc_id_for_lookup('2AJGM-UV5R')
        self.assertEqual(result, '2AJGM-UV5R')

    def test_normalize_compact_id_preserves_dash(self):
        result = normalize_fcc_id_for_lookup('LKD1')
        self.assertEqual(result, 'LKD-1')

    def test_normalize_compact_numeric_prefix(self):
        result = normalize_fcc_id_for_lookup('2AJGMUV5R')
        self.assertEqual(result, '2AJGM-UV5R')

    def test_normalize_empty_returns_empty(self):
        result = normalize_fcc_id_for_lookup('')
        self.assertEqual(result, '')

    def test_normalize_with_preferred_grantee(self):
        result = normalize_fcc_id_for_lookup('VO6200UV', preferred_grantee_code='VO6')
        self.assertEqual(result, 'VO6-200UV')

    def test_normalize_already_normalized_form(self):
        result = normalize_fcc_id_for_lookup('2AJGM-UV5R')
        self.assertEqual(result, '2AJGM-UV5R')

    def test_normalize_preserves_long_prefix_hyphen(self):
        """YC2VEV-V8 is the FCC's own spelling (grantee YC2 + product VEV-V8)."""
        self.assertEqual(normalize_fcc_id_for_lookup('YC2VEV-V8'), 'YC2VEV-V8')

    def test_canonical_preserves_long_prefix_hyphen(self):
        """canonical_fcc_id keeps the FCC's own spelling when a hyphen exists."""
        self.assertEqual(canonical_fcc_id('YC2VEV-V8'), 'YC2VEV-V8')

    def test_split_still_derives_grantee_for_long_prefix(self):
        """split_fcc_id still derives grantee=YC2 from YC2VEV-V8."""
        grantee, product = split_fcc_id('YC2VEV-V8')
        self.assertEqual(grantee, 'YC2')
        self.assertEqual(product, 'VEV-V8')


class ExtractFCCKeyTest(TestCase):
    """Tests for _extract_fcc_key — strips dashes for comparison."""

    def test_extract_key_removes_dash(self):
        self.assertEqual(_extract_fcc_key('2AJGM-UV5R'), '2AJGMUV5R')

    def test_extract_key_already_clean(self):
        self.assertEqual(_extract_fcc_key('2AJGMUV5R'), '2AJGMUV5R')

    def test_extract_key_empty(self):
        self.assertEqual(_extract_fcc_key(''), '')

    def test_extract_key_none(self):
        self.assertEqual(_extract_fcc_key(None), '')

    def test_extract_key_lowercase(self):
        self.assertEqual(_extract_fcc_key('2ajgm-uv5r'), '2AJGMUV5R')

    def test_extract_key_multiple_dashes(self):
        self.assertEqual(_extract_fcc_key('2AO8L-RT-920'), '2AO8LRT920')

    def test_extract_key_whitespace(self):
        self.assertEqual(_extract_fcc_key(' 2AJGM-UV5R '), '2AJGMUV5R')


class FCCLookupVariantsTest(TestCase):
    """Tests for _fcc_lookup_variants — generates all lookup forms."""

    def test_variants_for_hyphenated_id(self):
        variants = _fcc_lookup_variants('2AJGM-UV5R')
        self.assertIn('2AJGM-UV5R', variants)
        self.assertIn('2AJGMUV5R', variants)

    def test_variants_for_dash_in_product(self):
        variants = _fcc_lookup_variants('2A4FBTDBL-1')
        self.assertIn('2A4FBTDBL-1', variants)
        self.assertNotIn('2A4FB-TDBL-1', variants)
        self.assertIn('2A4FBTDBL1', variants)

    def test_variants_deduplicated(self):
        variants = _fcc_lookup_variants('2AJGMUV5R')
        # Should not contain duplicates
        self.assertEqual(len(variants), len(set(variants)))

    def test_variants_empty(self):
        variants = _fcc_lookup_variants('')
        self.assertEqual(variants, [])

    def test_variants_none(self):
        variants = _fcc_lookup_variants(None)
        self.assertEqual(variants, [])
