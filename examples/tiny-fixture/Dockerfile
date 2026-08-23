# Tiny fixture environment: the harness's own regression target.
#
# Multi-arch base, so this builds and runs natively everywhere and the
# development loop never pays the emulation cost that real SWE-Bench Pro
# instance images do.
#
# git is not optional. BASE preparation truncates history into a single
# synthetic commit, and the solve container computes solution.diff with
# `git diff --cached` -- both run *inside* the container, so the image must
# carry git regardless of how small the project is.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/repo

# Both installs before the COPY, so editing the fixture repo re-runs neither.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==8.3.4

WORKDIR /workspace/repo
COPY repo/ /workspace/repo/
