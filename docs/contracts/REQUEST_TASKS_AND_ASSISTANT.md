# Request task view and request assistant — contract `ripple-request-task/1` (RUX-00 freeze, 2026-10-19)

Status: frozen for the first slices (RUX-01 task projection, RQA-01 visibility and context). Later slices extend this file; a change to a frozen field is a new version.
Nothing here grants a permission or changes a workflow: the task view is a read-only projection of the existing workflows, and the assistant is read-only.

## 1. Request families and locators
Two request families exist; their state machines stay separate and are never merged.

| Family | Locator `id` | What it is | Existing state source |
|---|---|---|---|
| `intent_flow` | `intent-…` (canonical once an intent exists) or `chat-…` (a change-request conversation before its intent) | free-text change request -> intent -> draft -> impact / simulation -> proposal -> review -> approval -> apply -> verification -> result decision (Changes page) | `v2_chat_conversations`, `v2_intents`, jobs `change.draft`, `proposal`, `review`, `approval`, `apply`, review notes, verification contracts (`infra/v2_work_routes.py::_chain/_stage`) |
| `business_change` | `chg-…` (the `change.understand` job id) | confirmed change of the accepted business knowledge -> draft -> knowledge decision -> commit -> file-write decision -> source apply (Questions page, change review) | `infra/adapters/v2_business_review.py::review_view` (decisions, `what_actor_can`, commit, source, completion) |

The server resolves tenant, workspace and the current actor from the session and the validated workspace; a client never sends actor, roles or grants. A `chat-…` locator
whose conversation has an intent resolves to that intent (`canonical_ref`); lineage comes from stored bindings (conversation.intent_id, proposal payload `intent_id`, the
newest proposal of an intent replaces older ones through `previous_proposal_job_id`), never from a client list of job ids.

## 2. Read rules (reused, not new)
| Family / part | Who may read | Source |
|---|---|---|
| `intent_flow` conversation before its intent | its creator, or a holder of `knowledge.confirm` | `v2_chat_routes._visible` (unchanged; the assistant does not widen it) |
| `intent_flow` intent and its jobs | any member with `project.read` | job and intent routes |
| `business_change` | the person who asked, holders of `knowledge.confirm` or `change.approve` | `v2_business_routes._change_record_for` |
Anyone else gets the generic "request not found" (404), the same answer as for a request that does not exist. The generic job read (`GET /v2/jobs/{id}`) must not reveal a
`business_change` record or a job bound to one (`change_id` in its payload) to someone who may not read that change (RQA-01 closes this gap; before it the job route returned
such records to every `project.read` member).

## 3. Task view (read-only) — `GET /v2/requests/{family}/{id}/task-view?workspace_id=`
```
{schema: "ripple-request-task/1", request_ref {family, id, canonical_ref?}, title, as_of, availability: available|partial|unavailable,
 stage {code, label, family_state}, obligations[], waiting_on[], blockers[], input_refs[], expected_deliverables[], permitted_actions[], stages[], completion_axes{}, coverage{}}
```
- `obligations[]`: `{code, label, required: bool, holder: you|role_pool|requester|system|unassigned|unknown, holder_label, eligible_me: bool, assigned_me: bool,
  state: current|waiting|blocked|future|done, reason?}`. `assigned_me` is true only where the workflow binds the obligation to this account (the requester of their own
  request); a permission alone gives `eligible_me`, never `assigned_me`. An administrator who is not a `change_approver` is not asked to decide (eligible, not obligated),
  the same rule as the inbox. A reviewer's review is `required: false` unless the workflow requires it. `label` names the step to do (e.g. "Ask for the AI draft of the
  change"), never the stage; `step` is the canonical step it belongs to (absent when it cannot be placed). The unblock path is the real one: after `request_changes` the
  requester holds `respond_to_changes` and the decision is `waiting` (the approver may still decide but is not asked); when the knowledge moved, `restate_request` (a new
  change request) is the only way forward and the dependent steps are `blocked` with nobody waited on; a committed change is not "moved" by its own commit.
- `waiting_on[]`: `{holder, holder_label, code, step?}`; the system is named as the system, never as "someone". No person's name, deadline or SLA is invented.
- `blockers[]`: `{code, text, source}` from the workflow (open "request changes" notes, semantic blockers, knowledge moved, failed jobs); never hidden behind a toggle.
- `permitted_actions[]`: `{action, label, allowed: bool, current: bool, reason?}` from the same predicates the action routes recheck; the task view never performs an action.
  `current` = allowed AND serving an obligation the reader is asked to do now; only current actions are offered as next steps (the assistant's `next_actions`, the page's
  next step). A permitted action is never a recommended obligation by itself. Labels are business words, not identifiers.
