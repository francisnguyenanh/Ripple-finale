'use strict';
/* Support (matrix): what a person may view, simulate, apply or only analyse, per Harbor case. States are text + icon, each
 * supported claim names the test that proves it, and "config does not control this behaviour" shows the code evidence. The data
 * comes from GET /v2/support/matrix (code line numbers are resolved against the pinned Harbor copy at request time). */
(function () {
  const {el, replace, api, enc, S} = Object.assign({}, App, {S: App.S});
  const ICON = {supported: '✓', manual: '✎', final: '⚒', 'not-controlled': '⊘', 'not-supported': '✕', unknown: '?'};
  const LABEL = {supported: 'supported', manual: 'manual', final: 'needs code change (Final)', 'not-controlled': 'config does not control this', 'not-supported': 'not supported', unknown: 'unknown / not rehearsed'};
  const CAPS = [['view', 'View'], ['simulate', 'Simulate (before/after)'], ['apply', 'Apply (typed edit)'], ['analyse', 'Analyse only']];

  async function viewSupport(ctx) {
    const {container} = ctx;
    replace(container, App.loadingEl('support matrix'));
    let m;
    try { await App.loadHead(); m = await api('GET', '/v2/support/matrix?workspace_id=' + enc(ctx.ws)); } catch (e) {
      if (ctx.gen !== S.gen) return;
      if (!(e instanceof App.ApiError)) throw e;
      App.readFailed(container, e, () => App.reloadView()); return;
    }
    if (ctx.gen !== S.gen) return;
    const cell = c => el('td', {data: {state: c.state}}, el('span', {class: 'pill', text: ICON[c.state] + ' ' + LABEL[c.state]}), c.note ? el('div', {class: 'muted', text: c.note}) : null);
    const rows = m.rows.map(r => el('tr', {testid: 'support-row', data: {row: r.id}},
      el('td', {}, el('strong', {text: r.title}), App.T(el('div', {class: 'muted', text: r.where.join('; ')}))),
      CAPS.map(([k]) => cell(r[k])),
      App.T(el('td', {}, (r.code_evidence || []).map(c => el('div', {class: 'muted', text: c.file + ':' + c.line + '  ' + c.text})),
        (r.traces || []).map(t => el('div', {class: 'muted', testid: 'support-trace', text: 'trace: ' + t.claim + ' (' + t.matches + ' match' + (t.matches === 1 ? '' : 'es') + ')'})),
        (r.tests || []).map(t => el('div', {class: 'muted', text: 'test: ' + t.split('/').pop()}))))));
    replace(container, el('h2', {text: 'Support matrix (Harbor demo)'}),
      el('p', {class: 'muted', text: 'What you can view, simulate, apply or only analyse. Nothing outside this table is promised.'}),
      App.B(el('p', {class: 'muted', testid: 'support-biz-note', text: 'Where a cell says "config does not control this behaviour", changing the setting would not change what the system does: the behaviour is written in the program itself, so changing it needs a code change, planned for the final product. The Technical view shows the code lines that prove each statement.'})),
      el('p', {class: 'muted', testid: 'support-ai', text: m.ai}), el('p', {class: 'muted', testid: 'support-isolation', text: m.isolation}),
      el('table', {testid: 'support-matrix'}, el('thead', {}, el('tr', {}, ['Case'].concat(CAPS.map(c => c[1])).map(t => el('th', {text: t})).concat([App.T(el('th', {text: 'Code evidence and proof'}))]))), el('tbody', {}, rows)),
      el('section', {class: 'card'}, el('h3', {text: 'Legend'}), el('ul', {}, Object.entries(m.legend).map(([k, v]) => el('li', {text: ICON[k] + ' ' + LABEL[k] + ' - ' + v})))));
  }

  App.register('support', viewSupport);
})();
