# The HA Supervisor passes a BUILD_FROM build-arg (its Alpine base without
# Python); we deliberately ignore it and pin a multi-arch Python base so pip
# and prebuilt wheels are available on every architecture, for Supervisor
# builds and standalone docker compose alike.
ARG BUILD_FROM=python:3.12-slim
FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Config dir resolution (app/config.py): $CONFIG_DIR if set, /data when
# running as a HA add-on, /config otherwise (standalone Docker volume).
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
