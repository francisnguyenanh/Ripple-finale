# -*- coding: utf-8 -*-
"""Semantic retrieval adapter (role `retrieval.semantic`): contract, validation, caps, cache and fallback. Pure, no I/O of its own; a provider object is injected.

Three separate layers, each with its own request, its own validated output and its own fallback, because an embedding or a model does not fix all three failure kinds:
  expand  - CANDIDATE EXPANSION: proposes extra query terms and extra candidate item ids. It can only ADD candidates, and only from the items it was offered.
  rank    - RANKING: scores the candidates it was offered. It can only reorder; it adds nothing.
  judge   - EVIDENCE SUFFICIENCY: says per offered item whether it supports the question, and whether the whole set is enough. It only annotates; it never removes
            counter-evidence, never adds an item, and "unknown" is the label when it did not run.
Rules that hold for every layer: ids must be among the offered ids (which belong to the pinned snapshot and the viewer's view); output keys and counts are bounded; a
violation discards that layer's output and labels the fallback; no provider output becomes evidence (items always come from the stored snapshot).
Providers here: `DisarmedProvider` (never calls anything), `ReplayProvider` (looks answers up in a SYNTHETIC fixture; it proves the pipeline, not model quality) and
`ScriptedProvider` (tests). A model-backed provider does not exist yet: it needs an approved model, price and caps.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Iterable, Mapping, Optional

from core.change_intelligence.v2.serialize import canonical_hash

CONFIG_VERSION = "semantic-retrieval-config-v1"
# How the bounded payload is chosen (part of the cache key, so a verdict judged on another selection is never reused). v1 (2026-10-25, F-1): the items the
# lexical / vector search already selected are offered first, in their relevance order, then the rest of the authorized corpus in id order while it fits; before
# v1 the corpus was cut in id order only, so an item the answer needed could be missing from what expand, rank and judge saw.
SELECTION_POLICY = "priority-candidates-v2"        # v2 (2026-10-25, continuation): rank / judge candidates composed by group within explicit budgets (below)
# The envelope every layer works in, derived from one set of numbers so the parts agree (continuation of the AI quality round, 2026-10-25):
#  * a layer's answer is at most PROVIDER_OUTPUT_TOKENS (the provider's max_output_tokens, which includes any thinking; the semantic role runs at MINIMAL); a
#    scores / labels answer costs about len(id) + ANSWER_CHARS_PER_ITEM characters per candidate plus a small wrapper, and the provider's tokenizer gives at least
#    OUTPUT_CHARS_PER_TOKEN characters per token on such JSON (observed cut points: 898-1,368 characters at 512 tokens, the lowest 1.75 per token), so rank / judge get only the candidates
#    whose answer fits;
#  * rank / judge inputs carry the candidates' text, at most half the expand payload (`max_payload_chars` / 2);
#  * the token total therefore stays below (payload + 2 x payload / 2) / INPUT_CHARS_PER_TOKEN + 3 x (instruction + output) — DEFAULT_CAPS["max_tokens"].
PROVIDER_OUTPUT_TOKENS = 512
OUTPUT_CHARS_PER_TOKEN = 1.7
ANSWER_CHARS_PER_ITEM = 18
ANSWER_WRAPPER_CHARS = 60
INPUT_CHARS_PER_TOKEN = 3.0
INSTRUCTION_TOKENS = 200
LAYERS = ("expand", "rank", "judge")
RELEVANCE_LABELS = ("supports", "partial", "unrelated")
SUFFICIENCY = ("sufficient", "partial", "insufficient")
DEFAULT_CAPS = {"max_calls": 3, "max_payload_chars": 12000, "max_terms": 8, "max_candidates": 10, "max_extra_candidates": 10, "min_lexical_candidates": 10, "timeout_seconds": 8.0, "max_retries": 1, "max_tokens": 12000,
                "max_term_chars": 40, "fail_closed_on_missing_usage": True}
SEMANTIC_WEIGHT = 0.3          # final score = 0.7 x lexical + 0.3 x rank score, only where the rank layer scored the item (fixed, not tuned)
TERM_WEIGHT = 0.5


class ProviderUnavailable(Exception):
    """The provider cannot answer (disarmed, no record, network)."""


class ProviderTimeout(ProviderUnavailable):
    """The provider did not answer in time."""


class InvalidOutput(ValueError):
    """The provider's answer violates the layer schema; `args[0]` is the reason."""


