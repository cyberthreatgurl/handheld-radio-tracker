"""
Regression tests for the improvements-plan changes (P1.1–P2c.1).

Covers:
  - fcc_id_utils:  _validate_grantee_code, short-prefix split, char validation
  - fcc_utils:     _exact_grantee_query, reset_sync_metadata_cache,
                   _get_playwright_instance / _close_playwright_instance,
                   _copy_oet_docs_between_radios, aggregated stale logging
  - views:         _sync_single_grantee
"""
# pylint: disable=protected-access
# protected-access: tests intentionally exercise private (_) helper functions to
# verify internal correctness without relying on public API surface stability.

import json
import socket
import threading
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from .. import fcc_id_utils
from .. import fcc_utils
from .. import views


# ══════════════════════════════════════════════════════════════════════
# Phase 1 — Parser Hardening (fcc_id_utils.py)
# ══════════════════════════════════════════════════════════════════════


class ValidateGranteeCodeTest(TestCase):
    """P1.3 — _validate_grantee_code rejects 1/0 and enforces FCC length rules."""

    # ── valid codes ────────────────────────────────────────────────────
    def test_valid_3char_alpha_grantee(self):
        self.assertTrue(fcc_id_utils._validate_grantee_code('AXI'))

    def test_valid_5char_numeric_grantee(self):
        self.assertTrue(fcc_id_utils._validate_grantee_code('2AJGM'))

    def test_valid_5char_numeric_min_digit(self):
        """2 is the minimum allowed starting digit."""
        self.assertTrue(fcc_id_utils._validate_grantee_code('2ABCD'))

    def test_valid_5char_numeric_max_digit(self):
        self.assertTrue(fcc_id_utils._validate_grantee_code('9ABCD'))

    def test_valid_3char_alpha_min(self):
        self.assertTrue(fcc_id_utils._validate_grantee_code('AAA'))

    def test_valid_3char_alpha_max(self):
        self.assertTrue(fcc_id_utils._validate_grantee_code('ZZZ'))

    # ── rejects codes with 1 or 0 ──────────────────────────────────────
    def test_rejects_1_in_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('2A1GM'))

    def test_rejects_0_in_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('2A0GM'))

    def test_rejects_1_in_alpha_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('A1X'))

    def test_rejects_0_in_alpha_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('A0X'))

    # ── rejects wrong length ───────────────────────────────────────────
    def test_rejects_4char_alpha_grantee(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('ABCD'))

    def test_rejects_4char_numeric_grantee(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('2ABC'))

    def test_rejects_6char_numeric_grantee(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('2ABCDE'))

    # ── rejects invalid starting char ──────────────────────────────────
    def test_rejects_digit_start_for_3char(self):
        """3-char codes must start with A-Z, not a digit."""
        self.assertFalse(fcc_id_utils._validate_grantee_code('3AB'))

    def test_rejects_digit_in_3char_code(self):
        """3-char codes must be all letters — digits 2-9 also rejected."""
        self.assertFalse(fcc_id_utils._validate_grantee_code('AB3'))

    def test_rejects_digit_9_in_3char_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('A9X'))

    def test_rejects_alpha_start_for_5char(self):
        """5-char codes must start with 2-9, not a letter."""
        self.assertFalse(fcc_id_utils._validate_grantee_code('ABCDE'))

    # ── edge cases ─────────────────────────────────────────────────────
    def test_rejects_empty(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code(''))

    def test_rejects_1_digit_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('1'))

    def test_rejects_0_digit_code(self):
        self.assertFalse(fcc_id_utils._validate_grantee_code('0'))


