import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** The status colours shared by every page: readable on the light and dark themes. */
export type StatusTone = "ok" | "info" | "warn" | "danger" | "neutral";

const TONE: Record<StatusTone, string> = {
  ok: "border-gain/50 bg-gain/10 text-gain",
  info: "border-accent/50 bg-accent/10 text-accent",
  warn: "border-warn/50 bg-warn/10 text-warn",
  danger: "border-loss/50 bg-loss/10 text-loss",
  neutral: "border-border bg-card text-muted",
};

/** A small status pill (signals, protection, strategy and portfolio states...). */
export function StatusBadge({ tone, title, className, children }: { tone: StatusTone; title?: string; className?: string; children: ReactNode }) {
  return (
    <span
      title={title}
      className={cn("inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-semibold", TONE[tone], className)}
    >
      {children}
    </span>
  );
}
