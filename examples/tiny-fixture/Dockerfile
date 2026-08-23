# Tiny fixture environment: the harness's own regression target.
#
# Multi-arch base, so this builds and runs natively everywhere and the
# development loop never pays the emulation cost that real SWE-Bench Pro
# instance images do. No package install step -- PYTHONPATH is enough for a
# two-module library, which keeps the build to one network round trip.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Before the COPY, so editing the fixture repo does not re-run the install.
RUN pip install --no-cache-dir pytest==8.3.4

WORKDIR /app
COPY repo/ /app/