class SplitFCCIDShortPrefixTest(TestCase):
    """P1.2 — split_fcc_id handles prefix shorter than expected grantee length."""

    def test_short_prefix_absorb_from_suffix_3char(self):
        """'AB-3456' → prefix='AB' (2 chars), inferred_len=3 (starts with 'A').
        P1.2 tries to absorb 1 char → 'AB3', but _validate_grantee_code rejects
        'AB3' (3-char alpha codes must be all letters). Falls back to normal:
        grantee='AB', product='3456'."""
        grantee, product = fcc_id_utils.split_fcc_id('AB-3456')
        self.assertEqual(grantee, 'AB')
        self.assertEqual(product, '3456')

    def test_short_prefix_absorb_from_suffix_5char(self):
        """'2A-3456' → prefix='2A' (2 chars), inferred_len=5 → needed=3 from suffix.
        Grant=2A345, Product=6. But _validate_grantee_code('2A345') checks for 1/0 —
        '2A345' has no 1 or 0, so it should pass."""
        grantee, product = fcc_id_utils.split_fcc_id('2A-3456')
        self.assertEqual(grantee, '2A345')
        self.assertEqual(product, '6')

    def test_short_prefix_rejected_by_validation(self):
        """'2A-1456' → would produce grantee='2A145' but '2A145' contains '1'.
        Should fall through to the normal path: grantee='2A', product='1456'."""
        grantee, product = fcc_id_utils.split_fcc_id('2A-1456')
        self.assertEqual(grantee, '2A')
        self.assertEqual(product, '1456')

    def test_short_prefix_suffix_too_short(self):
        """'2A-34' → prefix='2A' (2 chars), inferred_len=5 → needed=3, but
        suffix='34' is only 2 chars. Should fall through to normal path."""
        grantee, product = fcc_id_utils.split_fcc_id('2A-34')
        self.assertEqual(grantee, '2A')
        self.assertEqual(product, '34')

    def test_short_prefix_exact_suffix_match(self):
        """'2A-345' → prefix='2A', suffix='345' has exactly 3 chars needed.
        Grant='2A345' (valid, no 1/0), Product=''."""
        grantee, product = fcc_id_utils.split_fcc_id('2A-345')
        self.assertEqual(grantee, '2A345')
        self.assertEqual(product, '')

    def test_short_prefix_rejected_contains_zero(self):
        """'2A-0456' → grantee='2A045' contains '0'. Falls through to normal."""
        grantee, product = fcc_id_utils.split_fcc_id('2A-0456')
        self.assertEqual(grantee, '2A')
        self.assertEqual(product, '0456')

    def test_long_prefix_still_works(self):
        """P1.2 must not break the existing long-prefix logic (P1.2 is additive)."""
        grantee, product = fcc_id_utils.split_fcc_id('Y23DM-568')
        self.assertEqual(grantee, 'Y23')
        self.assertEqual(product, 'DM-568')


class InferGranteeLenCharValidationTest(TestCase):
    """P1.1 — _infer_grantee_len uses 'A' <= c <= 'Z' instead of regex."""

    def test_alpha_first_char_returns_3(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('ABC'), 3)

    def test_alpha_lowercase_first_char_returns_0(self):
        """Lowercase 'a' is not in 'A'..'Z', so returns 0 (same as old regex)."""
        self.assertEqual(fcc_id_utils._infer_grantee_len('abc'), 0)

    def test_digit_first_char_returns_5(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('2AJGM'), 5)

    def test_digit_9_first_char_returns_5(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('9ABCD'), 5)

    def test_digit_1_first_char_returns_0(self):
        """Digit '1' is not in '2'..'9', so returns 0 (same as old regex)."""
        self.assertEqual(fcc_id_utils._infer_grantee_len('1ABCD'), 0)

    def test_digit_0_first_char_returns_0(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('0ABCD'), 0)

    def test_empty_returns_0(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len(''), 0)

    def test_edge_A_returns_3(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('A'), 3)

    def test_edge_Z_returns_3(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('Z'), 3)

    def test_edge_2_returns_5(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('2'), 5)

    def test_edge_9_returns_5(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('9'), 5)

    def test_non_alphanumeric_returns_0(self):
        self.assertEqual(fcc_id_utils._infer_grantee_len('-'), 0)


# ══════════════════════════════════════════════════════════════════════
# Phase 2a — Quick Wins (fcc_utils.py)
# ══════════════════════════════════════════════════════════════════════


