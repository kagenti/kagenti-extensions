FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY . .
# No uv.lock is committed for this test app — resolve at build time.
RUN uv sync --no-cache --link-mode copy

RUN chown -R 1001:1001 /app
USER 1001

CMD ["uv", "run", "--no-sync", "server"]
