// The document reader never interprets HTML and never renders a link with a dangerous scheme (DOC-04 negative cases).
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown, safeHref } from "./markdown";

const html = (source: string) => renderToStaticMarkup(createElement(Markdown, { source }));
const BS = "\\";

describe("safe markdown", () => {
  it("shows raw HTML and scripts as text, never as elements", () => {
    const out = html("# Title <b>x</b>\n\n<script>alert(1)</script>\n\n- item <img src=x onerror=alert(1)>");
    expect(out).not.toContain("<script>");
    expect(out).not.toContain("<img");
    expect(out).not.toContain("<b>");
    expect(out).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(out).toContain("<h2");
  });
  it("renders only http(s) and in-page links; other schemes become text", () => {
    const out = html("[ok](https://example.org/a) [bad](javascript:alert(1)) [data](data:text/html;base64,AAAA) [here](#section)");
    expect(out).toContain('href="https://example.org/a"');
    expect(out).toContain('href="#section"');
    expect(out).not.toContain('href="javascript:');                                   // the target is shown as text, never as a link
    expect(out).not.toContain('href="data:');
    expect(out).toContain("bad (javascript:alert(1)");
    expect(out).toContain('data-testid="md-unsafe-link"');
    expect(safeHref("JAVASCRIPT:alert(1)")).toBeNull();
    expect(safeHref("vbscript:x")).toBeNull();
    expect(safeHref("https://x.y")).toBe("https://x.y");
  });
  it("honours the generator's backslash escapes: an escaped character is text and never opens markup", () => {
    const out = html(`- title: Pay ${BS}*now${BS}* ${BS}<tag${BS}> a ${BS}| b ${BS}[x${BS}]\n\n${BS}# not a heading`);
    expect(out).not.toContain("<em>");
    expect(out).toContain("Pay *now* &lt;tag&gt; a | b [x]");
    expect(out).toContain("<p># not a heading</p>");
    expect(out).not.toContain("");
  });
  it("keeps the generator's structure: headings, bullets, code fences, inline code, tables", () => {
    const out = html("## Rules\n\n- a: `rule-1`\n- b\n\n```\n{\"op\": \"gte\"}\n```\n\n| h1 | h2 |\n|---|---|\n| c1 | c2 |\n\nText **bold** and *em*.");
    expect(out).toContain("<h3");
    expect(out).toContain("<li>");
    expect(out).toContain("<pre");
    expect(out).toContain("<code");
    expect(out).toContain("<th");
    expect(out).toContain("<td");
    expect(out).toContain("<strong>bold</strong>");
    expect(out).toContain("<em>em</em>");
  });
});