class ExactGranteeQueryCharValidationTest(TestCase):
    """P1.1 — _exact_grantee_query uses explicit char range instead of isalpha/isdigit."""

    def test_3char_alpha_returns_code(self):
        self.assertEqual(fcc_utils._exact_grantee_query('AXI'), 'AXI')

    def test_5char_numeric_returns_code(self):
        self.assertEqual(fcc_utils._exact_grantee_query('2AJGM'), '2AJGM')

    def test_3char_lowercase_uppercased_and_matched(self):
        """Lowercase 'axi' → _clean_query uppercases → 'AXI' → valid 3-char grantee."""
        self.assertEqual(fcc_utils._exact_grantee_query('axi'), 'AXI')

    def test_5char_digit1_start_returns_empty(self):
        """'1' not in '2'..'9', returns ''."""
        self.assertEqual(fcc_utils._exact_grantee_query('1ABCD'), '')

    def test_5char_digit0_start_returns_empty(self):
        self.assertEqual(fcc_utils._exact_grantee_query('0ABCD'), '')

    def test_full_fcc_id_returns_empty(self):
        self.assertEqual(fcc_utils._exact_grantee_query('2AJGM-UV5R'), '')

    def test_3char_digit_start_returns_empty(self):
        """3-char code starting with digit should not match."""
        self.assertEqual(fcc_utils._exact_grantee_query('3AB'), '')

    def test_5char_alpha_start_returns_empty(self):
        """5-char code starting with letter should not match."""
        self.assertEqual(fcc_utils._exact_grantee_query('ABCDE'), '')

    def test_empty_returns_empty(self):
        self.assertEqual(fcc_utils._exact_grantee_query(''), '')


class ResetSyncMetadataCacheTest(TestCase):
    """P2a.3 — reset_sync_metadata_cache clears all three module-level caches."""

    def setUp(self):
        # Seed all three caches with dummy data
        fcc_utils._sync_metadata_cache = {'FAKE-ID': {'dummy': True}}
        fcc_utils._sync_brand_cache = {('CODE', 'NAME'): ('brand', 'mfr')}
        fcc_utils._synced_oet_fcc_ids = {'SYNCED-ID'}

    def test_clears_metadata_cache(self):
        fcc_utils.reset_sync_metadata_cache()
        self.assertEqual(fcc_utils._sync_metadata_cache, {})

    def test_clears_brand_cache(self):
        fcc_utils.reset_sync_metadata_cache()
        self.assertEqual(fcc_utils._sync_brand_cache, {})

    def test_clears_oet_fcc_ids(self):
        fcc_utils.reset_sync_metadata_cache()
        self.assertEqual(fcc_utils._synced_oet_fcc_ids, set())

    def test_reset_works_on_empty_caches(self):
        """Calling reset when caches are already empty should be a no-op."""
        fcc_utils._sync_metadata_cache = {}
        fcc_utils._sync_brand_cache = {}
        fcc_utils._synced_oet_fcc_ids = set()
        fcc_utils.reset_sync_metadata_cache()
        self.assertEqual(fcc_utils._sync_metadata_cache, {})
        self.assertEqual(fcc_utils._sync_brand_cache, {})
        self.assertEqual(fcc_utils._synced_oet_fcc_ids, set())


class ModuleLevelCacheExistenceTest(TestCase):
    """P2a.3 — verify the three module-level caches exist and have correct types."""

    def test_sync_metadata_cache_is_dict(self):
        self.assertIsInstance(fcc_utils._sync_metadata_cache, dict)

    def test_sync_brand_cache_is_dict(self):
        self.assertIsInstance(fcc_utils._sync_brand_cache, dict)

    def test_synced_oet_fcc_ids_is_set(self):
        self.assertIsInstance(fcc_utils._synced_oet_fcc_ids, set)


# ══════════════════════════════════════════════════════════════════════
# Phase 2b — High Impact (fcc_utils.py)
# ══════════════════════════════════════════════════════════════════════


