import { useMemo } from "react";
import { renderMarkdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";

/** Rendered Markdown: raw HTML is escaped and the output sanitized (DOMPurify). */
export function Markdown({ source, className }: { source: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(source), [source]);
  return <div className={cn("md-doc", className)} dangerouslySetInnerHTML={{ __html: html }} />;
}
