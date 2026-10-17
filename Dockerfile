FROM python:3.11-slim

WORKDIR /app

# Pinned runtime dependencies (P10-T04): the same file a fresh clone installs.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose standard Cloud Run port
EXPOSE 8080

# One service: infra.judge serves the UI and runs /rpc in-process.
# ONE worker, stated explicitly: without --workers uvicorn reads $WEB_CONCURRENCY,
# and several processes per instance would each hold their own session cache
# (see docs/verification/cloud_readiness.md).
CMD ["uvicorn", "infra.judge:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
