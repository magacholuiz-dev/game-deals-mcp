FROM python:3.12-slim

# uv resolves and installs from the lockfile, so the image has exactly the
# versions the tests ran against.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# State lives in one volume: the database, the HTTP cache and the backups.
ENV GAMEDEALS_DB=/data/deals.db \
    HTTP_CACHE_PATH=/data/http_cache.db \
    GAMEDEALS_BACKUP_DIR=/data/backups \
    GAMEDEALS_WEB_HOST=0.0.0.0 \
    GAMEDEALS_WEB_PORT=8787 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER app
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8787/api/health', timeout=8)"

ENTRYPOINT ["/entrypoint.sh"]
