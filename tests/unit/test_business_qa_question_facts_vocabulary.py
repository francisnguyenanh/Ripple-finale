"""The question reader reads hyphenated categories, "non-emergency" / "ordinary", and a stated NORMAL supplier risk (sealed-set v2 finding, 2026-10-23: the server asked
for facts the question had already given)."""
from core.change_intelligence.v2 import business_qa as qa


def facts(q):
    return qa.question_facts(q)["facts"]


def test_hyphenated_categories_and_normal_risk_are_read():
    f = facts("A standard operating-expense request for 3,000,000 JPY with an active supplier with normal risk.")
    assert f["request.category"] == "OPEX" and f["supplier.risk"] == "NORMAL" and f["process"] == "STANDARD"
    assert facts("a 7,500,000 JPY capital-equipment request with a supplier of normal risk")["request.category"] == "CAPEX"
    assert facts("a normal-risk supplier")["supplier.risk"] == "NORMAL"
    assert facts("the supplier is not on the risk register")["supplier.risk"] == "NORMAL"
    assert facts("the supplier is not flagged")["supplier.risk"] == "NORMAL"


def test_non_emergency_and_ordinary_are_standard_and_emergency_still_reads():
    assert facts("an ordinary (non-emergency) request")["process"] == "STANDARD"
    assert facts("An emergency OPEX requisition of 1,300,000 JPY")["process"] == "EMERGENCY"
    assert facts("a supplier on the HIGH-risk list")["supplier.risk"] == "HIGH"
    assert "supplier.risk" not in facts("Who approves a 1,200,000 JPY purchase?")
