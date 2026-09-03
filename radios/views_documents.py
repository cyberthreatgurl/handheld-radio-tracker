"""Document-serving views for FCC paperwork and manuals.

Artifact files (OET exhibits, FCC test reports, manuals) are stored on the
Docker server's artifacts share.  When a file is missing from the server —
for example because it was downloaded during a local development sync and
never copied to the share — these views fall back to re-downloading the
authoritative copy from the FCC and serving it on demand.

Serving files through a Django view (instead of linking directly to the
``/media/...`` path) also means documents keep working when ``DEBUG`` is
off, when Django's static media serving is unavailable.
"""

import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from django.core.files.base import ContentFile
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404

from .fcc_utils import _build_oet_document_filename, _download_oet_document_bytes
from .fcc_utils import _is_fcc_authoritative_url
from .models import RadioFCCTestReport, RadioManual, RadioOETDocument

logger = logging.getLogger(__name__)

# FCC attachment URLs (GetApplicationAttachment.html?id=...) return 403 unless
# the request carries a Referer from the exhibits listing page.
_FCC_EXHIBITS_REFERER = 'https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm'


def _content_type(filename):
    """Return the MIME type for a filename, defaulting to PDF for OET docs."""
    guessed = mimetypes.guess_type(filename or '')[0]
    if guessed:
        return guessed
    if (filename or '').lower().endswith('.pdf'):
        return 'application/pdf'
    return 'application/octet-stream'


def _serve_bytes(content, filename):
    """Build an inline HTTP response for the given file content."""
    safe_name = Path(filename or 'document.pdf').name.replace('"', '')
    response = HttpResponse(content, content_type=_content_type(safe_name))
    response['Content-Disposition'] = f'inline; filename="{safe_name}"'
    response['Content-Length'] = str(len(content))
    return response


def _referer_for_record(record):
    """Return the referer the FCC expects when downloading this record's file."""
    radio = getattr(record, 'radio', None)
    oet_url = getattr(radio, 'oet_page_url', '') if radio else ''
    return (oet_url or '').strip() or _FCC_EXHIBITS_REFERER


def _fetch_missing_file(record, file_field, source_url, filename_hint):
    """Re-download a missing document from its authoritative FCC source.

    Returns (content, filename) or (b'', '') when the download failed or the
    URL is not an FCC host.
    """
    if not source_url or not _is_fcc_authoritative_url(source_url):
        logger.warning(
            "Document serve skipped non-authoritative source doc_type=%s pk=%s url=%s",
            type(record).__name__, record.pk, source_url,
        )
        return b'', ''

    content = _download_oet_document_bytes(
        source_url,
        referer_url=_referer_for_record(record),
    )
    if not content:
        logger.info(
            "Document serve re-download empty doc_type=%s pk=%s url=%s",
            type(record).__name__, record.pk, source_url,
        )
        return b'', ''

    filename = filename_hint or Path(urlparse(source_url).path).name or 'document.pdf'
    file_field.save(filename, ContentFile(content), save=True)
    logger.info(
        "Document serve re-downloaded doc_type=%s pk=%s url=%s filename=%s bytes=%s",
        type(record).__name__, record.pk, source_url, filename, len(content),
    )
    return content, filename


def serve_oet_document_view(_request, pk):
    """Serve an FCC OET exhibit, re-downloading it from FCC if missing."""
    doc = get_object_or_404(RadioOETDocument, pk=pk)
    file_field = doc.document_file

    name = file_field.name or ''
    if name and file_field.storage.exists(name):
        try:
            content = file_field.read()
        except (OSError, ValueError):
            content = b''
        if content:
            return _serve_bytes(content, name)

    content, filename = _fetch_missing_file(
        doc,
        file_field,
        (doc.document_url or '').strip(),
        Path(name).name if name else _build_oet_document_filename(
            fcc_id=doc.fcc_id,
            view_attachment=doc.view_attachment,
            document_url=doc.document_url,
            display_type=doc.display_type,
        ),
    )
    if not content:
        raise Http404(
            f'OET document {pk} is not available on this server and could '
            'not be re-downloaded from the FCC.'
        )
    return _serve_bytes(content, filename)


def serve_test_report_view(_request, pk):
    """Serve an FCC test report, re-downloading it from FCC if missing."""
    report = get_object_or_404(RadioFCCTestReport, pk=pk)
    file_field = report.report_pdf

    name = file_field.name or ''
    if name and file_field.storage.exists(name):
        try:
            content = file_field.read()
        except (OSError, ValueError):
            content = b''
        if content:
            return _serve_bytes(content, name)

    content, filename = _fetch_missing_file(
        report,
        file_field,
        (report.source_url or '').strip(),
        Path(name).name if name else '',
    )
    if not content:
        raise Http404(
            f'Test report {pk} is not available on this server and could '
            'not be re-downloaded from the FCC.'
        )
    return _serve_bytes(content, filename)


def serve_manual_view(_request, pk):
    """Serve a manual, re-downloading it from its source URL if missing."""
    manual = get_object_or_404(RadioManual, pk=pk)
    file_field = manual.manual_pdf

    name = file_field.name or ''
    if name and file_field.storage.exists(name):
        try:
            content = file_field.read()
        except (OSError, ValueError):
            content = b''
        if content:
            return _serve_bytes(content, name)

    content, filename = _fetch_missing_file(
        manual,
        file_field,
        (manual.source_url or '').strip(),
        Path(name).name if name else '',
    )
    if not content:
        raise Http404(
            f'Manual {pk} is not available on this server and could not be '
            're-downloaded from its source.'
        )
    return _serve_bytes(content, filename)
