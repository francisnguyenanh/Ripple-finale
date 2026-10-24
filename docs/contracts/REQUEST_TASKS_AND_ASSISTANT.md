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
- `stages[].phase` / `phase_label` (2026-10-21): where the step stands, derived from what the record holds for it, never from the word `current`:
  - `running` = a system wait or an unfinished job on the step ("in progress");
  - `not_started` = the current obligation starts the step's work (ask for the draft, run the analysis / review, prepare the approval, apply, state a new request);
  - `awaiting_decision` = the current obligation is a decision; `awaiting_action` = another current obligation (confirm, answer, respond, commit);
  - `awaiting_other_step` = the step waits until another step is done; `blocked`, `failed` (failed or stopped), `done`, `future`, `not_applicable` follow the state;
  - `unknown` = a current step with no recorded wait, obligation or running work, shown as such.
  The task frame shows this phase (a spinning "in progress" badge only for `running`); the request assistant's context states the same phases.
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
| `GET /v2/request-assistant/threads/{thread_id}/messages/{message_id}/citations/{fact_ref}/source` | EUI-03: the source of a cited fact, resolved by the server from the pack the answer was built on and rechecked at read time. Requires: the asker's own thread, the same access, a fact this answer cites, an unchanged manifest (otherwise `stale_context`; nothing is regenerated), and `evidence.read` for any path or excerpt (otherwise `needs_evidence_rights`). Only impact and draft rule-change facts carry sources; any other fact gives `no_source_locator`, and the page's own section is shown instead. Each source has: path, locator, binding revision, state against the current sources (`current` / `changed_since` / `absent_now`), and for a YAML key the stored value at that key, read through the source blob seam (no excerpt when no blob store is configured; never a guess). No model call; no client-supplied path |
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

### 6.x Answer integrity v3 (AIG-03 v5: `request-assistant-answer/3`, `request-assistant-checks/5`, config version 3)
- **`cause` block:** a new block kind that says WHY the request is where it is, written about the request (never about who may, must or can act).
  - It must cite a fact with business substance (blocker, decision, rule change, impact, request, waiting, review note); otherwise it is refused as `cause_without_cause_fact`.
  - Authority in its prose is refused like any free block (`authority_in_free_text`). The whole block is refused; nothing is cut out of it.
- **Blocker coverage:** computed by the server for every answer, from the offered pack and for any request, with no expected ids: `blocker_coverage {offered, explained, unexplained}`.
  - An offered blocker that no kept explanation cites is disclosed as a limitation, "Not explained in this answer: …", using the blocker's own text. Nothing is made up for it, and the status is unchanged.
  - With no blocker offered, nothing is added.
- **Compatibility:** answers stored under `request-assistant-checks/4` are shown as stored (their authority is already typed and server-rendered). Older ones keep the legacy view. Nothing is regenerated.
- **Quality:** the prompt asks for every offered blocker to be explained or named in the limitations. Model compliance is measured only by a live cohort under its own approved allocation; the R1 / Q1 runs were made on answer/2 and stay as recorded.

