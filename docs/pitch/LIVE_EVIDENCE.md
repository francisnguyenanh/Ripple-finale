# Live evidence - first live run, Harbor six-file scope, 2026-10-05
Candidate <candidate URL, private>, image digest sha256:f84e929b... (commit 46814bf), budget secret v5, tenant pitch-t1 / workspace pitch-ws1, SIMULATED accounts (admin, requester, change_approver); human acceptance NOT_RUN. Production untouched.
The secret-free run journal (events in order) is kept outside this repository.

## Chain (request -> evidence -> model -> draft -> proposal -> approval -> apply -> re-extraction -> query)
- extraction `ui-extract-cb66006eca6d` model gemini-3.8-flash calls 1 cost 0.0187740 usage 601/1112
- extraction `ui-extract-f8be71abcfd7` model gemini-3.8-flash calls 1 cost 0.0158895 usage 668/640
- extraction `ui-extract-3b2ef68e2a12` model gemini-3.8-flash calls 1 cost 0.0135705 usage 1192/1571
- extraction `ui-extract-f89e137aa83b` model gemini-3.8-flash calls 1 cost 0.0067905 usage 737/201
- extraction `ui-extract-750dd77a592c` model gemini-3.8-flash calls 1 cost 0.0261705 usage 1062/592
- extraction `ui-extract-020951d4b2a0` model gemini-3.8-flash calls 1 cost 0.0244410 usage 909/816

- draft `ui-draft-78959fd19cce` model gemini-3.8-flash (endpoint {'backend': 'vertex', 'location': 'global', 'pinned_model': 'gemini-3.8-flash', 'served_model': 'gemini-3.8-flash'}) usage 3078/703 cost 0.0329295 status **PARTIAL** hash 4a7059e5f5c53945
  - items: approval.yaml PROPOSED; approval_matrix.xlsx AMBIGUITY; procurement_policy.md MANUAL_CHANGE; procurement_policy.md EXCLUDED
  - unaddressed: ['workflow.yaml']; unsupported citations: 1; not covered: [' applies_to:emergency: no classified evidence item ', 'calculations.py no_evidence_retrieved_from_this_artifact', 'calculations.py python parser observes only module-level literal a', 'finance_desk_guide.md no_evidence_retrieved_from_this_artifact']
  - stored findings shown: 30 (all GAP/missing_scope; NO scope_mismatch (C02) was produced by the live finding job - C02 is NOT visible in this live run)
- impact `ui-impact-11d4783d6364` COMPLETED
- simulation `ui-simulation-6f38ee02d7c2` COMPLETED
- proposal `ui-proposal-0aa43ec3216d` COMPLETED
- review `ui-review-adc8b16655a7` COMPLETED
- approval `ui-approval-26ac4b6e8837` COMPLETED
- apply `ui-apply-e05c66830778` COMPLETED
  - proposal_result: {"job": "ui-proposal-0aa43ec3216d", "status": "COMPLETED", "reason": null, "classification": "recommended", "readiness": "READY", "blockers": [], "edits": [["routing_rules.standard_manager_threshold_jpy", 1000000, 500000]], "left_for_humans": [["AMBIGUITY", "approval_matrix.xlsx"], ["MANUAL_CHANGE", "procurement_policy.md"], ["UNADDRESSED", "workflow.yaml"]]}
  - review_result: {"job": "ui-review-adc8b16655a7", "status": "COMPLETED", "readiness": "READY_FOR_HUMAN_REVIEW", "acceptance": {"detail": "contract_sha256=5e978c3a42849f1c10e3dfcae7e30dc637e7d0b45a0b94631ae8bc5cfbb94a28; contract_id=vc-58f910dbcab94884b60dce535557d6e1; kind=yaml_value_equals; locator=routing_rules.standard_manager_threshold_jpy; expected=500000; actual=500000; diff=['routing_rules.standard_manager_threshold_jpy']; st
  - decision: {"http": 200, "approver": "pitch-approver-1005"}
  - apply_result: {"job": "ui-apply-e05c66830778", "status": "COMPLETED", "reason": null, "published": "rev-apply-ui-apply-e05c66830778"}
  - query_result: {"job_id": "ui-query-0819b3623a9c", "kind": "gap.answer", "status": "APPROVED", "execution_status": "COMPLETED", "execution_reason": null, "execution_model_id": "gemini-3.5-flash-lite", "execution_provider_calls": 1, "execution_cost_usd": "0.0005738", "execution_usage": {"input_tokens": 796, "output_tokens": 134, "provider_calls": 1, "cost_estimate_usd": "0.0379200"}, "execution_executor_id": "gap.answer.v1", "revisi

## Loop status after apply (read from /v2/change/loop-status)
- status **PARTIAL**, reasons ['knowledge_blockers:40', 'residual_sources:2']
- stages {'apply': 'COMPLETED', 'verification_job': 'COMPLETED', 'verification': 'PASSED', 'knowledge_rebuild': 'COMPLETED', 'reanalysis': {'completed': 8, 'total': 8}, 'knowledge_blockers': 40}
- residual sources [['approval_matrix.xlsx', 'AMBIGUITY', 'at_new_value'], ['procurement_policy.md', 'MANUAL_CHANGE', 'still_old_value'], ['workflow.yaml', 'UNADDRESSED', 'not_classified_by_the_draft']]

## Honest limits
- Overall PARTIAL: the YAML sub-loop (approval.yaml 1,000,000 -> 500,000) was applied, read back, verification PASSED and the new revision answers 500000 with a citation from the new revision. procurement_policy.md (manual) still holds the old value; approval_matrix.xlsx was left AMBIGUOUS by the model (value already 500000); workflow.yaml was not classified by the model; emergency scope is not covered; knowledge acceptance has open blockers.
- Approver/confirmer accounts are SIMULATED. No claim of Harbor COMPLETE, of AI superiority, or that the held-out AI-quality targets were met (they were not; see `../evaluation/HELD_OUT_EVALUATION.md`).
