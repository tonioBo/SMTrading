import { describe, expect, it } from "vitest";
import { renderMarkdown } from "@/lib/markdown";

describe("renderMarkdown", () => {
  it("never renders images: Markdown images become their alt text", () => {
    const html = renderMarkdown("Logo: ![il logo](https://evil.example/x.png)");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("evil.example");
    expect(html).toContain("il logo");
  });

  it("never renders images: raw <img> is text, and DOMPurify forbids the tag", () => {
    const html = renderMarkdown('<img src="https://evil.example/y.png" onerror="alert(1)">');
    expect(html).not.toMatch(/<img/i);
    const div = document.createElement("div");
    div.innerHTML = html;
    expect(div.querySelector("img")).toBeNull();
  });

  it("still renders links and tables", () => {
    const html = renderMarkdown("[doc](https://example.com)\n\n| a |\n|---|\n| 1 |\n");
    expect(html).toContain('<a href="https://example.com">doc</a>');
    expect(html).toContain('<div class="md-table"><table>');
  });
});
