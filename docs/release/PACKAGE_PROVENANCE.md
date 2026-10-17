# Package provenance

This repository is a filtered export of the development repository, followed by a few reviewed changes made here. This page records what the export contains, how it differs from the development repository and the serving image, and what was verified. It replaces the earlier per-pass packaging notes; the integrity list of every file of the Pitch package was its root `MANIFEST.json`; an export of this repository instead carries `SUBMISSION_MANIFEST.json` at the export root (source commit, file hashes, packaging decisions), and the Pitch-era text below describes the Pitch package, not the Final one.

## How the package was made

| Step | Commit here | What |
|---|---|---|
| Export | `5df90a1` | tracked files of development commit `8d55232` selected by rule; every addition and transformation was logged in a build inventory that is kept outside this repository |
| Trim | `afcac4c` | 35 files not needed to run or verify the system (superseded plans, process notes, a client-specific profile) |
| Scope pruning | `0addada`, `16cdbc9` | the earlier legacy-source analysis pipeline, its tests and fixtures were removed (see "What the package does not contain") |
| Adapters, re-analysis | `687d03c` | legacy adapters and the adapter registry removed; an exhausted AI re-analysis became a terminal, explained failure |
| Worker seconds | `20ec7b9` | worker-seconds reservation ownership |
| Documentation | `a477c54`, `4aaa8bb` and later | README, judge guide, and the clean-up recorded in "Documentation and comment clean-up" |

Transformations applied by the export: one deployment/operator identifier replaced by `<private-identifier>` in two files; links to excluded files rewritten as text; `.gitattributes` rules that keep the Harbor sample bytes identical on Windows and POSIX checkouts (the three `.xlsx` workbooks are never converted). Operator cloud tooling with private identifiers, agent configuration and deployment files were not exported. The `LICENSE` file (proprietary, Competition Evaluation Only) was added after the export; it is not part of the exported development tree.

## What the package does not contain

- Parsers and adapters for source types outside the supported list (`.md`, `.yml`/`.yaml`, `.py`, `.xlsx`, `.csv`, `.jsonl`; see [`../product/ai_closed_loop_support.md`](../product/ai_closed_loop_support.md)). Files of any other type are refused per file and never read. A test (`tests/pitch_brief/test_source_scope.py`) pins this.
- The earlier analysis pipeline (command-line stages, project profiles, report renderer, scheduler) and the tests that only exercised it. Test counts therefore fell from 5,297 collected at the first export to about 3,500; a smaller count is not a quality claim, because the removed tests belonged to capabilities this package no longer ships.
- The development plans, task records, hand-offs and the cloud operator scripts.

Production modules were removed in these steps (187 modules under `core/` plus 64 adapter-related files). Nothing a kept file reads was removed (checked by import closure, dynamic imports, path references, build configuration, tests and links); this is evidence, not proof, that the behaviour of the supported path is unchanged.

## Differences from the development repository and the serving image

Two runtime changes exist in this repository and, since 2026-10-14, in the serving image, but not in the development repository:

1. **Terminal re-analysis.** An AI re-analysis that used every approved provider attempt is a terminal, explained failure: `POST /v2/jobs/{id}/retry` answers `409 provider_attempts_exhausted` before anything is reserved or booked; the change read model carries `reanalysis.terminal`; the applied source and the verification result are kept; the change cannot be completed or accepted; Request changes or Reject remain open to the right role. Evidence: `tests/ai8t03/test_rebuild_and_recovery.py` and the browser test `tests/pitch_brief/test_ui_reanalysis_terminal.py` (stub provider; no live failure was provoked). Files: `infra/adapters/v2_postgres.py`, `infra/adapters/v2_change.py`, `infra/v2_policy_routes.py`, `infra/worker/job_quota.py`, `frontend/src/features/changes/` and `frontend/src/features/sources/SourcesPage.tsx`.
2. **Worker-seconds reservation ownership** (`infra/worker/wake.py`, `infra/worker/job_worker.py`, additive table `v2_worker_reservations`). Before this change, a manually started worker execution that never made a reservation still subtracted the reserved 480 s at exit, which reset the counter `v2_worker_wake.worker_seconds` from 61 to 0 once (about 0.08 percent of the 80,000 s cap; no provider money involved; the 61 s were not restored). Now a wake-up kick writes one reservation row, tied to the Cloud Run execution it started; a worker settles only the row whose `execution_name` equals the platform-set `CLOUD_RUN_EXECUTION`, in one transaction; an execution started by hand neither takes nor releases anyone else's seconds, and its runtime is not counted; a lost execution keeps its reservation counted. Evidence: `tests/pitch_brief/test_worker_wake.py`.

