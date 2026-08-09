FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ARG RELEASE_VERSION="main"

WORKDIR /app
COPY . .
RUN uv sync --no-cache --locked --link-mode copy

ENV PRODUCTION_MODE=True \
    HOME=/app \
    RELEASE_VERSION=${RELEASE_VERSION}

RUN chown -R 1001:1001 /app
USER 1001

CMD ["uv", "run", "--no-sync", "server"]
