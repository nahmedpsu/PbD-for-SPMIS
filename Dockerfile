# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY catalog ./catalog
COPY policy ./policy

RUN pip install --upgrade pip && pip install ".[postgres]"

# Run as an unprivileged user; data directory for SQLite in the all-in-one mode.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app:app /app
USER app

ENV PBD_CATALOG_DIR=/app/catalog \
    PBD_DATABASE_URL=sqlite:////app/data/{service}.sqlite3

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

ENTRYPOINT ["pbd-spmis"]
CMD ["serve", "all", "--host", "0.0.0.0", "--port", "8000"]
