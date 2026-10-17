# Procurement Audit Checklist

**Document ID:** CTRL-PROC-005
**Owner:** Internal Audit
**Version:** 2.0
**Last reviewed:** 2022-01-19

---

Use this checklist for the quarterly procurement controls review.

## Control PA-01 — Approval threshold enforced

- [ ] Sample 20 purchase orders from the period.
- [ ] Confirm every order **of 1,000,000 JPY or more** carries a manager approval record.
- [ ] Confirm no order below that amount was routed for approval unnecessarily.

## Control PA-02 — Approval matrix matches the system

- [ ] Open `data/approval_matrix.xlsx`.
- [ ] Confirm the threshold recorded there matches the workflow configuration.
- [ ] Record the date of the last change to each.

> Audit note, 2022-06: PA-02 was raised as an observation. The approval
> matrix was updated in June 2022; the workflow configuration was not
> checked at the time. Carried forward.

## Control PA-03 — Exceptions are registered

- [ ] Every exception applied in routing appears in the exception register.
- [ ] Every register entry has an owner and an end date.

## Control PA-04 — Emergency purchases are justified

- [ ] Sample 5 emergency purchases.
- [ ] Confirm each has a same-day manager notification.
