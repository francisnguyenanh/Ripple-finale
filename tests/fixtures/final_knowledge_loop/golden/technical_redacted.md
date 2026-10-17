# Business knowledge - technical view

## Snapshot

- snapshot: `snap-doc` (ACCEPTED, business revision 4)
- source revision: `src-4`; knowledge revision: `km-9`; decision epoch: 3
- content hash: `abc123`
- rules: 6 (active 6, decided 1); open questions 4 (critical 1); accepted unknown 0; partial true
- template: `fkl-doc/1` v1, locale `en`, scope `{}`

## Rules

### CAPEX of 2,000,000 JPY or more goes to Finance (configuration and code)

- Rule ID: `rule-capex-finance-config`
- Status: `OBSERVED / VALIDATED`
- Actor: `role-finance`
- Condition (expression):

```json
{"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "unit": "JPY", "value": 2000000}]}
```

- Outcome: `{"kind": "review_required"}`
- Exceptions: none
- Scope: process: STANDARD, EMERGENCY
- Provenance: decisions `[]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `verified`; unknown fields `[]`

### CAPEX of 5,000,000 JPY or more goes to Finance Controller (policy and manager guide)

- Rule ID: `rule-capex-finance-policy`
- Status: `OBSERVED / VALIDATED`
- Actor: `role-finance`
- Condition (expression):

```json
{"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "unit": "JPY", "value": 5000000}]}
```

- Outcome: `{"kind": "review_required"}`
- Exceptions: none
- Scope: process: STANDARD
- Provenance: decisions `[]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `not_required`; unknown fields `[]`

### CAPEX of 2,000,000 JPY or more is reviewed by Finance

- Rule ID: `rule-capex-finance-target`
- Status: `TARGET / CONFIRMED`
- Actor: `role-finance`
- Condition (expression):

```json
{"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "unit": "JPY", "value": 2000000}]}
```

- Outcome: `{"kind": "review_required"}`
- Exceptions: none
- Scope: process: STANDARD, EMERGENCY
- Provenance: decisions `["decision-s1-capex"]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `partial`; unknown fields `[]`

### Emergency purchases

- Rule ID: `rule-emergency-routing`
- Status: `OBSERVED / PENDING_HUMAN`
- Actor: null
- Condition (expression):

```json
null
```

- Outcome: `{"kind": "unknown"}`
- Exceptions: none
- Scope: process: EMERGENCY
- Provenance: decisions `[]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `divergent`; unknown fields `["actor_ref", "condition"]`

### Purchases from high-risk suppliers go to the Operations Director regardless of amount

- Rule ID: `rule-high-risk-director`
- Status: `OBSERVED / VALIDATED`
- Actor: `role-director`
- Condition (expression):

```json
{"field": "supplier.risk", "op": "eq", "value": "HIGH"}
```

- Outcome: `{"kind": "review_required"}`
- Exceptions: none
- Scope: process: STANDARD, EMERGENCY
- Provenance: decisions `[]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `verified`; unknown fields `[]`

### Purchases of 1,000,000 JPY or more need manager approval

- Rule ID: `rule-standard-manager-review`
- Status: `OBSERVED / VALIDATED`
- Actor: `role-manager`
- Condition (expression):

```json
{"field": "request.net_amount_jpy", "op": "gte", "unit": "JPY", "value": 1000000}
```

- Outcome: `{"kind": "review_required"}`
- Exceptions: none
- Scope: process: STANDARD
- Provenance: decisions `[]`; sources [redacted: source references need the evidence.read permission]
- Implementation: `verified`; unknown fields `[]`

## Relations

- `edge-09175f2917e29432` defines: `concept-purchase-approval` -> `rule-capex-finance-config` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-093dc13fc4574eb4` defines: `concept-capex` -> `rule-capex-finance-policy` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-0ceaff570b9e31a0` approves: `role-finance` -> `rule-capex-finance-policy` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-1a0e8efb28c39358` defines: `concept-high-risk-supplier` -> `rule-high-risk-director` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-1cdcbb11460d6808` approves: `role-manager` -> `rule-standard-manager-review` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-4ac9fad8190c40fd` defines: `concept-purchase-approval` -> `rule-capex-finance-target` (CONFIRMED); provenance [redacted: source references need the evidence.read permission]
- `edge-51ca7ba2ed1b18db` conflicts\_with: `rule-capex-finance-config` -> `rule-capex-finance-policy` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-529665df64fe2329` defines: `concept-purchase-approval` -> `rule-capex-finance-policy` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-597939109f5bb7d8` approves: `role-finance` -> `rule-capex-finance-target` (CONFIRMED); provenance [redacted: source references need the evidence.read permission]
- `edge-5ccb534e50ac2cca` defines: `concept-emergency-purchase` -> `rule-emergency-routing` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-864c5316b1ff485f` defines: `concept-purchase-approval` -> `rule-high-risk-director` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-a0bb5659ed55a2e3` approves: `role-finance` -> `rule-capex-finance-config` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-b19a35ced13dffee` defines: `concept-purchase-approval` -> `rule-standard-manager-review` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-b280a2ef2dee6e18` defines: `concept-capex` -> `rule-capex-finance-config` (PROPOSED); provenance [redacted: source references need the evidence.read permission]
- `edge-e1b88cb50118a6a4` defines: `concept-capex` -> `rule-capex-finance-target` (CONFIRMED); provenance [redacted: source references need the evidence.read permission]
- `edge-e38ec091227888ec` approves: `role-director` -> `rule-high-risk-director` (PROPOSED); provenance [redacted: source references need the evidence.read permission]

## Conflicts

- `edge-51ca7ba2ed1b18db`: `rule-capex-finance-config` conflicts with `rule-capex-finance-policy`

## Open and accepted-unknown questions

- How exactly does the rule "Emergency purchases" decide when it applies?
  - `gap-1f7e605426061c7b` missing\_condition OPEN normal rules `["rule-emergency-routing"]`
- What happens when the rule "Emergency purchases" applies?
  - `gap-6d7fb9a8322f422e` missing\_outcome OPEN normal rules `["rule-emergency-routing"]`
- The sources disagree about request.net\_amount\_jpy: "CAPEX of 2,000,000 JPY or more goes to Finance (configuration and code)" says 2000000, "CAPEX of 5,000,000 JPY or more goes to Finance Controller (policy and manager guide)" says 5000000. Which one is the business rule?
  - `gap-d952efc5ab356b73` conflicting\_facts OPEN critical rules `["rule-capex-finance-config", "rule-capex-finance-policy"]`
- Who acts on the rule "Emergency purchases"?
  - `gap-ef1c3b0120a19021` missing\_actor OPEN normal rules `["rule-emergency-routing"]`

## Implementation status

- `rule-capex-finance-target`: `partial`

source files and code are not modified by the product

## Decision records

- “Finance reviews CAPEX from 2,000,000 JPY.” (`decision-s1-capex`, gap `gap-x`)
