"""
Regression test for fcc_utils.py refactoring.

Compares the refactored (current working tree) version against the
last-committed version to verify functional equivalence.
Uses AST comparison and runtime output comparison on pure functions.
"""
import ast as ast_mod
import importlib
import importlib.util
import inspect
import os
import re
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

# Set up Django before importing anything from radios
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'radio_database.settings')
import django
django.setup()


# ── Helpers ──────────────────────────────────────────────────────────────

def _extract_old_version():
    """Return the committed version of fcc_utils.py as a string."""
    return subprocess.check_output(
        ['git', 'show', 'HEAD:radios/fcc_utils.py'],
        text=True,
    )


def _load_module_from_source(name, source_text):
    """Load a Python module from source text in a clean namespace."""
    import types
    mod = types.ModuleType(name)
    mod.__dict__.setdefault('__builtins__', __builtins__)
    code = compile(source_text, f'<{name}>', 'exec')
    exec(code, mod.__dict__)
    return mod


# ── AST Structure Comparison ─────────────────────────────────────────────

def _compare_ast_structure(old_source, new_source):
    """Compare ASTs to ensure same function/class structure."""
    import ast as ast_mod
    old_tree = ast_mod.parse(old_source)
    new_tree = ast_mod.parse(new_source)

    def _collect_defs(tree):
        funcs = set()
        classes = set()
        for node in ast_mod.walk(tree):
            if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef)):
                funcs.add(node.name)
            elif isinstance(node, ast_mod.ClassDef):
                classes.add(node.name)
        return funcs, classes

    old_funcs, old_classes = _collect_defs(old_tree)
    new_funcs, new_classes = _collect_defs(new_tree)

    issues = []
    if removed := old_funcs - new_funcs:
        issues.append(f'Functions removed: {removed}')
    if added := new_funcs - old_funcs:
        issues.append(f'Functions added: {added}')
    if removed_c := old_classes - new_classes:
        issues.append(f'Classes removed: {removed_c}')
    if added_c := new_classes - old_classes:
        issues.append(f'Classes added: {added_c}')
    return issues


# ── Test Suite ────────────────────────────────────────────────────────────

