# BrainFrog browser-runner image (operator-built; BrainFrog never builds or pulls images itself).
#
# Base: official Playwright Python image v1.64.0 (Ubuntu 24.04), pinned by registry index digest.
# The official image ships Chromium/Firefox/WebKit for Playwright 1.64.0 but not the `playwright`
# Python package, so it is added here from a hash-locked requirements file (no dependency resolution).
#
# Build (from this directory):
#   docker build --pull=false -t brainfrog-browser-runner:v1.64.0 .
# Then review the resulting image ID and record it in APPROVED_BROWSER_IMAGES
# (core/runtime/browser_sandbox.py). A rebuild yields a new image ID that must be re-approved.
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23

COPY requirements.lock /tmp/brainfrog-requirements.lock
RUN python3 -m pip install --no-cache-dir --no-deps --require-hashes --break-system-packages \
        -r /tmp/brainfrog-requirements.lock \
    && rm /tmp/brainfrog-requirements.lock \
    && python3 -c "import importlib.metadata as m; assert m.version('playwright') == '1.64.0'"

USER pwuser
