import * as React from "react";
import { SessionProvider, useSession } from "./session";
import { Login } from "./pages/Login";
import { Shell } from "./shell/Shell";
import { Overview } from "./pages/Overview";
import { Rebuilding } from "./pages/Rebuilding";
import { Lab } from "./pages/Lab";
import { ChangesPage } from "./features/changes/ChangesPage";
import { QuestionsPage } from "./features/questions/QuestionsPage";
import { SupportPage } from "./features/support/SupportPage";
import { SourcesPage } from "./features/sources/SourcesPage";
import { ExplorerPage } from "./features/explorer/ExplorerPage";
import { ImpactPage } from "./features/impact/ImpactPage";
import { HarborPage } from "./features/harbor/HarborPage";
import { HistoryPage } from "./features/history/HistoryPage";
import { DocumentsPage } from "./features/documents/DocumentsPage";
import { Denied } from "./components/bits";
import { ApiError } from "./lib/api";
import { NotifyProvider } from "./lib/notify";
import { NAV } from "./lib/copy";
import { hashFor } from "./lib/route";
import { TooltipProvider } from "./components/ui/tooltip";

function Pages() {
  const s = useSession();
  const { ws, page } = s.route;
  React.useEffect(() => {
    // No workspace in the link: go to the first workspace this account can see.
    if (!ws && s.workspaces[0]) location.replace(hashFor(s.workspaces[0].workspace_id, "overview"));
  }, [ws, s.workspaces]);
  if (!ws) return <p className="text-muted" role="status">{s.workspaces.length ? "Opening your workspace..." : "This account has no workspace yet."}</p>;
  if (!s.workspaces.some((w) => w.workspace_id === ws))
    return <Denied err={new ApiError(403, {})} />;
  const k = s.reloadNonce;
  if (page === "lab") return <Lab />;
  if (page === "overview") return <Overview key={k} />;
  if (page === "changes") return <ChangesPage key={k} />;
  if (page === "questions") return <QuestionsPage key={k} />;
  if (page === "support") return <SupportPage key={k} />;
  if (page === "sources") return <SourcesPage key={k} />;
  if (page === "explorer") return <ExplorerPage key={k} />;
  if (page === "impact") return <ImpactPage key={k} />;
  if (page === "harbor") return <HarborPage key={k} />;
  if (page === "history") return <HistoryPage key={k} />;
  if (page === "documents") return <DocumentsPage key={k} />;
  const n = NAV.find((x) => x.id === page);
  return n ? <Rebuilding id={n.id} /> : <p role="alert">There is no page called "{page}".</p>;
}

export function App() {
  return (
    <TooltipProvider>
      <SessionProvider login={(message, onDone) => <Login message={message} onDone={onDone} />}>
        {(sess) => <NotifyProvider onSignedOut={sess.expire}><Shell><Pages /></Shell></NotifyProvider>}
      </SessionProvider>
    </TooltipProvider>
  );
}
