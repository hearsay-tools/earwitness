# syntax=docker/dockerfile:1
#
# Jeden obraz, trzy tryby startu (env SERVICE):
#   web    — uvicorn (serwer)
#   worker — kolejka zadań (autosync + reaper + N procesów)
#   all    — oba procesy w jednym kontenerze (domyślnie; dla platform
#            bez wspólnego wolumenu między serwisami)
#
# Stan (SQLite + assety Recall + transkrypty) leży w /app/output —
# na platformie to jeden trwały wolumen podmontowany pod /app/output.

FROM python:3.12-slim AS builder

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /bin/uv

# Najpierw lockfile — cache warstwy przy zmianach samego kodu.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY webapp ./webapp
COPY transcripts ./transcripts
COPY README.md ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------

FROM python:3.12-slim

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY webapp ./webapp
COPY transcripts ./transcripts
COPY README.md ./
COPY docker/entrypoint.sh /entrypoint.sh

# /app/output istnieje w obrazie → wolumen inicjalizuje się z poprawnymi
# uprawnieniami użytkownika appuser (a nie roota).
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/output \
    && chown -R appuser:appuser /app \
    && chmod +x /entrypoint.sh

USER appuser
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
