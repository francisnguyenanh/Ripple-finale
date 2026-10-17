# Recorded four-role case (run on 2026-10-13, "T1")

This scenario is **B. Recorded four-role case** (CAPEX). It is a historical record with its own baseline, input and results; none of its numbers, screenshots or evidence apply to the manager-threshold demo.

**Through apply, verification and Q&A; final acceptance pending.** A real run on the deployed candidate with a real AI model (no stub), executed by the project owner through the UI with four accounts. The owner stopped at step 13: **no result acceptance and no knowledge acceptance was recorded**, and none is implied.

## Scenario summary
<dl class="summary">
<dt>Business objective</dt><dd>Lower the CAPEX finance review threshold in the approval configuration only, keeping the other places visible as unresolved.</dd>
<dt>Baseline → applied</dt><dd><code>approval.yaml</code> <code>capex_finance_threshold_jpy</code> 1,500,000 → <strong>1,000,000 JPY</strong> - <strong>applied</strong> by the requester account and verified (the file was re-read).</dd>
<dt>Still unresolved</dt><dd><code>workflow.yaml</code>, <code>approval_matrix.xlsx</code>, <code>procurement_policy.md</code> still state 2,000,000. Loop status <strong>PARTIAL</strong>.</dd>
<dt>Measured (simulation, deterministic)</dt><dd>15 of 240 requisitions change, all NEEDS_MANAGER → NEEDS_FINANCE (7 standard + 8 emergency CAPEX). The AI reading was stored separately as interpretation.</dd>
<dt>Machine verification</dt><dd><strong>PASSED</strong> (the changed value read back as 1,000,000).</dd>
<dt>Human acceptance</dt><dd><strong>PENDING</strong>: result decision and knowledge candidate not accepted; "Accept result" is disabled with reasons.</dd>
<dt>Post-change question</dt><dd><strong>ABSTAINED</strong> because the sources still disagree; the system did not pick one value.</dd>
</dl>

## Business problem
The sources disagreed about one rule - the **CAPEX finance review threshold**:

| Source | Statement at the start |
|---|---|
| `approval.yaml` (read by the program) | 1,500,000 JPY |
| `workflow.yaml`, `approval_matrix.xlsx`, `procurement_policy.md` | 2,000,000 JPY |

The requester wanted it **lowered to 1,000,000 JPY in the approval configuration only**, with the other places kept visible as unresolved. Whether Finance's other documents should change too is a business decision that was **not** made in this run.

## Applied change and simulated effect
@@BEFOREAFTER:capex@@

