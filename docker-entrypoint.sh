#!/bin/sh
set -e

# Start a virtual display so headed Chrome can run (FCC OET browser fallback).
# Harmless when the browser runs headless.
if command -v Xvfb >/dev/null 2>&1; then
    export DISPLAY="${DISPLAY:-:99}"
    Xvfb "${DISPLAY}" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
fi

# Apply any pending database migrations (idempotent, safe on every start).
python manage.py migrate --noinput

# Refresh static files (ensures collectstatic output exists in the image).
python manage.py collectstatic --noinput

# Start the application server.
exec gunicorn radio_database.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --threads "${GUNICORN_THREADS:-2}" \
    --access-logfile - \
    --error-logfile -
