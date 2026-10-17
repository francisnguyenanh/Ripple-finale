"""FB-H1: validate a Firebase Hosting configuration for the Ripple frontend BEFORE anyone deploys it (pure file check; no Firebase call, no deploy).

Rules (from the hybrid contract, docs/contracts/EXTENSION_FIREBASE_HYBRID.md section 6):
  1. no rewrite may route `/v2/**` (the API) to a backend unless `ripple.allow_api_rewrite` is true AND `ripple.session_strategy` is decided (not `undecided`):
     Hosting forwards only the `__session` cookie, so the product's `ripple_v2` session would be dropped silently;
  2. the SPA fallback must exclude the API paths (`!/v2/**` or an explicit list), so API errors are never swallowed into index.html;
  3. `index.html` must be `no-store`; hashed assets may be immutable; no header may make `/v2/**` publicly cacheable;
  4. `public` must be a build output directory, never the repository root or a directory holding secrets/config;
  5. no `.env`, key or credential file may be among the served files (ignore list must cover dotfiles).
Exit code 0 = no finding; 1 = findings (printed one per line as `code<TAB>detail`).
"""
from __future__ import annotations

import json
import pathlib
import sys

API_PREFIXES = ("/v2/", "/v2/**", "/api", "/api/**")
PUBLIC_FORBIDDEN = {".", "", "/", "config", "infra", "core", "tests", "developing"}


def validate(cfg: dict) -> list:
    findings = []
    hosting = cfg.get("hosting") if isinstance(cfg, dict) else None
    if not isinstance(hosting, dict):
        return [("no_hosting_block", "the file has no hosting object")]
    ripple = cfg.get("ripple") or {}
    public = str(hosting.get("public", ""))
    if public.strip("/") in PUBLIC_FORBIDDEN or public.startswith(".."):
        findings.append(("public_dir_unsafe", f"public={public!r} must be a build output directory"))
    ignore = hosting.get("ignore") or []
    if not any(str(i).startswith("**/.") or i == "**/.*" for i in ignore):
        findings.append(("dotfiles_not_ignored", "ignore must cover dotfiles (**/.*)"))
    spa_ok = False
    for rw in hosting.get("rewrites") or []:
        src = str(rw.get("source", ""))
        dest = rw.get("destination")
        if rw.get("run") or rw.get("function") or (isinstance(dest, str) and dest.startswith("http")):
            if any(src.startswith(p) or src.rstrip("*").rstrip("/") in ("/v2", "/api") for p in API_PREFIXES) or src in ("**", "/**"):
                if not (ripple.get("allow_api_rewrite") is True and ripple.get("session_strategy") not in (None, "", "undecided")):
                    findings.append(("api_rewrite_blocked", f"rewrite {src!r} routes the API through Hosting; only the __session cookie is forwarded, the ripple_v2 session would be dropped: decide ripple.session_strategy first"))
        if dest == "/index.html":
            if src in ("**", "/**"):
                findings.append(("spa_fallback_swallows_api", "the SPA fallback must exclude /v2/** (use source \"!/v2/**\")"))
            else:
                spa_ok = True
    if not spa_ok and hosting.get("rewrites"):
        findings.append(("spa_fallback_missing", "no SPA fallback rewrite that excludes the API paths"))
    index_no_store = False
    for h in hosting.get("headers") or []:
        src = str(h.get("source", ""))
        values = {str(x.get("key", "")).lower(): str(x.get("value", "")) for x in h.get("headers") or []}
        cc = values.get("cache-control", "")
        if src in ("/index.html", "index.html") and "no-store" in cc:
            index_no_store = True
        if (src.startswith("/v2") or src.startswith("/api")) and ("public" in cc or "max-age" in cc) and "no-store" not in cc:
            findings.append(("api_publicly_cacheable", f"headers for {src!r} allow caching of private API responses"))
    if not index_no_store:
        findings.append(("index_not_no_store", "index.html must be served with Cache-Control: no-store"))
    return findings


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m tools.firebase.validate_hosting <firebase.json>")
        return 2
    cfg = json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
    findings = validate(cfg)
    for code, detail in findings:
        print(f"{code}\t{detail}")
    print(f"findings={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
