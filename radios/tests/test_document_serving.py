"""Regression tests for the self-healing document-serving views."""

# pylint: disable=no-member, missing-function-docstring
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# missing-function-docstring: test methods are self-documenting by name

import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from ..models import Radio, RadioOETDocument


class DocumentServingTest(TestCase):
    """The serving views must serve stored files and re-fetch missing ones."""

    def _create_oet_doc(self, with_file=True, source_url=''):
        radio = Radio.objects.create(
            brand='TestBrand',
            model='M1',
            fcc_id='2TEST-M1',
        )
        doc = RadioOETDocument.objects.create(
            radio=radio,
            fcc_id='2TEST-M1',
            view_attachment='Instruction Manual',
            document_url=source_url,
        )
        if with_file:
            doc.document_file.save(
                '2TEST-M1_manual.pdf',
                ContentFile(b'%PDF-1.4 fake-pdf'),
                save=True,
            )
        return doc

    def test_serves_existing_oet_document(self):
        with tempfile.TemporaryDirectory(prefix='radio-tracker-test-media-') as tempdir:
            with self.settings(MEDIA_ROOT=tempdir):
                doc = self._create_oet_doc()
                url = reverse('serve_oet_document', kwargs={'pk': doc.pk})
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'%PDF-1.4 fake-pdf')
                self.assertEqual(response['Content-Type'], 'application/pdf')

    def test_re_downloads_missing_oet_document(self):
        source_url = 'https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=123'
        with tempfile.TemporaryDirectory(prefix='radio-tracker-test-media-') as tempdir:
            with self.settings(MEDIA_ROOT=tempdir):
                doc = self._create_oet_doc(with_file=False, source_url=source_url)
                url = reverse('serve_oet_document', kwargs={'pk': doc.pk})
                with patch(
                    'radios.views_documents._download_oet_document_bytes',
                    return_value=b'%PDF-1.4 refetched',
                ) as mock_download:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'%PDF-1.4 refetched')

                # The FCC attachment endpoint requires an exhibits-page referer.
                mock_download.assert_called_once_with(
                    source_url,
                    referer_url='https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm',
                )

                # The re-fetched file must now be persisted on the storage backend.
                doc.refresh_from_db()
                self.assertTrue(doc.document_file.storage.exists(doc.document_file.name))

    def test_missing_document_without_source_returns_404(self):
        with tempfile.TemporaryDirectory(prefix='radio-tracker-test-media-') as tempdir:
            with self.settings(MEDIA_ROOT=tempdir):
                doc = self._create_oet_doc(with_file=False, source_url='')
                url = reverse('serve_oet_document', kwargs={'pk': doc.pk})
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