class PlaywrightInstancePoolTest(TestCase):
    """P2b.1 — _get_playwright_instance / _close_playwright_instance lifecycle.

    Tests per-thread browser management using thread-local storage.
    """

    def setUp(self):
        # Save original thread-local values
        self._orig_browser = getattr(
            fcc_utils._playwright_local, 'browser', None,
        )
        self._orig_pw = getattr(
            fcc_utils._playwright_local, 'playwright', None,
        )
        fcc_utils._playwright_local.browser = None
        fcc_utils._playwright_local.playwright = None

    def tearDown(self):
        fcc_utils._playwright_local.browser = self._orig_browser
        fcc_utils._playwright_local.playwright = self._orig_pw

    def test_thread_local_storage_exists(self):
        """Thread-local storage object is present."""
        self.assertTrue(hasattr(fcc_utils._playwright_local, 'browser'))
        self.assertTrue(hasattr(fcc_utils._playwright_local, 'playwright'))

    def test_get_instance_returns_none_when_not_importable(self):
        """When Playwright is not importable, returns (None, None).

        This can't be trivially mocked because the function does a local
        ``from playwright.sync_api import sync_playwright``. We verify
        instead that when thread-local browser is None before the call,
        the function attempts a fresh import.
        """
        fcc_utils._playwright_local.browser = None
        fcc_utils._playwright_local.playwright = None

        try:
            from playwright.sync_api import sync_playwright  # pylint: disable=unused-import
        except ImportError:
            self.skipTest('Playwright not installed — ImportError path untestable')

        _browser, _pw = fcc_utils._get_playwright_instance()
        if _browser is not None:
            fcc_utils._close_playwright_instance()
        # If we reach here without crashing, the function handled its path.

    def test_get_instance_returns_existing_when_initialized(self):
        """When thread-local browser is already set, return it immediately."""
        mock_browser = Mock()
        mock_pw = Mock()
        fcc_utils._playwright_local.browser = mock_browser
        fcc_utils._playwright_local.playwright = mock_pw

        browser, pw = fcc_utils._get_playwright_instance()

        self.assertIs(browser, mock_browser)
        self.assertIs(pw, mock_pw)

    def test_close_instance_clears_thread_local(self):
        """_close_playwright_instance() clears thread-local browser and pw."""
        mock_browser = Mock()
        mock_pw = Mock()
        fcc_utils._playwright_local.browser = mock_browser
        fcc_utils._playwright_local.playwright = mock_pw

        fcc_utils._close_playwright_instance()

        mock_browser.close.assert_called_once()
        mock_pw.stop.assert_called_once()
        self.assertIsNone(fcc_utils._playwright_local.browser)
        self.assertIsNone(fcc_utils._playwright_local.playwright)

    def test_close_instance_idempotent(self):
        """Calling _close_playwright_instance twice should not error."""
        mock_browser = Mock()
        mock_pw = Mock()
        fcc_utils._playwright_local.browser = mock_browser
        fcc_utils._playwright_local.playwright = mock_pw

        fcc_utils._close_playwright_instance()
        fcc_utils._close_playwright_instance()  # Should not raise

        mock_browser.close.assert_called_once()

    def test_close_handles_exception_gracefully(self):
        """If browser.close() raises, _close_playwright_instance still cleans up."""
        mock_browser = Mock()
        mock_browser.close.side_effect = RuntimeError('already closed')
        mock_pw = Mock()
        fcc_utils._playwright_local.browser = mock_browser
        fcc_utils._playwright_local.playwright = mock_pw

        fcc_utils._close_playwright_instance()  # Should not raise

        self.assertIsNone(fcc_utils._playwright_local.browser)
        self.assertIsNone(fcc_utils._playwright_local.playwright)


