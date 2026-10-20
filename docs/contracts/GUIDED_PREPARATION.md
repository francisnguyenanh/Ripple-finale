# Guided proposal preparation (EUI-08) — contract

Status: implemented locally, **off by default** (`RIPPLE_GUIDED_PREPARATION=on` enables it). It is verified with deterministic jobs and synthetic fixtures only. It has had no live run, and no usability or latency measurement exists.

## What it is
This is a finite, prepare-only run over a **confirmed** intent_flow request that already has a **completed** draft. It runs three stages, all deterministic (0 paid model calls):
1. impact
2. Harbor sample simulation, built from the draft
3. proposal (yaml typed edit from the draft)

Asking for the draft remains a separate, paid step that the person starts themselves.

## Manifest (read-only)
`GET /v2/intents/{id}/preparation/manifest?workspace_id=&target=&accepted=` is read under the request's read rule and needs `change.prepare`. It creates no job and makes no call. It returns:
- **pins:** draft job, source revision, decision epoch;
- **stages** and their paid calls (0);
- **selected inputs:**
  - the target rule: proposed by the server only when the draft names exactly one; otherwise the person chooses;
  - the impact inputs computed from that target, the same way the manual page computes them;
- **the draft's proposed edits**, with **none preselected**;
- **unknowns** (for example, no relation coverage means some rules stay UNKNOWN), and **choices still needed**;
- **manual points after the run:** verification contract, review, approval, apply;
- **the statement that the run changes no file.**

`manifest_hash` covers all of the above.

## Start and run
`POST /v2/intents/{id}/preparation {workspace_id, manifest_hash, target_candidate_id, accepted_refs}`:
- Needs the person's explicit choices: at least one accepted edit, all from one file.
- Needs the hash of the manifest they saw. If what would run has changed, the server answers 409 `manifest_changed`.
- A repeated start with the same choices returns the same run.

Every child is an ordinary job created **as the starter**, through the normal job-creation contract. That contract applies the same permissions, payload checks, epoch and lineage guards, quota and audit as `POST /v2/jobs`.

The run gives **no job approval**; these executors do not need one. If one ever does, the run pauses with `needs_job_approval`. The workspace's distinct-approver policy is untouched.

Admission follows the actual workspace policy. In a sandbox workspace without the writing session, the start is refused (`sandbox_lease_required`). A person who loses the right to prepare a change cannot take the next step. A workspace that requires distinct approvers is unaffected, because the run approves nothing.

`POST /v2/preparation/runs/{id}/advance` makes one idempotent step and only the starter may call it:
- Before each stage, the pins and that stage's inputs are recomputed. If any moved, the run pauses with `inputs_changed`; a new manifest and a new start are required, and the old start is not reused.
- A failed child ends the run with its reason.

`POST .../cancel` stops further steps. Results of jobs that already finished stay.

## Never done by a run
- accepting knowledge
- authoring an independent check
- a review verdict
- an approval, commit, apply, publish or deploy
- choosing edits for the person
- treating a proposed target as the person's choice before they start
