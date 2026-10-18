// Offline design-system guard (see docs/design-system/UI_DEVELOPMENT_RULES.md).
// Static, bounded checks only: they prove style/token/docs contracts, not business correctness or permissions.
// Exit 0 iff there is no new rule violation, no invalid/expired exception and the docs/token parity checks pass.
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative, dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const FRONTEND = resolve(HERE, "..");
export const REPO = resolve(FRONTEND, "..");

const PALETTE = "red|green|blue|gray|grey|slate|zinc|neutral|stone|amber|yellow|orange|lime|emerald|teal|cyan|sky|indigo|violet|purple|fuchsia|pink|rose";
const RULES = [
  { id: "E01", re: new RegExp(`\\b(?:bg|text|border|ring|fill|stroke|from|to|via|outline|divide)-(?:${PALETTE})-\\d{2,3}\\b`, "g"), why: "utility palette colour instead of a semantic token" },
  { id: "E01", re: /\b(?:bg|text|border|ring|fill|stroke|outline)-\[(?:#[0-9a-fA-F]{3,8}|rgb|hsl)[^\]]*\]/g, why: "arbitrary colour value in a class" },
  { id: "E01", re: /(?:className|style|cn\(|clsx\()[^\n]*?(#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\()/g, why: "raw colour literal in styling code" },
  { id: "E02", re: /\b(?:bg|text|border|ring|fill|stroke|outline)-(?:background|foreground|card|accent|destructive|popover|muted-foreground|primary-foreground)\b/g, why: "class names a token that is not defined (use the tokens in index.css)" },
  { id: "E03", re: /<select\b/g, why: "use the shared Select component instead of a raw select", skipPath: "src/components/bits.tsx" },
  { id: "E09", re: /(?:from\s+|import\()\s*["'][^"']*design-system\/(?:Catalog|catalog-main)[^"']*["']/g, why: "the development catalog must not be imported by application code", appOnly: true },
];

export const normalize = (s) => s.replace(/\s+/g, " ").trim();

/** Pure: findings of one source text. fingerprint = rule + path + normalised offending expression (line numbers are not identity). */
export function scanSource(path, text) {
  if (!/\.(tsx?|css)$/.test(path)) return [];
  if (path.endsWith("src/index.css")) return []; // foundation: raw values are defined here on purpose
  const out = [];
  const lines = text.split("\n");
  lines.forEach((line, i) => {
    for (const r of RULES) {
      if (r.appOnly && path.startsWith("src/design-system/")) continue;
      if (r.skipPath && path === r.skipPath) continue;
      r.re.lastIndex = 0;
      let m;
      while ((m = r.re.exec(line))) {
        const expr = normalize(m[1] ?? m[0]);
        out.push({ rule: r.id, path, line: i + 1, expr, why: r.why, fingerprint: `${r.id}|${path}|${expr}` });
        if (m.index === r.re.lastIndex) r.re.lastIndex++;
      }
    }
  });
  return out;
}

/** Pure: colour tokens must exist in the light, prefers-dark and [data-theme=dark] blocks. Fails visibly on unsupported CSS. */
export function tokenParity(css) {
  const grab = (start, label) => {
    const i = css.indexOf(start);
    if (i < 0) throw new Error(`token parity: block not found: ${label}`);
    const j = css.indexOf("}", i);
    return css.slice(i, j);
  };
  const blocks = {
    light: grab(":root {", "light"),
    media: grab(':root:not([data-theme="light"]) {', "prefers-dark"),
    dark: grab(':root[data-theme="dark"] {', "data-theme dark"),
  };
  const colours = (b) => new Set([...b.matchAll(/--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{3,8}|rgb\([^)]*\))/g)].map((m) => m[1]));
  const L = colours(blocks.light), M = colours(blocks.media), D = colours(blocks.dark);
  const problems = [];
  for (const t of L) {
    if (!M.has(t)) problems.push(`--${t} missing in prefers-dark block`);
    if (!D.has(t)) problems.push(`--${t} missing in data-theme=dark block`);
  }
  for (const t of D) if (!L.has(t)) problems.push(`--${t} defined for dark only`);
  return { tokens: [...L].sort(), problems };
}

/** Pure: policy sanity (no wildcard, required fields, expiry). */
export function validatePolicy(policy, today = new Date().toISOString().slice(0, 10)) {
  const errs = [];
  for (const e of policy.exceptions ?? []) {
    for (const k of ["rule", "path", "fingerprint", "reason", "owner", "reviewBy", "verificationRef", "replacementPlan"]) if (!e[k]) errs.push(`exception ${e.fingerprint ?? "?"}: missing ${k}`);
    if (String(e.path ?? "").includes("*") || String(e.fingerprint ?? "").includes("*")) errs.push(`exception ${e.fingerprint}: wildcard not allowed`);
    if (e.reviewBy && e.reviewBy < today) errs.push(`exception ${e.fingerprint}: expired ${e.reviewBy}`);
  }
  for (const b of policy.baseline ?? []) {
    if (String(b.path).includes("*") || String(b.fingerprint).includes("*")) errs.push(`baseline ${b.fingerprint}: wildcard not allowed`);
  }
  return errs;
}

/** Pure: classify findings against the policy. A baseline entry grandfathers exactly `count` (default 1) occurrences. */
export function classify(findings, policy) {
  const budget = new Map();
  for (const b of policy.baseline ?? []) budget.set(b.fingerprint, (budget.get(b.fingerprint) ?? 0) + (b.count ?? 1));
  const excepted = new Set((policy.exceptions ?? []).map((e) => e.fingerprint));
  const fresh = [], legacy = [];
  for (const f of findings) {
    if (excepted.has(f.fingerprint)) continue;
    const left = budget.get(f.fingerprint) ?? 0;
    if (left > 0) { budget.set(f.fingerprint, left - 1); legacy.push(f); } else fresh.push(f);
  }
  return { fresh, legacy };
}

export const DOCS = ["UI_DEVELOPMENT_RULES", "DESIGN_TOKENS", "COMPONENT_GUIDELINES", "LAYOUT_PATTERNS", "CONTENT_AND_STATES", "ACCESSIBILITY"];

/** Pure: docs must exist, not point into the internal development folder, resolve relative links, and name every colour token. */
export function checkDocs(readDoc, exists, tokens) {
  const probs = [];
  for (const d of DOCS) {
    const text = readDoc(d);
    if (text == null) { probs.push(`docs/design-system/${d}.md is missing`); continue; }
    if (/developing\//.test(text)) probs.push(`${d}.md refers to the internal development folder`);
    for (const m of text.matchAll(/\]\((?!https?:|#|mailto:)([^)#\s]+)/g)) if (!exists(m[1])) probs.push(`${d}.md: broken link ${m[1]}`);
  }
  const tk = readDoc("DESIGN_TOKENS");
  if (tk != null) for (const t of tokens) if (!tk.includes(`--${t}`)) probs.push(`DESIGN_TOKENS.md does not document --${t}`);
  return probs;
}

function walk(dir, acc = []) {
  for (const n of readdirSync(dir)) {
    if (n === "node_modules") continue;
    const p = join(dir, n);
    if (statSync(p).isDirectory()) walk(p, acc); else acc.push(p);
  }
  return acc;
}

export function run({ frontend = FRONTEND, repo = REPO, log = console.log } = {}) {
  const policy = JSON.parse(readFileSync(join(frontend, "design-system-policy.json"), "utf8"));
  let code = 0;
  for (const e of validatePolicy(policy)) { log(`POLICY ${e}`); code = 1; }
  const files = walk(join(frontend, "src")).filter((p) => /\.(tsx?|css)$/.test(p) && !/\.test\.ts$/.test(p));
  const findings = [];
  for (const f of files) findings.push(...scanSource(relative(frontend, f).split(sep).join("/"), readFileSync(f, "utf8")));
  findings.sort((a, b) => (a.fingerprint + String(a.line).padStart(6, "0")).localeCompare(b.fingerprint + String(b.line).padStart(6, "0")));
  const { fresh, legacy } = classify(findings, policy);
  for (const f of fresh) { log(`VIOLATION ${f.rule} ${f.path}:${f.line} ${f.why}: ${f.expr}`); code = 1; }
  for (const f of legacy) log(`legacy-debt ${f.rule} ${f.path}:${f.line} ${f.expr}`);
  let tokens = [];
  try {
    const par = tokenParity(readFileSync(join(frontend, "src/index.css"), "utf8"));
    tokens = par.tokens;
    for (const p of par.problems) { log(`VIOLATION E02 src/index.css ${p}`); code = 1; }
  } catch (e) { log(`ERROR ${e.message}`); code = 1; }
  const dir = join(repo, "docs", "design-system");
  const dprobs = checkDocs(
    (d) => (existsSync(join(dir, `${d}.md`)) ? readFileSync(join(dir, `${d}.md`), "utf8") : null),
    (link) => existsSync(resolve(dir, link)),
    tokens,
  );
  for (const p of dprobs) { log(`VIOLATION E08 ${p}`); code = 1; }
  log(`check:design ${code === 0 ? "OK" : "FAILED"} - ${files.length} files, ${fresh.length} new violations, ${legacy.length} legacy-debt entries (debt is not a pass of visual quality)`);
  return code;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exit(run());
