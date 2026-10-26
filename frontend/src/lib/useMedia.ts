// A media query as React state (layout decisions that CSS alone cannot make, e.g. folding a navigator). Re-renders only when the match changes.
import * as React from "react";

export function useMedia(query: string): boolean {
  const [hit, setHit] = React.useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  React.useEffect(() => {
    const m = window.matchMedia(query);
    const f = () => setHit(m.matches);
    f();
    m.addEventListener("change", f);
    return () => m.removeEventListener("change", f);
  }, [query]);
  return hit;
}