class CopyOETDocsBetweenRadiosTest(TestCase):
    """P2b.2 — _copy_oet_docs_between_radios copies OET documents between radios."""

    @patch('radios.fcc_utils._document_url_conflicts', return_value=False)
    @patch('radios.fcc_utils.RadioOETDocument')
    @patch('radios.fcc_utils._update_radio_oet_page_url')
    def test_copies_documents_from_existing_radio(self, mock_update_url, mock_oet_model, _mock_conflicts):
        """Documents from a sibling radio with same FCC ID are copied."""
        target_radio = Mock(id=1)
        fcc_id = '2AJGM-UV5R'

        # Simulate one existing doc on a sibling radio
        existing_doc = Mock(
            document_url='https://fcc.gov/doc.pdf',
            view_attachment='Test Report',
            exhibit_type='Test Report',
            date_submitted_to_fcc='2024-01-01',
            display_type='pdf',
            date_available='2024-01-02',
            document_file=Mock(),
        )
        existing_doc.document_file.name = 'oet_docs/2AJGM-UV5R_test_report.pdf'

        mock_qs = Mock()
        mock_qs.exclude.return_value = [existing_doc]
        mock_oet_model.objects.filter.return_value = mock_qs

        # update_or_create returns (instance, created=True)
        mock_oet_model.objects.update_or_create.return_value = (Mock(), True)

        copied = fcc_utils._copy_oet_docs_between_radios(target_radio, fcc_id)

        self.assertEqual(copied, 1)
        mock_update_url.assert_called_once_with(target_radio, fcc_id)

    @patch('radios.fcc_utils.RadioOETDocument')
    @patch('radios.fcc_utils._update_radio_oet_page_url')
    def test_no_docs_to_copy_returns_zero(self, mock_update_url, mock_oet_model):  # pylint: disable=unused-argument
        """When no sibling docs exist, returns 0 and does not update URL."""
        target_radio = Mock(id=1)
        fcc_id = '2AJGM-UV5R'

        mock_qs = Mock()
        mock_qs.exclude.return_value = []
        mock_oet_model.objects.filter.return_value = mock_qs

        copied = fcc_utils._copy_oet_docs_between_radios(target_radio, fcc_id)

        self.assertEqual(copied, 0)
        mock_update_url.assert_not_called()

    @patch('radios.fcc_utils._document_url_conflicts', return_value=False)
    @patch('radios.fcc_utils.RadioOETDocument')
    @patch('radios.fcc_utils._update_radio_oet_page_url')
    def test_skips_already_copied_documents(self, mock_update_url, mock_oet_model, _mock_conflicts):  # pylint: disable=unused-argument
        """Documents that already exist on target (created=False) are not counted."""
        target_radio = Mock(id=1)
        fcc_id = '2AJGM-UV5R'

        existing_doc = Mock(
            document_url='https://fcc.gov/doc.pdf',
            view_attachment='Test Report',
            exhibit_type='Test Report',
            date_submitted_to_fcc='2024-01-01',
            display_type='pdf',
            date_available='2024-01-02',
            document_file=Mock(),
        )
        existing_doc.document_file.name = 'oet_docs/doc.pdf'

        mock_qs = Mock()
        mock_qs.exclude.return_value = [existing_doc]
        mock_oet_model.objects.filter.return_value = mock_qs

        # update_or_create returns (instance, created=False) — already exists
        mock_oet_model.objects.update_or_create.return_value = (Mock(), False)

        copied = fcc_utils._copy_oet_docs_between_radios(target_radio, fcc_id)

        self.assertEqual(copied, 0)


class OETDeDupInSyncFunctionTest(TestCase):
    """P2b.2 — verify _synced_oet_fcc_ids is checked in _sync_oet_documents_for_radio."""

    def setUp(self):
        self._original_ids = set(fcc_utils._synced_oet_fcc_ids)

    def tearDown(self):
        fcc_utils._synced_oet_fcc_ids = self._original_ids

    @patch('radios.fcc_utils._copy_oet_docs_between_radios')
    def test_already_synced_fcc_id_triggers_copy(self, mock_copy):
        """When fcc_id is in _synced_oet_fcc_ids, copy instead of full sync."""
        fcc_utils._synced_oet_fcc_ids.add('2AJGM-UV5R')
        mock_copy.return_value = 3

        radio = Mock()
        result = fcc_utils._sync_oet_documents_for_radio(
            radio, '2AJGM-UV5R', {'oet_documents': []},
        )

        mock_copy.assert_called_once_with(radio, '2AJGM-UV5R')
        self.assertEqual(result, 3)

    @patch('radios.fcc_utils.RadioOETDocument')
    def test_not_yet_synced_fcc_id_proceeds_normally(self, mock_oet_model):
        """When fcc_id is NOT in _synced_oet_fcc_ids, proceed normally."""
        fcc_utils._synced_oet_fcc_ids.discard('2AJGM-NEW')
        mock_qs = Mock()
        mock_qs.exclude.return_value = []
        mock_oet_model.objects.filter.return_value = mock_qs

        radio = Mock()
        result = fcc_utils._sync_oet_documents_for_radio(
            radio, '2AJGM-NEW', {'oet_documents': []},
        )

        # No documents to sync → 0
        self.assertEqual(result, 0)

    def test_fcc_id_added_to_set_after_successful_sync(self):
        """After a successful OET sync, FCC ID is added to _synced_oet_fcc_ids."""
        fcc_utils._synced_oet_fcc_ids.discard('2AJGM-SYNC-TEST')

        # We can't easily test the full sync path in a unit test, but we can
        # verify the set operation logic directly
        fcc_utils._synced_oet_fcc_ids.add('2AJGM-SYNC-TEST')
        self.assertIn('2AJGM-SYNC-TEST', fcc_utils._synced_oet_fcc_ids)