### 6.y Corrected authority (`request-assistant-checks/6`; answer schema, prompt and config unchanged)
- A typed assertion the cited fact contradicts stays refused (`assertion_contradicts_fact`, counted in `rejected`). When the same fact decides the right answer to the question the model raised, the server shows that sentence instead, as an `authority` block with `corrected: true`. The UI marks it "corrected: the assistant's claim did not match the task record".
- What the fact decides:
  - a step held by someone else that the model gave to the reader (holds / must) → "This step is not yours" (true whatever the step's stage);
  - a claim about the step's own stage → its opposite;
  - a current claim about a blocked step → "This step is blocked";
  - a current claim about a later step → the same claim for later.
  Every candidate is re-checked against the fact before it is shown. Nothing is corrected from a finished step, an unsupported combination or an unknown fact. A sentence already shown is not repeated.
- Authority stays deterministic: the model only chose what was asked about. A corrected sentence is not a model explanation (`semantic_success` and blocker coverage ignore authority blocks). Evaluations count corrected sentences apart from the model's own correct assertions.
- **Compatibility:** answers stored under checks/4 and checks/5 are shown as stored.
- **Source of a correction:** the pack the worker rebuilds at answer time, under the asker's CURRENT roles, for this request only. If that pack differs from the pack of the question (request moved, access changed), the job refuses with `context_changed` and nothing is corrected. A revoked asker refuses with `authority_revoked`. The correction never reads another person's record, and a fact not in the pack (`assertion_unknown_fact`) gets no correction.
- **Business causes are untouched:** a correction is only an authority sentence. Cause, fact and interpretation blocks keep all their checks (numbers in cited facts, cause facts, no free-text authority). A wrong cause is refused even when an authority correction is shown. A correction is not a business explanation: `semantic_success` and blocker coverage ignore it, and an unexplained blocker is still disclosed.
- **Three records:**
  1. the raw model object, `model_answer`, stored for evaluation and never returned by a route;
  2. the validated answer: kept blocks and `rejected`, where each contradicted assertion carries `corrected: true|false`;
  3. the server corrections: `authority` blocks with `corrected: true`, `origin: "server_correction"` and `corrects: <index of the refused assertion>`.
- **`authority_accounting`:** model_assertions, model_kept, model_contradicted, model_unsupported, model_unknown_fact, server_corrections, shown_authority and shown_from_model. Evaluations score the model from the `model_*` counts only: a contradicted assertion is a model error even when the reader sees the corrected sentence. What the reader sees is scored separately (`aig-03-scorer/2`, developing tool).
- **Q2:** collected under checks/5 and scored by `aig-03-q2-scorer/1`. It stays as recorded and says nothing about checks/6 live behaviour.

### 6.z Server-stated authority (`request-assistant-answer/5`, `request-assistant-checks/14`, config version 10, `request-assistant-context/3`; supersedes the model-chosen assertions of 6.y for new answers)
- **Config version 10 (2026-10-23):** the prompt tells the model that request and draft facts describe what is asked and must be written as proposals; checks/14 enforces it.
- **checks/14 (2026-10-23):** a block whose cited facts are all proposal facts (`request`, or a `rule_change` from the draft) must say it is proposed / requested ("proposes", "requested", "should", ...); otherwise it is refused as `proposal_stated_as_in_effect` (what is asked is never shown as the rule in effect). A block that also cites the current rule, the state or a decision is not affected.
- **Responsibility split:**
  - Who holds each step, what the reader may do and who the request is waiting on comes only from the reader's task record, filtered by the reader's rights and bound to the pack version.
  - The model does not state it and is not the source for it.
  - The model's part is the why: it understands the question, explains causes and blockers from the cited facts, links evidence, distinguishes same-stage / same-holder requests by their own causes, says what is unknown, and refuses honestly when the question is out of scope.
- **Authority section** (`blocks` with `kind: authority`, `origin: task_record`), rendered by the server from the pack:
  - every current or blocked step with the reader's relation:
    - "You hold the current step: X" (`(optional)` when not required) only when the record assigns it to the reader;
    - "This step is not assigned to you: X. It is open to G; you may act on it, but you are not asked to / you cannot act on it now" for a pool step;
    - "This step is not yours: X (held by N)" for a named holder;
    - "This step is blocked: X";
  - each wait ("The request is waiting on N");
  - each action the reader may do that is not their current step;
  - "No step on this request is open right now" when the record has neither steps nor waits.
  - At most 6 sentences. Assignment comes only from the record (`obligation_me` / `assigned`), never from a role or from eligibility.
  - The same section is given to the model in the prompt as data; it must not restate or contradict it.
- **Requests to act (config version 7):** the assistant only reads and explains. When the question asks it to perform an action, the model is instructed to say in `limitations` that the assistant does not perform that action, then it may explain or point to the valid step from the facts. An instruction to ignore or change its instructions is not followed; any legitimate part of the question is still answered. A question that only asks how something is done is answered, not declined. This is model behaviour (evaluated live, not guaranteed by a check); no path to perform an action exists either way.
- **Model output:** exactly `{outcome, answer_blocks[], limitations[]}`. The provider schema has no authority field. An object that still carries one is a `schema_violation`: the whole answer is unavailable, never re-read.
- **Kept / refused:**
  - Every explanation block keeps all its checks: offered facts only, numbers in cited facts, cause blocks need a cause fact, and no who-holds / may / must / waiting prose. Prose authority is refused and counted (`authority_accounting.model_prose_authority_refused`, including a third-person holder claim not grounded in a cited step).
  - **Causes (checks/10, config version 8):** a `cause` block must cite at least one fact that records a reason: a blocker, decision, open question, rule change, impact or review note (`CAUSE_FACTS`). The stage, steps, waits, obligations, actions and the request itself say what is asked and where it stands; they belong in `fact` blocks. The prompt lists exactly this pack's reason facts (`cause_basis`), so the model and the validator apply one rule. A cause without one is refused, never relabelled. The server then adds "An explanation of why was not shown: …", saying whether the pack records no reason or the block did not cite it. Whether a grounded cause actually answers the question is not decided by a check; it is part of the usefulness evaluation.
  - **Authority prose (checks/9–10):** naming a role is not an authority claim by itself.
    - A third-person claim needs the role as the subject of a holding, obligation or permission predicate in the same clause ("the approver must / can / cannot decide", also after an elliptical ", and must …"). A new clause with its own subject (", and the reason given is to …") is a business description.
    - Claims about THIS request by anyone, whatever the title or name, are caught by structure, not by a list of titles: a decision act with the request or one of its steps as object ("the Finance Controller has to approve it", "someone in finance has to sign this off", "Hanako will approve this request"); a passive with an agent ("it must be decided by …"); "the decision lies with …"; "… is the approver for this request"; "it is X's call"; "waiting on …"; and (checks/11) a statement that who acts is not stated or unknown ("the facts do not state who holds …"), since the server section states it.
    - A business rule about a class of cases ("purchases from high-risk suppliers go to the Finance Controller") is kept. The assistant as subject ("the assistant cannot approve this request") is a decline and is kept.
    - **Protection level:** this is a lexical defence in depth, not a parser. A claim with none of these structures (for example "Hanako decides next") is not caught. The authority the reader can rely on is the server section. Residual model prose is measured in the live evaluation by a manual review of every shown sentence (scorer `aig-03-scorer/8`: critical wrong to the reader = audit + manual flags).
  - **Stage reason and status (context/3, checks/12; D1 / D4):**
    - The step fact states each step's recorded phase. Without a recorded blocker, a `stage_reason` fact (a reason fact a cause may cite) says what the record shows for the current step(s), e.g. "AI draft: current step, not started yet"; parallel steps are listed together. It never says that nothing blocks the request.
    - With a recorded blocker, the blocker is the reason and no stage reason is added. A current step whose phase the record does not show, or an unfinished request without a current step, gives a `reason_gap` fact (not a reason): the cause is not determined, and the model is told to say so.
    - A status the model states must agree with the recorded phases: "underway / in progress / being written" needs a running step; "stopped / halted / stalled" needs a blocker or a blocked / failed step; negated phrases are not claims. Otherwise the block or limitation is refused (`status_contradicts_record`) and the live audit treats a shown one as a problem.
  - **Who acts (config version 9; D2):** the server section answers who holds each step, who may act and the next step; the model uses it to understand the question, does not repeat it and never says that the facts or the system do not state who acts (refused by checks/11; checks/13 also refuses the same claim about "the person / holder who holds, acts or decides", while "the facts do not name the people who asked" stays a limitation).
  - **Limitations (checks/10):** the model's limitations are shown to the reader ("Not covered: …") and pass the same authority guard. A refused limitation is listed in `rejected` (`kind: limitation`) and counted as prose authority. The server's own notes are added after the guard and are never filtered.
  - A wrong explanation is refused on its own; the authority section never keeps or excuses it.
  - Status `refused` means the model's answer kept nothing. The authority section is still shown but never counts as an answer (`semantic_success` ignores it).
- **Coverage and missing information (server notes in `limitations`):**
  - every offered blocker no kept explanation cites → "Not explained in this answer: …". This is a disclosure, never an explanation;
  - when the pack omitted facts (support / history tiers, upstream caps) → "Some facts about this request were not available to the assistant: N omitted" (counts only);
  - a refused cause → "An explanation of why was not shown: …" (checks/10);
  - none of these notes on an out-of-scope answer.
- **Unchanged:**
  - context built under the reader's rights before the model;
  - the worker rebuilds the pack under the asker's CURRENT roles and refuses on a changed manifest (`context_changed`) or revoked permission (`authority_revoked`) before the call;
  - stored answers are labelled historical when the request moved, and withheld when access changed;
  - the raw model object is stored for evaluation only and never returned;
  - answers stored under checks/4–7 are shown as stored; older ones keep the legacy view; nothing is regenerated;
  - the assistant never acts.
- **Evaluation:** model quality is the explanation (supported claims, causes, usefulness, honest refusals; prose authority counts as a model error). Authority / status correctness is a server property, verified deterministically against the record. The final answer is scored as the reader sees it.
