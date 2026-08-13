# Multi-stage: `--target test` runs lint+tests (used by CI — the build
# context is streamed to the daemon, so this works even when the runner's
# checkout isn't visible to the host); the default final stage is the
# minimal runtime image.

FROM python:3.12-slim AS base

WORKDIR /app

# Code is baked into the image — the server is stateless (inventory is
# derived, config comes from env). No bind mounts, no volumes needed.
COPY pyproject.toml README.md LICENSE ./
COPY eveng_mcp/ ./eveng_mcp/
RUN pip install --no-cache-dir .

FROM base AS test
COPY tests/ ./tests/
RUN pip install --no-cache-dir -e .[dev] \
    && ruff check . \
    && pytest -q

FROM base AS runtime

EXPOSE 8080

CMD ["python", "-m", "eveng_mcp"]
