// D2a equivalent flow (owner decision 2026-10-27): beside an AI control whose job kind this server does not enable (the Pitch-era kinds of the Changes page and
// of the free-text chats), say where the equivalent, enabled step is. It renders nothing when the kind is enabled or when AI is switched off for the whole server
// (the shell says that). It never enables, hides or replaces the refused control; that control keeps its own refusal reason.
import { useSession } from "@/session";
import { hashFor } from "@/lib/route";

export type EquivalentTarget = "change" | "ask" | "direct";

const WORDS: Record<EquivalentTarget, string> = {
  change: "Questions › Request a change: the AI reads your request, checks dependencies and writes guidance on the draft",
  ask: "Questions › Ask: the AI answers from the business knowledge, with citations",
  direct: "",
};

export function EquivalentStep({ kind, to }: { kind: string; to: EquivalentTarget }) {
  const s = useSession();
  if (s.aiOff || s.aiKind(kind) || !s.route.ws) return null;
  if (to === "direct")
    return <p className="text-xs text-muted" data-testid="ai-equivalent" data-kind={kind} data-to={to}>No AI step is needed here: confirm or answer this item directly with its own controls.</p>;
  return (
    <p className="text-xs text-muted" data-testid="ai-equivalent" data-kind={kind} data-to={to}>
      Use the equivalent step: <a className="underline text-primary" href={hashFor(s.route.ws, "questions", undefined, { ui_section: to })}>{WORDS[to]}</a>.
    </p>
  );
}
