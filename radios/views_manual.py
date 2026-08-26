import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from .forms import DocumentUploadForm
from .models import Radio, RadioManual
from .accounts_decorators import staff_required

logger = logging.getLogger(__name__)


def _actor_label(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        return str(user)
    return 'anonymous'


@staff_required
def manual_upload_view(request):
    """Document library: upload files of any type and browse/download them."""

    # ------------------------------------------------------------------
    # Handle file upload
    # ------------------------------------------------------------------
    if request.method == 'POST':
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            radio = form.cleaned_data.get('radio')
            doc_type = form.cleaned_data['doc_type']
            uploaded_file = form.cleaned_data['document_file']

            logger.info(
                "User action document_upload submit actor=%s radio_id=%s doc_type=%s filename=%s size=%s",
                _actor_label(request),
                radio.pk if radio else None,
                doc_type,
                getattr(uploaded_file, 'name', ''),
                getattr(uploaded_file, 'size', 0),
            )

            RadioManual.objects.create(
                radio=radio,
                manual_pdf=uploaded_file,
                doc_type=doc_type,
                status=(
                    RadioManual.ProcessingStatus.LINKED
                    if radio
                    else RadioManual.ProcessingStatus.UPLOADED
                ),
            )

            messages.success(
                request,
                f'"{uploaded_file.name}" uploaded successfully'
                + (f' and linked to {radio}.' if radio else '.'),
            )

            # Redirect, preserving any radio filter
            redirect_url = reverse('manual_upload')
            if radio:
                redirect_url += f'?radio={radio.pk}'
            return redirect(redirect_url)

        logger.warning(
            "User action document_upload invalid actor=%s errors=%s",
            _actor_label(request),
            form.errors,
        )
    else:
        logger.info("User action document_library view actor=%s", _actor_label(request))
        # Pre-populate radio from query string (e.g. linked from radio edit page)
        initial = {}
        radio_pk = request.GET.get('radio')
        if radio_pk:
            try:
                initial['radio'] = int(radio_pk)
            except (ValueError, TypeError):
                pass
        form = DocumentUploadForm(initial=initial)

    # ------------------------------------------------------------------
    # Build document list (optionally filtered by radio)
    # ------------------------------------------------------------------
    radio_pk = request.GET.get('radio')
    filter_radio = None
    documents_qs = (
        RadioManual.objects
        .exclude(manual_pdf='')
        .select_related('radio')
        .order_by('-created_at')
    )
    if radio_pk:
        try:
            filter_radio = Radio.objects.get(pk=int(radio_pk))
            documents_qs = documents_qs.filter(radio=filter_radio)
        except (Radio.DoesNotExist, ValueError, TypeError):
            pass

    return render(request, 'radios/manual_upload.html', {
        'upload_form': form,
        'documents': documents_qs[:200],
        'filter_radio': filter_radio,
        'doc_type_labels': dict(RadioManual.DocType.choices),
    })
