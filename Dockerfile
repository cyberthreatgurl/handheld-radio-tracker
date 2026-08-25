# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99

WORKDIR /app

# System dependencies:
#   tesseract-ocr  -> pytesseract OCR for FCC OET document parsing
#   libgl1/libglib -> Playwright Chromium runtime libraries
#   xvfb/dbus-x11  -> virtual display for headed Chrome (FCC OET fallback)
#   curl, gnupg    -> fetch/verify the Google Chrome apt repo
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
        curl \
        gnupg \
        xvfb \
        dbus-x11 \
    && if [ "$(uname -m)" = "x86_64" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
        && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends google-chrome-stable; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (installed in a dedicated layer for cache reuse).
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application source.
COPY . .

# Compile Tailwind CSS (uses the bundled pytailwindcss binary - no Node required)
# and collect static assets into STATIC_ROOT for whitenoise to serve.
RUN python manage.py tailwind build \
    && python manage.py collectstatic --noinput

# Install the Chromium browser used by the FCC OET exhibit fallback.
RUN playwright install chromium --with-deps

EXPOSE 8000

RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