## Approach
1. The AI restates the request; the person confirms it. 2. The AI writes a draft with citations; the **server** checks grounding and generates only the supported edit. 3. **Simulation** (Harbor's own program, 240 public requisitions, no AI) gives measured numbers; the **AI reading** is stored separately as interpretation. 4. An independent approver states the check after the change; a reviewer reads the evidence; the approver approves the exact fingerprint; the requester applies; the server re-reads the file and verifies. 5. A question after the change is answered from sources or **abstains**.

## Order of the run (who acted)
@@FLOW@@

The administrator took part only for one decision on an open point. The order is the order the server recorded, not a tidy four-account sequence. **The requester account pressed Apply**; the system only processed and verified it.

## Steps
Each card shows who acted, what kind of step it was (AI, deterministic or human decision) and the input. Open **Result, evidence and caveats** for what was recorded. Evidence ids are job or revision ids kept on the server.

### Step 1-2 - Request and confirmation
- **Role:** requester. **Kind:** AI + human decision.
- **Input:** "lower the CAPEX finance review threshold 1,500,000 → 1,000,000 JPY, approval.yaml only".
- **Result** <span class="tag t-live">Recorded live result</span>: card READY; intent `intent-ddf0f579fb59...` confirmed.
- **Caveat** <span class="tag t-lim">Limitation</span>: the scope limit was recorded as a note, **not enforced**; "applies to" showed "not known yet".

### Step 3 - Draft
- **Role:** requester. **Kind:** AI + server check.
- **Input:** generate the draft from the confirmed request.
- **Result** <span class="tag t-live">Recorded live result</span>: PARTIAL; one supported edit 1,500,000 → 1,000,000; open: one question, `workflow.yaml` steps, 2 findings. Draft `ui-draft-084893443fec...`.

### Step 4 - Decision on an open point
- **Role:** administrator. **Kind:** human decision.
- **Input:** reply on the open question; words kept verbatim; `approval.yaml` in scope, `workflow.yaml` and `approval_matrix.xlsx` not changed by this request.
- **Result** <span class="tag t-live">Recorded live result</span>: decision epoch 2 → 3; "Recorded by the server" only after the save; chat `chat-6eeb290b7899...`.

### Step 5 - Revise the draft
- **Role:** requester. **Kind:** AI.
- **Input:** revise after the decision.
- **Result** <span class="tag t-live">Recorded live result</span>: the new draft uses the decision (not stale); open items carried over. Draft `ui-draft-60012d4f3b0b...`.

### Step 6 - Impact and simulation
- **Role:** requester. **Kind:** deterministic (the AI reading is a separate AI step).
- **Input:** run impact, then simulation.
- **Result** <span class="tag t-live">Recorded live result</span>: coverage 73/73; 240 cases, baseline reproduced 240/240, **15 changed** (NEEDS_MANAGER → NEEDS_FINANCE): 7 standard + 8 emergency CAPEX requisitions. Impact `ui-impact-13ee32c7...`, simulation `ui-simulation-8bede444...`.
- **Caveat** <span class="tag t-lim">Limitation</span>: for the 8 emergency rows Harbor's `emergency.py` checks the CAPEX threshold **before** its hard-coded 1,000,000 manager line, so a CAPEX edit moves them; the page's limit text implied otherwise and was corrected afterwards (the numbers were not).

### Step 7 - Proposal
- **Role:** requester. **Kind:** deterministic.
- **Input:** tick the edit and prepare the proposal.
- **Result** <span class="tag t-live">Recorded live result</span>: "Left for people": `workflow.yaml` (unresolved), `procurement_policy.md`. `ui-proposal-0701a3f3...`.
- **Caveat** <span class="tag t-lim">Limitation</span>: this list omitted `approval_matrix.xlsx` because of a defect found in this run and fixed afterwards (a model "exclude" closed the item). The old record is kept as it was.

### Step 8 - Check after the change
- **Role:** approver. **Kind:** human decision.
- **Input:** confirm the suggested check that the server built from the confirmed request.
- **Result** <span class="tag t-live">Recorded live result</span>: the file must say 1,000,000; contract `vc-b5d9801cace7...`.

### Step 9-10 - Review and approval
- **Role:** reviewer, then approver. **Kind:** deterministic review + human approval.
- **Input:** read the evidence; approve the exact fingerprint.
- **Result** <span class="tag t-live">Recorded live result</span>: review READY_FOR_HUMAN_REVIEW; approved. `ui-review-5bb3cd11...`, `ui-approval-185c7ffc...`.

### Step 11 - Apply and verification
- **Role:** requester pressed **Apply**; the system then processed and verified. **Kind:** deterministic + AI re-read of the changed file.
- **Input:** apply the approved change.
- **Result** <span class="tag t-live">Recorded live result</span>: COMPLETED, verification **PASSED**; head moved to version 8. `ui-apply-dfcd1335...`.

### Step 13 - Ask again
- **Role:** requester. **Kind:** AI.
- **Input:** a question about the CAPEX threshold on the new revision.
- **Result** <span class="tag t-live">Recorded live result</span>: **ABSTAINED**: `approval.yaml` 1,000,000 against `workflow.yaml`, `approval_matrix.xlsx`, `procurement_policy.md` at 2,000,000. The answering model **declared** that it examined all 13 authorised files; the server accepts a "not found" answer only when that declared scope equals the authorised list, and it checks every citation against the stored text. That is a scope check, **not proof that every file was understood**. Quotes are hidden for the requester by design.
- **Caveat** <span class="tag t-lim">Limitation</span>: abstaining is the correct behaviour when sources disagree - the system does not pick one value. It does **not** mean the knowledge is now correct; it means the disagreement is still open.

## Results
- **Source that changed:** `approval.yaml` `capex_finance_threshold_jpy` 1,500,000 → **1,000,000**, nothing else; verification **PASSED**.
- **Still different:** `workflow.yaml`, `approval_matrix.xlsx`, `procurement_policy.md` still say 2,000,000. Loop status **PARTIAL**.
- **Knowledge / result:** the knowledge candidate is pending a person; "Accept result" is disabled with reasons (`knowledge_not_accepted`, `critical_gap_open`). **Open, and the project owner's.**
- **Cost** <span class="tag t-live">Recorded live result</span>: 7 AI jobs, 7 calls, 0.1504 USD verified; worker about 55 minutes.

## Limits and honesty
- Stopped at step 13: no result acceptance, no knowledge acceptance, no Finance-scope decision.
- Figures are **historical** (2026-10-13) and belong to the CAPEX case.
- Two defects found in this run were fixed afterwards; the old evidence was not rewritten.
- The two screenshots below are **illustrations** from an earlier run (2026-10-10) of the same screens with a different request; they are not evidence of this run.
- A real AI model's wording varies between runs.

## Supporting references
[Replay guide](FOUR_ROLE_REPLAY_GUIDE.md) · repository documents `docs/judges/`.
