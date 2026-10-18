import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "@radix-ui/react-slot";
import * as React from "react";
import { cn } from "@/lib/cn";

const button = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium px-4 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed border border-transparent",
  {
    variants: {
      variant: {
        primary: "bg-primary text-on-primary hover:opacity-90",
        secondary: "bg-surface text-text border-border hover:bg-surface-2",
        ghost: "bg-transparent text-text hover:bg-surface-2",
        danger: "bg-danger text-on-danger hover:opacity-90",
      },
      size: { sm: "min-h-8 px-3", md: "min-h-9", lg: "min-h-11 px-5" },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof button> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild, type, ...p },
  ref,
) {
  const C = asChild ? Slot : "button";
  return <C ref={ref} type={asChild ? undefined : (type ?? "button")} className={cn(button({ variant, size }), className)} {...p} />;
});
