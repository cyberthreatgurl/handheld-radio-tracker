from io import StringIO
from unittest.mock import ANY, patch
import tempfile

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings

from .models import Brand, IgnoredGrantee, Manufacturer, Radio, RadioManual, RadioFCCTestReport, RadioOETDocument, RadioFirmware


class AuditOETSyncCommandTest(TestCase):
    def test_missing_only_reports_zero_oet_fcc_ids(self):
        Radio.objects.create(brand='BrandA', model='A1', fcc_id='2AJGM-ONE')
        radio_with_docs = Radio.objects.create(brand='BrandB', model='B1', fcc_id='2AJGM-TWO')
        RadioOETDocument.objects.create(
            radio=radio_with_docs,
            fcc_id='2AJGM-TWO',
            view_attachment='User Manual',
            exhibit_type='Users Manual',
            document_url='https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=1',
        )

        output = StringIO()
        call_command(
            'audit_oet_sync',
            '--fcc-id', '2AJGM-ONE',
            '--fcc-id', '2AJGM-TWO',
            '--missing-only',
            stdout=output,
        )

        rendered = output.getvalue()
        self.assertIn('FCC 2AJGM-ONE', rendered)
        self.assertNotIn('FCC 2AJGM-TWO', rendered)
        self.assertIn('Breakdown 0=1 1-5=1 6-10=0 11+=0', rendered)

    def test_grantee_summary_counts_radios_with_and_without_oet(self):
        Brand.objects.create(name='BrandA', grantee_code='2AJGM')
        radio_with_docs = Radio.objects.create(brand='BrandA', model='A1', fcc_id='2AJGM-ONE')
        Radio.objects.create(brand='BrandA', model='A2', fcc_id='2AJGM-TWO')
        RadioOETDocument.objects.create(
            radio=radio_with_docs,
            fcc_id='2AJGM-ONE',
            view_attachment='User Manual',
            exhibit_type='Users Manual',
            document_url='https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=2',
        )

        output = StringIO()
        call_command('audit_oet_sync', '--grantee', '2AJGM', stdout=output)

        rendered = output.getvalue()
        self.assertIn('GRANTEE 2AJGM | radios=2 | with_oet=1 | zero_oet=1', rendered)

    def test_sync_first_calls_fetcher_for_fcc_ids_and_grantees(self):
        Brand.objects.create(name='BrandA', grantee_code='2AJGM')
        Radio.objects.create(brand='BrandA', model='A1', fcc_id='2AJGM-ONE')

        output = StringIO()
        with patch('radios.management.commands.audit_oet_sync.fetch_and_sync_fcc_id') as mocked_fetch:
            call_command(
                'audit_oet_sync',
                '--fcc-id', '2AJGM-ONE',
                '--grantee', '2AJGM',
                '--sync-first',
                stdout=output,
            )

        self.assertEqual(mocked_fetch.call_count, 2)
        mocked_fetch.assert_any_call('2AJGM-ONE')
        mocked_fetch.assert_any_call('2AJGM')


