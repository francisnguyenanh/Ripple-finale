"""Builds the static judge website (site/judges/*.html, style.css, site.js) from docs/judges/*.md. Build-time only (python-markdown). The output is plain HTML + one CSS file + one tiny script
(copy button, expand/collapse); no framework, no network call, no Ripple API, no analytics. The Markdown stays the source and stays readable on GitHub; run this build after editing it.
Usage: python -m tools.judge_site.build [--ripple-url https://...] [--serving-until 2026-11-06T15:59:59+00:00]
Without --ripple-url the "Open Ripple" button says the link is added at deployment; without --serving-until the pages say the AI steps are not open. Neither value is stored in the Markdown."""
from __future__ import annotations

import argparse
import html
import pathlib
import re
from datetime import datetime, timedelta, timezone

import markdown

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC, OUT = ROOT / "docs" / "judges", ROOT / "site" / "judges"
PAGES = [("START_HERE", "index", "Start here"), ("SINGLE_ROLE_GUIDE", "one-role-guide", "A. Try it yourself"),
         ("VERIFIED_FOUR_ROLE_CASE", "four-role-case", "B. Recorded four-role case"), ("FOUR_ROLE_REPLAY_GUIDE", "four-role-replay", "Supporting: replay guide")]
SLUG = {src: slug for src, slug, _ in PAGES}
WINDOW_TEXT = "the AI steps are **not open at the moment**; Ripple refuses new work with an explanation and everything stored stays readable."

LANES = ["Requester", "Admin", "Approver", "Reviewer"]
STEPS = [("Requester", "Confirm request", "human"), ("Requester", "Draft", "AI"), ("Admin", "Decide open point", "human"), ("Requester", "Revise draft", "AI"),
         ("Requester", "Impact + simulation", "deterministic"), ("Requester", "Proposal", "deterministic"), ("Approver", "State the check", "human"),
         ("Reviewer", "Review", "deterministic"), ("Approver", "Approve", "human"), ("Requester", "Apply + verify", "deterministic"), ("Requester", "Ask again", "AI")]
KIND_FILL = {"human": "#1d4ed8", "AI": "#b45309", "deterministic": "#047857"}
TAGS = {"Judge action": "t-judge", "Recorded live result": "t-live", "Offline expectation": "t-off", "Not yet verified": "t-nv", "Limitation": "t-lim"}
TAG_ORDER = ["Not yet verified", "Limitation", "Judge action", "Offline expectation", "Recorded live result"]

FIGS = {
    "SINGLE_ROLE_GUIDE": [("img/stages_start.png", "Illustration only: the stage list of one change as a requester sees it before confirming, from an earlier 2026-10-10 run (a different request); not evidence of this guide's run.")],
    "VERIFIED_FOUR_ROLE_CASE": [("img/understanding.png", "Illustration only, not T1 evidence: a \"This is what I understood\" card from an earlier 2026-10-10 run of the same screens (a different request, 2,000,000 to 1,500,000 JPY)."),
                                ("img/ask_again_abstained.png", "Illustration only, not T1 evidence: an earlier 2026-10-10 run where the post-change question ABSTAINS and shows the conflicting files, with cited text hidden for this role.")],
}

# stored measurements (no live data is fetched by the site)
MANAGER = {"before": {"AUTO": 58, "NEEDS_MANAGER": 109, "NEEDS_FINANCE": 61, "NEEDS_DIRECTOR": 6, "REJECTED": 6},
           "after": {"AUTO": 93, "NEEDS_MANAGER": 74, "NEEDS_FINANCE": 61, "NEEDS_DIRECTOR": 6, "REJECTED": 6}}
CAPEX = {"before": {"NEEDS_MANAGER": 124, "NEEDS_FINANCE": 46}, "after": {"NEEDS_MANAGER": 109, "NEEDS_FINANCE": 61}}


def window_text(until: str | None) -> str:
    if not until:
        return WINDOW_TEXT
    e = datetime.fromisoformat(until).astimezone(timezone.utc)

    def f(tz: int, name: str) -> str:
        return e.astimezone(timezone(timedelta(hours=tz))).strftime("%Y-%m-%d %H:%M:%S") + " " + name
    return (f"open until **{f(8, 'SGT')}** (= {f(9, 'JST')} = {e.strftime('%Y-%m-%d %H:%M:%S')} UTC). After that, or when a quota or the provider budget is used up, "
            "Ripple refuses new work with a clear message, nothing is charged, and everything stored stays readable.")


