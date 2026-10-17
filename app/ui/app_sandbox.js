'use strict';
/* Sandbox (J5): who may write right now, queued jobs, the bounded "Run queued jobs" button (no shell), spend and limits.
 * Everything is read from /v2/sandbox/status; the server enforces the lease on every write, these buttons only help.
 * Approval is never done here: a job waiting for approval needs a different person with the approver role. */
(function () {
  const {el, replace, api, act, can, enc, FAIL, S} = Object.assign({}, App, {S: App.S});

  async function viewSandbox(ctx) {
    const {container} = ctx;
    replace(container, App.loadingEl('sandbox'));
    let st;
    async function load() {
      try { st = await api('GET', '/v2/sandbox/status?workspace_id=' + enc(ctx.ws)); } catch (e) {
        if (ctx.gen !== S.gen) return false;
        if (e instanceof App.ApiError && e.status === 404) {
          replace(container, el('h2', {text: 'Sandbox'}), el('p', {class: 'muted', testid: 'sandbox-off', text: 'This workspace is not a shared sandbox: ordinary permissions apply and no lease is needed.'}));
          return false;
        }
        if (!(e instanceof App.ApiError)) throw e;
        App.readFailed(container, e, () => App.reloadView()); return false;
      }
      return ctx.gen === S.gen;
    }
    async function lease(action, btn) {
      const parts = (S.drafts['sb-participants'] || '').split(',').map(x => x.trim()).filter(Boolean);
      const r = await act(() => api('POST', '/v2/sandbox/lease', {workspace_id: ctx.ws, action, participants: parts}), {button: btn});
      if (r === FAIL || ctx.gen !== S.gen) return;
      st = r; draw();
    }
    async function pump(btn) {
      const r = await act(() => api('POST', '/v2/sandbox/pump', {workspace_id: ctx.ws}), {button: btn});
      if (r === FAIL || ctx.gen !== S.gen) return;
      App.say(r.busy ? 'Another run is in progress; try again in a moment.' : r.processed + ' job(s) run (at most ' + r.bounded_by.max_jobs + ' per click).');
      if (await load()) draw();
    }
    function draw() {
      const L = st.lease, mine = st.i_can_write, held = L && L.active;
      const sp = st.spend;
      replace(container, el('h2', {text: 'Sandbox'}),
        el('p', {class: 'muted', text: 'One session writes at a time. Everyone signed in with access can read. Source files only change through an approved change; the seed revision is the reset point.'}),
        el('section', {class: 'card', testid: 'sandbox-lease'},
          el('h3', {text: 'Writing session'}),
          el('p', {testid: 'sandbox-lease-state', data: {state: !held ? 'FREE' : (mine ? 'MINE' : 'OTHER')},
            text: !held ? 'Free: nobody is writing.' : (mine ? 'You (or your participants) may write until ' + L.expires_at + '.' : 'Held by another session until ' + L.expires_at + '; you can read only.')}),
          held ? el('p', {class: 'muted', text: 'Holder ' + L.holder + (L.participants.length ? ', participants ' + L.participants.join(', ') : '')}) : null,
          !held || mine ? el('div', {class: 'row'},
            el('label', {for: 'sb-participants', text: 'Other accounts of your session (comma separated, e.g. your approver)'}),
            App.bindDraft(el('input', {id: 'sb-participants', testid: 'sandbox-participants'}), 'sb-participants')) : null,
          el('div', {class: 'row'},
            !held ? el('button', {type: 'button', testid: 'sandbox-acquire', text: 'Take the writing session', on: {click: ev => lease('acquire', ev.currentTarget)}}) : null,
            mine ? el('button', {type: 'button', testid: 'sandbox-renew', text: 'Extend', on: {click: ev => lease('renew', ev.currentTarget)}}) : null,
            mine ? el('button', {type: 'button', testid: 'sandbox-release', text: 'Release', on: {click: ev => lease('release', ev.currentTarget)}}) : null)),
        el('section', {class: 'card', testid: 'sandbox-jobs'},
          el('h3', {text: 'Jobs'}),
          el('p', {text: st.jobs.queued + ' queued, ' + st.jobs.running + ' running, ' + st.jobs.waiting_for_approval + ' waiting for a different person to approve. Jobs used ' + st.jobs.used_in_lease + ' of ' + st.jobs.limit_per_lease + ' in this session; model-calling jobs ' + st.jobs.provider_used_in_lease + ' of ' + st.jobs.provider_limit_per_lease + ' (the re-extraction after an apply counts).'}),
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'sandbox-pump', text: 'Run queued jobs (bounded)', disabled: !mine || st.jobs.queued === 0, on: {click: ev => pump(ev.currentTarget)}}),
            el('button', {type: 'button', text: 'Refresh', on: {click: async () => { if (await load()) draw(); }}})),
          el('p', {class: 'muted', text: 'Runs at most ' + st.limits.pump_max_jobs + ' queued jobs (about ' + st.limits.pump_seconds + ' s) per click. It never approves a job and never raises a budget.'})),
        el('section', {class: 'card', testid: 'sandbox-spend'},
          el('h3', {text: 'Spend'}),
          el('p', {text: 'Settled ' + sp.settled_usd + ' USD' + (sp.cap_usd !== null ? ' of ' + sp.cap_usd + ' USD (remaining ' + sp.remaining_usd + ')' : '; cap not available')}),
          el('p', {class: 'muted', text: sp.note + '. Viewing the graph calls no model; a provider job shows its cost when it settles. Jobs stop when the remaining budget is below ' + st.limits.min_remaining_usd + ' USD.'})),
        el('section', {class: 'card', testid: 'sandbox-reset'},
          el('h3', {text: 'Reset to the seed'}),
          el('p', {testid: 'sandbox-at-seed', text: st.seed_revision_id ? (st.at_seed ? 'The sources are at the seed revision ' + st.seed_revision_id + '.' : 'The sources moved on from the seed revision ' + st.seed_revision_id + ' (head ' + st.head_source_revision_id + ').') : 'No seed revision is recorded.'}),
          el('p', {class: 'muted', text: 'Reset = the existing history restore toward the seed (preview, then approval by a different person). History, audit and spend are kept.'}),
          st.seed_revision_id && !st.at_seed && can('source.restore') ? el('button', {type: 'button', testid: 'sandbox-prepare-reset', text: 'Prepare reset in History', on: {click: () => App.go('history', null, {restore: st.seed_revision_id})}}) : null));
      if (held && mine && st.jobs.queued > 0) App.schedule(async () => { if (await load()) draw(); }, 4000);
    }
    if (await load()) draw();
  }

  App.register('sandbox', viewSandbox);
})();
