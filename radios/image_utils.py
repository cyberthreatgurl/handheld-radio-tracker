"""Utilities for ingesting and resizing radio product images."""

import io
import logging
import uuid
from pathlib import Path

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# Maximum dimension (width or height) for stored images.
# Images larger than this are scaled down proportionally; smaller images are
# never upscaled.
MAX_DIMENSION = 1200


def _safe_image_filename(radio_pk: int, original_name: str, fmt: str) -> str:
    """Build a collision-resistant filename for storage."""
    short_uid = uuid.uuid4().hex[:8]
    ext = '.jpg' if fmt == 'JPEG' else '.png'
    safe_stem = Path(original_name).stem[:40] if original_name else 'image'
    safe_stem = ''.join(c if c.isalnum() or c in '-_' else '_' for c in safe_stem).strip('_') or 'image'
    return f"radio{radio_pk}_{safe_stem}_{short_uid}{ext}"


def ingest_radio_image(source, radio_instance, caption: str = '') -> 'RadioImage | None':
    """
    Download (if URL) or read (if file-like) an image, resize it to at most
    MAX_DIMENSION px on the longest side while preserving aspect ratio, and
    save it as a RadioImage record linked to ``radio_instance``.

    Parameters
    ----------
    source : str | file-like
        Either a URL string or an in-memory file object (e.g. from
        ``request.FILES``).
    radio_instance : Radio
        The Radio ORM instance to link the image to.
    caption : str
        Optional caption for the image.

    Returns
    -------
    RadioImage | None
        The saved RadioImage instance, or None if ingestion failed.
    """
    # Import here to avoid circular imports at module load time.
    from .models import RadioImage  # noqa: PLC0415

    try:
        from PIL import Image, UnidentifiedImageError  # noqa: PLC0415
    except ImportError:
        logger.error("Pillow is not installed — cannot process images.")
        return None

    source_url = ''
    original_name = 'image'

    # --- 1. Obtain raw bytes ---
    if isinstance(source, str):
        # URL import path
        import requests as _requests  # noqa: PLC0415
        source_url = source.strip()
        try:
            resp = _requests.get(source_url, timeout=15, stream=False)
            resp.raise_for_status()
        except Exception:
            logger.exception("RadioImage URL download failed url=%s radio_pk=%s", source_url, radio_instance.pk)
            return None

        content_type = resp.headers.get('Content-Type', '')
        if not content_type.startswith('image/'):
            logger.warning(
                "RadioImage URL did not return an image content_type=%s url=%s",
                content_type, source_url,
            )
            return None

        raw_bytes = resp.content
        original_name = Path(source_url.split('?')[0]).name or 'image'
    else:
        # File upload path
        try:
            source.seek(0)
            raw_bytes = source.read()
            original_name = getattr(source, 'name', 'image')
        except Exception:
            logger.exception("RadioImage file read failed radio_pk=%s", radio_instance.pk)
            return None

    # --- 2. Open with Pillow and capture original dimensions ---
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.verify()  # catch truncated files
        img = Image.open(io.BytesIO(raw_bytes))  # re-open after verify (verify closes the file)
    except (UnidentifiedImageError, Exception):
        logger.exception("RadioImage Pillow open failed radio_pk=%s source_url=%s", radio_instance.pk, source_url)
        return None

    original_width, original_height = img.size

    # --- 3. Determine output format ---
    # Preserve PNG only when the source has an alpha channel to retain
    # transparency.  All other formats (WEBP, BMP, GIF, TIFF, etc.) are
    # converted to JPEG for broad compatibility and smaller file sizes.
    has_alpha = img.mode in ('RGBA', 'LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info)
    output_fmt = 'PNG' if has_alpha else 'JPEG'

    # --- 4. Resize (thumbnail never upscales) ---
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
    display_width, display_height = img.size

    # --- 5. Convert colour mode as needed before saving ---
    if output_fmt == 'JPEG' and img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # --- 6. Encode to bytes ---
    buf = io.BytesIO()
    save_kwargs = {'format': output_fmt}
    if output_fmt == 'JPEG':
        save_kwargs['quality'] = 88
        save_kwargs['optimize'] = True
    img.save(buf, **save_kwargs)
    buf.seek(0)

    # --- 7. Build a safe filename and save the RadioImage record ---
    filename = _safe_image_filename(radio_instance.pk, original_name, output_fmt)

    radio_image = RadioImage(
        radio=radio_instance,
        caption=caption,
        source_url=source_url,
        original_width=original_width,
        original_height=original_height,
        display_width=display_width,
        display_height=display_height,
    )
    radio_image.image_file.save(filename, ContentFile(buf.read()), save=True)

    logger.info(
        "RadioImage ingested radio_pk=%s image_pk=%s original=%dx%d display=%dx%d fmt=%s source_url=%r",
        radio_instance.pk,
        radio_image.pk,
        original_width,
        original_height,
        display_width,
        display_height,
        output_fmt,
        source_url[:100] if source_url else '',
    )
    return radio_image
