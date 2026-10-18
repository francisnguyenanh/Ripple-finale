// Development-only entry of the component catalog (`npm run dev`, then open /app-next/catalog.html). It is not imported by the app and is
// not an input of the production build. Everything it renders is synthetic; callbacks do nothing and nothing is requested.
import * as React from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { SessionContext, type Mode, type Session } from "@/session";
import { Catalog } from "./Catalog";

function Root() {
  const [mode, setMode] = React.useState<Mode>("business");
  const session: Session = {
    account: "catalog-user", workspaces: [], route: { ws: "ws-catalog", page: "catalog", sub: "", params: {} }, mode, setMode,
    head: null, sandbox: null, notice: "", aiOff: false, can: () => false, roles: [], signOut: async () => undefined,
    expire: () => undefined, refresh: async () => undefined, reloadWorkspaces: async () => undefined, reloadNonce: 0, reload: () => undefined,
  };
  return <SessionContext.Provider value={session}><Catalog /></SessionContext.Provider>;
}

createRoot(document.getElementById("root")!).render(<Root />);