class SyncFCCCommandIgnoreListTest(TestCase):
    def test_all_grantees_skips_ignored_grantee_codes(self):
        Brand.objects.create(name='Allowed Brand', grantee_code='2AJGM')
        Brand.objects.create(name='Ignored Brand', grantee_code='XH8')
        IgnoredGrantee.objects.create(grantee_code='XH8', reason='Projector screens')

        output = StringIO()
        with patch('radios.management.commands.sync_fcc.fetch_and_sync_fcc_id', return_value=(0, 0, [])) as mocked_fetch:
            call_command('sync_fcc', '--all-grantees', stdout=output)

        rendered = output.getvalue()
        self.assertIn('Found 1 grantees to process.', rendered)
        mocked_fetch.assert_called_once_with('2AJGM', start_date=None, end_date=ANY)


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class CleanupOrphanedRecordsCommandTest(TestCase):
    def _create_orphaned_radio_fixture(self, *, brand_name='ScreenCo', grantee='XH8', shared_manufacturer=False):
        existing_brand = Brand.objects.create(name='Existing Brand', grantee_code='EX123')
        deleted_brand = Brand.objects.create(name=brand_name, grantee_code=grantee)
        manufacturer = Manufacturer.objects.create(full_name=f'{brand_name} OEM')
        manufacturer.brands.add(deleted_brand)
        if shared_manufacturer:
            manufacturer.brands.add(existing_brand)

        radio = Radio.objects.create(
            brand=deleted_brand.name,
            model='Model 100',
            manufacturer=manufacturer,
            fcc_id=f'{grantee}-MODEL100',
        )
        RadioManual.objects.create(
            radio=radio,
            manual_pdf=SimpleUploadedFile('manual.pdf', b'manual', content_type='application/pdf'),
        )
        RadioFCCTestReport.objects.create(
            radio=radio,
            fcc_id=radio.fcc_id,
            report_pdf=SimpleUploadedFile('report.pdf', b'report', content_type='application/pdf'),
        )
        RadioOETDocument.objects.create(
            radio=radio,
            fcc_id=radio.fcc_id,
            view_attachment='User Manual',
            exhibit_type='Users Manual',
            document_url='https://example.com/oet.pdf',
            document_file=SimpleUploadedFile('oet.pdf', b'oet', content_type='application/pdf'),
        )
        RadioFirmware.objects.create(radio=radio, label='Main', version='1.0')

        Brand.objects.filter(pk=deleted_brand.pk).delete()
        return manufacturer, radio

    def test_dry_run_reports_orphaned_radios_and_manufacturers(self):
        manufacturer, radio = self._create_orphaned_radio_fixture()

        output = StringIO()
        call_command('cleanup_orphaned_records', '--grantee', 'XH8', stdout=output)

        rendered = output.getvalue()
        self.assertIn('Orphaned radios found: 1', rendered)
        self.assertIn(f'Radio {radio.pk} | brand=ScreenCo | model=Model 100 | fcc_id=XH8-MODEL100', rendered)
        self.assertIn(f'Orphaned manufacturers found: 1', rendered)
        self.assertIn(f'Manufacturer {manufacturer.pk} | name=ScreenCo OEM', rendered)
        self.assertTrue(Radio.objects.filter(pk=radio.pk).exists())
        self.assertTrue(Manufacturer.objects.filter(pk=manufacturer.pk).exists())

    def test_apply_removes_targeted_orphaned_radios_and_manufacturers(self):
        manufacturer, radio = self._create_orphaned_radio_fixture()

        output = StringIO()
        call_command('cleanup_orphaned_records', '--grantee', 'XH8', '--apply', stdout=output)

        rendered = output.getvalue()
        self.assertIn('Deleted orphaned radios: 1', rendered)
        self.assertIn('Deleted orphaned manufacturers: 1', rendered)
        self.assertFalse(Radio.objects.filter(pk=radio.pk).exists())
        self.assertFalse(Manufacturer.objects.filter(pk=manufacturer.pk).exists())
        self.assertFalse(RadioManual.objects.filter(radio_id=radio.pk).exists())
        self.assertFalse(RadioFCCTestReport.objects.filter(radio_id=radio.pk).exists())
        self.assertFalse(RadioOETDocument.objects.filter(radio_id=radio.pk).exists())
        self.assertFalse(RadioFirmware.objects.filter(radio_id=radio.pk).exists())

    def test_apply_keeps_shared_manufacturer(self):
        manufacturer, radio = self._create_orphaned_radio_fixture(shared_manufacturer=True)

        call_command('cleanup_orphaned_records', '--grantee', 'XH8', '--apply', stdout=StringIO())

        self.assertFalse(Radio.objects.filter(pk=radio.pk).exists())
        self.assertTrue(Manufacturer.objects.filter(pk=manufacturer.pk).exists())

    def test_filtered_run_with_no_matching_orphaned_radios_does_not_touch_global_orphaned_manufacturers(self):
        manufacturer, _radio = self._create_orphaned_radio_fixture(grantee='ZZZ')

        output = StringIO()
        call_command('cleanup_orphaned_records', '--grantee', 'XH8', '--apply', stdout=output)

        rendered = output.getvalue()
        self.assertIn('Orphaned radios found: 0', rendered)
        self.assertIn('Orphaned manufacturers found: 0', rendered)
        self.assertIn('Deleted orphaned manufacturers: 0', rendered)
        self.assertTrue(Manufacturer.objects.filter(pk=manufacturer.pk).exists())
