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
| Origin | did a model, a source record or the engine produce this? | correctness, a check result, approval |
| AI interpretation | what does the model say? | a decision |
| Coverage | what was and was not examined? | zero impact |
| Permission | may this person do the step? | being asked to do it (assignment / obligation) |
| Obligation state | current, waiting, blocked, future, done | the request's overall stage |

## Rules
- Business and Technical views show the same facts. Raw enum values stay in `data-state` and in the Technical view.
- Unknown, missing, not requested and zero are different. Show "Unavailable" or "Not measured", never 0 for a missing value.
- Proposed, observed, confirmed and accepted are different words; do not reuse one for another.
- `success` names its scope ("Job finished", "Source read-back aligned"); there is no universal "verified" badge.
- Source quotes and user statements are shown unaltered; copy adapters never paraphrase them.
- Critical blockers, consequences, paid-use consent, the current actor, stale or partial warnings and unknown-impact counts stay visible next to the relevant action; they are never truncated or clamped.
- Routine copy: a one-sentence purpose, explanations of about two lines, repeated list explanations grouped once. These are targets; the safety text above is exempt.
- Announce job transitions politely once; announce an actionable failure once; never announce every poll or timer tick.
- Model-written content carries the AI origin label (`OriginTag` / `OriginSection`); source observations and engine results carry their own. Origin is shown with text and icon, never colour alone, and never implies the content is correct.
- Evidence or ids that need rights are not disclosed through tooltips, hidden DOM or paraphrase.
- Request steps (task frame, whole-request view, My work, assistant) come from the one task projection: "your step" only where the person is asked now; "you may do this, but you are not asked to" where they only have the permission; a waiting step names the role pool or "the system", never "someone"; a step that cannot be placed or a list that could not be read is "unavailable", never empty.
- A timestamp says what it times: the task frame's "Task status read ..." is when the projection was read (`as_of`), not when a source or rule last changed; a missing timestamp is not replaced by the render clock.
- A count names its unit and scope ("request entries in the loaded list"); it is never presented as a workspace total unless the API states completeness.
- Empty, no match, failed, partial and denied are five different states (`EmptyState` kinds). A no-match offers to clear the filter; an error never reads as "nothing here"; a denied read never confirms that the item exists.
