# Ripple - judge guide

@@STATUS@@

Ripple keeps a company's rules consistent when one of them changes. It reads the sources (configuration, workbooks, policy documents), keeps what it found with citations, and when a person asks for a change in plain words it shows **what would change, what else may be affected, and what stays unresolved** - before anyone applies anything. People decide; the AI suggests; the server owns rights, grounding, status and integrity.

## What am I testing?
That a plain-language change request becomes a checked, grounded proposal - and that Ripple does **not** hide what it could not resolve. You test one rule of a procurement system: the **standard manager approval threshold**.

> **Starting point.** This walkthrough uses a specific starting baseline. If earlier changes have been applied in this workspace, the sample values and expected results may no longer match. Check the current source revision and values before starting. For subsequent runs, follow the same workflow using the latest data.

## Three ways to evaluate
<ol class="route">
<li><strong>Understand Harbor</strong> - the system and its 13 analysed files (below).</li>
<li><strong>Try the one-role demo - recommended.</strong> Raise a manager threshold yourself with a judge account and stop at a proposal that waits for an approver. <a href="one-role-guide.html">Open the one-role guide</a>.</li>
<li><strong>Run the four-role workflow - optional, not yet available.</strong> A hands-on run through review, approval and apply is planned for a separate workspace; it is offered only once that environment is set up and checked. <a href="four-role-replay.html">What it will involve</a>.</li>
<li><strong>Read the recorded four-role case - read-only evidence</strong> of a past run, with its own rule (CAPEX) and baseline. <a href="four-role-case.html">Open the recorded case</a>.</li>
</ol>

You can also read the recorded case first. The two scenarios use **different rules, baselines and runs**; their numbers, screenshots and evidence are never mixed.

## The two scenarios side by side
<div class="cards">
<section class="card" aria-labelledby="sc-a">
<h3 id="sc-a">A. Try it yourself <span class="tag t-judge">Judge action</span></h3>
<dl>
<dt>Business objective</dt><dd>Raise the standard manager approval threshold and see the effect before anything is applied.</dd>
<dt>Baseline → proposed</dt><dd><code>approval.yaml</code> 500,000 → <strong>800,000 JPY</strong> (proposed, <strong>not applied</strong>).</dd>
<dt>Scope and unresolved</dt><dd>Scope <code>approval.yaml</code>. Other sources that state a manager threshold stay open.</dd>
<dt>Measured (simulation)</dt><dd>35 of 240 requisitions NEEDS_MANAGER → AUTO.</dd>
<dt>Verification</dt><dd>Exact request verified <strong>only up to the confirmed understanding</strong>; draft → proposal come from an earlier run (different wording, manual fallback).</dd>
<dt>Acceptance</dt><dd>None. The run ends waiting for a reviewer and an approver.</dd>
</dl>
<p><a class="cta-link" href="one-role-guide.html">Go to the one-role guide</a></p>
</section>
<section class="card" aria-labelledby="sc-b">
<h3 id="sc-b">B. Recorded four-role case (T1) <span class="tag t-live">Recorded live result</span></h3>
<dl>
<dt>Business objective</dt><dd>Lower the CAPEX finance review threshold in the approval configuration only.</dd>
<dt>Baseline → applied</dt><dd><code>approval.yaml</code> 1,500,000 → <strong>1,000,000 JPY</strong> (applied by the requester, verified).</dd>
<dt>Scope and unresolved</dt><dd><code>workflow.yaml</code>, <code>approval_matrix.xlsx</code>, <code>procurement_policy.md</code> still say 2,000,000.</dd>
<dt>Measured (simulation)</dt><dd>15 of 240 requisitions NEEDS_MANAGER → NEEDS_FINANCE.</dd>
<dt>Verification</dt><dd>Machine verification PASSED; the post-change question ABSTAINED; loop status PARTIAL.</dd>
<dt>Acceptance</dt><dd><strong>Final acceptance pending</strong> (result and knowledge not accepted).</dd>
</dl>
<p><a class="cta-link" href="four-role-case.html">Go to the recorded case</a></p>
</section>
</div>

## How do I verify the result?
Do not compare the AI's wording: check **scope, value, grounding and status**. Each step in the guides says what to look for and which evidence supports it, using these labels:

- <span class="tag t-judge">Judge action</span> - what you do.
- <span class="tag t-live">Recorded live result</span> - something that was observed on the deployed system.
- <span class="tag t-off">Offline expectation</span> - computed from the shipped sample data, not observed live.
- <span class="tag t-nv">Not yet verified</span> - not run yet; do not rely on it.
- <span class="tag t-lim">Limitation</span> - something Ripple does not do or does not claim.

## The system under test: Harbor Procurement Mini
Harbor is an internal procurement reference application for a fictional office-supply company: it routes purchase requisitions to approvers, handles emergency purchases and checks invoices against purchase orders and receipts. Ripple works on a **13-file subset** of Harbor that was **analysed in advance by a real AI model** (with citations):

- **Code (4):** `routing.py` (standard approval matrix), `emergency.py` (emergency purchases), `calculations.py` (arithmetic, tax, payment terms), `config_loader.py` (reads the configuration).
- **Configuration (2):** `approval.yaml` (the thresholds the program reads), `workflow.yaml` (workflow steps; it also states thresholds, but this program does not read it).
- **Workbook (1):** `approval_matrix.xlsx` (routing rules as a table).
- **Documents (6):** `procurement_policy.md`, `finance_desk_guide.md`, `requisition_sop.md`, `emergency_purchases.md`, `invoice_matching_manual.md`, `data_dictionary.md`.

Coverage is the subset above, not the whole of Harbor and not a whole company: Harbor's own records, forms and tests are not part of it. The route simulation runs Harbor's own program over its 240 public sample requisitions; it does not read `workflow.yaml`, the documents or the workbook.

@@SOURCEMAP@@

**How to read the picture:** solid arrows are relations that can be checked in the code (the program reads the value). Dashed lines mean a file *states* a similar threshold in text; that is a statement to compare, **not** a dependency - Ripple shows it as "may be affected / unresolved" instead of assuming it moves with the configuration.

## Limits you should know
- The AI can ask for clarification or **abstain**; that is a correct outcome, not an error.
- A judge account can read and prepare, but cannot import sources, run extraction or re-extraction, review, approve or apply. Raw source quotes are hidden for this role by design.
- People sharing one judge account share its chats, My work, history and AI quota. Everyone in the workspace can read the workspace's stored jobs and history.
- Each conversation allows at most 6 AI turns and each judge account at most 12 AI jobs per day. These are ceilings, not promises: the AI budget is shared by everyone, so when it (or the evaluation window, or the processing capacity set aside for it) is used up, Ripple refuses new work at once with a clear message and nothing is charged.
- The first AI step after a quiet period can take about a minute because the processing worker starts on demand.
- Availability of the AI steps is limited to the evaluation window: @@WINDOW@@

## Supporting references
[Four-role workflow (not yet available) and how to follow the recorded case](FOUR_ROLE_REPLAY_GUIDE.md) · repository documents under `docs/judges/` (the source of these pages) · this site is read-only documentation and calls no Ripple API.