## Test results by snapshot

No full suite, provider call or serving database was used for these records; results belong to their snapshot and are not added together.

| Snapshot | What was run | Result |
|---|---|---|
| `687d03c`, clean clone | database-backed groups; non-database groups; 11 browser tests; image build; frontend rebuild | 415 passed / 2 skipped; 2,230 passed / 3 skipped; 11 passed; image built and started (`/`, `/app-next/` answer 200); `npm ci && npm run build` reproduced the shipped bundle byte for byte; manifest and 1,079 file hashes matched |
| `20ec7b9`, clean clone, Python 3.11.9, 2026-10-14 | `tests/contract` + `tests/unit` | 1,166 passed, 3 skipped, 1 deselected |
| same | the three database-smoke files named in the README, empty PostgreSQL 16 | 32 passed |
| same | collection | 3,539 tests selected, 1 deselected (live-model marker) |

Skipped and deselected tests are the live-model tests (opt-in), tests needing a database or browser that was not provided, and tests that read operator-only files that are not shipped.

## Judge guide

`docs/judges/` is the Markdown source of the static guide in `site/judges/` (`python -m tools.judge_site.build`). The pages in `site/judges/` differ from the deployed guide in one line of `four-role-case.html`: the "13 of 13 files" statement now says that the answering model **declares** its examined scope and the server checks that declared scope; it is not proof that every file was understood.

## Documentation and comment clean-up

Recorded in the commit that adds this section; see that commit's message and the refreshed `MANIFEST.json` (`file_count`, `overall_sha256`). The clean-up removed deck/video/description drafts, reviewer dialogue and per-pass packaging history from `docs/`, rewrote development-process headers in the remaining documents, and removed development-coordination notes from code comments and docstrings. Only comments and docstrings changed in Python files: the syntax trees of every changed file are identical once docstrings are ignored. Files whose bytes are pinned by a hash were deliberately left unchanged: the reference-system seed under `reference_system/` that feeds the committed baseline id, the frozen evaluation configuration `config/ai10_*.json` (their sha256 is stamped into evaluation results), the Harbor fixture with its integrity manifest, and the frozen contract documents `docs/contracts/P6_FROZEN.md` to `P9_FROZEN.md`, which contract tests read. Migration file names under `infra/migrations/` and test directory names still carry the labels of the work items they were created for, because code and tests address them by name. Docstrings of web route handlers were not edited, because they are served as API descriptions.

## Git history of this repository

This repository was created on 2026-10-15. Its first commit holds the tree of an earlier private submission repository, which is kept as a private archive; the files and their bytes were not changed when the first commit was made. The date of the first commit is the date the repository was created. It does not date the code, which was developed in the development repository (`source_commit` in `MANIFEST.json`, development commit `8d55232`) and exported as described above.

The commit ids named in this page (`5df90a1`, `afcac4c`, `0addada`, `16cdbc9`, `687d03c`, `e2ded41`, `20ec7b9`, `a477c54`, `4aaa8bb`, `357e026`) and in the `build` records of `MANIFEST.json` belong to that archive. They are kept as a record of how the package was made and of which snapshot each test result was obtained on; they do not exist in this repository.