- `expected_deliverables[]`: `{code, label, later?}`: what the CURRENT step hands over (a decision is not a commit, a commit is not a file decision or a source sync);
  outcomes of later steps are marked `later: true` and shown apart.
- The route adds `features: {request_assistant: on|off}` (the page hides the launcher; the assistant routes refuse by themselves).
- `stages[]`: the family's canonical steps `{code, label, state: done|current|waiting|blocked|failed|future|not_applicable, parallel_group?}` in order; steps done by different
  people in either order share a `parallel_group` (business change: knowledge commit and file decision, `after_knowledge_decision`).
- Work list `GET /v2/work/inbox` (RUX-04) places every readable request of both families by this projection only: `sections` = `mine` (a current required obligation the
  reader is asked to do), `optional`, `blocked` (the reader's request that nobody can move on its current path), `waiting_human`, `waiting_system`, `recent`; each item
  carries `family`, `page`, `params`, `stage`, `step`, `blocked`, `blockers`; a request whose view fails is counted in `unavailable`, never shown as "no work". The legacy
  `todo` / `waiting` / `recent` lists keep their meaning.
- `completion_axes`: separate axes (knowledge, file writes, source apply, verification, result decision) as the family has them; never one combined badge.
- Unknown or failed reads are `unknown` / `availability: partial`, never "no work". The read writes nothing, runs no job and calls no model.

## 4. Assistant capability (RQA-01 freeze; routes in RQA-02)
- New permission `request.assistant.ask`, granted to readable human role bundles for this feature only; it admits only the job kind `request.qa` and nothing else; `ai.use`
  does not admit `request.qa` by itself. `ai_suspended` removes `request.assistant.ask` as it removes `ai.use`.
- Threads are private to their creator per tenant / workspace / request; no role inherits another person's threads.
- The context of an answer is built only from what the asking person may read through section 2 and the field catalogue of RQA-01, before retrieval and prompt.

## 5. Assistant routes, storage and answers (RQA-02 / RQA-03)
**Status: routes, storage, admission and worker implemented and verified locally with a stub model transport (no live model; answer quality not measured). The browser panel is not yet verified.**
| Route | Rule |
|---|---|
| `POST /v2/requests/{family}/{id}/assistant/threads {workspace_id}` | `request.assistant.ask` + the request readable; creates a private thread; **no model call** |
| `POST /v2/requests/{family}/{id}/assistant/ask {workspace_id, question, idempotency_key}` | EUI-02 first question: creates the private thread AND admits the question as one idempotent action. The thread id is derived from the caller, the request and the key, so a lost response, a retry or a concurrent click with the same key returns the same thread and message (`replayed: true`; at most one job and one reservation); another question under the same key: 409 `idempotency_conflict`; question and key are validated before any row is written; a failed admission leaves the empty thread and the same key retries once. Denied / suspended / switched off: the same refusals as below, nothing written |
| `GET /v2/requests/{family}/{id}/assistant/threads` | the caller's own threads of that request only |
| `GET /v2/request-assistant/threads/{thread_id}` | own thread and the request still readable, else the generic 404 (the whole thread is withheld) |
| `POST /v2/request-assistant/threads/{thread_id}/messages {workspace_id, question, idempotency_key, after_seq}` | explicit ask; idempotent per thread + key (another question under the same key: 409 `idempotency_conflict`); `after_seq` must be the last message number (another window asked first: 409 `sequence_conflict`); one admitted `request.qa` job |
| `GET /v2/request-assistant/threads/{thread_id}/messages/{message_id}/citations/{fact_ref}/source` | EUI-03: the source of a cited fact, resolved by the server from the pack the answer was built on and rechecked at read time. Requires: the asker's own thread, the same access, a fact this answer cites, an unchanged manifest (otherwise `stale_context`; nothing is regenerated), and `evidence.read` for any path or excerpt (otherwise `needs_evidence_rights`). Only impact and draft rule-change facts carry sources; any other fact gives `no_source_locator`, and the page's own section is shown instead. Each source has: path, locator, binding revision, state against the current sources (`current` / `changed_since` / `absent_now`), and for a YAML key the stored value at that key. No model call; no client-supplied path |
| `POST /v2/request-assistant/threads/{thread_id}/messages/{message_id}/cancel` | the asker stops a pending answer where the job has not started; a started one keeps its result and cost |
Storage: `v2_request_threads`, `v2_request_messages` (migration `ext_t10_request_assistant.sql`): question, context manifest hash and the asker's authorization fingerprint
at ask time, job id, and the validated answer once published. v1 retention: threads live with the workspace; no user delete, no sharing, no export (owner may revise
before release). `request.qa` jobs are not readable, approvable, retriable or cancellable through the generic job routes, and cannot be created through `POST /v2/jobs`.

Worker (`request.qa`, pinned role `request.assistant`, `config/request_assistant.json`): immediately before the provider call it rechecks that the asker still holds
`request.assistant.ask` and may read the request, and that the context rebuilt now under the asker's rights has the same manifest hash (`context_changed` otherwise: no call,
the person asks again). Answer `{status: answered|partial|insufficient_context|out_of_scope|refused, blocks[{text, fact_refs, kind: fact|interpretation|suggestion}],
rejected[{index, kind, reason}], limitations[], next_actions[], as_of, manifest_hash}`; a block is kept only if every cited fact was offered, every number in it is in a
cited fact, a suggestion rests on an action / obligation / blocker fact and does not contradict a cited "not allowed for you", it does not claim the assistant acted, and the
text is plain; `next_actions` are the reader's allowed actions from the task view, computed by the server. An answer is shown only under the same authorization fingerprint
it was asked with (otherwise `withheld` until asked again) and is marked historical when the request's context moved since.

