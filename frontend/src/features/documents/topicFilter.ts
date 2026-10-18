// Local filter over the topics the server already returned. It only narrows what is listed: nothing is requested, and clearing the
// text lists every loaded topic again. It never decides which documents exist.
export function filterTopics<T extends { title: string }>(topics: T[], text: string): T[] {
  const needle = text.trim().toLowerCase();
  if (!needle) return topics;
  return topics.filter((t) => t.title.toLowerCase().includes(needle));
}
