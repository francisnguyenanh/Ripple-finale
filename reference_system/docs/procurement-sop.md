# Standard Operating Procedure — Procurement

**Document ID:** SOP-PROC-002
**Owner:** Procurement Operations
**Version:** 4.0
**Last reviewed:** 2021-06-30

---

## 1. Scope

Day-to-day steps for raising and approving a purchase request.

## 2. Raising a request

1. Open the procurement system and select **New Request**.
2. Enter supplier, amount (JPY), category and justification.
3. Attach a quotation where one exists.
4. Submit.

## 3. What happens next

The system routes the request automatically. You do not choose the approver.

## 4. Approval thresholds

| Amount (JPY) | Who approves |
|---|---|
| Below 1,000,000 | Automatic — no human approval |
| 1,000,000 and above | Department manager |
| 5,000,000 and above (capital) | Finance Controller |

> **Note.** The threshold in this table must match the approval matrix
> maintained by Finance (`data/approval_matrix.xlsx`). If you find they
> disagree, raise a ticket with Procurement Operations.

## 5. Escalation

A request that has waited more than two working days escalates to the next
level automatically.

## 6. Emergency purchases

Where a production line stoppage is imminent:

1. Raise the request with process type **Emergency**.
2. Emergency requests are fast-tracked and approved automatically.
3. Notify your manager the same day.

The emergency route has its own ceiling, above which a manager must approve.
**The current ceiling is set in the system configuration.**

## 7. Records

All decisions are written to the audit trail with the rule that produced them.
