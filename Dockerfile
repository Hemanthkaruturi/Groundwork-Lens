FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LENS_DATA_DIR=/data \
    LENS_HOST=0.0.0.0 \
    LENS_PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash lens \
    && mkdir -p /data \
    && chown -R lens:lens /data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt Pillow>=10.0

COPY engine /app/engine
COPY lens /app/lens
COPY pyproject.toml LICENSE README.md /app/

RUN chown -R lens:lens /app

USER lens
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["python3", "-m", "lens"]
