# Layout patterns

Pick the pattern that fits the task; do not force every page into a dashboard of cards. A page has exactly one `h1`, a one-sentence purpose, then the main outcome and next action before detail.

| Pattern | Use when | Do not use when | Notes |
|---|---|---|---|
| Worklist | many items need triage (inbox, gaps, source files) | few items with long detail | Search and filter only cover loaded items, and the page says so. Each row shows state, what is needed and the allowed action. Counts above a worklist are counts of the loaded list, named as such; a cut or partial read is stated beside them (My work: `DataTable` per server group). |
| Master-detail | compare many items and open one in depth | the detail is the whole task | `Inspector`: a pane beside the list from 1280 px, a drawer / sheet below (never two narrow columns); keep the selection in the URL; an older answer never fills a newer selection. |
| Workflow | an ordered process with stages | unordered tasks | Show orientation (where am I, what is next) plus the current stage; blocked or partial stages stay visible. |
| Evidence and provenance | showing why a fact is believed | decoration | Quote the source unaltered; redacted or unavailable evidence is stated, never omitted silently. |
| Compare (before/after) | two versions of the same thing | unrelated items | Two columns from `md`; stacked and labelled below. |
| Timeline | what happened, when, by whom | claiming the full audit when only a window was loaded | State the window that was loaded. |
| Graph | relationships | as the only representation | Provide a text or tree equivalent. Items with no edge to another shown item go to a counted, bounded chip list under the canvas ("no link in the loaded graph", never "unrelated"; a link hidden by the filter or a cut graph is said), with "show them on the canvas too"; canvas + list = items shown (`looseSplit` in `features/explorer/graphSplit.ts`). Dashed only for the server's explicit `projection` flag. |
| Table | dense comparable records | narrative content | Caption, row and column headers, labelled local scroll; never hide essential columns. `DataTable` turns rows into labelled stacks under 768 px instead of scrolling sideways. Compact density only for comparison tables. |
| Form | collecting a decision or input | read-only facts | Visible labels, errors associated with fields, the consequence stated beside the submit control. |
| Reader | long documents | short facts | Reading width 65-80 characters; stable toolbar. |
| Drawer or dialog | focused detail or confirmation | primary navigation | Use `Modal`; focus is restored on close. |

Responsive targets: 1440, 1280, 1024 and 390 px wide; no horizontal page scroll; critical flows usable at 320 px and 200% zoom.

## Request workspace (SCN-02)
A request page leads with its task: title, step now, who holds it and the reader's part, then blockers, then the action block (Input, What to do, Hand-over), then evidence, history and help. A request opened already confirmed shows the task frame and decisions first; what the AI understood and the composer for another request are one click away (mounted, so typed text stays). Nothing page-level may contradict the task projection (for example the open-questions line is hidden while one request is open).
