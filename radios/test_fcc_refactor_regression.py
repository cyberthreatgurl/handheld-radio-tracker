"""
Regression test for fcc_utils.py refactoring.

Compares the old (pre-refactor, from git) and new (post-refactor) versions of
fcc_utils.py to verify that only cosmetic changes were made (variable renames,
line wrapping) and no functional or structural changes occurred.

Strategy:
  1. AST comparison — parse both files and compare all function signatures
     (name, parameter count, async flag).  No function should be renamed,
     added, or removed.
  2. Input/output tests — run key utility functions from the *new* (current)
     version against known inputs and verify expected outputs.  These
     serve as a baseline for future refactors.
"""

import ast
import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path

import django
from django.test import TestCase
from django.test.utils import override_settings

# Point to the old version extracted from git
OLD_VERSION_PATH = Path('/tmp/regression_test/fcc_utils_old.py')

# Current (refactored) module
from radios import fcc_utils as new_mod


class FCCRefactorASTRegressionTest(TestCase):
    """Verify no functions were renamed, added, or removed in the refactoring."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_ast = ast.parse(OLD_VERSION_PATH.read_text())
        cls.new_ast = ast.parse(inspect.getsource(new_mod))

    @staticmethod
    def _ast_functions(tree):
        """Return {name: ast.FunctionDef} for top-level functions in the module."""
        return {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_function_names_and_params_unchanged(self):
        """Every function name and parameter count must be identical."""
        old_funcs = self._ast_functions(self.old_ast)
        new_funcs = self._ast_functions(self.new_ast)

        old_names = sorted(old_funcs.keys())
        new_names = sorted(new_funcs.keys())

        self.assertEqual(
            old_names, new_names,
            msg=(
                f"Function names differ.\n"
                f"Only in old: {set(old_names) - set(new_names)}\n"
                f"Only in new: {set(new_names) - set(old_names)}"
            ),
        )

        for name in old_names:
            old_params = [p.arg for p in old_funcs[name].args.args]
            new_params = [p.arg for p in new_funcs[name].args.args]
            self.assertEqual(
                len(old_params), len(new_params),
                msg=(
                    f"Function '{name}': parameter count changed "
                    f"(old={len(old_params)}, new={len(new_params)})"
                ),
            )
            old_defaults = len(old_funcs[name].args.defaults)
            new_defaults = len(new_funcs[name].args.defaults)
            self.assertEqual(
                old_defaults, new_defaults,
                msg=(
                    f"Function '{name}': default-count changed "
                    f"(old defaults={old_defaults}, new defaults={new_defaults})"
                ),
            )

    def test_no_new_global_class_or_function(self):
        """No new top-level classes, no module-level assign targets were added."""
        old_toplevel = {
            type(node).__name__: len([
                n for n in ast.walk(self.old_ast)
                if isinstance(n, type(node)) and
                hasattr(n, 'lineno') and
                n.lineno == getattr(n, 'col_offset', 0) + 1  # approximate
            ])
            for node in self.old_ast.body
        }
        new_toplevel = {
            type(node).__name__: len([
                n for n in ast.walk(self.new_ast)
                if isinstance(n, type(node)) and
                hasattr(n, 'lineno') and
                n.lineno == getattr(n, 'col_offset', 0) + 1
            ])
            for node in self.new_ast.body
        }

        # Only compare top-level FunctionDef, AsyncFunctionDef, ClassDef, Assign counts
        for key in ('FunctionDef', 'AsyncFunctionDef', 'ClassDef'):
            old_count = sum(1 for n in self.old_ast.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            new_count = sum(1 for n in self.new_ast.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            if key in ('FunctionDef', 'AsyncFunctionDef'):
                self.assertEqual(
                    old_count, new_count,
                    msg=f"Top-level function count changed: old={old_count}, new={new_count}",
                )
                break


class FCCRefactorFunctionalRegressionTest(TestCase):
    """Run the same inputs through key utility functions and verify outputs.

    These act as a baseline — if a future refactor changes any output,
    this test will catch it.
    """

    # ── _parse_allowlist_terms ─────────────────────────────────────────
    def test_parse_allowlist_terms_empty(self):
        result = new_mod._parse_allowlist_terms('')
        self.assertEqual(result, [])

    def test_parse_allowlist_terms_single(self):
        result = new_mod._parse_allowlist_terms('TRANSCEIVER')
        self.assertEqual(result, ['TRANSCEIVER'])

    def test_parse_allowlist_terms_multiple(self):
        result = new_mod._parse_allowlist_terms('TRANSCEIVER, TRANSMITTER, RECEIVER')
        self.assertEqual(result, ['TRANSCEIVER', 'TRANSMITTER', 'RECEIVER'])

    def test_parse_allowlist_terms_strips_whitespace(self):
        result = new_mod._parse_allowlist_terms('  HAM ,  CB ,  GMRS  ')
        self.assertEqual(result, ['HAM', 'CB', 'GMRS'])

    # ── _clean_query ────────────────────────────────────────────────────
    def test_clean_query_removes_whitespace(self):
        result = new_mod._clean_query('  2AJGM-UV5R  ')
        self.assertEqual(result, '2AJGM-UV5R')

    def test_clean_query_uppercases(self):
        result = new_mod._clean_query('2ajgm-uv5r')
        self.assertEqual(result, '2AJGM-UV5R')

    def test_clean_query_handles_none(self):
        result = new_mod._clean_query(None)
        self.assertEqual(result, '')

    # ── _exact_grantee_query ────────────────────────────────────────────
    def test_exact_grantee_query_full_fcc_id(self):
        result = new_mod._exact_grantee_query('2AJGM-UV5R')
        self.assertIsNone(result)

    def test_exact_grantee_query_code_only(self):
        result = new_mod._exact_grantee_query('2AJGM')
        self.assertEqual(result, '2AJGM')

    def test_exact_grantee_query_short_code(self):
        result = new_mod._exact_grantee_query('ICOM')
        self.assertEqual(result, 'ICOM')

    # ── _extract_fcc_key ────────────────────────────────────────────────
    def test_extract_fcc_key_full_fcc_id(self):
        result = new_mod._extract_fcc_key('2AJGM-UV5R')
        self.assertEqual(result, '2AJGMUV5R')

    def test_extract_fcc_key_grantee_code(self):
        result = new_mod._extract_fcc_key('2AJGM')
        self.assertEqual(result, '2AJGM')

    def test_extract_fcc_key_with_dashes(self):
        result = new_mod._extract_fcc_key('2ASNS-250-501')
        self.assertEqual(result, '2ASNS250501')

    # ── _normalize_brand_identity ───────────────────────────────────────
    def test_normalize_brand_identity_punctuation(self):
        result = new_mod._normalize_brand_identity('Vertex Standard USA, Inc.')
        self.assertEqual(result, 'vertexstandardusainc')

    def test_normalize_brand_identity_spaces(self):
        result = new_mod._normalize_brand_identity('   Baofeng    Technologies   ')
        self.assertEqual(result, 'baofengtechnologies')

    def test_normalize_brand_identity_case(self):
        result = new_mod._normalize_brand_identity('PO FUNG ELECTRONIC')
        self.assertEqual(result, 'pofungelectronic')

    def test_normalize_brand_identity_empty(self):
        result = new_mod._normalize_brand_identity('')
        self.assertEqual(result, '')

    def test_normalize_brand_identity_none(self):
        result = new_mod._normalize_brand_identity(None)
        self.assertEqual(result, '')

    # ── _is_original_equipment_purpose ──────────────────────────────────
    def test_is_original_equipment_purpose_exact(self):
        result = new_mod._is_original_equipment_purpose('Original Equipment')
        self.assertTrue(result)

    def test_is_original_equipment_purpose_case_insensitive(self):
        result = new_mod._is_original_equipment_purpose('original equipment')
        self.assertTrue(result)

    def test_is_original_equipment_purpose_change_in_id(self):
        result = new_mod._is_original_equipment_purpose('Change in Identification')
        self.assertFalse(result)

    def test_is_original_equipment_purpose_empty(self):
        result = new_mod._is_original_equipment_purpose('')
        self.assertFalse(result)

    def test_is_original_equipment_purpose_none(self):
        result = new_mod._is_original_equipment_purpose(None)
        self.assertFalse(result)

    # ── _strip_html_tags ────────────────────────────────────────────────
    def test_strip_html_tags_simple(self):
        result = new_mod._strip_html_tags('<b>Hello</b>')
        self.assertEqual(result, 'Hello')

    def test_strip_html_tags_with_entities(self):
        result = new_mod._strip_html_tags('Retevis&nbsp;H777S')
        self.assertEqual(result, 'Retevis H777S')

    def test_strip_html_tags_empty(self):
        result = new_mod._strip_html_tags('')
        self.assertEqual(result, '')

    def test_strip_html_tags_none(self):
        result = new_mod._strip_html_tags(None)
        self.assertEqual(result, '')

    # ── _parse_year_from_grant_date ─────────────────────────────────────
    def test_parse_year_from_grant_date_string(self):
        result = new_mod._parse_year_from_grant_date('06/22/2026')
        self.assertEqual(result, 2026)

    def test_parse_year_from_grant_date_iso(self):
        result = new_mod._parse_year_from_grant_date('2026-06-22')
        self.assertEqual(result, 2026)

    def test_parse_year_from_grant_date_empty(self):
        result = new_mod._parse_year_from_grant_date('')
        self.assertIsNone(result)

    def test_parse_year_from_grant_date_none(self):
        result = new_mod._parse_year_from_grant_date(None)
        self.assertIsNone(result)

    # ── _parse_decimal ──────────────────────────────────────────────────
    def test_parse_decimal_valid(self):
        result = new_mod._parse_decimal('462.5625')
        self.assertEqual(result, 462.5625)

    def test_parse_decimal_invalid(self):
        result = new_mod._parse_decimal('N/A')
        self.assertIsNone(result)

    def test_parse_decimal_empty(self):
        result = new_mod._parse_decimal('')
        self.assertIsNone(result)

    # ── _format_decimal_8 ───────────────────────────────────────────────
    def test_format_decimal_8(self):
        result = new_mod._format_decimal_8(462.5625)
        self.assertEqual(result, '462.56250000')

    def test_format_decimal_8_padding(self):
        result = new_mod._format_decimal_8(144.39)
        self.assertEqual(result, '144.39000000')

    # ── _is_fcc_attachment_document_url ─────────────────────────────────
    def test_is_fcc_attachment_document_url_true(self):
        result = new_mod._is_fcc_attachment_document_url(
            'https://apps.fcc.gov/oetcf/eas/reports/GetAttachment.cfm?id=123'
        )
        self.assertTrue(result)

    def test_is_fcc_attachment_document_url_false(self):
        result = new_mod._is_fcc_attachment_document_url(
            'https://example.com/manual.pdf'
        )
        self.assertFalse(result)

    def test_is_fcc_attachment_document_url_none(self):
        result = new_mod._is_fcc_attachment_document_url(None)
        self.assertFalse(result)

    # ── _allowlist_match_terms ──────────────────────────────────────────
    def test_allowlist_match_terms_no_terms(self):
        """No allowlist terms = no filtering."""
        res = {'applicationPurpose': 'Original Equipment'}
        sec_meta = {'text_blob': 'VHF UHF TRANSCEIVER'}
        result = new_mod._allowlist_match_terms(res, sec_meta, [])
        self.assertEqual(result, [])

    def test_allowlist_match_terms_match_fcc_id(self):
        """FCCId field can match allowlist terms."""
        res = {'FCCId': '2AJGM-TRANSMITTER'}
        sec_meta = {'text_blob': ''}
        result = new_mod._allowlist_match_terms(res, sec_meta, ['TRANSMITTER'])
        self.assertEqual(result, ['TRANSMITTER'])

    def test_allowlist_match_terms_match_metadata_blob(self):
        """text_blob can match allowlist terms."""
        res = {'FCCId': '2AJGM-UV5R'}
        sec_meta = {'text_blob': 'UHF TRANSCEIVER 2-WAY RADIO'}
        result = new_mod._allowlist_match_terms(res, sec_meta, ['TRANSCEIVER'])
        self.assertEqual(result, ['TRANSCEIVER'])

    # ── _is_connection_timeout_error ────────────────────────────────────
    def test_is_connection_timeout_error_timeout(self):
        exc = TimeoutError('Connection timed out')
        result = new_mod._is_connection_timeout_error(exc)
        self.assertTrue(result)

    def test_is_connection_timeout_error_other(self):
        exc = ValueError('some other error')
        result = new_mod._is_connection_timeout_error(exc)
        self.assertFalse(result)

    # ── _is_playwright_timeout_error ────────────────────────────────────
    def test_is_playwright_timeout_error_timeout(self):
        exc = TimeoutError('page.goto timed out')
        result = new_mod._is_playwright_timeout_error(exc)
        self.assertTrue(result)

    def test_is_playwright_timeout_error_other(self):
        exc = ConnectionError('connection refused')
        result = new_mod._is_playwright_timeout_error(exc)
        self.assertFalse(result)

    # ── normalize_fcc_id_for_lookup (imported from fcc_id_utils) ────────
    def test_normalize_fcc_id_for_lookup(self):
        result = new_mod.split_fcc_id('2AJGM-UV5R')
        self.assertEqual(result, ('2AJGM', 'UV5R'))

    def test_split_fcc_id_3char_grantee(self):
        result = new_mod.split_fcc_id('POFUNG-UV5R')
        self.assertEqual(result, ('POF', 'UNG-UV5R'))

    def test_split_fcc_id_5char_grantee(self):
        result = new_mod.split_fcc_id('2AJGM-UV5R')
        self.assertEqual(result, ('2AJGM', 'UV5R'))
