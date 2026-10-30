# Request assistant: operating it

For the operator of a Ripple server. What users see is described in [Request tasks and the request assistant](../product/request_tasks_and_assistant.md); the
rules are in the [contract](../contracts/REQUEST_TASKS_AND_ASSISTANT.md).

## Default: off and simulated
A fresh checkout runs the assistant against a simulated model: every question gets a labelled decline, nothing is sent to a provider and nothing is spent. A real model
is used only when all of the following hold:
1. The budget config (`RIPPLE_BUDGET_CONFIG_PATH`, example [final_budgets.example.yaml](../../config/ai_closed_loop/final_budgets.example.yaml)) pins a real model for the
   job kind `request.qa` and the role `request.assistant` (the example pins `stand_in`).
2. The provider is not disarmed: `RIPPLE_PROVIDER_MODE` is not `LIVE_DISARMED` and the runtime AI switch (Support & Access) is on and the trial window is open
   (`infra/provider_gate.py`).
3. The asking person holds `request.assistant.ask` in that workspace and may read the request. `ai.use` alone does not admit `request.qa`; the member marker
   `ai_suspended` removes both.

Arming a live model is a paid action: do it only under an approved allocation with its own call and cost caps.

## Limits
- Per question: one provider call, plus at most one supplementary call (role `request.assistant.complete`, pinned beside `request.assistant`; its own thinking level and deadline) when the first pass leaves a part of the question open; no automatic retry. The budget config must allow at least two calls per job (`max_provider_calls_per_job`), otherwise request.qa jobs are refused at admission. Opening the panel, starting a thread, reading history or polling makes no call.
- Deadline: `role_request_timeouts.request.assistant` and `.request.assistant.complete` in the budget config (production example 45 s; the request.assistant value is the deadline of the WHOLE job: the second call gets the time left, the `.complete` value only caps it; a question's end-to-end time includes both calls; it must not exceed `request_timeout_seconds`, which the worker
  lease covers; other roles keep `request_timeout_seconds`). The call is sent with that timeout; an answer that still arrives later is not used and its reported usage
  is booked; with no usage reported the attempt stays booked at its worst case (an estimate, `cost_basis: estimate_no_usage`). The person sees "did not answer in time"
  with the message id as reference (`v2_request_messages.message_id` -> `job_id`). The deadline is an operating bound: a timed-out question still counts as an answer
  that took the deadline in any latency measure.
- [request_assistant.json](../../config/request_assistant.json) `limits`: question length, parts of a question (`max_asks`), blocks per answer, names declared as outside the record (`max_unrecorded`), history turns sent, messages per thread, prompt size.
- The budget config bounds input, output and generation tokens per call; the worst case per call is reserved before the call and settled with the provider's usage.

## Switching it off
- Only the assistant: the runtime switch "Request assistant (new questions)" in Support & Access (key `request_assistant`, no redeploy), or the deployment setting
  `RIPPLE_REQUEST_ASSISTANT=off`. The server refuses new threads and questions (`409 assistant_off`), the launcher is hidden, an answer queued before the switch is stopped
  before its provider call and settled by the normal path, earlier answers stay readable to their askers, and request pages and business actions keep working.
- All AI at once: switch AI off in Support & Access (no redeploy), or set `RIPPLE_PROVIDER_MODE=LIVE_DISARMED`. New asks are refused with a reason; saved
  answers stay readable to their askers; every other request page and action keeps working.
- For one person: add the member marker `ai_suspended`, or remove the role that grants `request.assistant.ask`. Their earlier answers are then withheld (the
  authorization fingerprint no longer matches) until they ask again with access.
- An answer already being generated when access is removed is not shown; its usage is still booked.

## Reconciling cost
Every call is booked in `v2_job_cost_ledger` (reserved before the call, settled with the provider's usage, late usage included). Before and after any paid run,
compare the settled total and open reservations with the allocation's own ledger; a run with an open reservation or a mismatch is not closed until it is explained.

## Retention (v1)
Threads and messages (`v2_request_threads`, `v2_request_messages`) live with the workspace. There is no user delete, no sharing and no export of threads in v1; the
stored question, context manifest hash, authorization fingerprint, job id and checked answer stay for audit. Review this before a release with real users.
