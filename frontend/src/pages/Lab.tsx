// CSP proof page (Slice A): an interactive dialog and a React Flow node with dynamic positioning,
// both under the real CSP. Reachable only by URL (#/ws/<id>/lab); not in the nav.
import * as React from "react";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { Modal } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";

export function Lab() {
  const [open, setOpen] = React.useState(false);
  const [nodes, setNodes] = React.useState<Node[]>([
    { id: "a", position: { x: 20, y: 40 }, data: { label: "Rule: limit" } },
    { id: "b", position: { x: 260, y: 120 }, data: { label: "Source: policy" } },
  ]);
  const edges: Edge[] = [{ id: "e", source: "a", target: "b", label: "projection, not proof" }];
  return (
    <section data-testid="lab">
      <h1 className="text-2xl font-semibold mb-4">CSP check</h1>
      <div className="flex gap-3 mb-4">
        <Button data-testid="lab-open" onClick={() => setOpen(true)}>Open dialog</Button>
        <Modal open={open} onClose={() => setOpen(false)} title="Example dialog" description="Focus is trapped here and returns to the button on close.">
          <Button variant="primary" data-testid="lab-close" onClick={() => setOpen(false)}>Done</Button>
        </Modal>
        <Tip label="Moves the first node">
          <Button data-testid="lab-move" onClick={() => setNodes((n) => n.map((x) => (x.id === "a" ? { ...x, position: { x: x.position.x + 40, y: x.position.y } } : x)))}>Move node</Button>
        </Tip>
      </div>
      <div className="h-80 rounded-md border border-border" data-testid="lab-flow">
        <ReactFlow nodes={nodes} edges={edges} fitView><Background /><Controls /></ReactFlow>
      </div>
    </section>
  );
}