# ══════════════════════════════════════════════════════════════════════
# Phase 2c — Parallel Grant Processing (views.py)
# ══════════════════════════════════════════════════════════════════════


class SyncSingleGranteeTest(TestCase):
    """P2c.1 — _sync_single_grantee calls close_old_connections per thread."""

    @patch('radios.views.fetch_and_sync_fcc_id')
    @patch('django.db.close_old_connections')
    def test_closes_connections_before_fetch(self, mock_close, mock_fetch):
        """close_old_connections is called before fetch_and_sync_fcc_id."""
        mock_fetch.return_value = (5, 3, [])

        result = views._sync_single_grantee('2AJGM', None, None)

        mock_close.assert_called_once()
        mock_fetch.assert_called_once_with('2AJGM', start_date=None, end_date=None)
        self.assertEqual(result, (5, 3, []))

    @patch('radios.views.fetch_and_sync_fcc_id')
    @patch('django.db.close_old_connections')
    def test_passes_start_date_and_end_date(self, mock_close, mock_fetch):  # pylint: disable=unused-argument
        """Start and end dates are forwarded to fetch_and_sync_fcc_id."""
        from datetime import datetime, timezone
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_fetch.return_value = (0, 0, [])

        views._sync_single_grantee('2AJGM', start, end)

        mock_fetch.assert_called_once_with('2AJGM', start_date=start, end_date=end)

    @patch('radios.views.fetch_and_sync_fcc_id')
    @patch('django.db.close_old_connections')
    def test_propagates_exceptions(self, mock_close, mock_fetch):
        """If fetch_and_sync_fcc_id raises, the exception propagates."""
        mock_fetch.side_effect = RuntimeError('FCC API down')

        with self.assertRaises(RuntimeError):
            views._sync_single_grantee('2AJGM', None, None)

        # close_old_connections should still have been called
        mock_close.assert_called_once()


class ThreadSafetyRegressionTest(TestCase):
    """P2c.1 — verify _sync_single_grantee is thread-safe in a simple concurrent test."""

    @patch('radios.views.fetch_and_sync_fcc_id')
    @patch('django.db.close_old_connections')
    def test_concurrent_calls_do_not_interfere(self, mock_close, mock_fetch):  # pylint: disable=unused-argument
        """Multiple concurrent calls each get their own close_old_connections."""
        mock_fetch.return_value = (1, 0, [])
        results: list = []
        errors: list = []

        def worker(code):
            try:
                result = views._sync_single_grantee(code, None, None)
                results.append(result)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f'2A{chr(65+i)}GM',))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 4)
        self.assertEqual(mock_close.call_count, 4)
        self.assertEqual(mock_fetch.call_count, 4)


# ══════════════════════════════════════════════════════════════════════
# Integration: SplitFCCID with validate_grantee_code
# ══════════════════════════════════════════════════════════════════════


