# One container: the engine, the API and the web UI.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[server]" && useradd --create-home --uid 10001 app

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
CMD ["sh", "-c", "uvicorn mqresolver.server:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
