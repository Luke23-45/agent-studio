# Neryva Agent Studio - worker image (Arch 16, P0-12)
# Shares the backend base; entrypoint is the worker console script.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend

RUN pip install --no-cache-dir -e '.[all]'

CMD ["neryva-worker"]
