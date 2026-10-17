"""QA of the static judge website (offline, read-only): horizontal overflow, link/asset existence, text and link contrast (light and dark), keyboard focus, copy button (granted and denied clipboard),
expand/collapse, requests made by the page (must be same-origin only). Screenshots go outside Git. These are QA images of the guide, NOT product-run evidence.
    python -m tools.judge_site.qa [--base https://host]     (default: the local files under site/judges)"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site" / "judges"
SHOTS = pathlib.Path(os.environ.get("JUDGE_QA_OUT") or (pathlib.Path(__import__("tempfile").gettempdir()) / "judge_site_qa"))
PAGES = ["index", "one-role-guide", "four-role-case", "four-role-replay"]


def lum(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse(c: str):
    m = re.findall(r"[\d.]+", c)
    return tuple(int(float(x)) for x in m[:3])


JS_COLORS = r"""() => {
  const parse = (c) => { const m = c.match(/[\d.]+/g).map(Number); return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1]; };
  const bg = (el) => { const chain = []; while (el) { chain.push(parse(getComputedStyle(el).backgroundColor)); el = el.parentElement; }
    let cur = [255, 255, 255]; for (let i = chain.length - 1; i >= 0; i--) { const [r, g, b, a] = chain[i]; cur = [r * a + cur[0] * (1 - a), g * a + cur[1] * (1 - a), b * a + cur[2] * (1 - a)]; }
    return 'rgb(' + cur.map(Math.round).join(',') + ')'; };
  const pick = (sel) => Array.from(document.querySelectorAll(sel)).slice(0, 40).map(e => ({sel, text: (e.textContent||'').trim().slice(0, 30), color: getComputedStyle(e).color, bg: bg(e)}));
  const root = getComputedStyle(document.documentElement);
  return {items: [...pick('main p'), ...pick('main li'), ...pick('main a:not(.btn)'), ...pick('header nav a'), ...pick('.btn'), ...pick('.tag'), ...pick('button'), ...pick('figcaption'), ...pick('th'), ...pick('dt')],
          visited: root.getPropertyValue('--visited').trim(), link: root.getPropertyValue('--link').trim(), pagebg: bg(document.body)};
}"""


def hexrgb(h: str):
    h = h.strip().lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    a = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []
    url = lambda p: (a.base.rstrip("/") + f"/{p}.html") if a.base else (SITE / f"{p}.html").resolve().as_uri()
    # 1. internal links and assets
    for p in PAGES:
        txt = (SITE / f"{p}.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', txt))
        for ref in re.findall(r'(?:href|src)="([^"]+)"', txt):
            if ref.startswith(("http://", "https://", "mailto:")):
                if "pitchrc---ripple" not in ref:
                    fails.append(f"{p}: external URL {ref}")
                continue
            if ref.startswith("#"):
                if ref[1:] not in ids:
                    fails.append(f"{p}: missing anchor {ref}")
                continue
            if not (SITE / ref.split("#")[0]).exists():
                fails.append(f"{p}: broken internal {ref}")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for scheme in ("light", "dark"):
            for name, w in (("desktop", 1100), ("mobile", 390)):
                ctx = b.new_context(viewport={"width": w, "height": 900}, color_scheme=scheme, permissions=["clipboard-read", "clipboard-write"])
                for p in PAGES:
                    pg = ctx.new_page()
                    reqs = []
                    pg.on("request", lambda r: reqs.append(r.url))
                    errs = []
                    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
                    pg.goto(url(p))
                    pg.wait_for_load_state("load")
                    sw = pg.evaluate("document.documentElement.scrollWidth")
                    if sw > w:
                        fails.append(f"{scheme}/{name}/{p}: horizontal scroll {sw}>{w}")
                    foreign = [r for r in reqs if not (r.startswith("file:") or (a.base and r.startswith(a.base)) or r.startswith("data:"))]
                    if foreign:
                        fails.append(f"{p}: third-party/other requests {foreign[:2]}")
                    if errs and a.base:
                        fails.append(f"{p}: console errors {errs[:2]}")
                    data = pg.evaluate(JS_COLORS)
                    for it in data["items"]:
                        c = contrast(parse(it["color"]), parse(it["bg"]))
                        if c < 4.5 and it["text"]:
                            fails.append(f"{scheme}/{p}: contrast {c:.2f} for {it['sel']} '{it['text']}' {it['color']} on {it['bg']}")
                    bgc = parse(data["pagebg"])
                    vc = contrast(hexrgb(data["visited"]), bgc)
                    lc = contrast(hexrgb(data["link"]), bgc)
                    if vc < 4.5 or lc < 4.5:
                        fails.append(f"{scheme}/{p}: link {lc:.2f} / visited {vc:.2f}")
                    if scheme == "light" and p in ("index", "one-role-guide", "four-role-case"):
                        pg.screenshot(path=str(SHOTS / f"{p}_{name}.png"), full_page=True)
                    pg.close()
                ctx.close()
        # keyboard focus + copy + expand on the guide page (light, desktop)
        ctx = b.new_context(viewport={"width": 1100, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
        pg = ctx.new_page()
        pg.goto(url("one-role-guide"))
        seen = 0
        for _ in range(14):
            pg.keyboard.press("Tab")
            ok = pg.evaluate("() => { const e = document.activeElement; if (!e || e === document.body) return null; const s = getComputedStyle(e); return parseFloat(s.outlineWidth) >= 2 && s.outlineStyle !== 'none'; }")
            if ok is False:
                fails.append("keyboard: focused element without a visible outline")
            seen += 1 if ok else 0
        if seen < 6:
            fails.append(f"keyboard: only {seen} focus stops with an outline")
        pg.locator(".copybtn").click()
        pg.wait_for_timeout(500)
        msg = pg.locator("#copystatus").inner_text()
        if "Copied" not in msg:
            fails.append(f"copy (granted): status was '{msg}'")
        clip = pg.evaluate("navigator.clipboard.readText()")
        if not clip.startswith("Please raise the standard manager approval threshold from 500,000 JPY to 800,000 JPY"):
            fails.append("copy: clipboard text differs from the request")
        pg.locator("[data-expand='1']").first.click()
        if pg.locator(".stepcard details[open]").count() != pg.locator(".stepcard details").count():
            fails.append("expand all did not open every card")
        pg.locator("[data-expand='0']").first.click()
        if pg.locator(".stepcard details[open]").count() != 0:
            fails.append("collapse all did not close every card")
        ctx.close()
        ctx = b.new_context(viewport={"width": 1100, "height": 900})          # no clipboard permission: the fallback message and the selected text
        pg = ctx.new_page()
        pg.goto(url("one-role-guide"))
        pg.locator(".copybtn").click()
        pg.wait_for_timeout(600)
        msg = pg.locator("#copystatus").inner_text()
        sel = pg.evaluate("window.getSelection().toString()")
        if not ("Copied" in msg or "Ctrl+C" in msg) or "Please raise" not in sel:
            fails.append(f"copy (denied): status '{msg}', selection '{sel[:30]}'")
        ctx.close()
        b.close()
    print("QA FAILURES:" if fails else "QA PASS (no failures)")
    for f in fails:
        print(" -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
