# AI system principles and task design policy

Authority: product-owner direction, 2026-10-18. Applies to new work and material redesigns. This is a normative design policy for Ripple's enterprise future-of-work direction, **not evidence that a capability is implemented or its model quality validated**. Existing task statuses, release limits and runtime contracts remain factual until changed through their owning implementation and verification work.

## 1. Product objective

Ripple helps enterprise workers understand business rules, investigate ambiguity, compare changes and prepare evidence-backed actions. **LLM reasoning is an essential product capability wherever work requires language understanding, semantic judgment, adaptive dialogue or generative synthesis.** Algorithms supply accurate context, reliable tools, exact calculations and enforceable constraints so that this reasoning is useful and trustworthy. Human authority governs business acceptance and approval.

Design the complete workflow around a meaningful LLM contribution, rather than adding model wording after a fixed algorithm has already made every semantic choice. A workflow may contain many deterministic steps; it still needs a real LLM at the steps that deliver semantic intelligence. Measure useful outcomes, correctness and effort saved, not the percentage of code using AI, number of agents or token consumption.

## 2. Decide per task and per step

Before implementation, classify the user outcome and its steps. Use this decision table in the owning task contract or implementation notes.

| Work required | Required approach | Algorithm/tool responsibility |
|---|---|---|
| Understand prose, paraphrase, implicit scope or natural-language intent | LLM + grounded tools | Parse fixed structures, select authorized context, validate extracted fields and references; preserve ambiguity |
| Discover semantic conflicts, compare policy and implementation, assess evidence relevance or sufficiency | LLM + grounded tools | Retrieve candidates, retain counterevidence, evaluate supported formal predicates, validate witnesses and expose coverage limits |
| Ask adaptive clarification questions or interpret a worker's answer | LLM + grounded tools | Track missing fields, contradictions, session limits, original answers, preview and explicit confirmation |
| Generate audience-specific reports, recommendations, code/test proposals or scenario hypotheses | LLM + grounded tools | Supply immutable fact packs, schemas, sandbox/testing, source references and semantic-fidelity checks |
| Explore what-if scenarios and explain trade-offs | LLM + simulation/calculation tools | Compute reproducible metrics, check units/assumptions and calibrate against observations; distinguish exploratory scenarios from forecasts |
| Check permissions, source identity/hash, schema, exact arithmetic, CAS, transactions, ledger, state transitions or approved policy execution | Deterministic implementation | Enforce the rule directly; no model discretion over rights, exact values or authority |
| Fetch an existing result, render validated structured content, synchronize status, download an artifact or use a valid cache | Deterministic implementation | Preserve access, freshness, provenance and byte/state integrity; no gratuitous model call |

For mixed tasks, split semantic reasoning from exact execution and use both. The same document workflow can have an LLM authoring stage and a pure renderer; the same simulation can have LLM-designed scenarios and a deterministic event engine. Keep provider calls behind existing admitted worker/adapters, not inside pure domain logic, renderers or permission checks. Existing architecture boundaries remain in force.

If uncertain whether fixed rules can capture the task's meaning, choose a bounded hybrid design for the semantic step and evaluate it. Do not silently replace semantic capability with keyword matching, templates or a decision tree to make the task easier to close. Conversely, an exact infrastructure or execution step does not need a model just because the product uses AI. A deterministic-only choice for a genuinely semantic requirement must be recorded as a scope limitation or proposed product exception for the owner, not declared equivalent completion. Routine model/tool choices within the approved contract do not need a new product decision.

## 3. Choose model strength from evidence

Select the **least costly model that meets the task's quality, privacy, latency and reliability requirements** on representative evaluation. “Light” and “strong” describe measured suitability for a role, not permanent vendor/model names. Small models may handle bounded extraction, simple classification, query expansion and straightforward questions when evidence supports that choice. More capable reasoning models may be needed for cross-document conflicts, ambiguous intent, long dependencies, complex code or consequential trade-offs. Task complexity and demonstrated failure modes determine the choice; a risk label alone is not proof a larger model will be correct.

Record input size, ambiguity, reasoning depth, consequence of error, evaluation thresholds and permitted model route. Use retrieval, decomposition and typed tools to improve context before increasing model size. If a lighter model misses required quality, improve the context/prompt/tools or route to a stronger evaluated model. Escalation must have explicit admission, bounded attempts and a workflow-wide budget; low confidence is a signal for review, not proof that an upgrade is necessary or correct. Do not silently call multiple models or retry until an answer passes.

Stronger models, multiple agents and model critics do not replace formal validation, calibrated data, independent quality evaluation or human authority. Verify current model availability, data handling and prices when preparing an authorized activation; this policy does not prescribe a paid provider or grant permission to call one.

