# Third-party notices

Generated at packaging time from `requirements.txt` and the installed distribution metadata; full licence texts ship inside each distribution. Frontend dependencies: `frontend/THIRD_PARTY_NOTICES.md`.

## Python runtime dependencies
- fastapi==0.138.0 - licence (from installed metadata, version 0.138.0): MIT
- starlette==1.3.1 - licence (from installed metadata, version 1.3.1): BSD-3-Clause
- uvicorn==0.49.0 - licence (from installed metadata, version 0.49.0): BSD-3-Clause
- requests==2.33.1 - licence (from installed metadata, version 2.33.1): Apache-2.0
- google-auth==2.49.2 - licence (from installed metadata, version 2.49.2): Apache 2.0
- google-cloud-storage==2.19.0 - licence (from installed metadata, version 2.19.0): Apache 2.0
- psycopg2-binary==2.9.12 - licence (from installed metadata, version 2.9.12): LGPL with exceptions
- PyYAML==6.0.3 - licence (from installed metadata, version 6.0.3): MIT
- openpyxl==3.1.5 - licence (from installed metadata, version 3.1.5): MIT
- google-genai==2.9.0 - licence (from installed metadata, version 2.9.0): Apache-2.0
- jsonschema==4.26.0 - licence (from installed metadata, version 4.26.0): MIT

## Vendored files
- None: no third-party file is vendored in this package (the Mermaid library used by the earlier HTML-report renderer was removed together with that renderer).

## Container base images (Dockerfile, site/Dockerfile)
- Python base image and `nginxinc/nginx-unprivileged` (NGINX, BSD-2-Clause): see the image documentation for licence notices.
