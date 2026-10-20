# Request tasks, the whole-request view, My work and the request assistant

What a person sees about one request (a change request in **Changes**, or a business change in **Questions & Decisions -> Request a change**), and what it does not do.
The server computes one task projection per request and per reader ([contract](../contracts/REQUEST_TASKS_AND_ASSISTANT.md)); the task frame, the whole-request
view, My work and the request assistant all read that same projection, so they cannot disagree about the stage, who holds a step or what blocks it.

## The task frame ("My task")
At the top of a request: the stage, **Input** (what to read), **What to do** (only the steps this person is asked to do now) and **Result to hand over**. Blockers are shown
above the action, never behind a toggle.
- **Permission is not assignment.** A person who may do a step but is not the one asked (an administrator who is not an approver, a judge who may prepare a change that
  someone else requested) sees the step with the reason "you may do this, but you are not asked to"; it is never offered as their task.
- **Optional is optional.** A reviewer's review before the decision is marked optional; it never blocks.
- **Waiting is named.** "Not waiting for you: waiting on the requester / an approver / the system". No person's name, deadline or service level is invented.
- **The real way forward.** After an approver requests changes, the requester answers first (the approver may still decide but is not asked). When the business knowledge
  changed after a request was confirmed, the request cannot continue: a new request is the way forward, and nobody is shown as waiting on an approver.

## The whole-request view
Every canonical step of the request with a text state (Done, Now, Waiting, Blocked, Failed, Later, Not needed). Opening a step shows who holds each obligation there, who is
waited on, and which steps run in parallel (for a business change: committing the knowledge and deciding the file edits, done by different people in either order). The
view and the opened step are part of the link, so a reload, Back and a shared link keep them; a linked step takes keyboard focus.

## My work
On Overview and Changes: requests of both kinds placed in **Waiting for you**, **Optional for you**, **Blocked**, **You are waiting for someone**, **You are waiting for the
system** and **Recently finished**. A waiting item opens the request on the step it waits at. If part of the list could not be read, the page says so; a failed read is never
shown as "nothing to do". The list only shows where to go: every action is checked again by the server.

## The request assistant ("Ask about this request")
A private conversation about one request, for people with the read-only capability `request.assistant.ask` who may read that request.
- **Reads only what you may read**, built before the model sees anything; private creation chats, other people's threads and hidden evidence are never part of it.
- **Never acts.** "I agree" or "approve it" approves nothing; next steps are the server's allowed actions. A "Go to" button only moves the focus to that step's own
  control on the page (for a step you are still asked to do); it never submits it.
- **No call without a question.** Opening the panel, seeing or editing a suggested question, switching views or polling starts no model call, no thread and no job.
  "Ask AI" on a suggestion, or Ask, sends one paid question (the note is next to the buttons); the first question creates your private conversation in the same call,
  and repeating it after a lost response does not ask twice.
- **Checked answers.** Every kept statement cites the facts it rests on. Who holds a step, who must or may act, who the request waits on and what blocks it is written
  by the server from a typed statement it checked against the facts; the model's own wording about authority is refused. Answers are in English. If a request has more
  required facts than one question can carry, the question is refused instead of being answered from part of them. An answer stored under older checks shows a note and
  hides its free-text authority statements.
- **Privacy follows access.** Threads are private to their creator; switching request, account or workspace shows nothing of the previous one; an answer is withheld when the
  asker's access changed since it was asked, and is marked historical when the request moved.

## Limits (read before relying on it)
- In the exported submission the AI is **simulated**: the assistant answers with a labelled decline. A real model runs only in an operator's armed local window with an
  approval and a budget.
- The live evaluations of the assistant are **self-authored and non-independent** (diagnostic only); see the evaluation records. They do not establish answer quality in general.
- Typed authority statements (checks 4, 2026-10-20) are verified with a synthetic stub model only; how often a real model follows them (and so how often an answer
  is partial or refused) has not been measured live. Quality stays OPEN.
- Product usefulness and the layout (suggested questions, the panel beside the task, "Go to") still need the owner's human acceptance.

Operators: [running the request assistant](../runbooks/REQUEST_ASSISTANT_OPERATIONS.md) (enable, limits, switching off, cost reconciliation, retention).
