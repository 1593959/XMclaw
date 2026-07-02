FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy application code and install Python dependencies
COPY pyproject.toml ./
COPY README.md ./
COPY xmclaw/ ./xmclaw/
COPY docs/ ./docs/
RUN pip install --no-cache-dir -e .

# Create data directories
RUN mkdir -p /data/.xmclaw

# Expose port
EXPOSE 8766

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8766/api/v2/health || exit 1

# Run the daemon
CMD ["python", "-m", "xmclaw.daemon.app", "--host", "0.0.0.0", "--port", "8766"]
