FROM node:20-alpine AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json /frontend/package.json
RUN npm install
COPY frontend /frontend
RUN npm run build

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LD_LIBRARY_PATH=/opt/picoscope/lib:${LD_LIBRARY_PATH}

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys 6964D13AA2A43CCE && \
    gpg --batch --yes --export 6964D13AA2A43CCE > /usr/share/keyrings/picoscope-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/picoscope-archive-keyring.gpg] https://labs.picotech.com/debian picoscope main" > /etc/apt/sources.list.d/picoscope.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends libpicoipp libusb-1.0-0 && \
    apt-get download libps2000 libps2000a && \
    dpkg-deb -x libps2000_*.deb / && \
    dpkg-deb -x libps2000a_*.deb / && \
    echo "/opt/picoscope/lib" > /etc/ld.so.conf.d/picoscope.conf && \
    ldconfig && \
    rm -f libps2000_*.deb && \
    rm -f libps2000a_*.deb && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/pyproject.toml /app/backend/pyproject.toml
RUN pip install --no-cache-dir uv && \
    cd /app/backend && uv pip install --system -e .

COPY backend /app/backend
COPY --from=frontend-builder /frontend/dist /app/frontend/dist
COPY config /app/config
COPY scripts /app/scripts

RUN mkdir -p /data/spool

EXPOSE 8000
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
