import * as T from "@radix-ui/react-tooltip";
import * as React from "react";

export const TooltipProvider = T.Provider;

export function Tip({ label, children }: { label: string; children: React.ReactElement }) {
  return (
    <T.Root>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content sideOffset={6} className="rounded-md bg-text text-bg text-xs px-2 py-1 shadow-pop z-50">{label}</T.Content>
      </T.Portal>
    </T.Root>
  );
}
