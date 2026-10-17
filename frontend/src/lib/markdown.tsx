// Safe renderer for the Markdown the document generator writes (DOC-01 template: headings, paragraphs, bullet lists, fenced code, inline code, bold, italics,
// tables). Raw HTML is never interpreted: every character of the source becomes TEXT in a React element, so a document can contain "<script>" and it is shown
// as those characters. Links are rendered only for http(s) and in-page (#) targets; any other scheme (javascript:, data:, vbscript:, file:) is shown as text.
import * as React from "react";

const SAFE_HREF = /^(https?:\/\/|#)/i;
// The generator escapes every Markdown-significant character of untrusted text with a backslash (document_ast.md_text). An escaped character is marked here
// before tokenising so it can never open markup, a list, a heading, a fence or a table cell; the marker is dropped when the character is written out as text.
const ESCAPABLE = "\\`*_[]()<>|~&#+-.!";
const ESC = /\\([\\`*_[\]()<>|~&#+\-.!])/g;
const SUBST = /[\uE000-\uE01F]/g;
const mark = (text: string) => text.replace(ESC, (_m, c: string) => String.fromCharCode(0xe000 + ESCAPABLE.indexOf(c)));   // one private-use character per escaped one: no markup regex can match it
const plain = (text: string) => text.replace(SUBST, (ch) => ESCAPABLE[ch.charCodeAt(0) - 0xe000]);
const raw = (text: string) => text.replace(SUBST, (ch) => "\\" + ESCAPABLE[ch.charCodeAt(0) - 0xe000]);                      // inside a code fence the generator's bytes are shown as written

export function safeHref(href: string): string | null {
  const h = href.trim();
  return SAFE_HREF.test(h) ? h : null;
}

type Inline = React.ReactNode;

/** Inline markup: `code`, **bold**, *italic*, [text](href). Everything else is text. */
export function renderInline(text: string, key = "i"): Inline[] {
  const out: Inline[] = [];
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\([^)\s]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(plain(text.slice(last, m.index)));
    const tok = m[0];
    n += 1;
    if (tok.startsWith("`")) out.push(<code key={`${key}${n}`} className="rounded bg-surface-2 px-1">{plain(tok.slice(1, -1))}</code>);
    else if (tok.startsWith("**")) out.push(<strong key={`${key}${n}`}>{plain(tok.slice(2, -2))}</strong>);
    else if (tok.startsWith("*")) out.push(<em key={`${key}${n}`}>{plain(tok.slice(1, -1))}</em>);
    else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok)!;
      const href = safeHref(plain(lm[2]));
      out.push(href ? <a key={`${key}${n}`} className="text-primary underline" href={href} rel="noopener noreferrer" target={href.startsWith("#") ? undefined : "_blank"}>{plain(lm[1])}</a>
                    : <span key={`${key}${n}`} data-testid="md-unsafe-link">{plain(lm[1])} ({plain(lm[2])})</span>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(plain(text.slice(last)));
  return out;
}

/** Block structure of the generator's Markdown. Unknown constructs fall back to paragraphs of text. */
export function renderMarkdown(src: string): React.ReactNode[] {
  const lines = mark(src.replace(/\r\n/g, "\n")).split("\n");
  const out: React.ReactNode[] = [];
  let i = 0;
  let k = 0;
  const key = () => `b${++k}`;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const Tag = (`h${Math.min(level + 1, 6)}`) as keyof React.JSX.IntrinsicElements;   // the page has its own h1
      out.push(<Tag key={key()} className={level === 1 ? "text-xl font-semibold mt-2" : level === 2 ? "text-lg font-semibold mt-4" : "font-semibold mt-3"}>{renderInline(h[2])}</Tag>);
      i += 1; continue;
    }
    if (/^```/.test(line)) {
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;                                                                                   // closing fence (or end of text)
      out.push(<pre key={key()} className="rounded-md border border-border bg-surface-2 p-3 text-xs overflow-x-auto"><code>{raw(buf.join("\n"))}</code></pre>);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*]\s+/, "")); i += 1; }
      out.push(<ul key={key()} className="list-disc pl-5 grid gap-1">{items.map((it, j) => <li key={j}>{renderInline(it, `l${j}`)}</li>)}</ul>);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i += 1; }
      out.push(<ol key={key()} className="list-decimal pl-5 grid gap-1">{items.map((it, j) => <li key={j}>{renderInline(it, `o${j}`)}</li>)}</ol>);
      continue;
    }
    if (/^\|/.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        const cells = lines[i].trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(cells);
        i += 1;
      }
      out.push(<table key={key()} className="text-sm border-collapse"><tbody>{rows.map((r, ri) => <tr key={ri}>{r.map((c, ci) => ri === 0 ? <th key={ci} className="border border-border px-2 py-1 text-left">{renderInline(c, `t${ri}${ci}`)}</th> : <td key={ci} className="border border-border px-2 py-1">{renderInline(c, `t${ri}${ci}`)}</td>)}</tr>)}</tbody></table>);
      continue;
    }
    const buf: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*[-*]\s+|\s*\d+\.\s+|\|)/.test(lines[i])) { buf.push(lines[i]); i += 1; }
    out.push(<p key={key()}>{renderInline(buf.join(" "))}</p>);
  }
  return out;
}

export function Markdown({ source, testid }: { source: string; testid?: string }) {
  return <div data-testid={testid} className="grid gap-2 text-sm leading-relaxed">{renderMarkdown(source)}</div>;
}
