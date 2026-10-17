import * as React from "react";
import { useNotify } from "./notify";

/**
 * Runs a mutation exactly once. A second click while it runs is ignored synchronously (ref, not
 * state), the failure is reported through the notice banner, and nothing is ever retried
 * automatically. Returns null when it failed or was ignored.
 */
export function useAction() {
  const busy = React.useRef(false);
  const [pending, setPending] = React.useState(false);
  const notify = useNotify();
  const run = React.useCallback(
    async <T,>(fn: () => Promise<T>, opts?: { stale?: boolean }): Promise<T | null> => {
      if (busy.current) return null;
      busy.current = true;
      setPending(true);
      notify.clear();
      try {
        return await fn();
      } catch (e) {
        notify.fail(e, opts);
        return null;
      } finally {
        busy.current = false;
        setPending(false);
      }
    },
    [notify],
  );
  return { pending, run };
}
