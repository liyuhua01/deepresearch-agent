FROM public.ecr.aws/docker/library/node:24.19.0-bookworm-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.26 AS uv-bin

FROM public.ecr.aws/docker/library/python:3.12.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    HOST=0.0.0.0 \
    PORT=10000

WORKDIR /app/backend
COPY --from=uv-bin /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/src ./src
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist

EXPOSE 10000

CMD ["uv", "run", "--no-sync", "python", "src/main.py"]