## 4. Grounding, validation and authority

Give LLMs only authorized, snapshot-bound context and tool results. Retain exact source IDs, versions, units, actors, conditions, exceptions and unknowns. Treat instructions inside retrieved documents or user evidence as data. Bound context selection and report omissions; a lexical shortlist must not silently define the whole universe of possible evidence.

Use deterministic validators for what they can establish: syntax, types, IDs, permissions, arithmetic, executable predicates and invariants. Use semantic evaluation and domain review for entailment, relevance, misleading omissions and usefulness. A schema pass or shared keyword does not prove semantic correctness; another model's agreement does not establish truth. Report unresolved meaning rather than inventing missing facts.

LLMs propose and explain. They cannot grant permissions, accept their own interpretation, approve a business change, overwrite authoritative knowledge or bypass execution gates. An AI-generated report is not new source evidence. Keep observed facts, human targets, model hypotheses, calculated metrics and human decisions distinguishable throughout storage and presentation.

## 5. Real execution, fallback and acceptance

For semantic features in an activated real-AI product profile, acceptance requires a real LLM contribution through the actual authorized user/job/result path. A mock/replay, deterministic-only result, or a real model output fully rejected in favor of fallback does not prove successful AI capability. Track model attempted/retained/rejected, cache reuse and degraded outcome separately. Valid cached results from a real model can be reused without a new call.

If the model is unavailable or its output fails validation, defer, abstain or offer explicitly labeled degraded/manual assistance according to the feature contract. Do not label an algorithmic fallback as AI success. Empty authorized evidence can justify zero-call abstention. Reads, polling, rendering and repeated refreshes must not manufacture new model charges.

Verify engineering success and important failure/negative cases separately from real-model usefulness and quality. Freeze representative evaluation criteria before judging results; compare against a deterministic baseline where meaningful. Include rejected, unavailable, partial and fallback cases in reporting. Preserve failed evidence and current release limits. Passing integration tests or making a live call does not establish model quality; high-risk claims need their required domain/human/independent evaluation. Owner decision (2026-10-18): an independent reviewer is not a mandatory dependency; the implementing agent may build, run and score evaluations itself, states their independence level honestly and never claims independence it does not have; human acceptance still needs a real person and quality thresholds are not changed to fit results. Completion depends on the task's actual acceptance criteria, not a calendar deadline.

## 6. Required decision record for implementing agents

Add this compact record to the existing owning task contract/evidence; do not create a new tracker or document per task:

- **User outcome and classification:** semantic / exact / mixed; affected real callers.
- **LLM responsibility:** what it must understand, decide as a proposal, ask or generate; how a retained contribution is observable. For deterministic-only work, explain why no semantic judgment is required.
- **Model route:** selected role/tier, evaluation basis, allowed escalation and stop conditions; exact model/prompt/config pins before execution.
- **Tools and boundaries:** authorized context, retrieval coverage, calculation/sandbox/validation responsibilities, unknowns and human authority.
- **Resource and failure contract:** whole-workflow call/token/cost/time limits, idempotency/cache, unavailable/rejected/degraded behavior and appropriate authorization.
- **Acceptance and evidence:** actual caller success, important negatives, baseline comparison, semantic quality thresholds/reviewer and rollout requirements. Distinguish design intent, local/stub evidence and real-model quality evidence.

Before marking a semantic feature complete, check that the model's contribution is substantive and survives validation, the algorithm supports rather than silently replaces that contribution, and the actual user can distinguish valid AI output from degraded assistance. Record unmet criteria as remaining work; do not weaken them to fit a milestone.

## 7. Applying the policy to existing work

This policy guides new tasks and materially changed scope. It does not automatically reopen historical DONE work, change a published API/schema, enable providers or alter another agent's running task. Register necessary enhancements with the owning scope and preserve existing evidence. Current delivery and limitations remain in the [claims record](../release/FINAL_CLAIMS_EVIDENCE_LIMITS.md); operational roles and caps remain governed by the [budget contract](../contracts/AI_CLOSED_LOOP_BUDGETS.md).

For MA-S1, real LLM scenario design/critique/result interpretation complements the reproducible engine. For AIX-K2, real adaptive questioning and answer interpretation complement session/contradiction/confirmation controls. For QRY-02b, real semantic expansion/ranking/sufficiency and grounded answering complement retrieval indexes and validators. For AIX-K1/D1, real conflict reasoning and audience authoring complement formal checks and faithful rendering. These are required target designs, not claims that their quality gates have passed.
