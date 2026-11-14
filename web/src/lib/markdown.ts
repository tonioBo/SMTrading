import DOMPurify from "dompurify";
import { Marked } from "marked";

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// raw HTML in the source is shown as text (e.g. the «<id>» placeholders), never rendered
const marked = new Marked({
  gfm: true,
  renderer: {
    html: ({ text }) => escapeHtml(text),
  },
});

/** Markdown → sanitized HTML; tables get a horizontally scrollable wrapper. */
export function renderMarkdown(source: string): string {
  const html = (marked.parse(source, { async: false }) as string)
    .replace(/<table>/g, '<div class="md-table"><table>')
    .replace(/<\/table>/g, "</table></div>");
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
