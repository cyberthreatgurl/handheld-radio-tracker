import os
import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ManualReviewForm, ManualUploadForm
from .manual_extraction import (
    candidate_matches,
    enrich_specs_from_product_url,
    extract_specs_from_text,
    extract_text_from_pdf_with_metadata,
    extraction_confidence,
    merge_extractions,
)
from .models import Brand, Radio, RadioManual

logger = logging.getLogger(__name__)


def _actor_label(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        return str(user)
    return 'anonymous'


def _apply_reviewed_fields_to_radio(radio, cleaned_data):
    radio.brand = cleaned_data.get('brand') or radio.brand
    radio.model = cleaned_data.get('model') or radio.model
    radio.fcc_id = cleaned_data.get('fcc_id') or radio.fcc_id
    radio.freq_bands_tx = cleaned_data.get('freq_bands_tx') or radio.freq_bands_tx
    radio.aprs = cleaned_data.get('aprs') or radio.aprs
    radio.gps = cleaned_data.get('gps') or radio.gps
    radio.power_watts = cleaned_data.get('power_watts') or radio.power_watts
    radio.cost_approx = cleaned_data.get('cost_approx') or radio.cost_approx
    radio.website = cleaned_data.get('website') or radio.website

    manufacturer_name = (cleaned_data.get('manufacturer') or '').strip()
    if manufacturer_name:
        manufacturer, _ = Brand.objects.get_or_create(name=manufacturer_name)
        radio.manufacturer = manufacturer

    radio.save()
    logger.info("Manual review apply fields radio_id=%s brand=%s model=%s", radio.id, radio.brand, radio.model)
    return radio


def _manual_storage_path(manual_obj):
    if not manual_obj.manual_pdf:
        return ''
    # manual_pdf.path is already under MEDIA_ROOT with upload_to=MANUALS_DIR
    return manual_obj.manual_pdf.path


def manual_upload_view(request):
    if request.method == 'POST' and request.POST.get('step') == 'confirm':
        logger.info("User action manual_confirm submit actor=%s", _actor_label(request))
        review_form = ManualReviewForm(request.POST)
        if not review_form.is_valid():
            logger.warning("Manual confirm invalid actor=%s errors=%s", _actor_label(request), review_form.errors)
            messages.error(request, 'Please correct the errors before confirming the manual import.')
            return render(request, 'radios/manual_upload.html', {
                'upload_form': ManualUploadForm(),
                'review_form': review_form,
                'candidates': [],
            })

        manual = get_object_or_404(RadioManual, pk=review_form.cleaned_data['manual_id'])
        action = review_form.cleaned_data['action']

        if action == 'existing':
            selected_radio_id = review_form.cleaned_data.get('selected_radio_id')
            if not selected_radio_id:
                logger.warning("Manual confirm missing selected radio actor=%s manual_id=%s", _actor_label(request), manual.id)
                messages.error(request, 'Select a candidate model or choose Add new model.')
                return redirect('manual_upload')
            radio = get_object_or_404(Radio, pk=selected_radio_id)
            radio = _apply_reviewed_fields_to_radio(radio, review_form.cleaned_data)
            logger.info("User action manual_confirm existing actor=%s manual_id=%s radio_id=%s", _actor_label(request), manual.id, radio.id)
        else:
            radio = Radio(
                brand=review_form.cleaned_data['brand'],
                model=review_form.cleaned_data['model'],
            )
            radio = _apply_reviewed_fields_to_radio(radio, review_form.cleaned_data)
            logger.info("User action manual_confirm new actor=%s manual_id=%s radio_id=%s", _actor_label(request), manual.id, radio.id)

        manual.radio = radio
        manual.status = RadioManual.ProcessingStatus.LINKED
        manual.extracted_data = {
            **(manual.extracted_data or {}),
            'user_confirmed_fields': {
                'brand': review_form.cleaned_data.get('brand', ''),
                'manufacturer': review_form.cleaned_data.get('manufacturer', ''),
                'model': review_form.cleaned_data.get('model', ''),
                'fcc_id': review_form.cleaned_data.get('fcc_id', ''),
                'freq_bands_tx': review_form.cleaned_data.get('freq_bands_tx', ''),
                'aprs': review_form.cleaned_data.get('aprs', ''),
                'gps': review_form.cleaned_data.get('gps', ''),
                'power_watts': review_form.cleaned_data.get('power_watts', ''),
                'cost_approx': review_form.cleaned_data.get('cost_approx', ''),
                'website': review_form.cleaned_data.get('website', ''),
            },
            'match_action': action,
        }
        manual.save(update_fields=['radio', 'status', 'extracted_data', 'updated_at'])
        logger.info("Manual linked manual_id=%s radio_id=%s", manual.id, radio.id)

        messages.success(request, 'Manual processed successfully and radio record updated.')
        return redirect('radio_detail', pk=radio.pk)

    if request.method == 'POST':
        logger.info("User action manual_upload submit actor=%s", _actor_label(request))
        upload_form = ManualUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            uploaded_file = upload_form.cleaned_data['manual_pdf']
            logger.info("Manual upload attempt actor=%s filename=%s size=%s", _actor_label(request), uploaded_file.name, getattr(uploaded_file, 'size', 0))
            manual = RadioManual.objects.create(
                manual_pdf=uploaded_file,
                source_url=upload_form.cleaned_data.get('product_url', ''),
                status=RadioManual.ProcessingStatus.UPLOADED,
            )

            manual_path = _manual_storage_path(manual)
            extracted_text, extraction_meta = extract_text_from_pdf_with_metadata(manual_path)
            manual_data = extract_specs_from_text(extracted_text, source_name=os.path.basename(manual_path))
            web_data = enrich_specs_from_product_url(manual.source_url)
            merged_data = merge_extractions(manual_data, web_data)
            confidence = extraction_confidence(merged_data)
            candidates = candidate_matches(merged_data, top_n=5)
            logger.info("Manual parse completed actor=%s manual_id=%s extraction_method=%s confidence=%s candidate_count=%s", _actor_label(request), manual.id, extraction_meta.get('method', 'unknown'), confidence, len(candidates))

            manual.extracted_text = extracted_text
            manual.extracted_data = {
                'extraction_meta': extraction_meta,
                'manual_extraction': manual_data,
                'web_enrichment': web_data,
                'merged_extraction': merged_data,
                'candidate_radio_ids': [item['radio'].id for item in candidates],
            }
            manual.extraction_confidence = confidence
            manual.status = RadioManual.ProcessingStatus.REVIEW
            manual.save(update_fields=['extracted_text', 'extracted_data', 'extraction_confidence', 'status', 'updated_at'])

            review_initial = {
                'manual_id': manual.id,
                'brand': merged_data.get('brand', ''),
                'manufacturer': merged_data.get('manufacturer', ''),
                'model': merged_data.get('model', ''),
                'fcc_id': merged_data.get('fcc_id', ''),
                'freq_bands_tx': merged_data.get('freq_bands_tx', ''),
                'aprs': merged_data.get('aprs', ''),
                'gps': merged_data.get('gps', ''),
                'power_watts': merged_data.get('power_watts', ''),
                'cost_approx': merged_data.get('cost_approx', ''),
                'website': merged_data.get('website', manual.source_url),
                'action': 'existing' if candidates else 'new',
                'selected_radio_id': candidates[0]['radio'].id if candidates else None,
            }

            messages.info(request, 'Review extracted values, choose a candidate match, or add a new model.')
            return render(request, 'radios/manual_upload.html', {
                'upload_form': ManualUploadForm(),
                'review_form': ManualReviewForm(initial=review_initial),
                'manual': manual,
                'candidates': candidates,
                'confidence_pct': int(confidence * 100),
                'extraction_method': extraction_meta.get('method', ''),
            })
        logger.warning("Manual upload invalid actor=%s errors=%s", _actor_label(request), upload_form.errors)
    else:
        logger.info("User action manual_upload view actor=%s", _actor_label(request))
        upload_form = ManualUploadForm()

    return render(request, 'radios/manual_upload.html', {'upload_form': upload_form})
