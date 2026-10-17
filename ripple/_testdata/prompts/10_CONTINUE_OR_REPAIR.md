# Utility prompt — continue one complete batch

Use with the system instruction, current stage prompt, accepted contracts and the relevant existing files. Replace every bracketed field before sending; omitted context must trigger NEEDS_INPUT.

---

Continue Harbor Procurement Mini generation.

STAGE: [stage identifier]
BATCH_ID: [next batch identifier]
CONTRACT_VERSION: [accepted contracts.json version]
FROZEN_SPEC_VERSION: v1 [or explicit approved amendment]
REQUESTED_FILES: [exact relative paths, maximum 2–4 small files]
ALREADY_ACCEPTED_FILES: [paths and contents needed for this batch, or attached archive]
ACTUAL_VALIDATION_OUTPUT: [real log/error, or NOT_RUN]
MODE: [generate / repair-approved-findings]
APPROVED_REPAIR_FINDINGS: [IDs or NONE]

Generate only the requested complete files in standard JSON file-bundle format. Do not regenerate previous accepted files or invent missing interfaces. Preserve all intentional baseline differences and private/public separation. If a previous response was truncated, re-emit the complete affected file in a smaller batch, not a continuation starting halfway through a string/function. Do not claim commands ran without execution evidence.

If context is too large, return NEEDS_INPUT with the smallest exact file/signature subset required. If still more work remains, next_batch must name the remaining batch and no partial file may be output. Checks must distinguish code inspection from actual runtime tests.
