# Stage 1: build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: backend runtime
FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 mailtriage
WORKDIR /srv/mailtriage
COPY backend/ ./
RUN pip install --no-cache-dir .
COPY --from=frontend-build /build/dist ./static

RUN mkdir -p /data && chown mailtriage:mailtriage /data
USER mailtriage
ENV DATA_DIR=/data \
    STATIC_DIR=/srv/mailtriage/static \
    PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8080/api/v1/status || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
