"""checks/28 (R23, after the config 29 development). Pure: no DB, no model. Expected outcomes are written by hand from the fixture record (who holds what is
stated by the server section; the record names no Chief Risk Officer; later-step holders are recorded only when each step starts), never derived from the guard.
Covers blocks and limitations; affirmative and negative claims; passive voice; several clauses; a current, a historical and an absent actor; a role, a title and a
name; a business rule about other work items; the honest absences of checks/27 and the H7-25 wording."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_checks27 import P, Q_NAME, kept_limitations
from tests.unit.test_request_assistant_integrity import fid

CRO = ["Chief Risk Officer"]

# (sentence, refused as authority in a LIMITATION, refused as authority in a BLOCK) - labels from the fixture record
CASES = [
    # the false negative of the config 29 run: any actor with an obligation / permission / final-say predicate on this request or requests of its kind
    ("The Chief Risk Officer must approve changes like this.", True, True),
    ("Only the Chief Risk Officer can approve this request.", True, True),
    ("The Chief Risk Officer has the final word.", True, True),
    ("The Chief Risk Officer does not have the final say on this request.", True, True),              # a denial is a claim too
    ("Finance must sign off.", True, True),                                                            # a bare act closing the clause
    ("Someone in finance has to approve requests of this kind.", True, True),
    ("The Chief Risk Officer has no authority over this request.", True, True),
    ("The decision on this change is approved by the Chief Risk Officer.", True, True),                # passive, a non-role actor
    ("This change is approved by the Chief Risk Officer.", True, True),
    ("The Chief Risk Officer approves this request.", True, True),                                     # simple present, a title
    ("Hanako approved this request earlier.", True, True),                                             # a historical act by a name the record lacks
    ("The Operations Director approved this request earlier.", True, True),                            # a title the record holds in the rule, not as a decider
    ("The approver must decide this request.", True, True),                                            # a role word (checks/9, unchanged)
    ("Changes like this are decided by the approver, and the Chief Risk Officer must sign off too.", True, True),   # several clauses
    # explanations that claim nothing about who acts on this request
    ("The request proposes that high-risk supplier purchases be approved by the Finance Controller instead of the Operations Director.", False, False),
    ("Under the proposal the Finance Controller would have the final say on high-risk supplier purchases.", False, False),   # another object: a business rule
    ("If approved, a knowledge owner commits the change later.", False, False),                        # the recorded decision choices, a role
    ("Approving records the decision only; a knowledge owner commits later.", False, False),
    ("The assistant cannot approve this request; it only reads and explains.", False, False),           # a decline
    ("No decision has been recorded on this request yet.", False, False),
]


@pytest.mark.parametrize("text, in_limitation, in_block", CASES)
def test_who_acts_on_this_request_is_refused_for_any_actor_and_explanations_are_kept(text, in_limitation, in_block):
    kept, refused = kept_limitations([text], question=Q_NAME, unrecorded=[])
    assert (text not in kept and any(r["reason"] == "authority_in_free_text" for r in refused)) is in_limitation, (kept, refused)
    out = run(P, [("what the record shows", "next", [blk(text, [fid(P, "state")])])], question="What happens to it now?")
    block_refused = any(r.get("index") == 0 and r["reason"] in ra.AUTHORITY_TEXT_REASONS for r in out["rejected"])
    assert block_refused is in_block, out["rejected"]


# honest absences (checks/27 forms, with the H7-25 wording) and their near misses, as LIMITATIONS
HONEST = [
    ("The facts do not record who acts on later steps until each step starts.", (), True),                       # H7-25: the checks/11 form
    ("The facts do not show who will hold later steps, as that is recorded only when each step starts.", (), True),
    ("The facts do not show who participates in or signs off on later steps until each step starts.", (), True),
    ("The facts do not show a date for the approval.", (), True),                                                  # a time, no actor
    ("The record does not show whether a Chief Risk Officer has authority or a role in approving changes of this type.", CRO, True),   # the H7-31 sentence
    ("The facts do not show whether the Chief Risk Officer has the final word on this request.", CRO, True),
    ("The facts do not show who will act on later steps until each step starts, nor whether anyone moves the request on.", (), False),   # an open current actor
    ("The facts do not record who acts on the current step.", (), False),
    ("The record does not show whether a Chief Risk Officer has authority, and the approver must decide it.", CRO, False),  # a claim in another clause
    ("The record does not show whether a Chief Risk Officer has authority over this request.", (), False),       # name not verified as absent
    ("The facts do not show whether the Chief Risk Officer or the approver decides this.", CRO, False),          # a record role in the sentence
]


@pytest.mark.parametrize("text, unrecorded, kept_expected", HONEST)
def test_honest_absences_are_kept_whichever_unstated_form_caught_them_and_near_misses_are_refused(text, unrecorded, kept_expected):
    st = fid(P, "state")
    asks = [("whether the Chief Risk Officer decides", "premise", [blk("The current step is the knowledge decision.", [st])])] if unrecorded else None
    kept, refused = kept_limitations([text], question=Q_NAME, asks=asks, unrecorded=list(unrecorded))
    assert (text in kept) is kept_expected, (kept, refused)


def test_the_version_is_checks28_and_checks27_answers_stay_shown_as_stored():
    assert ra.CHECKS_VERSION == "request-assistant-checks/30" and {"request-assistant-checks/27", "request-assistant-checks/28", "request-assistant-checks/29"} <= ra.COMPATIBLE_CHECKS