def source_svg() -> str:
    def box(x, y, w, t, sub, fill):
        return (f'<rect x="{x}" y="{y}" width="{w}" height="46" rx="6" fill="{fill}" stroke="#6b7280"/><text x="{x + w / 2}" y="{y + 19}" text-anchor="middle" font-size="13" font-weight="600" fill="#111">{t}</text>'
                f'<text x="{x + w / 2}" y="{y + 36}" text-anchor="middle" font-size="10.5" fill="#111">{sub}</text>')
    out = ['<svg viewBox="0 0 720 330" role="img" aria-labelledby="sm-t sm-d" class="flow"><title id="sm-t">Sources of the Harbor subset and how they relate</title>'
           '<desc id="sm-d">Solid arrows: the program reads the value (checked in the code). Dashed lines: a file states a similar threshold in text; this is a statement to compare, not a dependency.</desc>']
    for y in (43, 109, 175, 241):
        out.append(f'<line x1="220" y1="{y}" x2="260" y2="143" stroke="#9ca3af" stroke-width="2" stroke-dasharray="6 4"/>')
    for y in (43, 123, 203):
        out.append(f'<line x1="450" y1="143" x2="500" y2="{y}" stroke="#047857" stroke-width="2"/>')
    out.append(box(20, 20, 200, "workflow.yaml", "manager 1,000,000 / CAPEX 2,000,000", "#fef3c7"))
    out.append(box(20, 86, 200, "approval_matrix.xlsx", "routing rows (around 500,000)", "#fef3c7"))
    out.append(box(20, 152, 200, "procurement_policy.md", "manager 1,000,000 / CAPEX 2,000,000", "#fef3c7"))
    out.append(box(20, 218, 200, "finance_desk_guide.md", "Finance review from 2,000,000", "#fef3c7"))
    out.append(box(260, 120, 190, "approval.yaml", "thresholds the program reads", "#dbeafe"))
    out.append(box(500, 20, 200, "config_loader.py", "loads and validates approval.yaml", "#d1fae5"))
    out.append(box(500, 100, 200, "routing.py", "standard routing (reads both)", "#d1fae5"))
    out.append(box(500, 180, 200, "emergency.py", "reads CAPEX; manager limit hard-coded", "#d1fae5"))
    out.append('<text x="20" y="290" font-size="12" fill="#111">Dashed: states a threshold in text (not read by the program; may be affected, not proven dependent).</text>')
    out.append('<text x="20" y="310" font-size="12" fill="#111">Solid: the program reads the value (checked in the code). The other 6 subset files are not drawn.</text>')
    out.append("</svg>")
    return "".join(out)


def flow_svg() -> str:
    colw, rowh, left, top = 78, 58, 92, 20
    w, h = left + colw * len(STEPS) + 10, top + rowh * len(LANES) + 96
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="fd-t fd-d" class="flow"><title id="fd-t">Order of the recorded four-role run</title>'
           '<desc id="fd-d">Eleven steps in the order the server recorded them, drawn in the lane of the role that acted. Blue is a human decision, amber an AI call, green deterministic work.</desc>']
    for i, lane in enumerate(LANES):
        y = top + i * rowh
        out.append(f'<rect x="0" y="{y}" width="{w}" height="{rowh}" fill="{"#f3f4f6" if i % 2 == 0 else "#ffffff"}"/><text x="6" y="{y + rowh / 2 + 4}" font-size="13" font-weight="600" fill="#111">{lane}</text>')
    pts = [(left + n * colw + colw / 2, top + LANES.index(lane) * rowh + rowh / 2) for n, (lane, _l, _k) in enumerate(STEPS)]
    out.append('<polyline fill="none" stroke="#9ca3af" stroke-width="2" points="' + " ".join(f"{x:.0f},{y:.0f}" for x, y in pts) + '"/>')
    for n, ((lane, label, kind), (cx, cy)) in enumerate(zip(STEPS, pts), 1):
        out.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="14" fill="{KIND_FILL[kind]}"/><text x="{cx:.0f}" y="{cy + 5:.0f}" text-anchor="middle" font-size="13" fill="#fff" font-weight="700">{n}</text>')
        ty = top + rowh * len(LANES) + 16 + (n % 3) * 22
        out.append(f'<text x="{cx:.0f}" y="{ty}" text-anchor="middle" font-size="11" fill="#111">{n}. {html.escape(label)}</text>')
    out.append("</svg>")
    return "".join(out)