## 6. Answer integrity v2 (AIG-01 typed authority, AIG-02 bounded priority context)
**Status: contract frozen 2026-10-20 before implementation; English is the only supported answer locale.**

**Answer schema `request-assistant-answer/2`.** The model returns `outcome`, `answer_blocks` (free explanation: `fact | interpretation | suggestion`), `limitations` and
`authority_assertions[]` (at most 4): `{subject: self | holder, fact_ref, relation: holds | must | may | waiting_on | blocked, polarity: true | false, scope: current | later}`.
`fact_ref` names one offered fact of kind obligation, action, waiting or blocker; `subject: holder` means the holder that fact names (a role pool, the requester or the
system), never a person's name. There are no free role, account, action or URL strings.
- An assertion is kept only when it is true of the cited fact's typed values: `holds` = the obligation is the reader's (`self`) or the named holder's, in the stated scope
  (current / later); `must` = the reader's AND required; `may` = eligible (obligation) or allowed (action); `waiting_on` = a waiting fact's holder; `blocked` = the cited
  obligation is blocked or a blocker fact is cited. `polarity: false` asserts the negation and is checked the same way. Permitted is not current duty, eligible is not
  assigned, later is not now, optional is not required.
- The decisive authority sentence is RENDERED by the server from a kept assertion and the canonical fact (controlled English phrases), shown as a block of kind
  `authority` with that fact cited. The model cannot replace it with its own wording.
- Free blocks carry the model's explanation (why a request is blocked, which decisions and rules led here, what trade-off remains), with citations. A free block that
  itself claims who holds, must, may or is waited on, or that opens with a bare "Yes"/"No", is refused (`authority_in_free_text`, `unbound_affirmation`): authority goes
  through assertions only. The earlier lexical checks stay as defence in depth; they are not proof of correctness.
- `status`: `refused` when nothing survives, `partial` when something was refused. A refused or empty answer to an answerable question is not a useful answer.
- Result identity: `answer_schema`, `checks_version` (`request-assistant-checks/4`), `locale` (`en`), `context_version`, `config_sha256`. Stored answers are never edited.
  An answer stored under an older schema is shown with a note that it was checked by an earlier version, and its free blocks that make authority claims are withheld on
  read (no model call, no rewrite). A question queued before an upgrade is answered under the configuration current when it runs and records that identity.

**Context `request-assistant-context/2`.** After the read rules, every fact carries a `tier` and a stable `key` (its source identity: obligation code, decision id, rule
title, note id). Mandatory tier: request identity, stage, the reader's obligations / eligibility, waiting, blockers, allowed and denied actions, progress, completion axes.
Support tier: the confirmed request, rule changes, impact, current decisions. History tier: older decisions and reviewer notes.
- All mandatory facts are kept and must be complete: if they do not fit the fact count or one of them would be cut, the ask is refused with `409 context_overflow`
  (worker: `context_overflow` before any provider call). Nothing is dropped silently and no limit is raised.
- Support, then history facts fill the remaining room in a stable order (tier, kind, key), never by database enumeration or read time; optional facts that would be cut
  carry `truncated`. Structured values are never cut mid-object.
- `coverage` reports offered / selected / omitted per tier and upstream caps (for example the impact list limit), only from what the reader may read.
- Local ids `F1..Fn` are assigned on copies after ordering and belong to that stored pack; the manifest hash binds facts (with tier and key), coverage, context version and
  authorization, so two reads of the same frozen state give the same pack and a real change (a new blocker or decision, lost access) gives `context_changed`.
