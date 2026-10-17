import * as T from "@radix-ui/react-tabs";
import * as React from "react";
import { cn } from "@/lib/cn";

export const Tabs = T.Root;
export const TabsContent = T.Content;
export const TabsList = (p: React.ComponentProps<typeof T.List>) => (
  <T.List {...p} className={cn("flex gap-1 border-b border-border", p.className)} />
);
export const TabsTrigger = (p: React.ComponentProps<typeof T.Trigger>) => (
  <T.Trigger {...p} className={cn("px-3 py-2 text-sm text-muted cursor-pointer border-b-2 border-transparent data-[state=active]:text-text data-[state=active]:border-primary", p.className)} />
);
