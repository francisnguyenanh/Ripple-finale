# Content and states

## Dimensions that stay separate
| Dimension | Example question | Never conflate with |
|---|---|---|
| Job execution | did the AI or worker job finish? | accepted knowledge, correctness |
| Source reading and verification | does the source read-back match? | behaviour verified |
| Accepted knowledge | did a person accept this rule? | file written, code changed |
| Human approval | who approved, is it still current? | job completed |
| Result acceptance | did the user accept the outcome? | system success |
| Simulation measurement | what did the engine measure? | AI interpretation |
| AI interpretation | what does the model say? | a decision |
| Coverage | what was and was not examined? | zero impact |

## Rules
- Business and Technical views show the same facts. Raw enum values stay in `data-state` and in the Technical view.
- Unknown, missing, not requested and zero are different. Show "Unavailable" or "Not measured", never 0 for a missing value.
- Proposed, observed, confirmed and accepted are different words; do not reuse one for another.
- `success` names its scope ("Job finished", "Source read-back aligned"); there is no universal "verified" badge.
- Source quotes and user statements are shown unaltered; copy adapters never paraphrase them.
- Critical blockers, consequences, paid-use consent, the current actor, stale or partial warnings and unknown-impact counts stay visible next to the relevant action; they are never truncated or clamped.
- Routine copy: a one-sentence purpose, explanations of about two lines, repeated list explanations grouped once. These are targets; the safety text above is exempt.
- Announce job transitions politely once; announce an actionable failure once; never announce every poll or timer tick.
- Evidence or ids that need rights are not disclosed through tooltips, hidden DOM or paraphrase.
