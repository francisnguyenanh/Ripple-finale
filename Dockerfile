# OPS-01 supply chain (2026-10-31): the base image is pinned by digest (python:3.11-slim as pulled on this host for the C19 evidence image), so two
# builds of the same export start from the same bytes. Update the digest deliberately, with a new candidate; never by tag drift.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

WORKDIR /app

# Runtime dependency (2026-10-31): git is an executable the product calls at run time (business CI drift check over a declared repository,
# Git remote sync, code sandbox check-outs). It is installed from the pinned base image's Debian repository; nothing else from apt.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# OS package fixes (2026-10-31, Trivy 0.76.1 on the C23 digest): only the packages with a published Debian fix and a HIGH finding are raised to their fixed
# versions (TLS, libc and SQLite libraries the Python runtime loads, CA certificates, xz, gzip, pcre2); nothing else is upgraded. Findings without a fix are recorded in the scan record,
# not hidden. Re-run the scan on every new candidate digest; update this list deliberately.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade libssl3t64 openssl openssl-provider-legacy libsqlite3-0 libc6 libc-bin ca-certificates liblzma5 gzip libpcre2-8-0 && rm -rf /var/lib/apt/lists/*

# Pinned runtime dependencies (P10-T04) installed hash-locked (OPS-01, 2026-10-31): requirements.txt is the human-edited list of direct pins;
# requirements.lock.txt is generated from it for this image platform (linux, Python 3.11) with every transitive package pinned and hashed
# (`pip-compile --generate-hashes`, run inside this same base image: see docs/runbooks/FINAL_DEPLOY_ROLLBACK.md, "Dependencies"). pip refuses any
# package whose bytes do not match. Update procedure: edit requirements.txt, regenerate the lock in the base image, build, run the image suites.
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt
# The installer packages the base image ships (pip, setuptools, wheel, jaraco.context) are not used by the product at run time and carried the only
# language-package findings of the 2026-10-31 scan (pip 26 vendors flagged copies of setuptools / urllib3 / msgpack; fixed setuptools versions do not
# import cleanly on this base): all of them are removed after the hash-locked install. The image cannot install anything afterwards, which is
# the intent (every dependency is in the hash lock); the development image-test runner bootstraps its own pip with `ensurepip --user`.
RUN python -m pip uninstall -y jaraco.context setuptools wheel && python -m pip uninstall -y pip

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
