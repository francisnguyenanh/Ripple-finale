// Numbers in the structured sources that no business rule is linked to, grouped by file and collapsed. A disclosure for a person: never called an error or a missing
// rule, never turned into a rule or a question. Values and locations are source text, so only a reader who may read the sources sees them (the server withholds them).
import * as React from "react";
import { api, ApiError } from "@/lib/api";
import { Muted } from "@/components/bits";
import { hashFor } from "@/lib/route";
import type { Rec } from "@/features/changes/types";

const q = encodeURIComponent;

export function BusinessUnlinked({ ws }: { ws: string }) {
  const [data, setData] = React.useState<Rec | null>(null);
  React.useEffect(() => {
    let live = true;
    api<Rec>("GET", `/v2/business/unbound-values?workspace_id=${q(ws)}`).then((r) => { if (live) setData(r); })
      .catch((e) => { if (live && e instanceof ApiError) setData(null); });
    return () => { live = false; };
  }, [ws]);
  const files = ((data?.files ?? []) as Rec[]);
  if (!data || files.length === 0) return null;
  return (
    <section aria-labelledby="q-unlinked" data-testid="unlinked-values" className="grid gap-2">
      <h3 id="q-unlinked" className="text-base font-semibold">Values not linked to a rule ({String(data.total)})</h3>
      <Muted>{data.statement as string}</Muted>
      {data.values_withheld ? <Muted testid="unlinked-withheld">Your role cannot open the sources, so the values and their places are not shown.</Muted> : null}
      {files.map((f) => (
        <details key={f.file as string} data-testid="unlinked-file" className="rounded-md border border-border p-3 text-sm">
          <summary className="cursor-pointer">{f.file as string}: {String(f.count)} not linked to a rule</summary>
          {((f.values ?? []) as Rec[]).length > 0 && (
            <ul className="mt-2 grid gap-1">
              {(f.values as Rec[]).map((v) => (
                <li key={v.location as string} data-testid="unlinked-value">
                  <code>{v.location as string}</code>
                  {v.header || v.row_label ? ` - ${[v.header, v.row_label ? `for ${String(v.row_label)}` : null].filter(Boolean).join(" ")}` : ""} = {String(v.value)}
                </li>
              ))}
            </ul>
          )}
          {!data.values_withheld && <a className="mt-2 inline-block text-primary underline" href={hashFor(ws, "sources")}>Open the sources</a>}
        </details>
      ))}
    </section>
  );
}
