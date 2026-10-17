# Scenario status, stage by stage (what was verified, in which run)

"Verified" here means an agent self-check on the deployed prototype or an offline test, never human acceptance. Each row names the run that is the evidence, so a stage with evidence is not called unverified because the whole journey was not one continuous run.

## Scenario A - one role (judge): raise the standard manager approval threshold 500,000 -> 800,000 JPY
The exact request wording of the guide contains a scope sentence ("keep this change limited to approval.yaml ..."), which the system records as a note and does not enforce.

| Stage | Evidence | Run |
|---|---|---|
| 1. Request -> AI understanding (current 500,000 and new 800,000 quoted from the person's words, nothing recorded yet) | READY card, 1 AI job (`intent.interpret`), 1 provider call, 0.0155 USD, about 18 s | **exact wording**, 2026-10-13, second judge account |
| 2. Preview confirmed (conversation CONFIRMED) | screen `02_understanding` / `03_preview_confirmed` (redacted copies) | **exact wording**, same run |
| 3. Confirm intent -> AI draft with citations (supported edit, 17 open points kept visible, same-rule items shown as potentially affected) | live draft, status PARTIAL; offline `test_excluded_not_closed.py`, `test_wording_intent_unresolved.py` | earlier 2026-10-13 judge self-check, **different wording, same values** |
| 4. Impact and simulation (35 of 240 sample requisitions NEEDS_MANAGER -> AUTO) | `ui-simulation-98fb78b9e07e49c9` (also claims row 4); the offline expectation is identical (`test_harbor_sim.py`) | live run at the live baseline 500,000 (not the 1,000,000 of the fixture), same values |
| 5. AI reading, labelled as interpretation | screen `07_reading` | same self-check |
| 6. Proposal prepared, "Waiting for the approver" (a judge has no approve/apply button) | screen `08_proposal`; `test_judge_profile.py`, `test_ui_judge_profile.py` | same self-check |
| 7. Review, approval, apply, verification | **not part of scenario A** (a judge cannot do them); covered by scenario B | - |

What is NOT shown: one continuous run in which the exact wording goes from stage 1 to stage 6. Stages 3-6 were observed with a different wording that carried the same values; one step of that run needed a manual fallback. The guide marks this as "partly verified".

## Scenario B - four roles (recorded case T1): CAPEX threshold 1,500,000 -> 1,000,000 JPY
| Stage | Evidence |
|---|---|
| Request, draft, simulation, proposal, review, approval of the exact fingerprint | recorded case, 2026-10-13, operated by the project owner with four accounts; 7 AI jobs, 0.1504 USD verified provider usage |
| Apply | job `ui-apply-dfcd1335...` COMPLETED (requester pressed Apply) |
| Verification by readback | PASSED |
| Question on the new revision (step 13) | ABSTAINED: `approval.yaml` and three other sources disagree (the model declared all 13 authorised files examined; the server accepts absence only when the declared scope equals the authorised list) |
| **Final acceptance** (result decision, knowledge acceptance) | **pending, owner's decision**; nothing was accepted by the agent |
| Screenshots | none of T1 exist; the guide's recorded-case page carries a diagram and two illustration images from an earlier run (2026-10-10), labelled as such |

Live apply is proven for typed YAML edits only. The emergency CAPEX rows move in the simulation because the emergency module checks the CAPEX threshold before its hard-coded 1,000,000.
