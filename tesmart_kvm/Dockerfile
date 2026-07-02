# BUILD_FROM lets the Home Assistant Supervisor inject its per-arch base
# image (see build.yaml); standalone builds get the same default.
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

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