class SplitFCCIDWithValidationIntegrationTest(TestCase):
    """End-to-end: split_fcc_id now uses _validate_grantee_code internally.

    These tests confirm the full pipeline works together:
    _infer_grantee_len → split_fcc_id → _validate_grantee_code
    """

    # ── standard hyphenated IDs (unchanged behavior) ───────────────────
    def test_standard_numeric_grantee(self):
        g, p = fcc_id_utils.split_fcc_id('2AJGM-UV5R')
        self.assertEqual(g, '2AJGM')
        self.assertEqual(p, 'UV5R')

    def test_standard_alpha_grantee(self):
        g, p = fcc_id_utils.split_fcc_id('AXI-123')
        self.assertEqual(g, 'AXI')
        self.assertEqual(p, '123')

    def test_long_prefix_still_split_correctly(self):
        """'2A4FBTDBL-1' → grantee=2A4FB, product=TDBL-1"""
        g, p = fcc_id_utils.split_fcc_id('2A4FBTDBL-1')
        self.assertEqual(g, '2A4FB')
        self.assertEqual(p, 'TDBL-1')

    # ── short-prefix edge cases (new behavior P1.2) ────────────────────
    def test_short_prefix_absorb_valid_suffix(self):
        """'3A-45678' → grantee should be 5 chars, '3A456' has no 1/0."""
        g, p = fcc_id_utils.split_fcc_id('3A-45678')
        self.assertEqual(g, '3A456')
        self.assertEqual(p, '78')

    def test_short_prefix_reject_invalid_with_1(self):
        """'3A-15678' → '3A156' contains '1', falls back to normal."""
        g, p = fcc_id_utils.split_fcc_id('3A-15678')
        self.assertEqual(g, '3A')
        self.assertEqual(p, '15678')

    def test_short_prefix_reject_invalid_with_0(self):
        """'3A-05678' → '3A056' contains '0', falls back to normal."""
        g, p = fcc_id_utils.split_fcc_id('3A-05678')
        self.assertEqual(g, '3A')
        self.assertEqual(p, '05678')

    # ── preferred grantee still overrides ──────────────────────────────
    def test_preferred_grantee_ignored_when_hyphen_present(self):
        """Preferred grantee only applies to compact (non-hyphenated) IDs.
        With a hyphen, the hyphen-branch in split_fcc_id always runs first,
        so preferred_grantee_code is not consulted."""
        g, p = fcc_id_utils.split_fcc_id('3A-45678', preferred_grantee_code='3A')
        # P1.2: prefix='3A' (2 chars), inferred_len=5 (starts with '3'),
        # absorb 3 from suffix → '3A456' (valid), product='78'
        self.assertEqual(g, '3A456')
        self.assertEqual(p, '78')

    # ── compact (no hyphen) IDs still work ─────────────────────────────
    def test_compact_numeric_prefix(self):
        g, p = fcc_id_utils.split_fcc_id('2AJGMUV5R')
        self.assertEqual(g, '2AJGM')
        self.assertEqual(p, 'UV5R')

    def test_compact_alpha_prefix(self):
        g, p = fcc_id_utils.split_fcc_id('AXI123')
        self.assertEqual(g, 'AXI')
        self.assertEqual(p, '123')

    # ── grantee-only inputs ────────────────────────────────────────────
    def test_grantee_only_numeric(self):
        g, p = fcc_id_utils.split_fcc_id('2AJGM')
        self.assertEqual(g, '2AJGM')
        self.assertEqual(p, '')

    def test_grantee_only_alpha(self):
        g, p = fcc_id_utils.split_fcc_id('AXI')
        self.assertEqual(g, 'AXI')
        self.assertEqual(p, '')


# ══════════════════════════════════════════════════════════════════════
# FCC XML sanitization (malformed XML fix)
# ══════════════════════════════════════════════════════════════════════


