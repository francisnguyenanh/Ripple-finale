import * as React from "react";
import { hashFor, parseHash, replaceHash } from "./route";

/** One URL parameter as state (so a reload, the Back button and a shared link keep it). Setting it does not add a history entry. */
export function useParam(name: string): [string, (v: string) => void] {
  const read = () => parseHash(location.hash).params[name] ?? "";
  const [v, setV] = React.useState(read);
  React.useEffect(() => {
    const f = () => setV(read());
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = React.useCallback((next: string) => {
    const r = parseHash(location.hash);
    if (!r.ws) return;
    replaceHash(hashFor(r.ws, r.page, r.sub, { ...r.params, [name]: next || undefined }));
  }, [name]);
  return [v, set];
}
