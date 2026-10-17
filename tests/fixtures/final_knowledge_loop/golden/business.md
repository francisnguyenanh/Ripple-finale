# Business knowledge

## Overview

This is the business knowledge as an accepted version 4. It holds 6 rules, 1 of them decided by people.
4 question(s) are open (1 important) and 0 are left open on purpose. The knowledge is therefore partial.

## Rules

### CAPEX of 2,000,000 JPY or more goes to Finance (configuration and code)

- Rule ID: `rule-capex-finance-config`
- Status: What the sources say, not confirmed by a person
- Who acts: Finance
- When it applies: request category is CAPEX and request net amount jpy at least 2000000 JPY
- What happens: review required
- Exceptions: None recorded
- Applies to: process: STANDARD, EMERGENCY
- Where this comes from: Stated in: approval.yaml, routing.py
- In the files and code: Matches what the sources show

### CAPEX of 5,000,000 JPY or more goes to Finance Controller (policy and manager guide)

- Rule ID: `rule-capex-finance-policy`
- Status: What the sources say, not confirmed by a person
- Who acts: Finance
- When it applies: request category is CAPEX and request net amount jpy at least 5000000 JPY
- What happens: review required
- Exceptions: None recorded
- Applies to: process: STANDARD
- Where this comes from: Stated in: manager-guide.md, procurement-policy.md
- In the files and code: Nothing to compare (this rule describes the sources)

### CAPEX of 2,000,000 JPY or more is reviewed by Finance

- Rule ID: `rule-capex-finance-target`
- Status: Decided by a person
- Who acts: Finance
- When it applies: request category is CAPEX and request net amount jpy at least 2000000 JPY
- What happens: review required
- Exceptions: None recorded
- Applies to: process: STANDARD, EMERGENCY
- Where this comes from: Decided by a person: “Finance reviews CAPEX from 2,000,000 JPY.” | Related sources: approval.yaml
- In the files and code: Partly implemented

### Emergency purchases

- Rule ID: `rule-emergency-routing`
- Status: What the sources say, waiting for a person
- Who acts: Not stated in the sources
- When it applies: Not stated in the sources
- What happens: Not stated in the sources
- Exceptions: None recorded
- Applies to: process: EMERGENCY
- Where this comes from: Stated in: emergency.py, manager-guide.md
- In the files and code: Differs from what the sources show
- Not known: actor, condition

### Purchases from high-risk suppliers go to the Operations Director regardless of amount

- Rule ID: `rule-high-risk-director`
- Status: What the sources say, not confirmed by a person
- Who acts: Director
- When it applies: supplier risk is HIGH
- What happens: review required
- Exceptions: None recorded
- Applies to: process: STANDARD, EMERGENCY
- Where this comes from: Stated in: procurement-policy.md
- In the files and code: Matches what the sources show

### Purchases of 1,000,000 JPY or more need manager approval

- Rule ID: `rule-standard-manager-review`
- Status: What the sources say, not confirmed by a person
- Who acts: Manager
- When it applies: request net amount jpy at least 1000000 JPY
- What happens: review required
- Exceptions: None recorded
- Applies to: process: STANDARD
- Where this comes from: Stated in: approval.yaml, procurement-policy.md
- In the files and code: Matches what the sources show

## Where the sources disagree

- “CAPEX of 2,000,000 JPY or more goes to Finance (configuration and code)” and “CAPEX of 5,000,000 JPY or more goes to Finance Controller (policy and manager guide)” disagree. A person has to decide which one is the business rule.

## What is not known yet

- How exactly does the rule "Emergency purchases" decide when it applies?
- What happens when the rule "Emergency purchases" applies?
- The sources disagree about request.net\_amount\_jpy: "CAPEX of 2,000,000 JPY or more goes to Finance (configuration and code)" says 2000000, "CAPEX of 5,000,000 JPY or more goes to Finance Controller (policy and manager guide)" says 5000000. Which one is the business rule? (important)
- Who acts on the rule "Emergency purchases"?

## Files and code compared with the decided rules

- CAPEX of 2,000,000 JPY or more is reviewed by Finance: Partly implemented

The source files and code are never changed by this knowledge; a person changes them.

## Decisions people made

- “Finance reviews CAPEX from 2,000,000 JPY.”