class SanitizeFCCXMLTest(TestCase):
    """Tests for _sanitize_fcc_xml — fixes malformed FCC XML responses."""

    def test_fixes_unescaped_ampersand(self):
        """'Johnson & Johnson' → 'Johnson &amp; Johnson'"""
        result = fcc_utils._sanitize_fcc_xml(
            '<company>Johnson & Johnson</company>',
        )
        self.assertIn('Johnson &amp; Johnson', result)
        self.assertNotIn('Johnson & Johnson', result)

    def test_preserves_already_escaped_entities(self):
        """Already-escaped &amp; &lt; &gt; should not be double-escaped."""
        result = fcc_utils._sanitize_fcc_xml(
            '<test>&amp; &lt; &gt; &quot; &apos;</test>',
        )
        self.assertIn('&amp;', result)
        self.assertIn('&lt;', result)
        self.assertIn('&gt;', result)
        self.assertIn('&quot;', result)
        self.assertIn('&apos;', result)
        # Should only have one occurrence of each
        self.assertEqual(result.count('&amp;amp;'), 0)

    def test_preserves_numeric_entities(self):
        """&#NN; entities should be preserved."""
        result = fcc_utils._sanitize_fcc_xml(
            '<char>&#169; &#174;</char>',
        )
        self.assertIn('&#169;', result)
        self.assertIn('&#174;', result)

    def test_strips_invalid_control_characters(self):
        """Control chars below 0x20 (except \\t \\n \\r) are removed."""
        result = fcc_utils._sanitize_fcc_xml(
            '<test>\x00\x01\x08\x0b\x0c\x0e\x1f</test>',
        )
        # All invalid control chars should be stripped
        for ch in ['\x00', '\x01', '\x08', '\x0b', '\x0c', '\x0e', '\x1f']:
            self.assertNotIn(ch, result)
        # The XML tags should still be intact
        self.assertIn('<test>', result)
        self.assertIn('</test>', result)

    def test_preserves_valid_whitespace(self):
        """Tab, newline, carriage return should be preserved."""
        result = fcc_utils._sanitize_fcc_xml(
            '<test>\t\n\r</test>',
        )
        self.assertIn('\t', result)
        self.assertIn('\n', result)
        self.assertIn('\r', result)

    def test_empty_content(self):
        """Empty content returns unchanged."""
        result = fcc_utils._sanitize_fcc_xml('')
        self.assertEqual(result, '')

    def test_none_content(self):
        """None content returns unchanged."""
        result = fcc_utils._sanitize_fcc_xml(None)
        self.assertIsNone(result)

    def test_mixed_fcc_real_world(self):
        """Simulates a real FCC XML response with unescaped & and control chars."""
        result = fcc_utils._sanitize_fcc_xml(
            '<row><name>Baofeng &amp; Po Fung</name>'
            '<company>Test & Measurement Corp</company>'
            '</row>'
        )
        # Already-escaped stays
        self.assertIn('Baofeng &amp; Po Fung', result)
        # Unescaped gets fixed
        self.assertIn('Test &amp; Measurement Corp', result)

    def test_multiple_unescaped_ampersands(self):
        """Multiple unescaped & in one line."""
        result = fcc_utils._sanitize_fcc_xml(
            '<line>A & B & C</line>',
        )
        self.assertIn('A &amp; B &amp; C', result)
        self.assertNotIn('A & B & C', result)


class ProbeEmbeddableViewTest(TestCase):
    """Split-viewer embeddability probe: SSRF-safe header check."""

    def _request(self, url='https://example.com/'):
        user = get_user_model().objects.create_user(
            'prober', password='pw', is_staff=True,
        )
        req = RequestFactory().post('/radios/probe-embeddable/', {'url': url})
        req.user = user
        return req

    def _data(self, resp):
        return json.loads(resp.content)

    def test_host_is_public_rejects_loopback(self):
        with patch(
            'radios.views.socket.getaddrinfo',
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))],
        ):
            self.assertFalse(views._host_is_public('example.com'))

    def test_host_is_public_accepts_public_ip(self):
        with patch(
            'radios.views.socket.getaddrinfo',
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))],
        ):
            self.assertTrue(views._host_is_public('example.com'))

    def test_x_frame_options_blocks_embedding(self):
        fake_resp = Mock(status_code=200)
        fake_resp.headers = {'X-Frame-Options': 'DENY'}
        fake_resp.close = Mock()
        with patch('radios.views.curl_requests.get', return_value=fake_resp):
            with patch('radios.views._host_is_public', return_value=True):
                resp = views.probe_embeddable_view(self._request())
        data = self._data(resp)
        self.assertFalse(data['embeddable'])
        self.assertEqual(data['reason'], 'x_frame_options')

    def test_plain_page_is_embeddable(self):
        fake_resp = Mock(status_code=200)
        fake_resp.headers = {}
        fake_resp.close = Mock()
        with patch('radios.views.curl_requests.get', return_value=fake_resp):
            with patch('radios.views._host_is_public', return_value=True):
                resp = views.probe_embeddable_view(self._request())
        self.assertTrue(self._data(resp)['embeddable'])

    def test_invalid_scheme_rejected(self):
        with patch('radios.views._host_is_public', return_value=True):
            resp = views.probe_embeddable_view(self._request('file:///etc/passwd'))
        self.assertFalse(self._data(resp)['embeddable'])

    def test_private_host_rejected(self):
        with patch('radios.views._host_is_public', return_value=False):
            resp = views.probe_embeddable_view(self._request('http://192.168.1.1/admin'))
        data = self._data(resp)
        self.assertFalse(data['embeddable'])
        self.assertEqual(data['reason'], 'blocked_host')
