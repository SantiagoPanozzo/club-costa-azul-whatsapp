FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies first for better layer caching.
# Copy uv.lock and sync --frozen so the image is reproducible: an unpinned
# resolve picked up an incompatible new release (websockets 17.2) and broke the build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} (set by Railway at runtime) is expanded
CMD ["sh", "-c", "uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
