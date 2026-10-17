import * as React from "react";
import { NAV } from "@/lib/copy";

export function PageHeader({ id, action, next }: { id: (typeof NAV)[number]["id"]; action?: React.ReactNode; next?: string }) {
  const n = NAV.find((x) => x.id === id)!;
  return (
    <header className="mb-6">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold" data-testid="page-title">{n.label}</h1>
          <p className="text-muted text-sm mt-1">{n.purpose}</p>
        </div>
        {action}
      </div>
      {next && (
        <p className="mt-3 rounded-md border border-border bg-surface px-3 py-2 text-sm" data-testid="next-step">
          <strong>Next step:</strong> {next}
        </p>
      )}
    </header>
  );
}
