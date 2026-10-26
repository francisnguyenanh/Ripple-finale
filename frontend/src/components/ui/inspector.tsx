// Inspector shell (SCN-10): one focused detail beside a list. The DOMAIN BODY belongs to the feature (structured Business / Technical rendering; never a raw dump);
// this shell only places it:
// - from 1280 px: a non-modal pane in the page's own grid (the list stays usable; nothing is covered), heading focused when the selection changes;
// - below 1280 px: the shared `Modal` drawer (non-modal side panel from 640 px, a modal sheet on a phone), so a narrow screen never squeezes list and detail side by side.
// Selection lives with the caller (usually the URL), so a reload, Back and a breakpoint change keep it; closing is the caller's `onClose` (Esc / Close / route change).
// Nothing is fetched or mutated here.
import * as React from "react";
import { X } from "lucide-react";
import { Eyebrow } from "@/components/bits";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { useMedia } from "@/lib/useMedia";
import { cn } from "@/lib/cn";

export const INSPECTOR_WIDE = "(min-width: 1280px)";

export function useInspectorWide(): boolean {
  return useMedia(INSPECTOR_WIDE);
}

export function Inspector({ open, title, eyebrow, description, onClose, testid, data, closeTestid, selectionKey, children, className }: {
  open: boolean; title: React.ReactNode; eyebrow?: string; description: string; onClose: () => void; testid: string; data?: Record<string, string>;
  closeTestid?: string; selectionKey?: string; children: React.ReactNode; className?: string;
}) {
  const wide = useInspectorWide();
  const head = React.useRef<HTMLHeadingElement>(null);
  React.useEffect(() => { if (open && wide) head.current?.focus(); }, [open, wide, selectionKey]);  // a new selection is announced by moving focus to its heading
  if (!open) return null;
  const extra: Record<string, string> = {};
  for (const [k, v] of Object.entries(data ?? {})) extra[`data-${k}`] = v;
  if (!wide)
    return (
      <Modal open onClose={onClose} side="right" title={typeof title === "string" ? title : (eyebrow ?? "Details")} description={description}>
        <div data-testid={testid} data-placement="drawer" {...extra} className="grid gap-3 min-w-0">
          {typeof title !== "string" && <div className="font-semibold break-words">{title}</div>}
          {children}
        </div>
      </Modal>
    );
  return (
    <aside data-testid={testid} data-placement="pane" {...extra} aria-label={typeof title === "string" ? title : (eyebrow ?? "Details")}
      className={cn("rounded-lg border border-border bg-surface grid gap-3 p-4 min-w-0 content-start xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto", className)}>
      <header className="flex items-start gap-2">
        <div className="grid gap-0.5 min-w-0 flex-1">
          {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
          <h2 ref={head} tabIndex={-1} className="font-semibold break-words flex items-center gap-2">{title}</h2>
          <p className="text-sm text-muted">{description}</p>
        </div>
        <Button size="sm" variant="ghost" data-testid={closeTestid} onClick={onClose} aria-label="Close the details"><X size={16} aria-hidden="true" />Close</Button>
      </header>
      {children}
    </aside>
  );
}
