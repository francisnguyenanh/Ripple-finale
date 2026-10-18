// Local filter over the files of the loaded source version. It only narrows what is listed; every loaded file stays reachable by
// clearing the text, and nothing is requested.
export interface FileLike { logical_path?: unknown; media_type?: unknown }

export function filterFiles<T extends FileLike>(files: T[], text: string): T[] {
  const needle = text.trim().toLowerCase();
  if (!needle) return files;
  return files.filter((f) => `${String(f.logical_path ?? "")} ${String(f.media_type ?? "")}`.toLowerCase().includes(needle));
}
