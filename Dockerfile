FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/pyproject.toml /app/backend/pyproject.toml
RUN pip install --no-cache-dir uv && \
    cd /app/backend && uv pip install --system -e .

COPY backend /app/backend
COPY frontend /app/frontend
COPY config /app/config
COPY scripts /app/scripts

RUN mkdir -p /data/spool

EXPOSE 8000
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
