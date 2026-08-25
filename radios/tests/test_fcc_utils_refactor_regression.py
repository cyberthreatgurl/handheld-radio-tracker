"""Regression test for fcc_utils.py refactoring.

Verifies that cosmetic changes (variable renames, line wrapping) did not
alter function signatures or behavior for key pure functions.
"""

import ast
import os
import subprocess
from unittest import TestCase

from django.test import tag


@tag('refactor')
class FCCUtilsRefactorRegressionTest(TestCase):
    """Compares old vs new function signatures via AST."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..')
        )
        result = subprocess.run(
            ['git', 'show', 'HEAD:radios/fcc_utils.py'],
            capture_output=True, text=True, cwd=repo_root,
        )
        cls.old_source = result.stdout

        new_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', 'fcc_utils.py')
        )
        with open(new_path) as f:
            cls.new_source = f.read()

    @staticmethod
    def _get_func_info(src):
        """Return {name: [arg_names]} for all top-level functions."""
        tree = ast.parse(src)
        funcs = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                funcs[node.name] = [a.arg for a in node.args.args]
        return funcs

    def test_no_functions_removed(self):
        """Verify no function was deleted during refactoring."""
        old_f = self._get_func_info(self.old_source)
        new_f = self._get_func_info(self.new_source)
        removed = set(old_f) - set(new_f)
        self.assertEqual(removed, set(), f'Functions removed: {sorted(removed)}')

    def test_no_function_count_decrease(self):
        old_f = self._get_func_info(self.old_source)
        new_f = self._get_func_info(self.new_source)
        self.assertGreaterEqual(
            len(new_f), len(old_f),
            f'Function count decreased: {len(old_f)} → {len(new_f)}',
        )

    def test_no_arg_count_changes(self):
        """Arg count should never change (only renames) — report exceptions."""
        old_f = self._get_func_info(self.old_source)
        new_f = self._get_func_info(self.new_source)
        changed = []
        for name in sorted(set(old_f) & set(new_f)):
            if len(old_f[name]) != len(new_f[name]):
                changed.append(f'{name}: old={old_f[name]} new={new_f[name]}')
        self.assertEqual(changed, [], 'Arg count changes:\n' + '\n'.join(changed))

    def test_current_file_has_no_syntax_errors(self):
        try:
            compile(self.new_source, 'radios/fcc_utils.py', 'exec')
        except SyntaxError as e:
            self.fail(f'Syntax error in current fcc_utils.py: {e}')


@tag('refactor', 'behavior')
class FCCUtilsBehaviorRegressionTest(TestCase):
    """Verifies key pure functions still produce correct output."""

    _parse_allowlist_cases = [
        ('', []),
        ('GMRS, HAM', ['GMRS', 'HAM']),
        ('  GMRS , HAM , CB  ', ['GMRS', 'HAM', 'CB']),
        ('transceiver,Transmitter,', ['TRANSCEIVER', 'TRANSMITTER']),
    ]

    _clean_query_cases = [
        (' 2AJGM-UV5R ', '2AJGM-UV5R'),
        ('2ajgm-uv5r', '2AJGM-UV5R'),
        ('', ''),
        (None, ''),
    ]

    _exact_grantee_cases = [
        ('AXI', 'AXI'),
        ('2AJGM', '2AJGM'),
        ('2AJGM-UV5R', ''),
        ('AB', ''),
        ('', ''),
        (None, ''),
    ]

    _normalize_brand_cases = [
        ('Quanzhou, Inc.', 'quanzhouinc'),
        ('  Baofeng  ', 'baofeng'),
        ('', ''),
        (None, ''),
    ]

    _is_oe_purpose_cases = [
        ('Original Equipment', True),
        ('Change in Identification', False),
        ('', False),
        (None, False),
        ('  Original Equipment  ', True),
    ]

    _allowlist_match_cases = [
        ({'FCCId': 'XYZ-GMRS1', 'grantee': '', 'applicationPurpose': '', 'grantDate': ''},
         {'text_blob': ''}, ['GMRS'], ['GMRS']),
        ({'FCCId': 'XYZ-123', 'grantee': 'GMRS Transceiver Co', 'applicationPurpose': '', 'grantDate': ''},
         {'text_blob': ''}, ['TRANSCEIVER'], ['TRANSCEIVER']),
        ({'FCCId': 'XYZ-123', 'grantee': '', 'applicationPurpose': 'original equipment', 'grantDate': ''},
         {'text_blob': ''}, ['ORIGINAL EQUIPMENT'], ['ORIGINAL EQUIPMENT']),
        ({'FCCId': 'XYZ-123', 'grantee': '', 'applicationPurpose': '', 'grantDate': ''},
         {'text_blob': 'This is a 5W GMRS transceiver'},
         ['GMRS', 'TRANSCEIVER'], ['GMRS', 'TRANSCEIVER']),
    ]

    def test_parse_allowlist_terms(self):
        from ..fcc_utils import _parse_allowlist_terms as fn
        for inp, expected in self._parse_allowlist_cases:
            with self.subTest(inp=inp):
                self.assertEqual(fn(inp), expected)

    def test_clean_query(self):
        from ..fcc_utils import _clean_query as fn
        for inp, expected in self._clean_query_cases:
            with self.subTest(inp=inp):
                self.assertEqual(fn(inp), expected)

    def test_exact_grantee_query(self):
        from ..fcc_utils import _exact_grantee_query as fn
        for inp, expected in self._exact_grantee_cases:
            with self.subTest(inp=inp):
                self.assertEqual(fn(inp), expected)

    def test_normalize_brand_identity(self):
        from ..fcc_utils import _normalize_brand_identity as fn
        for inp, expected in self._normalize_brand_cases:
            with self.subTest(inp=inp):
                self.assertEqual(fn(inp), expected)

    def test_is_original_equipment_purpose(self):
        from ..fcc_utils import _is_original_equipment_purpose as fn
        for inp, expected in self._is_oe_purpose_cases:
            with self.subTest(inp=inp):
                self.assertEqual(fn(inp), expected)

    def test_allowlist_match_terms(self):
        from ..fcc_utils import _allowlist_match_terms as fn
        for record, meta, allowlist, expected in self._allowlist_match_cases:
            with self.subTest(record=record.get('FCCId')):
                self.assertEqual(
                    sorted(fn(record, meta, allowlist)),
                    sorted(expected),
                )

    def test_fcc_key_extraction(self):
        from ..fcc_utils import _extract_fcc_key as fn
        self.assertEqual(fn('2AJGM-UV5R'), '2AJGMUV5R')
        self.assertEqual(fn('2AO8L-RT-920'), '2AO8LRT920')
        self.assertEqual(fn(''), '')

    def test_is_fcc_authoritative_url(self):
        from ..fcc_utils import _is_fcc_authoritative_url as fn
        self.assertTrue(fn('https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm'))
        self.assertTrue(fn('https://transition.fcc.gov/oet/reports/sample.pdf'))
        self.assertFalse(fn('https://fcc.report/FCC-ID/2AJGM-UV82/5137067.pdf'))
        self.assertFalse(fn('https://evil.com/payload.pdf'))

    def test_strip_html_tags(self):
        from ..fcc_utils import _strip_html_tags as fn
        self.assertEqual(fn('<b>Hello</b>'), 'Hello')
        self.assertEqual(fn(''), '')

    def test_parse_year_from_grant_date(self):
        from ..fcc_utils import _parse_year_from_grant_date as fn
        self.assertEqual(fn('12/06/2020'), 2020)
        self.assertEqual(fn(''), None)
