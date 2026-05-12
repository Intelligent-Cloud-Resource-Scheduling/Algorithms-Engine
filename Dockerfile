# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools (needed for some native wheels like pygad deps)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY server.py .
COPY src/ ./src/

# ── Environment variables ─────────────────────────────────────────────────────
# CALLBACK_SERVER_URL – URL of the backend that will receive results (required)
# HOST                – bind address (default: 0.0.0.0)
# PORT                – listen port  (default: 8000)
ENV HOST=0.0.0.0 \
    PORT=8000 \
    CALLBACK_SERVER_URL=""

EXPOSE ${PORT}

# Run with uvicorn; workers=1 keeps session state in-process (single container).
# Increase --workers if you add a shared cache (Redis) instead.
CMD ["sh", "-c", "uvicorn server:app --host $HOST --port $PORT --workers 1"]