def bars_table(before: dict, after: dict, caption: str) -> str:
    top = max(list(before.values()) + list(after.values()))
    rows = []
    for k in before:
        b, a = before[k], after[k]
        mark = ' class="chg"' if b != a else ""
        rows.append(f'<tr{mark}><th scope="row">{html.escape(k)}</th>'
                    f'<td><span class="bar b w{round(b * 100 / top)}"></span><span class="num">{b}</span></td>'
                    f'<td><span class="bar a w{round(a * 100 / top)}"></span><span class="num">{a}</span></td></tr>')
    return (f'<table class="bars"><caption>{html.escape(caption)}</caption><thead><tr><th scope="col">Route</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def before_after(kind: str) -> str:
    if kind == "manager":
        left = ('<section class="panel" aria-labelledby="pc-m"><h4 id="pc-m">Proposed change <span class="tag t-lim">not applied</span></h4>'
                '<p><code>approval.yaml</code> &rarr; <code>standard_manager_threshold_jpy</code></p><p class="delta"><span class="from">500,000</span> &rarr; <span class="to">800,000</span> JPY</p>'
                '<p>Status: <strong>proposed</strong>. A judge account can never apply it; the run ends waiting for a reviewer and an approver.</p></section>')
        right = ('<section class="panel" aria-labelledby="se-m"><h4 id="se-m">Simulated effect <span class="tag t-live">Recorded live result</span></h4>'
                 + bars_table(MANAGER["before"], MANAGER["after"], "Requisitions by route (240 public samples): 35 move NEEDS_MANAGER to AUTO. Highlighted rows changed.")
                 + '<p>Stored measurement, job <code>ui-simulation-98fb78b9e07e49c9</code>, baseline reproduced 240/240. It is the effect of the <em>proposed</em> value on Harbor\'s own program, not an edit of any source.</p></section>')
        un = ('<section class="panel wide" aria-labelledby="un-m"><h4 id="un-m">Still unresolved</h4><ul><li><code>workflow.yaml</code> and <code>procurement_policy.md</code> state a manager limit of 1,000,000.</li>'
              '<li><code>approval_matrix.xlsx</code> has routing rows around 500,000 (potentially affected).</li><li><code>requisition_sop.md</code> was left open by the draft.</li></ul>'
              '<p>Not simulated: the simulation does not read these files.</p></section>')
    else:
        left = ('<section class="panel" aria-labelledby="pc-c"><h4 id="pc-c">Applied change <span class="tag t-live">Recorded live result</span></h4>'
                '<p><code>approval.yaml</code> &rarr; <code>capex_finance_threshold_jpy</code></p><p class="delta"><span class="from">1,500,000</span> &rarr; <span class="to">1,000,000</span> JPY</p>'
                '<p>Status: <strong>applied</strong> by the requester account after approval; machine verification <strong>PASSED</strong> (the value was read back). Head version 8. Human acceptance of the result: <strong>pending</strong>.</p></section>')
        right = ('<section class="panel" aria-labelledby="se-c"><h4 id="se-c">Simulated effect at the time <span class="tag t-live">Recorded live result</span></h4>'
                 + bars_table(CAPEX["before"], CAPEX["after"], "Requisitions that changed route (240 samples): 15 move NEEDS_MANAGER to NEEDS_FINANCE. Other routes did not change.")
                 + '<p>Stored measurement, simulation <code>ui-simulation-8bede444...</code>: 7 standard + 8 emergency CAPEX requisitions. No requisition list is shown because none was stored for this page.</p></section>')
        un = ('<section class="panel wide" aria-labelledby="un-c"><h4 id="un-c">Still unresolved after the apply</h4><ul><li><code>workflow.yaml</code>, <code>approval_matrix.xlsx</code> and <code>procurement_policy.md</code> still say 2,000,000.</li></ul>'
              '<p><strong>ABSTAINED:</strong> asked again after the apply, the system did not pick one value because the sources still disagree. That is correct behaviour; it is not an error and it does not mean the knowledge is now right.</p></section>')
    return f'<div class="panels">{left}{right}{un}</div>'


def request_box(text: str) -> str:
    t = html.escape(text)
    return (f'<div class="reqbox"><blockquote id="reqtext" tabindex="0">{t}</blockquote>'
            '<p><button type="button" class="copybtn" data-copy-target="reqtext">Copy request</button> <span id="copystatus" class="copystatus" role="status" aria-live="polite"></span></p>'
            '<p class="hint">If copying does not work, select the text above and copy it by hand (Ctrl+C / Cmd+C).</p></div>')


def tags(body: str) -> str:
    return re.sub(r"\[\[([^\]]+)\]\]", lambda m: f'<span class="tag {TAGS.get(m.group(1), "t-lim")}">{html.escape(m.group(1))}</span>', body)


VISIBLE = ("Role", "Kind", "Action", "Expected", "Input")


def step_cards(body: str) -> str:
    def card(m: re.Match) -> str:
        title, items = m.group(1), re.findall(r"<li>(.*?)</li>", m.group(2), flags=re.S)
        vis, hid = [], []
        for li in items:
            lab = re.match(r"\s*<strong>(.*?)</strong>", li, flags=re.S)
            first = re.sub(r"<[^>]+>|[:\s]+$", "", lab.group(1)).strip() if lab else ""
            (vis if first.startswith(VISIBLE) else hid).append(li)
        ai = any(re.search(r"AI:</strong>\s*yes", li) or re.search(r"Kind:</strong>[^<]*\bAI\b", li) for li in items)
        found = [t for t in TAG_ORDER if f">{t}<" in m.group(0)]
        strip = "".join(f'<span class="tag {TAGS[t]}">{t}</span> ' for t in found) + ('<span class="tag t-ai">AI step - uses quota</span>' if ai else "")
        det = f'<details><summary>Observed result, evidence and caveats</summary><ul>{"".join(f"<li>{x}</li>" for x in hid)}</ul></details>' if hid else ""
        return f'<article class="stepcard"><h3>{title}</h3><p class="strip">{strip}</p><ul class="vis">{"".join(f"<li>{x}</li>" for x in vis)}</ul>{det}</article>'
    new = re.sub(r"<h3>(Step [^<]*)</h3>\s*<ul>(.*?)</ul>", card, body, flags=re.S)
    if new != body:
        new = new.replace('<article class="stepcard">', '<div class="stepbar"><button type="button" data-expand="1">Expand all details</button> <button type="button" data-expand="0">Collapse all details</button></div><article class="stepcard">', 1)
    return new


def convert(md_text: str, src_name: str) -> str:
    md_text = md_text.replace("@@WINDOW@@", WINDOW_TEXT)
    live = WINDOW_TEXT.startswith("open until")
    status = ("Ripple is open for judges, " + WINDOW_TEXT) if live else "the AI steps are not open. You can read these guides and open Ripple to read stored results; new work is refused with an explanation until the evaluation window opens."
    md_text = md_text.replace("@@STATUS@@", "> **Current status:** " + status)
    md_text = re.sub(r"@@REQUEST:(.*?)@@", lambda m: "\n\n@@REQ@@" + m.group(1).replace("\n", " ") + "@@END@@\n\n", md_text, flags=re.S)
    for key in ("FLOW", "SOURCEMAP", "BEFOREAFTER:manager", "BEFOREAFTER:capex"):
        md_text = md_text.replace(f"@@{key}@@", f"\n\n@@{key}@@\n\n")
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
    body = re.sub(r"<p>@@REQ@@(.*?)@@END@@</p>", lambda m: request_box(html.unescape(m.group(1))), body, flags=re.S)
    body = body.replace("<p>@@FLOW@@</p>", f"<figure>{flow_svg()}<figcaption>Order recorded by the server. Blue = human decision, amber = AI, green = deterministic.</figcaption></figure>")
    body = body.replace("<p>@@SOURCEMAP@@</p>", f"<figure>{source_svg()}<figcaption>Sources of the 13-file subset that matter for the manager and CAPEX thresholds.</figcaption></figure>")
    for k in ("manager", "capex"):
        body = body.replace(f"<p>@@BEFOREAFTER:{k}@@</p>", before_after(k))
    body = re.sub(r'href="([A-Z_]+)\.md"', lambda m: f'href="{SLUG[m.group(1)]}.html"' if m.group(1) in SLUG else m.group(0), body)
    body = body.replace("<table>", '<div class="tw" tabindex="0" role="region" aria-label="Table (scrolls only if the screen is very narrow)"><table>').replace("</table>", "</table></div>")
    body = tags(body)
    body = step_cards(body)
    figs = "".join(f'<figure class="shot"><img src="{p}" alt="{html.escape(c)}" loading="lazy"><figcaption>{html.escape(c)}</figcaption></figure>' for p, c in FIGS.get(src_name, []))
    return body + figs


def page(title: str, nav_slug: str, content: str, ripple_url: str | None) -> str:
    nav = "".join(f'<a href="{slug}.html"{" aria-current=\"page\"" if slug == nav_slug else ""}>{html.escape(label)}</a>' for _, slug, label in PAGES)
    if ripple_url:
        button = f'<a class="btn" href="{html.escape(ripple_url)}" target="_blank" rel="noopener noreferrer">Open Ripple <span class="ext">(opens in a new tab)</span></a>'
    else:
        button = '<span class="btn off" aria-disabled="true">Open Ripple: the link is added when the prototype is deployed</span>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - Ripple judge guide</title><meta name="robots" content="noindex"><link rel="stylesheet" href="style.css"><script src="site.js" defer></script></head>
<body><a class="skip" href="#main">Skip to content</a>
<header><strong>Ripple - judge guide</strong><nav aria-label="Guide">{nav}</nav>{button}</header>
<main id="main">{content}</main>
<footer><p>Read-only documentation. It calls no Ripple API and no AI. Source: <code>docs/judges/*.md</code> in the repository. This site does not replace the working prototype. Screenshots of this site are QA images of the guide, not product-run evidence.</p></footer></body></html>
"""


CSS = """:root{--fg:#111827;--bg:#fff;--mut:#374151;--line:#9ca3af;--link:#0b4fb3;--linkh:#08357d;--visited:#5b21b6;--active:#9f1239;--focus:#b45309;--card:#f9fafb}
@media (prefers-color-scheme:dark){:root{--fg:#f3f4f6;--bg:#111827;--mut:#d1d5db;--line:#6b7280;--link:#93c5fd;--linkh:#dbeafe;--visited:#c4b5fd;--active:#fda4af;--focus:#fcd34d;--card:#1f2937}svg.flow{background:#fff;border-radius:6px}}
*{box-sizing:border-box}body{margin:0;font:16px/1.6 system-ui,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg)}
.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;background:var(--bg);color:var(--fg);padding:8px;z-index:9;outline:3px solid var(--focus)}
header{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
nav{display:flex;flex-wrap:wrap;gap:4px 16px;flex:1}
nav a{color:var(--link);text-decoration:none;padding:4px 2px;border-bottom:2px solid transparent}nav a:visited{color:var(--link)}nav a:hover{color:var(--linkh);border-bottom-color:currentColor}nav a[aria-current=page]{color:var(--fg);font-weight:700;border-bottom-color:var(--fg)}
main a:not(.btn){color:var(--link);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}main a:not(.btn):visited{color:var(--visited)}main a:not(.btn):hover{color:var(--linkh);text-decoration-thickness:3px}main a:not(.btn):active{color:var(--active)}
a:focus-visible,button:focus-visible,summary:focus-visible,.tw:focus-visible,blockquote:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:3px}
.btn{display:inline-block;background:var(--link);color:var(--bg);padding:8px 14px;border-radius:6px;text-decoration:none;font-weight:600;border:2px solid var(--link)}.btn:visited{color:var(--bg)}.btn:hover{background:var(--linkh);border-color:var(--linkh)}.btn:active{background:var(--active);border-color:var(--active)}
.btn.off{background:transparent;color:var(--mut);border:1px dashed var(--line);font-weight:400;font-size:14px}.ext{font-weight:400;font-size:13px}
.cta-link{font-weight:700}
main{max-width:920px;margin:0 auto;padding:16px}h1{line-height:1.25}h2{margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:4px}
.tw{overflow-x:auto;margin:12px 0}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left;word-break:break-word}th{background:rgba(127,127,127,.15)}
code{background:rgba(127,127,127,.18);padding:1px 4px;border-radius:3px;word-break:break-word}pre{overflow-x:auto;padding:10px;background:rgba(127,127,127,.15);border-radius:6px}
blockquote{margin:12px 0;padding:8px 14px;border-left:4px solid var(--link);background:rgba(127,127,127,.12)}
figure{margin:16px 0}img,svg.flow{max-width:100%;height:auto}figcaption{color:var(--mut);font-size:14px}.shot img{border:1px solid var(--line);border-radius:6px}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:0 8px;border-radius:10px;border:1px solid currentColor;white-space:nowrap;line-height:1.6}.t-judge{background:#dbeafe;color:#1e3a8a}.t-live{background:#d1fae5;color:#064e3b}.t-off{background:#ede9fe;color:#4c1d95}.t-nv{background:#fee2e2;color:#7f1d1d}.t-lim{background:#fef3c7;color:#78350f}.t-ai{background:#ffedd5;color:#7c2d12}
dl.summary,.card dl{display:grid;grid-template-columns:minmax(0,11rem) minmax(0,1fr);gap:6px 14px;margin:12px 0;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--card)}dl dt{font-weight:700}dl dd{margin:0}
@media (max-width:600px){dl.summary,.card dl{grid-template-columns:1fr}dl dt{margin-top:6px}}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr));gap:16px;margin:12px 0}.card{border:1px solid var(--line);border-radius:8px;padding:0 12px 8px;background:var(--card)}.card dl{border:0;padding:0;background:none}
ol.route{padding-left:1.2rem}ol.route li{margin:6px 0}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr));gap:14px;margin:12px 0}.panel{border:1px solid var(--line);border-radius:8px;padding:4px 12px 10px;background:var(--card)}.panel.wide{grid-column:1/-1}.panel h4{margin:8px 0}
.delta{font-size:1.3rem;font-weight:700}.delta .from{text-decoration:line-through}.delta .to{color:var(--link)}
table.bars td{position:relative;min-width:6rem}.bar{display:block;height:10px;border-radius:3px;margin-bottom:2px}.bar.b{background:#6b7280}.bar.a{background:#0b4fb3}.num{font-variant-numeric:tabular-nums}tr.chg th,tr.chg td{background:rgba(180,83,9,.15)}table.bars caption{caption-side:top;text-align:left;font-size:13px;color:var(--mut);padding-bottom:4px}
.stepbar{margin:12px 0 4px}button{font:inherit;padding:6px 12px;border:2px solid var(--link);border-radius:6px;background:transparent;color:var(--link);cursor:pointer}button:hover{background:var(--link);color:var(--bg)}button:active{background:var(--active);border-color:var(--active);color:var(--bg)}
.stepcard{border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:0 14px 10px;background:var(--card)}.stepcard h3{margin:12px 0 6px;font-size:1.1rem}.strip{margin:4px 0 6px;display:flex;flex-wrap:wrap;gap:6px}ul.vis{margin:6px 0;padding-left:1.1rem}
.stepcard details{margin-top:6px;border-top:1px dashed var(--line);padding-top:6px}.stepcard summary{cursor:pointer;font-weight:700;padding:4px 0}
.reqbox blockquote{font-size:1.05rem;font-weight:600;user-select:all;word-break:break-word}.copystatus{font-weight:600}.hint{color:var(--mut);font-size:14px;margin-top:0}
footer{max-width:920px;margin:24px auto;padding:0 16px;color:var(--mut);font-size:14px}
@media (max-width:600px){body{font-size:15px}table{font-size:13px}}
"""

JS = """(function(){
  function status(msg){var s=document.getElementById('copystatus');if(s){s.textContent=msg;}}
  function selectText(el){var r=document.createRange();r.selectNodeContents(el);var sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);}
  document.addEventListener('click',function(e){
    var b=e.target.closest('[data-copy-target]');
    if(b){var el=document.getElementById(b.getAttribute('data-copy-target'));var text=el.innerText.trim();selectText(el);
      var done=function(){status('Copied. Paste it into the chat box.');};
      var fail=function(){status('Copy was blocked by the browser. The text is selected: press Ctrl+C (or Cmd+C) to copy it.');};
      if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,function(){try{document.execCommand('copy')?done():fail();}catch(x){fail();}});}
      else{try{document.execCommand('copy')?done():fail();}catch(x){fail();}}
      return;}
    var x=e.target.closest('[data-expand]');
    if(x){var open=x.getAttribute('data-expand')==='1';document.querySelectorAll('.stepcard details').forEach(function(d){d.open=open;});}
  });
})();
"""


def main() -> int:
    global WINDOW_TEXT
    ap = argparse.ArgumentParser()
    ap.add_argument("--ripple-url")
    ap.add_argument("--serving-until")
    args = ap.parse_args()
    WINDOW_TEXT = window_text(args.serving_until)
    OUT.mkdir(parents=True, exist_ok=True)
    widths = "".join(f".w{n}{{width:{n}%}}" for n in range(0, 101))        # bar widths as classes: the page's CSP forbids inline style attributes
    (OUT / "style.css").write_text(CSS + widths + "\n", encoding="utf-8")
    (OUT / "site.js").write_text(JS, encoding="utf-8")
    for src, slug, label in PAGES:
        text = (SRC / f"{src}.md").read_text(encoding="utf-8")
        (OUT / f"{slug}.html").write_text(page(label, slug, convert(text, src), args.ripple_url), encoding="utf-8")
    print("built", [slug + ".html" for _, slug, _ in PAGES])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
