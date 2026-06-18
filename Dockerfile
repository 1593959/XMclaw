FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[all]"

# Copy application code
COPY xmclaw/ ./xmclaw/
COPY docs/ ./docs/

# Create data directories
RUN mkdir -p /data/.xmclaw

# Expose port
EXPOSE 8766

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8766/api/v2/health || exit 1

# Run the daemon
CMD ["python", "-m", "xmclaw.daemon.app", "--host", "0.0.0.0", "--port", "8766"]