class TestFccUtilsRegression(unittest.TestCase):
    """Regression tests for fcc_utils.py refactoring."""

    @classmethod
    def setUpClass(cls):
        # Load old (committed) version
        old_source = _extract_old_version()
        cls.old_mod = _load_module_from_source('fcc_utils_old', old_source)

        # Load new (current working tree) version
        with open('radios/fcc_utils.py') as f:
            new_source = f.read()
        cls.new_mod = _load_module_from_source('fcc_utils_new', new_source)

        # Store sources for AST comparison
        cls.old_source = old_source
        cls.new_source = new_source

    def test_ast_structure(self):
        """No functions or classes were added or removed."""
        issues = _compare_ast_structure(self.old_source, self.new_source)
        self.assertFalse(
            issues,
            f'AST structure changed:\n' + '\n'.join(issues),
        )

    def test_all_functions_present(self):
        """Every function in old version exists in new version."""
        old_funcs = {
            node.name
            for node in ast_mod.walk(ast_mod.parse(self.old_source))
            if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef))
        }
        new_funcs = {
            node.name
            for node in ast_mod.walk(ast_mod.parse(self.new_source))
            if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef))
        }
        missing = old_funcs - new_funcs
        self.assertFalse(missing, f'Functions missing in new version: {missing}')

    def _compare_pure_function(self, func_name, test_cases):
        """Compare a pure function's output between old and new versions."""
        old_func = getattr(self.old_mod, func_name, None)
        new_func = getattr(self.new_mod, func_name, None)
        self.assertIsNotNone(old_func, f'{func_name} missing in old version')
        self.assertIsNotNone(new_func, f'{func_name} missing in new version')

        for args in test_cases:
            try:
                old_result = old_func(*args)
            except Exception as e:
                old_result = f'EXCEPTION: {e}'
            try:
                new_result = new_func(*args)
            except Exception as e:
                new_result = f'EXCEPTION: {e}'
            label = f'{func_name}({", ".join(repr(a) for a in args)})'
            self.assertEqual(
                old_result, new_result,
                f'{label} mismatch\n  old: {old_result!r}\n  new: {new_result!r}',
            )

    def test_normalize_brand_identity(self):
        self._compare_pure_function('_normalize_brand_identity', [
            ('Radtel',), ('Xiamen Radtel Electronics Co., Ltd',),
            ('  Baofeng  ',), ('',), (None,),
        ])

    def test_clean_query(self):
        self._compare_pure_function('_clean_query', [
            ('2AZSA-RT890',), ('2AZSA',), (' 2AN62-GC5 ',), (None,),
        ])

    def test_exact_grantee_query(self):
        self._compare_pure_function('_exact_grantee_query', [
            ('2AZSA',), ('2AN62',), ('2AZSA-RT890',), ('ABC',), ('12',), (None,),
        ])

    def test_is_original_equipment_purpose(self):
        self._compare_pure_function('_is_original_equipment_purpose', [
            ('Original Equipment',), ('original equipment',),
            ('Change in Identification',), ('',), (None,),
        ])

    def test_is_fcc_attachment_document_url(self):
        self._compare_pure_function('_is_fcc_attachment_document_url', [
            ('https://apps.fcc.gov/oetcf/eas/reports/GetAttachment.cfm?id=123',),
            ('https://example.com/file.pdf',), ('',), (None,),
        ])

    def test_strip_html_tags(self):
        self._compare_pure_function('_strip_html_tags', [
            ('<b>Hello</b>',), ('<p>Test<br/>line</p>',),
            ('No tags',), ('',), (None,),
        ])

    def test_parse_date_only(self):
        self._compare_pure_function('_parse_date_only', [
            ('06/27/2023',), ('2023-06-27',), ('',), (None,), ('not-a-date',),
        ])

    def test_parse_year_from_grant_date(self):
        self._compare_pure_function('_parse_year_from_grant_date', [
            ('06/27/2023',), ('2023-06-27',), ('',), (None,), ('Granted in 2021',),
        ])

    def test_parse_decimal(self):
        self._compare_pure_function('_parse_decimal', [
            ('136.00000000',), ('400.5',), ('',), ('not-a-number',),
        ])

    def test_format_decimal_8(self):
        self._compare_pure_function('_format_decimal_8', [(136,), (400.5,)])

    def test_extract_fcc_key(self):
        self._compare_pure_function('_extract_fcc_key', [
            ('2AZSA-RT890',), ('2AZSART890',), ('',), (None,),
        ])

    def test_allowlist_matching(self):
        """_allowlist_match_terms produces same results."""
        old_func = self.old_mod._allowlist_match_terms
        new_func = self.new_mod._allowlist_match_terms
        allowlist = ['TRANSCEIVER', 'TRANSMITTER', 'RECEIVER', 'ORIGINAL EQUIPMENT']

        test_cases = [
            (
                {'FCCId': '2AZSA-RT890', 'grantee': 'Radtel',
                 'applicationPurpose': 'Change in Identification',
                 'grantDate': '06/27/2023'},
                {'text_blob': ''},
            ),
            (
                {'FCCId': '2AZSA-RT490', 'grantee': 'Radtel',
                 'applicationPurpose': 'Original Equipment',
                 'grantDate': '06/27/2023'},
                {'text_blob': 'ORIGINAL EQUIPMENT | 136-174 MHZ | TRANSCEIVER'},
            ),
            (
                {'FCCId': '2AZSA-RT5', 'grantee': 'Radtel',
                 'applicationPurpose': 'Change in Identification',
                 'grantDate': '01/01/2020'},
                {'text_blob': 'TRANSCEIVER | 400-520 MHZ'},
            ),
        ]

        for primary, meta in test_cases:
            old_result = old_func(primary, meta, allowlist)
            new_result = new_func(primary, meta, allowlist)
            self.assertEqual(
                old_result, new_result,
                f'_allowlist_match_terms mismatch for {primary.get("FCCId")}\n'
                f'  old: {old_result!r}\n'
                f'  new: {new_result!r}',
            )


if __name__ == '__main__':
    unittest.main()
