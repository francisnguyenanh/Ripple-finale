'use strict';
/* Explorer (J1): files -> evidence -> rules -> findings/decisions of the CURRENT workspace state, drawn as an SVG graph
 * and as an equivalent list from ONE server response (GET /v2/knowledge/graph). Read-only: opening this page never calls
 * a model. State is always icon + text (never colour alone). Relation edges are proposed identity edges, not causal
 * dependencies; "not analysed" / "stale" / "no evidence" are shown, not hidden. */
(function () {
  const {el, replace, api, act, can, enc, FAIL, S} = Object.assign({}, App, {S: App.S});
  const M = {sel: null, state: '', q: '', mode: 'graph', zoom: 1};
  function reset() { M.sel = null; M.state = ''; M.q = ''; M.mode = 'graph'; M.zoom = 1; }
  // Business wording of a node (deterministic formatting of the extracted fields; technical text stays in the other view).
  function bizLabel(n) {
    const d = n.detail || {};
    if (n.type === 'candidate') return App.fmt.ruleTitle(d.subject, d.predicate) + ' = ' + App.fmt.valueText(d.value, d.unit, d.unit);
    if (n.type === 'artifact') return App.fmt.basename(n.label);
    if (n.type === 'finding') return App.fmt.humanize(d.kind) + (d.reason ? ': ' + String(d.reason).slice(0, 60) : '');
    if (n.type === 'evidence') return 'Quoted text';
    return n.label;
  }
  const EDGE_BIZ = {'source-of': 'comes from the file', 'evidence-of': 'is backed by this quoted text', 'involved-in': 'is part of this finding',
    'decided-by': 'was decided by a person', relation: 'proposed link to another rule (not proven)'};
  const EVENT_BIZ = {extracted: 'Read from the file by the AI', value_changed_in_source: 'The rule reads differently than before',
    'decision:confirm': 'Confirmed by a person (the file was not changed)', source_changed: 'The source file was changed',
    knowledge_revision: 'Knowledge accepted', answer: 'Comment or answer'};

  const STATES = {
    'observed': ['●', 'observed from the file'],
    'not-analysed': ['○', 'not analysed (no completed extraction)'],
    'pending-confirmation': ['?', 'pending confirmation'],
    'confirmed': ['✓', 'confirmed by a person (source file unchanged)'],
    'conflict': ['✕', 'conflict finding open'],
    'scope-mismatch': ['≠', 'scope mismatch - needs review, not automatically a conflict'],
    'gap': ['∅', 'gap - expected coverage missing'],
    'ambiguity': ['~', 'ambiguity / insufficient context'],
    'authority-decided': ['⚖', 'authority chosen; opposing source still exists'],
    'stale': ['⌛', 'stale - from an older source revision'],
    'proposed': ['◇', 'proposed (projection, not a confirmed relation)'],
    'updated': ['↻', 'updated - re-extracted after a source change; earlier decisions are not carried over'],
  };
  const COL = {artifact: 0, evidence: 1, candidate: 2, finding: 3, decision: 4};
  const TYPE_LABEL = {artifact: 'File', evidence: 'Evidence', candidate: 'Rule', finding: 'Finding', decision: 'Decision'};
  const sym = s => (STATES[s] || ['•', s])[0];
  const NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, String(v));
    for (const k of kids) if (k) n.append(k);
    return n;
  }

  function matches(n) {
    if (M.state && n.state !== M.state) return false;
    if (M.q && !(n.label + ' ' + n.id).toLowerCase().includes(M.q.toLowerCase())) return false;
    return true;
  }

  function layout(nodes) {
    const rows = [0, 0, 0, 0, 0], pos = {};
    for (const n of nodes) { const c = COL[n.type] ?? 4; pos[n.id] = {x: 20 + c * 250, y: 20 + rows[c] * 34}; rows[c] += 1; }
    return {pos, height: 40 + Math.max(...rows) * 34};
  }

  function graphSvg(g, visible, select) {
    const vis = new Set(visible.map(n => n.id));
    const {pos, height} = layout(visible);
    const root = svg('svg', {width: Math.round(1250 * M.zoom), height: Math.round(height * M.zoom), viewBox: `0 0 1250 ${height}`, role: 'img', 'aria-label': 'Knowledge graph', 'data-testid': 'graph-svg'});
    for (const e of g.edges) {
      if (!vis.has(e.from) || !vis.has(e.to)) continue;
      const a = pos[e.from], b = pos[e.to];
      const x1 = a.x + 200, y1 = a.y + 12, x2 = b.x, y2 = b.y + 12;
      const same = a.x === b.x;
      const d = same ? `M${a.x + 200} ${y1} C${a.x + 240} ${y1} ${a.x + 240} ${y2} ${a.x + 200} ${y2}` : `M${x1} ${y1} L${x2} ${y2}`;
      root.append(svg('path', {d, fill: 'none', stroke: e.projection ? '#b07a00' : '#7a8696', 'stroke-width': 1.2,
        'stroke-dasharray': e.projection ? '5 3' : null, 'data-edge': e.id, 'data-edge-type': e.type}, svg('title', {}, document.createTextNode(e.type + (e.relation_type ? ' ' + e.relation_type : '') + ' [' + e.state + ']: ' + (e.reason || '')))));
    }
    for (const n of visible) {
      const p = pos[n.id];
      const gg = svg('g', {transform: `translate(${p.x},${p.y})`, tabindex: 0, role: 'button', 'data-node': n.id, 'data-type': n.type, 'data-state': n.state, style: 'cursor:pointer'});
      gg.append(svg('rect', {width: 200, height: 24, rx: 4, fill: M.sel === n.id ? '#dbe9ff' : '#fff', stroke: '#7a8696'}));
      const t = svg('text', {x: 6, y: 16, 'font-size': 11});
      const tb = svg('tspan', {class: 'biz-only'}), tt = svg('tspan', {class: 'tech-only'});
      tb.textContent = sym(n.state) + ' ' + bizLabel(n).slice(0, 34);
      tt.textContent = sym(n.state) + ' ' + n.label.slice(0, 30);
      t.append(tb, tt);
      gg.append(t, svg('title', {}, document.createTextNode(TYPE_LABEL[n.type] + ' - ' + App.phrase.node(n.state) + ': ' + bizLabel(n))));
      gg.addEventListener('click', () => select(n.id));
      gg.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); select(n.id); } });
      root.append(gg);
    }
    return el('div', {style: 'overflow:auto;border:1px solid var(--line);background:#fff'}, root);
  }

  function listView(g, visible, select) {
    const byFile = {};
    for (const n of visible) if (n.type === 'candidate') (byFile[n.detail.logical_path || '(unknown file)'] ||= []).push(n);
    const out = el('div', {testid: 'graph-list'});
    for (const a of visible.filter(n => n.type === 'artifact')) {
      const rules = byFile[a.label] || [];
      out.append(el('section', {class: 'card'},
        el('h3', {}, el('button', {type: 'button', on: {click: () => select(a.id)}}, sym(a.state) + ' ', App.both(App.fmt.basename(a.label), a.label)),
          ' ', el('span', {class: 'muted'}, App.B(el('span', {text: a.detail.extraction === 'PENDING_HUMAN' ? rules.length + ' rule(s) found' : 'not analysed'})), App.T(el('span', {text: a.detail.extraction + ' - ' + rules.length + ' rule(s)'})))),
        rules.length ? el('ul', {}, rules.map(n => el('li', {data: {node: n.id, state: n.state}},
          el('button', {type: 'button', on: {click: () => select(n.id)}}, sym(n.state) + ' ', App.both(bizLabel(n), n.label)),
          ' ', App.pill(n.state, App.phrase.node(n.state))))) : el('p', {class: 'muted', text: 'No rules from this file (see coverage).'})));
    }
    const others = visible.filter(n => n.type === 'finding' || n.type === 'decision');
    if (others.length) out.append(el('section', {class: 'card'}, el('h3', {text: 'Findings and decisions'}),
      el('ul', {}, others.map(n => el('li', {data: {node: n.id, state: n.state}},
        el('button', {type: 'button', text: TYPE_LABEL[n.type] + ' ' + sym(n.state) + ' ' + n.label, on: {click: () => select(n.id)}}))))));
    return out;
  }

  function detailPanel(g, id) {
    const n = g.nodes.find(x => x.id === id);
    if (!n) return el('p', {class: 'muted', text: 'Select a node to see its source, evidence, state and neighbours.'});
    const near = g.edges.filter(e => e.from === id || e.to === id);
    const label = nid => (g.nodes.find(x => x.id === nid) || {label: nid}).label;
    const d = n.detail || {};
    const bl = nid => { const x = g.nodes.find(y => y.id === nid); return x ? bizLabel(x) : nid; };
    const facts = n.type === 'candidate' ? el('dl', {class: 'biz-only', testid: 'node-facts'},
      el('dt', {text: 'Rule'}), el('dd', {text: App.fmt.ruleTitle(d.subject, d.predicate)}),
      el('dt', {text: 'Value'}), el('dd', {text: App.fmt.valueText(d.value, d.unit, d.unit)}),
      el('dt', {text: 'Applies to'}), el('dd', {text: App.fmt.scopeText(d.scope)}),
      el('dt', {text: 'Taken from'}), el('dd', {text: App.fmt.basename(d.logical_path)}),
      el('dt', {text: 'Status'}), el('dd', {text: App.phrase.node(n.state) + (n.provenance === 'model' && n.state === 'pending-confirmation' ? ' (read by the AI from the file)' : '')})) : null;
    return el('section', {class: 'card', testid: 'node-detail', data: {'detail-for': n.id}},
      el('h3', {}, TYPE_LABEL[n.type] + ': ', App.both(bizLabel(n), n.label)),
      el('p', {}, App.pill(n.state, sym(n.state) + ' ' + App.phrase.node(n.state), {testid: 'node-state'}), ' ', App.T(el('span', {class: 'muted', text: (STATES[n.state] || [])[1] || ''}))),
      facts,
      App.T(el('p', {class: 'muted', text: 'Provenance: ' + n.provenance + (n.source_revision_id ? ' - source revision ' + n.source_revision_id : '') + (n.stale ? ' - STALE' : '')})),
      App.T(el('dl', {}, Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== '').flatMap(([k, v]) =>
        [el('dt', {text: k}), el('dd', {text: typeof v === 'string' ? v : JSON.stringify(v)})]))),
      el('h4', {text: 'Connections (' + near.length + ')'}),
      el('ul', {}, near.map(e => el('li', {},
        App.T(el('span', {}, el('code', {text: e.type + (e.relation_type ? ':' + e.relation_type : '')}), ' [' + e.state + (e.projection ? ', projection' : '') + '] ',
          e.from === id ? '→ ' + label(e.to) : '← ' + label(e.from), e.reason ? ' - ' + e.reason : '')),
        App.B(el('span', {text: (e.from === id ? 'this ' + EDGE_BIZ[e.type] + ': ' + bl(e.to) : bl(e.from) + ' - ' + EDGE_BIZ[e.type] + ' (to this item)') + (e.projection ? ' - a guess from similar wording, not a dependency' : '')}))))),
      n.type === 'candidate' ? el('p', {class: 'muted', text: 'Confirming knowledge records a decision; it does not change the source file. Value changes go through Changes.'}) : null,
      n.type === 'candidate' && can('intent.create') && n.state !== 'stale' ? el('div', {class: 'row'}, el('button', {type: 'button', testid: 'request-change', text: 'Request a change to this rule', on: {click: () => App.go('changes', null, {rule: n.id})}})) : null,
      n.type === 'candidate' ? actions(g, n) : null,
      n.type === 'finding' ? resolvePanel(g, n) : null,
      n.type === 'candidate' ? timelinePanel(n) : null);
  }

  function actions(g, n) {
    if (!can('knowledge.confirm') || n.state === 'confirmed' || n.state === 'stale') return null;
    const why = el('input', {id: 'ex-why', testid: 'confirm-rationale', placeholder: 'Why (recorded with your name)'});
    return el('div', {class: 'row', testid: 'confirm-panel'}, why,
      el('button', {type: 'button', testid: 'confirm-rule', text: 'Confirm this rule as knowledge', on: {click: async ev => {
        const rationale = why.value.trim();
        const r = await act(() => api('POST', '/v2/knowledge/confirm', Object.assign({workspace_id: S.ws, claim_id: n.id}, rationale ? {rationale} : {})), {button: ev.currentTarget, stale: true});
        if (r === FAIL) return;
        App.say('Decision recorded; the source file is unchanged.');
        await App.reloadView();
      }}}));
  }

  function resolvePanel(g, n) {
    if (n.state === 'authority-decided') return el('p', {class: 'muted', text: 'An authority decision exists; the opposing source file still says what it said.'});
    if (!can('conflict.resolve') || (n.detail || {}).kind !== 'CONFLICT') return null;
    const cands = g.edges.filter(e => e.type === 'involved-in' && e.to === n.id).map(e => g.nodes.find(x => x.id === e.from)).filter(Boolean);
    let picked = null;
    const btn = el('button', {type: 'button', testid: 'resolve-conflict', text: 'Record this source as authoritative', disabled: true, on: {click: async ev => {
      const r = await act(() => api('POST', '/v2/conflict/resolve', {workspace_id: S.ws, conflict_id: n.id, authoritative_claim_id: picked}), {button: ev.currentTarget, stale: true});
      if (r === FAIL) return;
      App.say('Authority recorded as your decision; no source file was edited.');
      await App.reloadView();
    }}});
    return el('div', {testid: 'resolve-panel'}, el('p', {text: 'Which source is authoritative? (Records a decision; does not edit either file.)'}),
      cands.map(c => el('label', {class: 'row'}, el('input', {type: 'radio', name: 'auth', on: {change: () => { picked = c.id; btn.disabled = false; }}}), ' ' + c.label + ' (' + (c.detail.logical_path || '') + ')')), btn);
  }

  function timelinePanel(n) {
    const box = el('div', {testid: 'node-timeline'}, el('p', {class: 'muted', text: 'Loading history...'}));
    api('GET', '/v2/knowledge/timeline?workspace_id=' + enc(S.ws) + '&node_id=' + enc(n.id)).then(t => {
      const fmt = v => v ? JSON.stringify(v) : '';
      replace(box, el('h4', {text: 'History of this rule'}),
        t.lineage_candidate_ids.length > 1 ? el('p', {class: 'muted', text: 'The same rule, followed across ' + t.lineage_candidate_ids.length + ' readings of the file (same file, name and position).'}) : null,
        (t.lineage_notes || []).map(x => el('p', {class: 'muted', text: 'Note: ' + x})),
        el('ol', {}, t.events.map(e => el('li', {data: {category: e.category, kind: e.kind}},
          el('span', {class: 'pill', text: ({analysis: 'AI reading', knowledge: 'person decision', source: 'file changed'})[e.category] || e.category}), ' ', el('strong', {}, App.both(EVENT_BIZ[e.kind] || e.kind, e.kind)), ' ',
          el('span', {class: 'muted', text: [e.ts, e.actor, e.source_revision_id && 'source ' + e.source_revision_id, e.epoch !== undefined && 'epoch ' + e.epoch].filter(Boolean).join(' - ')}),
          e.why ? el('div', {text: 'Why: ' + e.why}) : null,
          e.before ? el('div', {text: 'Before: ' + fmt(e.before)}) : null, e.after ? el('div', {text: 'After: ' + fmt(e.after)}) : null,
          e.note ? el('div', {class: 'muted', text: e.note}) : null))));
    }).catch(() => replace(box, el('p', {class: 'muted', text: 'History could not be read; try again.'})));
    return box;
  }

  async function viewExplorer(ctx) {
    const {container} = ctx;
    replace(container, App.loadingEl('explorer'));
    let g;
    try {
      await App.loadHead();
      g = await api('GET', '/v2/knowledge/graph?workspace_id=' + enc(ctx.ws));
    } catch (e) {
      if (ctx.gen !== S.gen) return;
      if (!(e instanceof App.ApiError)) throw e;
      App.readFailed(container, e, () => App.reloadView()); return;
    }
    if (ctx.gen !== S.gen) return;
    if (ctx.route.params && ctx.route.params.node) M.sel = ctx.route.params.node;
    const body = el('div', {testid: 'explorer-body'});
    function select(id) { M.sel = id; App.setParams({node: id}); draw(); }
    function draw() {
      const visible = g.nodes.filter(matches);
      const states = [...new Set(g.nodes.map(n => n.state))].sort();
      const cov = g.coverage;
      const unanalysed = cov.artifacts.filter(a => a.extraction !== 'PENDING_HUMAN');
      replace(body,
        el('div', {class: 'row'},
          el('label', {for: 'ex-q', text: 'Search'}), el('input', {id: 'ex-q', testid: 'explorer-search', value: M.q, on: {input: ev => { M.q = ev.target.value; drawInner(); }}}),
          el('label', {for: 'ex-s', text: 'State'}),
          el('select', {id: 'ex-s', testid: 'explorer-state', on: {change: ev => { M.state = ev.target.value; drawInner(); }}},
            [el('option', {value: '', text: 'All'})].concat(states.map(s => el('option', {value: s, text: s, selected: s === M.state})))),
          el('button', {type: 'button', testid: 'explorer-mode', text: M.mode === 'graph' ? 'Show list' : 'Show graph', on: {click: () => { M.mode = M.mode === 'graph' ? 'list' : 'graph'; drawInner(); }}})),
        el('p', {class: 'muted', testid: 'explorer-counts', text: ''}),
        el('div', {class: 'row'}, el('button', {type: 'button', class: 'secondary', testid: 'zoom-out', text: 'Zoom out', on: {click: () => { M.zoom = Math.max(0.4, M.zoom - 0.2); drawInner(); }}}),
          el('button', {type: 'button', class: 'secondary', testid: 'zoom-in', text: 'Zoom in', on: {click: () => { M.zoom = Math.min(2, M.zoom + 0.2); drawInner(); }}}),
          el('button', {type: 'button', class: 'secondary', testid: 'zoom-fit', text: 'Fit to width', on: {click: () => { const w = (body.querySelector('#explorer-main') || {}).clientWidth || 900; M.zoom = Math.min(1, Math.max(0.4, (w - 8) / 1250)); drawInner(); }}}),
          el('span', {class: 'muted', text: 'Click a box to see its details on the right; the list view shows the same things.'})),
        el('div', {class: 'twocol'}, el('div', {id: 'explorer-main'}), el('aside', {id: 'explorer-detail', class: 'side', 'aria-label': 'Details of the selected item'})),
        el('section', {class: 'card', testid: 'explorer-coverage'},
          el('h3', {text: 'Coverage and limits'}),
          el('ul', {},
            unanalysed.map(a => el('li', {}, App.both(App.fmt.basename(a.logical_path) + ': not analysed - no rules from it are shown above', a.logical_path + ': ' + a.extraction + ' - not part of the rules above'))),
            el('li', {text: 'Stale rules: ' + cov.stale_candidates + '; rules without evidence: ' + cov.candidates_without_evidence}),
            el('li', {text: 'Relation comparisons omitted: ' + cov.relation_omitted_comparisons + ' (identity pairs omitted: ' + cov.relation_omitted_identity_comparisons + '); compared and not the same rule: ' + cov.relation_compared_not_same_rule}),
            cov.truncated ? el('li', {text: 'Graph truncated at the node cap.'}) : null,
            cov.evidence_redacted ? el('li', {text: 'Evidence quotes are hidden for your role.'}) : null),
          el('p', {class: 'muted', text: cov.note})),
        el('section', {class: 'card'}, el('h3', {text: 'Legend (icon + text)'}),
          el('ul', {class: 'row'}, Object.entries(STATES).map(([k, v]) => el('li', {}, App.both(v[0] + ' ' + App.phrase.node(k), v[0] + ' ' + k + ' - ' + v[1])))),
          el('p', {class: 'muted', text: 'Solid line = where something comes from (file, quoted text, person decision). Dashed amber line = a proposed link between two rules because they look alike: it is NOT a proof that one depends on the other. "Not analysed" or "UNKNOWN" never means "not affected".'})));
      drawInner();
    }
    function drawInner() {
      const visible = g.nodes.filter(matches);
      replace(body.querySelector('#explorer-main'), visible.length ? (M.mode === 'graph' ? graphSvg(g, visible, select) : listView(g, visible, select)) : App.emptyEl('No node matches the filter.'));
      replace(body.querySelector('#explorer-detail'), detailPanel(g, M.sel));
      const cands = g.nodes.filter(n => n.type === 'candidate'), cnt = st => cands.filter(n => n.state === st).length;
      const box = body.querySelector('[data-testid=explorer-counts]');
      replace(box, App.B(el('span', {text: g.nodes.filter(n => n.type === 'artifact').length + ' files, ' + cands.length + ' rules read by the AI: ' + cnt('confirmed') + ' confirmed by a person, ' + cnt('pending-confirmation') + ' not yet confirmed, ' + cnt('updated') + ' updated after a change, ' + cnt('stale') + ' from an older revision. Showing ' + visible.length + ' of ' + g.nodes.length + ' items.'})),
        App.T(el('span', {text: visible.length + ' of ' + g.nodes.length + ' nodes - ' + g.edges.length + ' edges - source revision ' + (g.head.source_revision_id || 'none') + ' - knowledge revision ' + (g.head.knowledge_revision_id || 'none') + ' - decision epoch ' + g.head.decision_epoch})));
    }
    replace(container, el('h2', {text: 'Explorer'}),
      el('p', {class: 'muted', text: 'The files, the rules the AI read from them, their quoted evidence and findings, for the current version. Looking at this page never calls an AI.'}), body);
    draw();
  }

  App.register('explorer', viewExplorer, {reset});
})();
