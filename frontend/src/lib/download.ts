import { api } from "./api";

interface Exported { content_b64: string; media_type?: string }

/** Download one published file through the same-origin export route (bytes arrive base64 inside JSON). */
export async function downloadArtifact(
  ws: string,
  revision: string,
  art: { artifact_id?: string; logical_path?: string },
  onError: (e: unknown) => void,
): Promise<void> {
  try {
    const r = await api<Exported>("GET", `/v2/sources/revisions/${encodeURIComponent(revision)}/artifacts/${encodeURIComponent(art.artifact_id ?? "")}/export?workspace_id=${encodeURIComponent(ws)}`);
    const bytes = Uint8Array.from(atob(r.content_b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: r.media_type || "application/octet-stream" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = (art.logical_path || art.artifact_id || "file").split("/").pop() ?? "file";
    document.body.append(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    onError(e);
  }
}
