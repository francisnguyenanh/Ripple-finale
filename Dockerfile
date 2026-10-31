FROM python:3.11-slim

WORKDIR /app

# Pinned runtime dependencies (P10-T04): the same file a fresh clone installs.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# OPS-01 (2026-10-30): the processes run as an unprivileged user. The application tree is read-only for it (nothing under /app is written at
# run time); the directories the worker writes are its home (git configuration of the remote sync) and the system temp directory (code and
# sandbox check-outs, staged uploads). No credential or configuration is baked in: secrets and the budget file are mounted or passed at run time.
RUN useradd --create-home --home-dir /srv/ripple --uid 10001 --shell /usr/sbin/nologin appuser
ENV HOME=/srv/ripple
USER 10001

# Expose standard Cloud Run port
EXPOSE 8080

# One service: infra.judge serves the UI and runs /rpc in-process.
# ONE worker, stated explicitly: without --workers uvicorn reads $WEB_CONCURRENCY,
# and several processes per instance would each hold their own session cache
# (see docs/verification/cloud_readiness.md).
CMD ["uvicorn", "infra.judge:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
