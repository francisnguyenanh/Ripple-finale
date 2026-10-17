import { useSession } from "@/session";
import { PageHeader } from "./Page";
import type { NAV } from "@/lib/copy";
import { StatusBadge } from "@/components/StatusBadge";

const LEGACY: Record<string, string> = { sources: "sources", explorer: "explorer", questions: "questions", changes: "changes", impact: "changes", history: "history", support: "support" };

export function Rebuilding({ id }: { id: (typeof NAV)[number]["id"] }) {
  const s = useSession();
  const legacy = s.route.ws ? `/app#/ws/${encodeURIComponent(s.route.ws)}/${LEGACY[id] ?? "sources"}` : "/app";
  return (
    <>
      <PageHeader id={id} />
      <div className="rounded-md border border-border bg-surface p-4" data-testid="rebuilding">
        <StatusBadge status="info" label="Not rebuilt yet" />
        <p className="mt-2 text-sm">This page has not been moved to the new interface yet. The classic page still works with the same data.</p>
        <p className="mt-2 text-sm"><a className="text-primary underline" href={legacy}>Open the classic page</a></p>
      </div>
    </>
  );
}
