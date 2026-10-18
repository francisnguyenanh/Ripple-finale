# Layout patterns

Pick the pattern that fits the task; do not force every page into a dashboard of cards. A page has exactly one `h1`, a one-sentence purpose, then the main outcome and next action before detail.

| Pattern | Use when | Do not use when | Notes |
|---|---|---|---|
| Worklist | many items need triage (inbox, gaps, source files) | few items with long detail | Search and filter only cover loaded items, and the page says so. Each row shows state, what is needed and the allowed action. |
| Master-detail | compare many items and open one in depth | the detail is the whole task | Detail stacks below the list under `lg`; keep the selection in the URL when the page already does. |
| Workflow | an ordered process with stages | unordered tasks | Show orientation (where am I, what is next) plus the current stage; blocked or partial stages stay visible. |
| Evidence and provenance | showing why a fact is believed | decoration | Quote the source unaltered; redacted or unavailable evidence is stated, never omitted silently. |
| Compare (before/after) | two versions of the same thing | unrelated items | Two columns from `md`; stacked and labelled below. |
| Timeline | what happened, when, by whom | claiming the full audit when only a window was loaded | State the window that was loaded. |
| Graph | relationships | as the only representation | Provide a text or tree equivalent. |
| Table | dense comparable records | narrative content | Caption, row and column headers, labelled local scroll; never hide essential columns. |
| Form | collecting a decision or input | read-only facts | Visible labels, errors associated with fields, the consequence stated beside the submit control. |
| Reader | long documents | short facts | Reading width 65-80 characters; stable toolbar. |
| Drawer or dialog | focused detail or confirmation | primary navigation | Use `Modal`; focus is restored on close. |

Responsive targets: 1440, 1280, 1024 and 390 px wide; no horizontal page scroll; critical flows usable at 320 px and 200% zoom.
