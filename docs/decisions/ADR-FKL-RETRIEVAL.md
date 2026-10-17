# ADR-FKL-RETRIEVAL — how business questions find business knowledge

Status: ACCEPTED for the deterministic method; **learned embeddings are NOT adopted and NOT claimed**. Date: 2026-10-16.

## Question

Business questions are phrased differently from the rules they need ("who has to sign off" for "manager approval") and sometimes need two rules at once (a CAPEX amount and a risky supplier). The existing index scores plain term frequency. Which method retrieves the right rules, decisions and counter-evidence, and what can honestly be claimed about it?

## Options

| Option | What it needs | Provable now | Decision |
|---|---|---|---|
| A. Term frequency only (the existing `termfreq-v1`) | nothing | baseline | kept as the measured baseline, not as the method |
| B. Normalized tokens + bounded expansion from a generic business lexicon + graph neighbors that disagree with a hit | a lexicon file, no model | yes, with the fixed Q-set | **adopted** |
| C. Learned embeddings or a model-written query expansion | an embedding/model provider, budget, price, usage records, cache | no: no model and no budget are approved | **not adopted**; the slot exists (`retrieval.semantic` role in `config/ai_closed_loop/final_budgets.example.yaml`, `embedding_version` and `learned_embeddings` fields in every pack) and stays empty |

## Decision

Method id `lexicon-expansion+tf-cosine+version-grouping+graph`, index version `business-index-v2/tf-normalized`:

1. Tokens: lower case, numbers without separators ("1.2 million" becomes 1200000), stop words dropped, light stemming.
2. Score = 0.5 x lexical coverage + 0.5 x cosine, on the query's tokens plus at most 4 lexicon neighbors per token and 24 in all, each weighted 0.5. The lexicon (`config/business_lexicon.json`) is generic business vocabulary with no company, product or rule names; its sha256 is stamped into every pack.
3. Versions: rules that say the same thing (same actor, condition, outcome, exceptions, scope), such as a decided rule and the observed rule it was copied from, share one slot and stay listed with their authority; a conflicting statement is a separate entry.
4. Graph: rules that conflict with one of the three best rule hits are added (via "graph", score 0, at most five), so contradicting evidence is not lost when it does not share words with the query.
5. Fail-safe: if the business index is not READY for the current version, the pack is read directly from the stored snapshot and says so; the method does not change.
6. An item that shares no term with the query is never returned; no relevant evidence returns no items; nothing is added to fill `top_k`.

The pack always states `learned_embeddings: false`, `embedding_version: null`, the lexicon version and hash, the query tokens and the expanded terms. Calling this "semantic embeddings" is wrong.

## Evidence (fixed Q-set, expectations written first)

`tests/fixtures/final_knowledge_loop/retrieval_expected.json` (12 questions: 4 paraphrases, 2 multi-hop, 2 exception/scope, 2 unknown/conflict, 2 current-versus-target) over 20 rules (6 fixture rules, 1 decided target, 14 distractors); a question passes when its required rules are in the first four rules returned and no forbidden rule is.

| Method | Passed | Paraphrases | 2-hop |
|---|---|---|---|
| A. term frequency (old tokenizer), 6 rules, window 6 | 10 of 12 | - | - (set too easy: see revision below) |
| A. term frequency, 20 rules, window 4 | 5 of 12 | 2 of 4 | 0 of 2 |
| B (lexicon + graph neighbors) | 9 of 12 | 4 of 4 | 0 of 2 |
| B + version grouping, adopted | 10 of 12 | 4 of 4 | 1 of 2 |

A separate validation set of 8 questions in another domain (written before the rework and not used to tune it) scores 4 of 8 for both B and the adopted method: its paraphrase about holiday versus leave has no shared term, one multi-rule question ranks behind generic matches, and the two no-evidence questions still return loosely related rules.

Misses of the adopted method: Q06 (2-hop), Q10 (an unknown question still ranks loosely related rules). They are kept visible as strict expected failures in `tests/final_loop/test_qry02_retrieval.py`. The first version of the set had 6 rules and a window of 6, so the plain baseline passed 10 of 12 without discriminating anything; 14 distractor rules were added and the window cut to 4 before the adopted method was tuned. After the first measurement of B (9 of 12) two further changes were tried on this same set and rejected because they scored lower or did not help: a coverage re-ranking (8 of 12) and a relative relevance floor (not kept with it). Nothing else was tuned.

## Consequences

- Paraphrases of the kind in the set are retrieved; 2-hop questions that need two unrelated rules are **not** reliably retrieved by this method. Answers built on a pack must say what they could not find (QRY-03) and must not treat an empty or partial pack as proof of absence.
- A learned or model-assisted method can be added behind a new `embedding_version` without removing this one, once the owner approves a specific model, price and budget, and it must be compared on unseen paraphrases (REL-03) before any claim.
- The sets are development and validation sets written by the task author. 10 of 12 and 4 of 8 are not accuracy statistics, and 4 of 8 on the validation set says the method is not yet adequate for the vocabulary gap and for abstention.

## Addendum: the semantic slot now has an adapter, and no model behind it

Option C is implemented as a seam only (contract section 12 of the knowledge-loop contract): three independent layers (candidate expansion, ranking, evidence sufficiency), validation, caps, cache and labelled fallback, verified with synthetic providers. This proves the pipeline, not that a model improves any of the three layers. The method that is actually in force is still the one decided above; the six recorded retrieval misses are unchanged. A model-backed provider needs an approved model, verified price and hard caps first.
