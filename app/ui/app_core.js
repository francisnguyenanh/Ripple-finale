'use strict';
/* Ripple v2 shell: core (api client, session, workspace, router, shared states).
 * Rules this file enforces for every view module:
 *  - text is rendered with textContent only (no innerHTML anywhere);
 *  - the client never sends an actor; the server resolves identity from the session cookie;
 *  - a mutation is sent once and is never retried automatically;
 *  - nothing is kept in localStorage/sessionStorage; state = URL hash + server reads;
 *  - all in-memory state is keyed by session/workspace and cleared on sign-out and workspace switch. */
(function () {
  const App = window.App = {views: {}, mods: []};
  const S = App.S = {account: null, workspaces: [], ws: null, head: null, perms: new Set(), roles: [],
                     route: null, gen: 0, drafts: {}, pollTimer: null};
  const $ = id => document.getElementById(id);

  // ------------------------------------------------------------------ dom helpers
  function append(node, kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false) continue;
      if (Array.isArray(kid)) append(node, kid);
      else if (kid instanceof Node) node.append(kid);
      else node.append(document.createTextNode(String(kid)));
    }
  }
  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') node.textContent = String(v);
      else if (k === 'class') node.className = v;
      else if (k === 'testid') node.setAttribute('data-testid', v);
      else if (k === 'data') {
        for (const [dk, dv] of Object.entries(v)) if (dv !== null && dv !== undefined) node.setAttribute('data-' + dk, String(dv));
      } else if (k === 'on') {
        for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      } else if (k === 'value') node.value = v;
      else if (k === 'checked') node.checked = !!v;
      else if (k === 'disabled') node.disabled = !!v;
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    append(node, kids);
    return node;
  }
  function replace(container, ...kids) { container.replaceChildren(); append(container, kids); }
  const str = v => (v === null || v === undefined) ? '' : (typeof v === 'string' ? v : JSON.stringify(v));
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    const b = new Uint8Array(16); crypto.getRandomValues(b);
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  }
  const can = perm => S.perms.has(perm);
  const enc = encodeURIComponent;
  Object.assign(App, {el, replace, str, uuid, can, enc, $});

  // ------------------------------------------------------------------ api client
  class ApiError extends Error {
    constructor(status, body, network) { super('api'); this.status = status; this.body = body || {}; this.network = !!network; }
  }
  async function api(method, path, body) {
    const opts = {method, credentials: 'same-origin', headers: {'X-Ripple-Judge': '1'}};
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let resp;
    try { resp = await fetch(path, opts); } catch (_) { throw new ApiError(0, null, true); }
    let data = null;
    try { data = await resp.json(); } catch (_) { data = null; }
    if (!resp.ok) throw new ApiError(resp.status, data);
    return data;
  }
  function errorText(e) {
    if (e.network) return 'Network error: the server could not be reached';
    if (e.status >= 500) return 'Server error (HTTP ' + e.status + ')';
    const b = e.body || {};
    if (b.code === 'sandbox_lease_required') return 'You cannot change anything right now: ' + (b.held_by_other ? 'another session holds the writing session.' : 'nobody has taken the writing session yet.') + ' Nothing was changed.';
    if (b.code === 'sandbox_job_limit' || b.code === 'sandbox_provider_job_limit') return (b.error || 'The limit of this writing session is reached.') + ' Nothing was changed.';
    if (e.status === 403) return 'Your account is not allowed to do this' + (S.roles && S.roles.length ? ' (your role: ' + S.roles.join(', ') + ')' : '') + ' (code 403). Nothing was changed.';
    let text = e.status + ' ' + (typeof b.error === 'string' ? b.error : 'request refused');
    if (Array.isArray(b.reasons) && b.reasons.length) text += ': ' + b.reasons.map(str).join(', ');
    if (Array.isArray(b.stale_fields) && b.stale_fields.length) text += ': ' + b.stale_fields.join(', ');
    if (typeof b.reason === 'string' && b.reason) text += ' (' + b.reason + ')';
    return text;
  }
  const FAIL = App.FAIL = Symbol('failed');
  Object.assign(App, {api, ApiError, errorText});

  // ------------------------------------------------------------------ shared states
  function say(msg) { $('status').textContent = msg || ''; }
  function clearBanner() { $('banner-slot').replaceChildren(); }
  function showBanner(text, code) {
    replace($('banner-slot'), el('div', {role: 'alert', testid: 'error-banner', data: {'error-code': code || ''}, text}));
  }
  function clearStale() { $('stale-slot').replaceChildren(); }
  function showStale(text) {
    replace($('stale-slot'), el('div', {testid: 'notice-stale'},
      el('span', {text}),
      el('button', {type: 'button', class: 'secondary', text: 'Reload state', on: {click: () => reloadView()}})));
  }
  const loadingEl = (what) => el('div', {testid: 'state-loading', text: 'Loading ' + (what || 'data') + '...'});
  const emptyEl = (text) => el('div', {testid: 'state-empty', text});
  function errorEl(e, retry) {
    return el('div', {testid: 'state-error'},
      el('p', {text: e.network ? 'The server could not be reached.' : 'The server could not complete the request (HTTP ' + e.status + ').'}),
      el('button', {type: 'button', text: 'Try again', on: {click: retry}}));
  }
  Object.assign(App, {say, clearBanner, showBanner, clearStale, showStale, loadingEl, emptyEl, errorEl});

  /* A failed read of a view. 401 -> back to the login form; 403/404 -> denial banner and no data;
   * anything else -> state-error with a Try again action. Returns nothing; the caller stops rendering. */
  function readFailed(container, e, retry) {
    if (e.status === 401) { signedOut(undefined, true); return; }
    if (e.status === 403 || e.status === 404) {
      replace(container, el('section', {class: 'card', testid: 'link-unavailable'},
        el('h2', {text: 'This page is not available to your account'}),
        el('p', {text: 'The link may be incomplete, may belong to another workspace, or your account has no access to it. Nothing was changed and nothing from that workspace is shown.'}),
        el('div', {class: 'row'}, S.ws ? el('a', {href: hashFor(S.ws, 'changes'), role: 'button', class: 'secondary', text: 'Go to Changes in this workspace'}) : null,
          el('a', {href: '#', role: 'button', class: 'secondary', text: 'Choose another workspace', on: {click: ev => { ev.preventDefault(); location.hash = ''; }}}))));
      showBanner(errorText(e), e.status);
      return;
    }
    replace(container, errorEl(e, retry));
  }
  /* A failed mutation. `stale` shows the reload prompt on 409 (a stale version/digest). */
  function actionFailed(e, opts) {
    if (e.status === 401) { signedOut(undefined, true); return; }
    showBanner(errorText(e), e.status || 'network');
    const b = e.body || {};
    if (b.code && String(b.code).startsWith('sandbox_')) {
      const bn = document.querySelector('[data-testid=error-banner]');
      if (bn) bn.append(' ', el('a', {href: hashFor(S.ws, 'sandbox'), text: 'Open the Sandbox page'}));
      refreshLease();
    }
    if (opts && opts.stale && e.status === 409) showStale('The server refused this because the state you were looking at is out of date. Nothing was changed. Reload to see the current state.');
  }
  /* Run a mutation exactly once. Returns FAIL when it failed (banner/notice already shown). */
  async function act(fn, opts) {
    clearBanner();
    const btn = opts && opts.button;
    if (btn) btn.disabled = true;
    try { return await fn(); } catch (e) {
      if (e instanceof ApiError) { App.lastError = e; actionFailed(e, opts); return FAIL; }
      throw e;
    } finally { if (btn && btn.isConnected) btn.disabled = false; }
  }
  Object.assign(App, {readFailed, actionFailed, act});

  // ------------------------------------------------------------------ drafts (in memory only)
  function bindDraft(node, key) {
    if (S.drafts[key] !== undefined) node.value = S.drafts[key];
    node.addEventListener('input', () => { S.drafts[key] = node.value; });
    return node;
  }
  App.bindDraft = bindDraft;

  // ------------------------------------------------------------------ polling
  function stopPolling() { if (S.pollTimer) clearTimeout(S.pollTimer); S.pollTimer = null; }
  function schedule(fn, ms) {
    stopPolling();
    const gen = S.gen;
    S.pollTimer = setTimeout(() => { S.pollTimer = null; if (gen === S.gen) fn(); }, ms);
  }
  App.stopPolling = stopPolling; App.schedule = schedule;
  function refreshFailedEl(retry) {
    return el('div', {testid: 'job-refresh-failed'},
      el('span', {text: 'The latest status could not be read. What is shown may be out of date.'}),
      el('button', {type: 'button', text: 'Refresh status', on: {click: retry}}));
  }
  App.refreshFailedEl = refreshFailedEl;

  // ------------------------------------------------------------------ session / login
  function showLogin(message) {
    $('login').hidden = false;
    $('login-context').hidden = !/^#\/ws\//.test(location.hash);
    $('mode-switch').hidden = true; $('lease-chip').hidden = true; replace($('lease-slot'));
    $('session').hidden = true; $('session').replaceChildren();
    $('workspace-bar').hidden = true; $('main-nav').hidden = true; $('main-nav').replaceChildren();
    $('ws-select').replaceChildren();
    for (const id of ['ws-roles', 'head-source', 'head-km']) $(id).textContent = '';
    $('login-error-slot').replaceChildren();
    if (message) replace($('login-error-slot'), el('p', {role: 'alert', testid: 'login-error', text: message}));
  }
  function purge() {
    S.gen += 1; stopPolling();
    S.account = null; S.workspaces = []; S.ws = null; S.head = null; S.perms = new Set(); S.roles = [];
    S.route = null; S.drafts = {};
    $('view').replaceChildren(); clearBanner(); clearStale(); say('');
    App.mods.forEach(m => m.reset && m.reset());
  }
  /* `keepLink` = the session ended while a link was open (401): keep the hash so signing in again returns to the same request.
   * An explicit sign-out clears it. Only this app's own hash route is ever kept: nothing is navigated to. */
  function signedOut(message, keepLink) {
    purge();
    if (!keepLink) history.replaceState(null, '', '/app');
    showLogin(message || (keepLink ? 'Your session ended. Sign in again to continue where you were.' : ''));
  }
  App.signedOut = signedOut;

  function showSession() {
    $('login').hidden = true;
    replace($('session'),
      el('span', {text: 'Signed in as '}),
      el('strong', {testid: 'session-account', text: S.account}),
      ' ',
      el('button', {type: 'button', id: 'signout', text: 'Sign out', on: {click: signOut}}));
    $('session').hidden = false;
    $('workspace-bar').hidden = false;
    $('mode-switch').hidden = false;
  }
  async function signOut() {
    try { await api('POST', '/v2/auth/logout', {}); } catch (_) { /* purge regardless */ }
    signedOut();
  }

  async function refreshWorkspaces() {
    const data = await api('GET', '/v2/workspaces');
    S.workspaces = data.workspaces || [];
    const select = $('ws-select');
    const keep = S.ws;
    select.replaceChildren(el('option', {value: '', text: 'Select a workspace'}));
    for (const w of S.workspaces) select.append(el('option', {value: w.workspace_id, text: w.workspace_id}));
    const mine = S.workspaces.find(w => w.workspace_id === keep);
    select.value = mine ? keep : '';
    S.perms = new Set(mine ? (mine.permissions || []) : []);
    S.roles = mine ? (mine.roles || []) : [];
    $('ws-roles').textContent = S.roles.join(', ');
  }

  async function loadHead() {
    S.head = null;
    const head = await api('GET', '/v2/workspaces/' + enc(S.ws) + '/head');
    S.head = head;
    showHead();
    return head;
  }
  function showHead() {
    const h = S.head;
    $('head-source').textContent = h ? (h.source_revision_id || 'none') : '';
    $('head-km').textContent = h ? (h.knowledge_revision_id || 'none') : '';
  }
  Object.assign(App, {loadHead, showHead, refreshWorkspaces});

  // ------------------------------------------------------------------ writing-session (lease) preflight
  /* Read-only: who may write in a sandbox workspace and for how long. 404 = this workspace is not a sandbox (nothing to show). */
  App.lease = null;
  async function refreshLease() {
    const ws = S.ws;
    let st = null;
    try { st = ws ? await api('GET', '/v2/sandbox/status?workspace_id=' + enc(ws)) : null; } catch (_) { st = null; }
    if (ws !== S.ws) return;
    App.lease = st;
    const chip = $('lease-chip'), slot = $('lease-slot');
    if (!st) { chip.hidden = true; replace(slot); return; }
    const L = st.lease, mins = L && L.expires_at ? Math.round((Date.parse(L.expires_at) - Date.now()) / 60000) : null;
    let text, kind;
    if (st.i_can_write) { text = 'Writing session: yours' + (mins !== null ? ' (' + Math.max(mins, 0) + ' min left)' : ''); kind = 'ok'; }
    else if (L && L.active) { text = 'Writing session: held by ' + L.holder + ' (' + Math.max(mins, 0) + ' min left) - you can read only'; kind = 'other'; }
    else if (L) { text = 'Writing session ended - nobody can write until it is taken again'; kind = 'ended'; }
    else { text = 'Writing session: nobody has taken it yet - nobody can write'; kind = 'free'; }
    chip.hidden = false; chip.textContent = text; chip.dataset.state = kind;
    if (st.i_can_write) { replace(slot); return; }
    replace(slot, el('div', {class: 'leasebar', testid: 'lease-bar', data: {state: kind}},
      el('strong', {text: kind === 'other' ? 'Read-only for you right now. ' : (kind === 'ended' ? 'The writing session ended. ' : 'Nobody has the writing session. ')}),
      kind === 'other' ? 'Another session is writing; you can still read everything. ' : 'Reading works; creating or approving anything needs the writing session. ',
      el('a', {href: hashFor(S.ws, 'sandbox'), text: 'Open the Sandbox page' + (kind === 'other' ? '' : ' to take it')})));
  }
  App.refreshLease = refreshLease;

  // ------------------------------------------------------------------ routing
  const NAV = [['Sources', 'sources'], ['Explorer', 'explorer'], ['Analysis', 'analysis'], ['Knowledge', 'knowledge'],
               ['Questions', 'questions'], ['Changes', 'changes'], ['History', 'history'], ['Sandbox', 'sandbox'], ['Support', 'support']];
  function parseHash() {
    const raw = location.hash.replace(/^#/, '');
    const q = raw.indexOf('?');
    const path = q >= 0 ? raw.slice(0, q) : raw;
    const params = {};
    if (q >= 0) new URLSearchParams(raw.slice(q + 1)).forEach((v, k) => { params[k] = v; });
    const parts = path.split('/').filter(Boolean);
    if (parts[0] === 'ws' && parts[1]) {
      return {ws: decodeURIComponent(parts[1]), view: parts[2] || 'sources',
              sub: parts[3] ? decodeURIComponent(parts[3]) : '', params};
    }
    return {ws: null, view: null, sub: '', params};
  }
  function hashFor(ws, view, sub, params) {
    let h = '#/ws/' + enc(ws) + '/' + view + (sub ? '/' + enc(sub) : '');
    const pairs = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '');
    if (pairs.length) h += '?' + pairs.map(([k, v]) => enc(k) + '=' + enc(v)).join('&');
    return h;
  }
  /* Update the URL without re-rendering (keeps the page a shareable deep link). */
  function setParams(patch, sub) {
    const r = parseHash();
    if (!r.ws) return;
    const params = Object.assign({}, r.params, patch);
    history.replaceState(null, '', hashFor(r.ws, r.view, sub === undefined ? r.sub : sub, params));
    S.route = parseHash();
  }
  function go(view, sub, params) { location.hash = hashFor(S.ws, view, sub, params); }
  Object.assign(App, {parseHash, hashFor, setParams, go});

  function buildNav() {
    const nav = $('main-nav');
    nav.replaceChildren();
    for (const [label, view] of NAV) {
      nav.append(el('a', {href: hashFor(S.ws, view), text: label, data: {view}, on: {click: ev => {
        const r = parseHash();
        if (r.view === view) { ev.preventDefault(); reloadView(); }
      }}}));
    }
    nav.hidden = false;
  }
  function markNav(view) {
    for (const a of $('main-nav').querySelectorAll('a')) {
      if (a.getAttribute('data-view') === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }

  let routing = false;
  async function route() {
    if (!S.account) return;
    const r = parseHash();
    if (r.params.mode && App.setMode) App.setMode(r.params.mode, true);
    const gen = ++S.gen;
    stopPolling(); clearBanner(); clearStale();
    const view = $('view');
    if (!r.ws) {
      S.ws = null; S.head = null; S.perms = new Set(); S.roles = [];
      showHead(); $('main-nav').hidden = true;
      try { await refreshWorkspaces(); } catch (e) {
        if (gen !== S.gen) return;
        if (e instanceof ApiError && e.status === 401) { signedOut(undefined, true); return; }
        readFailed(view, e, () => route()); return;
      }
      if (gen !== S.gen) return;
      replace(view, el('p', {class: 'muted', text: 'Choose a workspace to begin.'}));
      return;
    }
    const changed = S.ws !== r.ws;
    if (changed) { S.drafts = {}; App.mods.forEach(m => m.reset && m.reset()); S.head = null; showHead(); }
    S.ws = r.ws; S.route = r;
    replace(view, loadingEl('workspace'));
    try { await refreshWorkspaces(); } catch (e) {
      if (gen !== S.gen) return;
      if (e instanceof ApiError && e.status === 401) { signedOut(undefined, true); return; }
      readFailed(view, e, () => route()); return;
    }
    if (gen !== S.gen) return;
    buildNav(); markNav(r.view);
    refreshLease();
    const handler = App.views[r.view];
    if (!handler) { replace(view, emptyEl('This page does not exist.')); return; }
    const userReload = App.userReload; App.userReload = false;
    await handler({gen, ws: r.ws, route: r, container: view, userReload});
  }
  async function reloadView() { clearBanner(); clearStale(); App.userReload = true; await route(); }
  App.reloadView = reloadView;

  App.register = function (name, handler, mod) { App.views[name] = handler; if (mod) App.mods.push(mod); };

  // ------------------------------------------------------------------ boot
  async function boot() {
    let me;
    try { me = await api('GET', '/v2/auth/me'); } catch (e) {
      showLogin(e.status && e.status !== 401 ? 'The service is not available (HTTP ' + e.status + ').' : '');
      return;
    }
    S.account = me.account_id;
    showSession();
    await route();
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('login-form').addEventListener('submit', async ev => {
      ev.preventDefault();
      const account = $('acct').value, credential = $('cred').value;
      $('login-error-slot').replaceChildren();
      try {
        await api('POST', '/v2/auth/login', {account_id: account, credential});
      } catch (e) {
        $('cred').value = '';
        replace($('login-error-slot'), el('p', {role: 'alert', testid: 'login-error',
          text: e.status === 401 ? 'Sign-in failed: the account or credential was not accepted.' : 'Sign-in failed (' + errorText(e) + ').'}));
        return;
      }
      $('cred').value = ''; $('acct').value = '';
      await boot();
    });
    const select = $('ws-select');
    const pick = () => {
      const value = select.value;
      if (value && value !== S.ws) {
        const r = parseHash();
        location.hash = hashFor(value, r.view && r.ws ? r.view : 'sources');
      }
    };
    select.addEventListener('change', pick);
    select.addEventListener('keyup', ev => { if (ev.key === 'Enter') pick(); });
    window.addEventListener('hashchange', route);
    boot();
  });
})();
