# Private Authoring Documentation Consistency Checklist (Stage 05)

## 1. Traceability of Intentional Conflicts and Gaps

| Claim / Gap ID | Document Location | Stated Semantic Claim in Document | Canonical System Truth |
|---|---|---|---|
| **C01** | `data/approval_matrix.xlsx` | Sheet `RoutingRules`, Cell B4 states Manager threshold = `500,000 JPY` | Canonical code and `approval.yaml` execute `1,000,000 JPY`. |
| **C02** | `docs/finance_desk_guide.md` | Section 2 asserts "all purchasing commitments of 2,000,000 JPY or higher must be reviewed by Finance" (any category) | Policy `procurement_policy.md` and `app/routing.py` restrict Finance review strictly to `CAPEX >= 2,000,000 JPY`. |
| **G01** | `docs/emergency_purchases.md` | Section 2.3 advises "obtain appropriate sign-off" without resolving whether emergency limits change with general limits | `app/emergency.py` hardcodes `1,000,000 JPY`. Normative authority is unconfirmed pending human decision. |
| **G02** | `docs/requisition_sop.md` | Section 3.3 mandates escalation after 2 business days | System has no background worker, scheduler, or holiday calendar. Document-only requirement. |

---

## 2. Invariant Verification
- [x] No private IDs (`R01-R12`, `C01-C02`, `G01-G02`, `S0-S5`) are present in any public documentation file under `docs/`.
- [x] Unrelated freight threshold (500,000 JPY in `procurement_policy.md`) is clearly articulated as delivery freight logistics, separate from purchasing authorization limits.
- [x] Financial values match synthetic integer JPY standards without floating-point arithmetic.
- [x] 3-way matching rules in `invoice_matching_manual.md` match exact implementation logic in `app/matching.py`.
