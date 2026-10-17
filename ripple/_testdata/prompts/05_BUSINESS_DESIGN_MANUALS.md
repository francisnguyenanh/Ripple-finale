# Stage 05 — Business policy, design and manuals

Operator inputs: system instruction, frozen spec, accepted contracts and relevant application/config/data files. Request two or three documents per batch, not all at once.

---

Write the realistic business/design documentation for Harbor Procurement Mini. Author into `authoring/templates/input/docs/`. These documents are input for semantic understanding; keep natural vocabulary and useful detail, not an answer-key skeleton with identical rule IDs everywhere.

Required documents (adjust names only through the accepted artifact contract):

1. `procurement_policy.md`: baseline manager threshold 1m for standard purchases, risk/CAPEX precedence, maker/checker, boundaries/exceptions actually specified. Ordinary document owner/version/effective date, no private scenario labels.
2. `purchase_sop.md`: request→routing→human approval→PO→receiving→AP steps. Include the two-business-day manual escalation requirement. Do not falsely describe an implemented scheduler/calendar.
3. `emergency_purchase_procedure.md`: explain emergency handling realistically; retain exactly the unresolved future threshold-inheritance issue as "appropriate sign-off" without secretly answering it. Other explicit risk/CAPEX checks are consistent.
4. `finance_desk_guide.md`: canonical template correctly says CAPEX only >=2m; private C02 mutation later creates baseline stale all-category wording. Do not label it stale/intentional in final public document.
5. `accounts_payable_manual.md`: duplicate key normalization, approved PO, accepted receipt quantities, AND price tolerances, payment due dates; READY means scheduling eligibility, not payment execution.
6. `system_overview.md`: actors, boundaries, source/config/workbook guidance roles, integrations intentionally absent, Mermaid component diagram and process diagram.
7. `functional_design.md`: routing/invoice states, normal/exception paths, priority and explicit current implementation behavior; distinguish desired normative wording from documented observed code behavior. Do not introduce an automatic knowledge engine or AI assistant into the reference app.
8. `data_dictionary.md`: exact accepted field schemas, units/types/enums/nullability, ID/FK relationships, net/freight/tax/gross, Mermaid ER diagram. No private truth IDs.
9. `program_design.md`: actual modules/functions, input/output and error behavior; no invented functions. Emergency implementation can be described as a separate local limit naturally, but do not say "hardcoded bug planted for Ripple".
10. `api_cli_reference.md`: actual runnable CLI/API commands/schemas, working directory requirements, output paths and error codes. Optional web routes only if implemented.
11. `user_manual.md`: buyer/manager/receiving/AP instructions, example records that exist, role approval requirements. No authority to change Ripple configuration.
12. `operations_manual.md`: local setup, build/reset controlled output, backup generated inputs/results, troubleshooting; explicitly offline synthetic sample with no real payment. No hidden scenario/expected count/benchmark instructions in public manual.

Cross-document consistency rules:

- All docs match frozen policy and actual architecture except the explicitly planned C01/C02 mutations and G01/G02 gaps. Do not introduce random old limits, dates, roles or unimplemented functions.
- Use semantically equivalent paraphrases: manager sign-off, purchasing authority, approval limit, delegated spend. Do not attach a shared canonical rule ID everywhere to make linking trivial. Use exact numbers/conditions where policy requires precision.
- Make unrelated freight 500k threshold clearly about shipping, not approval. Source AI must understand context to avoid changing it during S1.
- Approval and payment are separate processes. Negative history examples do not establish new policy. Synthetic tax is a fixture assumption, not jurisdictional guidance.
- Public document versions and dates are business metadata; no Git commit history fabricated by the model. Crosslinks must point to actual delivered files and headings.

Return documents plus a **private** `authoring/doc_consistency_checklist.md` recording intended claims/gaps, not copied into input. Do not expose Cxx/Gxx identifiers in public content. Report source dependencies missing rather than inventing function names or measured counts.