# ---- requests ----------------------------------------------------------------------------------------------------------------------

def offered_selection(items: Iterable[Mapping], *, max_chars: int, priority: Iterable[str] = ()) -> tuple:
    """(offered, omitted_ids): id, kind and business text of the items the viewer may read, within `max_chars`. The `priority` ids (the candidates the lexical /
    vector search already selected, most relevant first) come first, then every other item in (kind, id) order. Whole records only: an item that does not fit is
    left out and listed, and smaller items after it may still fit. Never source quotes, locators, hashes or evidence refs."""
    items = list(items)
    by_id = {i["item_id"]: i for i in items}
    order, seen = [], set()
    for iid in priority:
        if iid in by_id and iid not in seen:
            order.append(by_id[iid])
            seen.add(iid)
    order += [i for i in sorted(items, key=lambda i: (i["kind"], i["item_id"])) if i["item_id"] not in seen]
    out, omitted, used = [], [], 0
    for it in order:
        entry = {"item_id": it["item_id"], "kind": it["kind"], "text": it["text"]}
        cost = len(entry["text"]) + len(entry["item_id"]) + 24
        if used + cost > max_chars:
            omitted.append(it["item_id"])
            continue
        out.append(entry)
        used += cost
    return out, omitted


def offered_items(items: Iterable[Mapping], *, max_chars: int, priority: Iterable[str] = ()) -> tuple:
    """(offered, truncated) — `offered_selection` with a yes / no for "something was left out"."""
    out, omitted = offered_selection(items, max_chars=max_chars, priority=priority)
    return out, bool(omitted)


