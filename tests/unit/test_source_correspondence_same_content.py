"""SUR-05 guard: a `moved_same_content` proposal whose complete old and new texts differ is refused (the server owns that exact fact); identical bytes, a
whitespace-only difference, or a cut excerpt (the server cannot see the whole text) keep the proposal for a person to judge."""
from core.change_intelligence.v2 import source_correspondence as sc

LIMITS = {"max_pairs": 10, "max_rationale_chars": 300, "max_notes": 3}
OLD = "Purchases from 1,000,000 JPY need manager approval.\nNew software subscriptions need a security review."


def _pair(relation="moved_same_content"):
    return {"pairs": [{"old_path": "policy.md", "new_path": "policy.md", "relation": relation, "rule_ids": [], "old_quote": "New software subscriptions need a security review",
                       "new_quote": "New software subscriptions need a security review", "rationale": "The text continues unchanged.", "confidence": "high"}], "notes": []}


def _sides(new_text, *, old_hash="h-old", new_hash="h-new", max_chars=4000):
    return ([sc.side("policy.md", OLD, hash_=old_hash, max_chars=max_chars)], [sc.side("policy.md", new_text, hash_=new_hash, max_chars=max_chars)])


def test_same_content_is_refused_when_the_complete_texts_differ():
    olds, news = _sides(OLD.replace("1,000,000", "1,500,000"))
    r = sc.check(_pair(), olds, news, LIMITS)
    assert not r["kept"] and r["rejected"][0]["reason"] == "same_content_contradicted_by_text"


def test_identical_bytes_whitespace_only_or_cut_excerpts_keep_the_proposal():
    assert sc.check(_pair(), *_sides(OLD, new_hash="h-old"), LIMITS)["kept"]
    assert sc.check(_pair(), *_sides(OLD.replace("\n", "\n\n")), LIMITS)["kept"]
    cut = _sides(OLD.replace("security review", "security and legal review"), max_chars=60)       # the change lies beyond the cut excerpt
    first = _pair()
    first["pairs"][0].update(old_quote="Purchases from 1,000,000 JPY need manager approval", new_quote="Purchases from 1,000,000 JPY need manager approval")
    assert sc.check(first, *cut, LIMITS)["kept"]                                                 # the server cannot see the whole text: it does not decide


def test_other_relations_are_not_affected():
    olds, news = _sides(OLD.replace("1,000,000", "1,500,000"))
    assert sc.check(_pair("obligation_changed"), olds, news, LIMITS)["kept"]
