# Stage 1: Build frontend assets
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-build
WORKDIR /app

COPY frontend/package*.json ./
RUN npm ci --no-audit

ARG VITE_APP_TITLE
ARG VITE_COGNITO_USER_POOL_ID
ARG VITE_COGNITO_CLIENT_ID
ENV VITE_APP_TITLE=$VITE_APP_TITLE
ENV VITE_COGNITO_USER_POOL_ID=$VITE_COGNITO_USER_POOL_ID
ENV VITE_COGNITO_CLIENT_ID=$VITE_COGNITO_CLIENT_ID
ENV NODE_OPTIONS="--max-old-space-size=768"

COPY frontend/ .
RUN npm run build

# Stage 2: Build Python virtual environment
FROM python:3.11-slim AS pydeps
WORKDIR /opt

COPY backend/requirements.pip requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir uv

# Stage 3: Runtime image
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx supervisor graphviz \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

COPY backend/migration_agent.py .
COPY backend/gateway_infra_utils.py .
COPY --from=frontend-build /app/dist ./static

RUN rm -f /etc/nginx/sites-enabled/default
COPY backend/nginx.conf /etc/nginx/sites-enabled/default
COPY backend/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8000
CMD ["/usr/bin/supervisord"]