def worst_case_tokens(caps: Mapping) -> int:
    """The most tokens the three layers can use inside the envelope above (inputs at the payload bounds, every answer at the output limit)."""
    inputs = (caps["max_payload_chars"] + 2 * (caps["max_payload_chars"] // 2)) / INPUT_CHARS_PER_TOKEN
    return int(inputs + 3 * (INSTRUCTION_TOKENS + PROVIDER_OUTPUT_TOKENS))


def layer_candidates(lexical: Iterable[str], protected: Iterable[str], expanded: Iterable[str], offered_by_id: Mapping[str, Mapping], caps: Mapping) -> tuple:
    """(candidates, omitted) for rank / judge. Groups in this order, each in its own relevance order: the first `min_lexical_candidates` of the pack's lexical /
    vector candidates, then the protected ids (counter-evidence of the selected rules), then the expansion's new candidates, then the rest of the lexical ones.
    An id joins only while the layer's answer still fits the provider's output limit (id length + JSON per item) and the candidates' text fits half the payload,
    up to `max_candidates + max_extra_candidates`. `omitted` counts what each group lost: {lexical, protected, expanded}."""
    lexical = [i for i in dict.fromkeys(lexical) if i in offered_by_id]
    protected = [i for i in dict.fromkeys(protected) if i in offered_by_id and i not in lexical[: caps["min_lexical_candidates"]]]
    expanded = [i for i in dict.fromkeys(expanded) if i in offered_by_id and i not in lexical and i not in protected]
    head, tail = lexical[: caps["min_lexical_candidates"]], [i for i in lexical[caps["min_lexical_candidates"]:] if i not in protected]
    order = [("lexical", i) for i in head] + [("protected", i) for i in protected] + [("expanded", i) for i in expanded] + [("lexical", i) for i in tail]
    out_budget = PROVIDER_OUTPUT_TOKENS * OUTPUT_CHARS_PER_TOKEN - ANSWER_WRAPPER_CHARS
    in_budget = caps["max_payload_chars"] // 2
    limit = caps["max_candidates"] + caps["max_extra_candidates"]
    chosen, omitted, out_used, in_used = [], {"lexical": 0, "protected": 0, "expanded": 0}, 0, 0
    for group, iid in order:
        out_cost = len(iid) + ANSWER_CHARS_PER_ITEM
        in_cost = len(offered_by_id[iid]["text"]) + len(iid) + 24
        if len(chosen) >= limit or out_used + out_cost > out_budget or in_used + in_cost > in_budget:
            omitted[group] += 1
            continue
        chosen.append(iid)
        out_used += out_cost
        in_used += in_cost
    return chosen, omitted


def build_request(layer: str, query: str, offered: list, *, candidates: Optional[list] = None) -> dict:
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {LAYERS}")
    by_id = {i["item_id"]: i for i in offered}
    req = {"layer": layer, "query": query, "items": offered if layer == "expand" else [by_id[c] for c in dict.fromkeys(candidates or []) if c in by_id]}   # relevance order
    req["request_hash"] = canonical_hash({"layer": layer, "query": query, "items": [(i["item_id"], i["text"]) for i in req["items"]]})
    return req


# ---- output validation -------------------------------------------------------------------------------------------------------------

def _keys(obj, allowed, what):
    if not isinstance(obj, Mapping):
        raise InvalidOutput(f"{what}: not an object")
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise InvalidOutput(f"{what}: unknown keys {extra}")


def _number(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0.0 <= float(v) <= 1.0:
        raise InvalidOutput(f"{what}: not a number from 0 to 1")
    return float(v)


def validate_expand(resp, offered_ids: set, caps: Mapping) -> dict:
    _keys(resp, ("terms", "candidate_ids"), "expand")
    terms, ids = resp.get("terms", []), resp.get("candidate_ids", [])
    if not isinstance(terms, list) or not isinstance(ids, list):
        raise InvalidOutput("expand: terms and candidate_ids must be lists")
    if len(ids) > caps["max_candidates"]:
        raise InvalidOutput("expand: over the term or candidate cap")
    # search terms are not evidence (they only widen the lexical query): a malformed or surplus term is dropped and counted, never allowed in, and it no longer
    # discards the grounded candidate ids beside it (acceptance run AIQ-ACC-QA-20261025: 4 of 12 expand answers were thrown away for one term, 2026-10-25)
    good = [t for t in terms if isinstance(t, str) and t.strip() and len(t) <= caps["max_term_chars"] and not any(c in t for c in "\n\r\x00")]
    kept_terms = good[: caps["max_terms"]]
    for i in ids:
        if not isinstance(i, str) or i not in offered_ids:
            raise InvalidOutput("expand: a candidate id was not offered")
    if len(set(ids)) != len(ids):
        raise InvalidOutput("expand: duplicate candidate id")
    return {"terms": [t.strip() for t in kept_terms], "candidate_ids": list(ids), "terms_dropped": len(terms) - len(kept_terms)}


def validate_rank(resp, offered_ids: set, caps: Mapping) -> dict:
    _keys(resp, ("scores",), "rank")
    scores = resp.get("scores")
    if not isinstance(scores, Mapping) or len(scores) > caps["max_candidates"] + caps.get("max_rank_extra", 40):
        raise InvalidOutput("rank: scores must be an object within the cap")
    out = {}
    for k, v in scores.items():
        if k not in offered_ids:
            raise InvalidOutput("rank: a scored id was not offered")
        out[k] = _number(v, "rank score")
    return {"scores": out}


def validate_judge(resp, offered_ids: set, caps: Mapping) -> dict:
    _keys(resp, ("labels", "sufficiency"), "judge")
    labels, suff = resp.get("labels"), resp.get("sufficiency")
    if suff not in SUFFICIENCY or not isinstance(labels, Mapping):
        raise InvalidOutput("judge: sufficiency or labels invalid")
    for k, v in labels.items():
        if k not in offered_ids or v not in RELEVANCE_LABELS:
            raise InvalidOutput("judge: a label names an id that was not offered or an unknown label")
    return {"labels": dict(labels), "sufficiency": suff}


VALIDATORS = {"expand": validate_expand, "rank": validate_rank, "judge": validate_judge}


# ---- providers ----------------------------------------------------------------------------------------------------------------------

class Provider:
    """Interface: `call(layer, request, timeout)` -> (response mapping, usage or None). `external` says whether it reaches a service outside the process."""
    provider_id = "none"
    config_version = CONFIG_VERSION
    external = False
    synthetic = False

    def call(self, layer: str, request: Mapping, timeout: float):
        raise NotImplementedError


class DisarmedProvider(Provider):
    provider_id = "disarmed"

    def call(self, layer, request, timeout):
        raise ProviderUnavailable("disarmed")


class ReplayProvider(Provider):
    """Answers from a fixture keyed by `request_hash`. The fixture is SYNTHETIC (written by the test author), so this proves the contract and the pipeline, not what a model does."""
    provider_id = "synthetic-replay"
    synthetic = True

    def __init__(self, records: Mapping[str, Mapping], *, usage: Optional[Mapping] = None):
        self.records, self.usage, self.calls = dict(records), usage, 0

    def call(self, layer, request, timeout):
        self.calls += 1
        rec = self.records.get(request["request_hash"])
        if rec is None:
            raise ProviderUnavailable("no_replay_record")
        return rec, (dict(self.usage) if self.usage is not None else None)


class ScriptedProvider(Provider):
    """Test double: `fn(layer, request, timeout)` returns a response or (response, usage), or raises."""
    provider_id = "scripted"
    synthetic = True

    def __init__(self, fn: Callable, *, external: bool = False, provider_id: str = "scripted", config_version: str = CONFIG_VERSION):
        self.fn, self.external, self.provider_id, self.config_version, self.calls = fn, external, provider_id, config_version, 0

    def call(self, layer, request, timeout):
        self.calls += 1
        out = self.fn(layer, request, timeout)
        return out if isinstance(out, tuple) else (out, {"input_tokens": 0, "output_tokens": 0})


# ---- cache --------------------------------------------------------------------------------------------------------------------------

class SemanticCache:
    """Bounded in-process cache. The key carries everything the answer depends on, so a different query, snapshot, view, access scope, provider or config never shares an entry.
    Access is still checked on every request by the route; the key only keeps scopes apart."""

    def __init__(self, maxsize: int = 256):
        self.maxsize, self.data, self.hits, self.misses = maxsize, OrderedDict(), 0, 0

    @staticmethod
    def key(*, query: str, snapshot_id, view: str, index_version: str, access_scope: str, provider: Provider, caps: Mapping) -> str:
        return canonical_hash({"q": " ".join(query.lower().split()), "snapshot": snapshot_id, "view": view, "index": index_version, "access": access_scope,
                               "provider": provider.provider_id, "config": provider.config_version, "caps": dict(caps), "selection": SELECTION_POLICY})

    def get(self, key):
        if key in self.data:
            self.data.move_to_end(key)
            self.hits += 1
            return self.data[key]
        self.misses += 1
        return None

    def put(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)


# ---- the adapter --------------------------------------------------------------------------------------------------------------------

class SemanticAdapter:
    def __init__(self, provider: Optional[Provider] = None, *, caps: Optional[Mapping] = None, cache: Optional[SemanticCache] = None,
                 gate: Optional[Callable[[], bool]] = None, clock: Callable[[], float] = time.monotonic):
        self.provider = provider or DisarmedProvider()
        self.caps = {**DEFAULT_CAPS, **(caps or {})}
        self.cache = cache if cache is not None else SemanticCache()
        self.gate = gate or (lambda: False)          # True = the provider switch is off; an external provider is then not called
        self.clock = clock

    def _call(self, layer, request, state):
        """One layer: bounded attempts, validated output; returns the validated output or raises InvalidOutput/ProviderUnavailable."""
        caps, provider = self.caps, self.provider
        if provider.external and self.gate():
            raise ProviderUnavailable("provider_switch_off")
        last = None

        def cap_reached(cause=None):
            # the cap stops a retry: the FIRST failure (why a retry was wanted at all) stays visible on the error, never masked by the cap
            exc = ProviderUnavailable("call_cap_reached")
            if last is not None:
                exc.first_reason, exc.detail = str(last), getattr(last, "detail", None)
            if cause is not None:
                exc.__cause__ = cause
            return exc

        for _attempt in range(1 + caps["max_retries"]):
            if state["calls"] >= caps["max_calls"]:
                raise cap_reached()
            budget_left = state["budget"].remaining() if state.get("budget") is not None and hasattr(state["budget"], "remaining") else None
            left = caps["max_calls"] - state["calls"] if budget_left is None else min(caps["max_calls"] - state["calls"], budget_left)
            if last is not None and left - 1 < state.get("layers_after", 0):
                raise last                                       # a retry never takes the call a later layer needs (the judge is the one that can say "insufficient")
            budget = state.get("budget")
            if budget is not None:                               # the job's own per-attempt call budget: reserved and journaled BEFORE the call, never after
                try:
                    budget.consume()
                except Exception as exc:                         # ProviderCallCeilingExceeded or a journal refusal: no call is made
                    raise cap_reached(exc)
            state["calls"] += 1
            state["attempts"][layer] = state["attempts"].get(layer, 0) + 1
            if last is not None:                                 # a retry: why it was needed stays visible even when it then succeeds (it used a slot of a later layer)
                state["retried_after"][layer] = {"reason": str(last), **({"detail": last.detail} if getattr(last, "detail", None) else {})}
            started = self.clock()
            try:
                response, usage = provider.call(layer, request, caps["timeout_seconds"])
            except ProviderUnavailable as exc:
                last = exc
                if str(exc) in ("disarmed", "no_replay_record"):
                    raise                                  # retrying cannot help
                continue
            late = self.clock() - started > caps["timeout_seconds"]
            # usage is booked BEFORE a late answer is discarded: the provider charged for it (live finding FNL-LIVE-20261018-03, a discarded expand call went unbooked)
            if usage is None and caps["fail_closed_on_missing_usage"]:
                raise ProviderUnavailable("usage_unknown")
            if usage is not None:
                state["tokens_in"] += int(usage.get("input_tokens", 0))
                state["tokens_out"] += int(usage.get("output_tokens", 0))
                state["tokens"] += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                if state["tokens"] > caps["max_tokens"]:
                    raise ProviderUnavailable("token_cap_reached")
            if late:
                last = ProviderTimeout("timeout")
                continue
            return response
        raise last or ProviderUnavailable("unavailable")

    @staticmethod
    def _attempts(layer, state) -> dict:
        """Calls this layer made and, after a retry, the reason of the failure that caused it (safe words only); empty for a single clean call."""
        n = state["attempts"].get(layer, 0)
        out = {"attempts": n} if n > 1 or layer in state["retried_after"] else {}
        if layer in state["retried_after"]:
            out["retried_after"] = state["retried_after"][layer]
        return out

    def run(self, query: str, items: list, *, snapshot_id, view: str, index_version: str, access_scope: str, lexical_candidates: Iterable[str] = (),
            budget=None, allow_external: bool = False, protected: Iterable[str] = ()) -> dict:
        """Run the three layers over `items` (the viewer's items of the pinned snapshot). Always returns a labelled result; never raises for provider trouble.
        An EXTERNAL provider is called only inside a provider-backed job that passes its per-attempt `budget` (`allow_external=True`): a read route never pays."""
        key = self.cache.key(query=query, snapshot_id=snapshot_id, view=view, index_version=index_version, access_scope=access_scope, provider=self.provider, caps=self.caps)
        cached = self.cache.get(key)
        if cached is not None:
            return dict(cached, cache="hit", calls=0, tokens=0, tokens_in=0, tokens_out=0)
        result = {"status": "fallback", "provider": self.provider.provider_id, "config_version": self.provider.config_version, "synthetic": self.provider.synthetic,
                  "layers": {l: {"status": "not_run"} for l in LAYERS}, "terms": [], "candidate_ids": [], "scores": {}, "labels": {}, "sufficiency": "unknown", "calls": 0,
                  "tokens": 0, "tokens_in": 0, "tokens_out": 0, "cache": "miss", "payload_truncated": False,
                  "coverage": {"policy": SELECTION_POLICY, "corpus": len(items), "offered": 0, "omitted": 0, "judged_ids": [], "judge_digest": None,
                               "layer_candidates": {}}}
        if isinstance(self.provider, DisarmedProvider):
            for l in LAYERS:
                result["layers"][l] = {"status": "fallback", "reason": "disarmed"}
            return result                                    # not cached: arming must take effect at once
        if self.provider.external and self.gate():
            for l in LAYERS:
                result["layers"][l] = {"status": "fallback", "reason": "provider_switch_off"}
            return result                                    # the switch is off: nothing external is called, whatever the caller
        if self.provider.external and not (allow_external and budget is not None):
            for l in LAYERS:
                result["layers"][l] = {"status": "fallback", "reason": "outside_budget_boundary"}
            return result                                    # a paid provider runs only under a job reservation and call budget
        lexical_candidates, protected = list(lexical_candidates), list(protected)
        offered, omitted = offered_selection(items, max_chars=self.caps["max_payload_chars"], priority=lexical_candidates + protected)
        result["payload_truncated"] = bool(omitted)
        result["coverage"].update(offered=len(offered), omitted=len(omitted))
        offered_ids = {i["item_id"] for i in offered}
        state = {"calls": 0, "tokens": 0, "tokens_in": 0, "tokens_out": 0, "budget": budget if self.provider.external else None, "attempts": {}, "retried_after": {}}
        stop = None
        # layer 1: candidate expansion
        out = {}
        for layer in LAYERS:
            if stop:
                result["layers"][layer] = {"status": "fallback", "reason": stop}
                continue
            if layer == "expand":
                req = build_request("expand", query, offered)
            else:
                # the pack's own candidates in relevance order, then the expansion's; bounded so the layer's answer fits the provider's output limit (512
                # tokens hold about 30 scores or labels: acceptance run AIQ-ACC-QA-20261025 cut 10 / 12 rank answers at MAX_TOKENS with 50 candidates in id order)
                offered_by_id = {i["item_id"]: i for i in offered}
                cands, lost = layer_candidates(lexical_candidates, protected, result["candidate_ids"], offered_by_id, self.caps)
                result["coverage"]["layer_candidates"][layer] = {"count": len(cands), "omitted": lost}
                if not cands:
                    result["layers"][layer] = {"status": "skipped", "reason": "no_candidates"}
                    continue
                req = build_request(layer, query, offered, candidates=cands)
            state["layers_after"] = len([l for l in LAYERS[LAYERS.index(layer) + 1:]])
            try:
                raw = self._call(layer, req, state)
                valid = VALIDATORS[layer](raw, {i["item_id"] for i in req["items"]} if layer != "expand" else offered_ids, self.caps)
            except InvalidOutput as exc:
                result["layers"][layer] = {"status": "fallback", "reason": "invalid_output", "detail": str(exc), **self._attempts(layer, state)}
                continue
            except ProviderUnavailable as exc:
                reason = str(exc) or "unavailable"
                result["layers"][layer] = {"status": "fallback", "reason": reason, **self._attempts(layer, state)}
                for extra in ("first_reason", "detail"):          # safe diagnostics only (reason words, exception class, sizes): no prompt, no response text
                    if getattr(exc, extra, None):
                        result["layers"][layer][extra] = getattr(exc, extra)
                if reason in ("call_cap_reached", "token_cap_reached", "usage_unknown", "provider_switch_off", "disarmed"):
                    stop = reason
                continue
            result["layers"][layer] = {"status": "applied", **self._attempts(layer, state)}
            if layer == "expand":
                result["terms"], result["candidate_ids"] = valid["terms"], valid["candidate_ids"]
                if valid.get("terms_dropped"):
                    result["layers"][layer]["terms_dropped"] = valid["terms_dropped"]
            elif layer == "rank":
                result["scores"] = valid["scores"]
            else:
                result["labels"], result["sufficiency"] = valid["labels"], valid["sufficiency"]
                # what the judge actually received: its verdict covers these items (same snapshot text, digest of the request), nothing else
                result["coverage"].update(judged_ids=sorted(i["item_id"] for i in req["items"]), judge_digest=req["request_hash"])
        result["calls"], result["tokens"], result["tokens_in"], result["tokens_out"] = state["calls"], state["tokens"], state["tokens_in"], state["tokens_out"]
        applied = [l for l, v in result["layers"].items() if v["status"] == "applied"]
        result["status"] = "applied" if len(applied) == len([l for l in LAYERS if result["layers"][l]["status"] != "skipped"]) and applied else ("partial" if applied else "fallback")
        if result["status"] == "applied":                  # only a fully applied answer is reused; a degraded one is retried next time
            self.cache.put(key, result)
        return result
