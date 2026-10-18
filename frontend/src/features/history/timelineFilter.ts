// Local filter over the activity events that were already loaded. It narrows the list only: it never claims to search the full
// audit history, and clearing the filter lists every loaded event again.
export interface EventLike { actor_id?: unknown; event_type?: unknown }

export function actorsOf(events: EventLike[]): string[] {
  return Array.from(new Set(events.map((e) => String(e.actor_id || "")))).filter(Boolean).sort();
}

export function filterEvents<T extends EventLike>(events: T[], f: { actor: string; text: string }, labelOf: (e: T) => string): T[] {
  const needle = f.text.trim().toLowerCase();
  return events.filter((e) => {
    if (f.actor && String(e.actor_id || "") !== f.actor) return false;
    return !needle || labelOf(e).toLowerCase().includes(needle) || String(e.event_type ?? "").toLowerCase().includes(needle);
  });
}
