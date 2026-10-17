# Standard Operating Procedure: Requisition & Purchasing Lifecycle

**Document Reference**: SOP-PROC-002  
**Effective Date**: September 1, 2026  
**Applicability**: All Requisitioners, Approvers, and Receiving Staff  

---

## 1. Process Overview
The procurement lifecycle at Harbor Office Supply follows five consecutive milestones:
1. Requisition Creation & Submission
2. Policy-Based Route Evaluation
3. Authorization & Purchase Order (PO) Issuance
4. Physical Receiving & Goods Inspection
5. Accounts Payable 3-Way Matching

---

## 2. Requisition Submission
- Buyers and department employees initiate purchasing requests by specifying the active vendor ID, category (OPEX or CAPEX), net goods amount, quantity, and detailed item description.
- Amounts must reflect the exact expected supplier price in integer JPY.

---

## 3. Approval Workflow & Review Escalation
1. **Automated Routing**: Upon submission, the procurement system checks vendor standing, category, and dollar threshold to assign the required approval route.
2. **Review Execution**:
   - For requisitions routed to Managers, Finance, or Directors, the approver reviews item justification in the queue.
   - Self-approval is rejected by system controls.
3. **Operational Escalation Requirement**:
   - Requisitions that remain in a pending approval state for more than **2 business days** must be escalated manually to the next-level department head.
   - *Operating Note*: Reviewers and buyers are responsible for monitoring their queues manually, as the current system does not provide automated holiday calendars or background escalation triggers.

---

## 4. PO Issuance & Delivery
- Once authorized, the system issues a formal Purchase Order (PO) in `APPROVED` status.
- POs record standard freight charges and calculate standard 10% consumption tax.
- In exceptional circumstances, requisitions under review may be held in draft status; goods may not be accepted against draft orders.

---

## 5. Goods Receiving & Damaged Lots
- Warehouse and logistics staff inspect all deliveries upon physical arrival.
- Only goods meeting quality standards are recorded with condition `ACCEPTED`.
- Damaged, incomplete, or rejected lots are logged under condition `DAMAGED` and excluded from valid payable inventory quantities.
