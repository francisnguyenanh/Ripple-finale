// SCN-07: split the shown graph items into linked (an edge to another SHOWN item) and loose (none). Pure; it reads only the loaded, filtered graph, so "loose" means
// "no link in what was loaded and shown", never "unrelated". `hiddenLink` = a loose item that does have an edge, to an item the filter hides.
export interface SplitNode { id: string }
export interface SplitEdge { from: string; to: string }

export function looseSplit<N extends SplitNode>(edges: SplitEdge[], shown: N[]): { linked: N[]; loose: N[]; looseIds: Set<string>; hiddenLink: Set<string> } {
  const ids = new Set(shown.map((n) => n.id));
  const linkedIds = new Set<string>();
  const anyEdge = new Set<string>();
  for (const e of edges) {
    anyEdge.add(e.from); anyEdge.add(e.to);
    if (e.from !== e.to && ids.has(e.from) && ids.has(e.to)) { linkedIds.add(e.from); linkedIds.add(e.to); }
  }
  const loose = shown.filter((n) => !linkedIds.has(n.id));
  return { linked: shown.filter((n) => linkedIds.has(n.id)), loose, looseIds: new Set(loose.map((n) => n.id)), hiddenLink: new Set(loose.filter((n) => anyEdge.has(n.id)).map((n) => n.id)) };
}
