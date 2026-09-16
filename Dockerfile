# Reproducible local image for the Market Replay service and demo. No wallets, no provider keys.
FROM node:22-bookworm-slim AS web
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
RUN pnpm --filter web build

FROM python:3.12-slim-bookworm AS service
ENV PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates nodejs && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src src
RUN uv pip install --system --no-cache .
COPY sdk sdk
COPY agents agents
COPY fixtures fixtures
COPY --from=web /app/apps/web/dist apps/web/dist
ENV MARKET_REPLAY_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
# The service binds to all interfaces inside the container; publish only to localhost on the host.
CMD ["market-replay", "serve", "--host", "0.0.0.0", "--port", "8000"]
