// Impact & Simulation: what a change would touch and what happens if you try it. It shows the
// impact analysis and simulation of the request in the link (read-only) and the what-if form. A
// relation drawn here is a proposed link or a re-checked citation: never a proven dependency, and
// UNKNOWN is never "unaffected".
import * as React from "react";
import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { api } from "@/lib/api";
import { Card, Muted } from "@/components/bits";
import { StatusBadge } from "@/components/StatusBadge";
import { basename } from "@/lib/fmt";
import { hashFor } from "@/lib/route";
import { PageHeader } from "@/pages/Page";
import { useSession } from "@/session";
import { ImpactReport, SimulationReport } from "@/features/changes/AnalysisStage";
import { FlowProvider, useFlow } from "@/features/changes/flow";
import { WhatIfStage } from "@/features/changes/WhatIfStage";
import { JobLine } from "@/features/changes/StageBits";
import { isDone, type Rec } from "@/features/changes/types";

interface IData extends Record<string, unknown> { label: string; kind: string; sub?: string }

function INode({ data }: NodeProps<Node<IData>>) {
  const unknown = data.kind === "unknown";
  return (
    <div data-impact-node={data.kind} className={`w-[240px] rounded-md border bg-surface px-3 py-2 text-sm shadow-card ${unknown ? "border-dashed border-warning" : data.kind === "target" ? "border-primary ring-2 ring-primary" : "border-border"}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="block text-[11px] uppercase tracking-wide text-muted">{({ target: "The rule you change", direct: "Directly affected", indirect: "Indirectly affected", unknown: "UNKNOWN - not covered" } as Rec)[data.kind]}</span>
      <span className="block truncate" title={data.label}>{data.label}</span>
      {data.sub && <span className="block text-xs text-muted truncate">{data.sub}</span>}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
const NODE_TYPES = { i: INode };

function ImpactGraph({ ws, res }: { ws: string; res: Rec }) {
  const s = useSession();
  const [labels, setLabels] = React.useState<Record<string, { label?: string; logical_path?: string }>>({});
  React.useEffect(() => {
    if (!res.target_candidate_id) return;
    let live = true;
    api<{ candidates?: typeof labels }>("GET", `/v2/change/impact-context?workspace_id=${encodeURIComponent(ws)}&target_candidate_id=${encodeURIComponent(res.target_candidate_id)}`)
      .then((cx) => { if (live) setLabels(cx.candidates ?? {}); }).catch(() => undefined);
    return () => { live = false; };
  }, [ws, res.target_candidate_id]);
  const lab = (id: string) => labels[id]?.label ?? "a related rule";
  const { nodes, edges } = React.useMemo(() => {
    const nodes: Node<IData>[] = [];
    const edges: Edge[] = [];
    const at = (col: number, row: number) => ({ x: col * 300, y: row * 76 });
    const tgt = res.target_candidate_id as string | undefined;
    nodes.push({ id: "target", type: "i", position: at(0, 0), data: { label: tgt ? lab(tgt) : "No target rule resolved", kind: "target", sub: tgt && labels[tgt]?.logical_path ? basename(labels[tgt].logical_path) : undefined }, draggable: false });
    ((res.direct ?? []) as Rec[]).forEach((d, i) => {
      nodes.push({ id: `d${i}`, type: "i", position: at(1, i), data: { label: lab(d.candidate_id), kind: "direct", sub: labels[d.candidate_id]?.logical_path ? basename(labels[d.candidate_id].logical_path) : undefined }, draggable: false });
      edges.push({ id: `ed${i}`, source: "target", target: `d${i}`, className: "rp-edge" });
    });
    ((res.indirect ?? []) as Rec[]).forEach((x, i) => {
      nodes.push({ id: `i${i}`, type: "i", position: at(2, i), data: { label: basename(x.logical_path), kind: "indirect", sub: s.mode === "technical" ? `rule ${x.rule}` : "found by a re-checked citation" }, draggable: false });
      edges.push({ id: `ei${i}`, source: "target", target: `i${i}`, className: "rp-edge" });
    });
    ((res.unknown ?? []) as Rec[]).forEach((u, i) => {
      nodes.push({ id: `u${i}`, type: "i", position: at(3, i), data: { label: lab(u.candidate_id), kind: "unknown", sub: "no relation evidence" }, draggable: false });
      edges.push({ id: `eu${i}`, source: "target", target: `u${i}`, className: "rp-edge-projection" });
    });
    return { nodes, edges };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [res, labels, s.mode]);
  return (
    <div data-testid="impact-graph" role="group" aria-label="Impact graph. The report below has the same information as text." className="h-[420px] rounded-lg border border-border bg-surface">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={NODE_TYPES} fitView nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} minZoom={0.3}>
        <Background /><Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function Body() {
  const { ws, ids, data } = useFlow();
  const impactDone = !!data && isDone(data.impact);
  const simDone = !!data && isDone(data.simulation);
  const sim = (data?.simulation?.execution_result ?? {}) as Rec;
  return (
    <div className="grid gap-4">
      {!ids.impact ? (
        <div data-testid="state-empty" className="rounded-lg border border-border bg-surface p-4 grid gap-2">
          <p>No change is open here. Open a change in <a className="text-primary underline" href={hashFor(ws, "changes")}>Changes</a>, run its impact analysis, and it appears here with its graph. You can still try a value below.</p>
        </div>
      ) : !data ? <StatusBadge status="loading" /> : (
        <>
          <Card title="Impact of this change" testid="impact-page-impact">
            <JobLine name="impact" job={data.impact} />
            {impactDone && (
              <>
                <ImpactGraph ws={ws} res={data.impact!.execution_result ?? {}} />
                <ImpactReport res={data.impact!.execution_result ?? {}} />
              </>
            )}
          </Card>
          {data.simulation && (
            <Card title="Simulation of this change" testid="impact-page-simulation">
              <JobLine name="simulation" job={data.simulation} />
              {simDone && (sim.harbor && sim.status === "EXECUTED" ? <SimulationReport h={sim.harbor} /> : <Muted>Simulation {sim.status ?? "unknown"}{sim.reason ? `: ${sim.reason}` : ""}. This is NOT a "no impact" result.</Muted>)}
            </Card>
          )}
        </>
      )}
      <WhatIfStage />
    </div>
  );
}

export function ImpactPage() {
  const s = useSession();
  const ws = s.route.ws!;
  return (
    <>
      <PageHeader id="impact" next="Open a change to see its impact here, or try a value in the what-if form." />
      <FlowProvider ws={ws} page="impact" key={ws}><Body /></FlowProvider>
    </>
  );
}
