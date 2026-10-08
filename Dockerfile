FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# SQLite lives here; mount a volume to keep it across rebuilds.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 reposter && \
    chown -R reposter:reposter /app
USER reposter

VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sys, sqlite3, pathlib; p = pathlib.Path('/app/data/reposter.sqlite3'); sys.exit(0 if not p.exists() or sqlite3.connect(p).execute('select 1').fetchone() else 1)"

CMD ["python", "-m", "bot.main"]
