# OpsForge image: one image, three entrypoints (migrate | api | worker).
# Stage 1 builds the React workbench; stage 2 is the Python runtime that serves
# the built SPA from workbench/dist alongside the API.
FROM node:24-slim AS spa
WORKDIR /spa
COPY workbench/package.json workbench/package-lock.json ./
RUN npm ci
COPY workbench/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/server

WORKDIR /app

# uv for fast, lockfile-consistent installs.
RUN pip install --no-cache-dir uv

# Install dependencies first (cache layer), then the package itself.
# README.md is required by the hatchling build (pyproject `readme = ...`).
COPY pyproject.toml README.md ./
COPY server ./server
RUN uv pip install --system .

COPY migrations ./migrations
COPY skills ./skills
COPY mappings ./mappings
COPY alembic.ini ./
# The compiled SPA (served by main.create_app at /).
COPY --from=spa /spa/dist ./workbench/dist
COPY docker/entrypoint.sh /entrypoint.sh
# Strip any CR (Windows authoring) so /bin/sh can run the shebang, then chmod.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
