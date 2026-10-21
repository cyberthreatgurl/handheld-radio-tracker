"""Grantee-code FCC syncs only process grants dated since the last sync."""

from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from .. import fcc_utils
from ..fcc_utils import fetch_and_sync_fcc_id
from ..models import Brand, FCCSyncState, GranteeSyncState, Radio

OLD_GRANT = '01/15/2020'
NEW_GRANT = '06/10/2025'

NON_RADIO_META = {
    'record_count': 1,
    'text_blob': 'charger',
    'rule_parts': ['15B'],
    'application_id': 'abc123',
    'original_equipment_rows': [],
    'oet_documents': [],
    'original_fcc_id_from_tcb': '',
}
EMPTY_META = {
    'record_count': 0,
    'text_blob': '',
    'matched_keys': [],
    'test_report_candidates': [],
    'original_equipment_rows': [],
    'oet_documents': [],
    'rule_parts': [],
    'original_fcc_id_from_tcb': '',
}


def _grantee_response(*records):
    body = ''.join(
        '<fccidInfo>'
        f'<FCCId>{fcc_id}</FCCId>'
        '<grantee>Test Grantee Ltd</grantee>'
        '<applicationPurpose>Original Equipment</applicationPurpose>'
        f'<grantDate>{grant_date}</grantDate>'
        '</fccidInfo>'
        for fcc_id, grant_date in records
    )
    resp = Mock()
    resp.status_code = 200
    resp.text = f'<?xml version="1.0"?><fCCIDInfoes>{body}</fCCIDInfoes>'
    return resp


def _utc(year, month, day):
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class GranteeIncrementalSyncTest(TestCase):

    def setUp(self):
        fcc_utils.reset_sync_metadata_cache()
        self.response = _grantee_response(
            ('2ASNS-OLD1', OLD_GRANT),
            ('2ASNS-NEW1', NEW_GRANT),
        )

    def _sync(self, query='2ASNS', meta=NON_RADIO_META, **kwargs):
        """Run a sync; return (messages, FCC IDs whose metadata was fetched)."""
        with patch('radios.fcc_utils._fcc_request_with_retry', return_value=self.response), \
             patch('radios.fcc_utils._radio_allowlist_terms', return_value=frozenset()), \
             patch('radios.fcc_utils.fetch_fcc_secondary_metadata', return_value=dict(meta)) as fetch:
            _added, _updated, messages = fetch_and_sync_fcc_id(query, **kwargs)
        return messages, sorted(call.args[0] for call in fetch.call_args_list)

    def test_first_sync_processes_all_grants_and_stamps_grantee(self):
        before = timezone.now()
        _messages, fetched = self._sync()

        self.assertEqual(fetched, ['2ASNS-NEW1', '2ASNS-OLD1'])
        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertGreaterEqual(state.last_synced_at, before)

    def test_skips_grants_dated_before_last_sync(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        messages, fetched = self._sync()

        self.assertEqual(fetched, ['2ASNS-NEW1'])
        self.assertTrue(any('dated before the last sync' in m for m in messages))
        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertGreater(state.last_synced_at, _utc(2024, 1, 1))

    def test_full_history_processes_every_grant(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        _messages, fetched = self._sync(full_history=True)

        self.assertEqual(fetched, ['2ASNS-NEW1', '2ASNS-OLD1'])

    def test_force_reload_processes_every_grant(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        _messages, fetched = self._sync(force_reload=True)

        self.assertEqual(fetched, ['2ASNS-NEW1', '2ASNS-OLD1'])

    def test_grant_one_day_before_cutoff_is_still_processed(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2025, 6, 11))

        _messages, fetched = self._sync()

        self.assertEqual(fetched, ['2ASNS-NEW1'])

    def test_empty_metadata_holds_stamp_at_deferred_grant_date(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        self._sync(meta=EMPTY_META)

        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertEqual(state.last_synced_at, _utc(2025, 6, 10))

    def test_non_radio_without_rule_parts_holds_stamp(self):
        meta = dict(NON_RADIO_META, rule_parts=[])

        self._sync(meta=meta)

        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertEqual(state.last_synced_at, _utc(2020, 1, 15))

    def test_falls_back_to_global_sync_for_known_grantee(self):
        FCCSyncState.objects.create(pk=1, last_grantee_sync_at=_utc(2024, 1, 1))
        Brand.objects.create(name='Test Grantee', grantee_code='2ASNS')
        Radio.objects.create(brand='Test Grantee', model='X1', fcc_id='2ASNS-X1')

        _messages, fetched = self._sync()

        self.assertEqual(fetched, ['2ASNS-NEW1'])

    def test_global_sync_ignored_for_grantee_without_radios(self):
        FCCSyncState.objects.create(pk=1, last_grantee_sync_at=_utc(2024, 1, 1))
        Brand.objects.create(name='Test Grantee', grantee_code='2ASNS')

        _messages, fetched = self._sync()

        self.assertEqual(fetched, ['2ASNS-NEW1', '2ASNS-OLD1'])

    def test_dated_window_after_last_sync_does_not_stamp(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        self._sync(start_date=_utc(2025, 1, 1), end_date=_utc(2025, 12, 1))

        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertEqual(state.last_synced_at, _utc(2024, 1, 1))

    def test_dated_window_covering_last_sync_advances_stamp(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))

        self._sync(start_date=_utc(2024, 1, 1), end_date=_utc(2025, 12, 1))

        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertEqual(state.last_synced_at, _utc(2025, 12, 1))

    def test_specific_fcc_id_query_is_not_filtered_or_stamped(self):
        GranteeSyncState.objects.create(grantee_code='2ASNS', last_synced_at=_utc(2024, 1, 1))
        self.response = _grantee_response(('2ASNS-OLD1', OLD_GRANT))

        _messages, fetched = self._sync(query='2ASNS-OLD1')

        self.assertEqual(fetched, ['2ASNS-OLD1'])
        state = GranteeSyncState.objects.get(grantee_code='2ASNS')
        self.assertEqual(state.last_synced_at, _utc(2024, 1, 1))
