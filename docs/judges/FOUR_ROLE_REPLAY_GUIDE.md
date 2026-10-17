# Four-role workflow (optional) - not yet available

> **Starting point.** This walkthrough uses a specific starting baseline. If earlier changes have been applied in this workspace, the sample values and expected results may no longer match. Check the current source revision and values before starting. For subsequent runs, follow the same workflow using the latest data.

**Status: not yet available.** There is no workspace, account or allowance for a hands-on four-role run yet, and this page does not give you one. It describes what the run will involve so you can see what is planned. Until it is announced as available, use the [one-role demo](SINGLE_ROLE_GUIDE.md) (recommended) and the [recorded case](VERIFIED_FOUR_ROLE_CASE.md) (read-only evidence).

## What it will be
A hands-on run, on a workspace **separate** from the one-role demo, in which four different accounts take one change from request to an applied change: a **requester**, a **knowledge owner**, a **reviewer** and an **approver**. Applying changes the source of that workspace, so it is kept away from the demo workspace and the one-role demo is not affected.

| Person | What they do |
|---|---|
| Requester | states the change, confirms it, asks for the draft and the analysis, builds the proposal, runs the review, prepares the approval bundle and applies the approved change |
| Knowledge owner | decides open points and accepts the updated knowledge |
| Reviewer | reads the proposal and its evidence and may add a note |
| Approver | states the check that must hold after the change, approves, and accepts the result |

Nobody can approve their own request; the server checks that.

## Guided example and continued exploration
- **Guided example:** one specific starting value and request, as in the [one-role guide](SINGLE_ROLE_GUIDE.md). Use it when the workspace is still at that starting value.
- **Continued exploration:** after someone has applied a change, the workspace starts from the newest data. Follow the same workflow with the current value and a new request; the numbers will differ from any example. Nothing is reset between runs, and earlier requests, decisions and history stay visible.

## Starting a further run (when available)
1. Read the current value first (**Explorer**, then the rule; its **Value** and **Taken from** tell you what it is now). Technical view shows the current source revision.
2. Open **Changes** from another page, so the conversation is new. Your own earlier requests are listed in **My work**; do not open someone else's.
3. Write the request with the **current** value and the new value you want. Check the AI's preview ("This is what I understood") before confirming.
4. Read the simulation of this run as it is; do not expect numbers from an earlier run.
5. A draft or proposal made before the source changed is marked out of date. Ask for a new draft instead of using it.
6. **Where it is waiting:** the **Next** card at the top of the Changes page and the row **Where this change is** name the person who has to act next.

## Follow the recorded case in the meantime (read-only)
With a judge account you can read and follow the evidence of [the recorded case](VERIFIED_FOUR_ROLE_CASE.md); you do not repeat its approve and apply steps.

| To follow | Where | What to check |
|---|---|---|
| The request and the draft | Changes, with the ids from the case (draft, intent) | One supported edit; open points listed; carried items marked with their origin |
| The decision on the open point | Draft stage, *Clarifications used* | The person's own words kept exactly; files named with role; "Recorded by the server" |
| The measures | stage 3 *Impact and simulation* | 240 cases, baseline reproduced 240/240; the AI reading is labelled as interpretation |
| The proposal | stage 4 *Proposal* | exact diff; *Left for people* |
| Approval and apply history | History, Changes (Technical view) | who approved which fingerprint; verification PASSED |
| The post-change answer | stage 11 *Ask again on the new revision* (stored) | ABSTAINED with the conflicting sources |

Opening a stored result never starts an AI job. If the page shows "not started" you opened it without the full link: use **My work** or the Changes chooser instead of typing ids.
