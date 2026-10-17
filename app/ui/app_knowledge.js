'use strict';
/* Analysis job view, Questions (human queue: answer / confirm), Knowledge (findings, conflict
 * resolution, knowledge revisions, compare, accept). The server is the only source of truth: a claim
 * is drawn as human-confirmed only when the server lists an active confirm decision for it. */
(function () {
  const {el, replace, api, act, can, enc, uuid, str, FAIL, S} = Object.assign({}, App, {S: App.S});
  const M = {open: new Set(), cmpFrom: '', cmpTo: '', focusQ: null};
  function reset() { M.open = new Set(); M.cmpFrom = ''; M.cmpTo = ''; }

  const TERMINAL = ['COMPLETED', 'FAILED', 'CANCELLED'];
  function jobState(job) {
    const s = job.execution_status;
    if (TERMINAL.includes(s)) return s;
    if (job.status === 'CANCELLED' || job.cancel_requested_at) return 'CANCELLED';
    return s || 'QUEUED';
  }
  App.jobState = jobState;
  App.TERMINAL_JOB = TERMINAL;

  async function retryJob(ctx, jobId, btn) {
    return act(() => api('POST', '/v2/jobs/' + enc(jobId) + '/retry', {workspace_id: ctx.ws}), {button: btn, stale: true});
  }
  App.retryJob = retryJob;

  // ------------------------------------------------------------------ analysis job
  function jobPanel(ctx, job, state, refreshFailed, reload) {
    const result = job.execution_result || {};
    const candidates = Array.isArray(result.candidates) ? result.candidates : null;
    const live = ['QUEUED', 'RUNNING', 'PENDING', 'PENDING_APPROVAL'].includes(state);
    const cost = job.execution_cost_usd;
    return el('section', {class: 'card', testid: 'job-panel', data: {'job-id': job.job_id}},
      el('h2', {text: 'Analysis job'}),
      el('p', {class: 'muted', text: 'Kind: ' + job.kind + (job.payload && job.payload.artifact_id ? ' - artifact ' + job.payload.artifact_id : '')}),
      refreshFailed ? App.refreshFailedEl(reload) : null,
      el('p', {}, 'Status: ', el('span', {class: 'pill', testid: 'job-status', data: {state}, text: state})),
      el('p', {testid: 'job-cost', text: 'Cost (USD): ' + (cost === null || cost === undefined ? 'not recorded yet' : cost)}),
      state === 'COMPLETED' && candidates
        ? el('p', {text: candidates.length + ' candidate' + (candidates.length === 1 ? '' : 's') + ' extracted' +
            (Array.isArray(result.rejected) && result.rejected.length ? ', ' + result.rejected.length + ' rejected' : '') +
            '. Review them on the Questions page.'}) : null,
      state === 'FAILED' ? el('p', {testid: 'job-error', text: 'Reason: ' + (job.execution_reason || 'the job failed without a recorded reason')}) : null,
      state === 'CANCELLED' ? el('p', {class: 'muted', text: 'This job was cancelled and will not produce a result.'}) : null,
      el('div', {class: 'row'},
        can('source.import') && live ? el('button', {type: 'button', text: 'Cancel', on: {click: async ev => {
          const r = await act(() => api('POST', '/v2/jobs/' + enc(job.job_id) + '/cancel', {workspace_id: ctx.ws}), {button: ev.currentTarget, stale: true});
          if (r !== FAIL && ctx.gen === S.gen) await reload();
        }}}) : null,
        can('source.import') && state === 'FAILED' ? el('button', {type: 'button', text: 'Retry', on: {click: async ev => {
          const r = await retryJob(ctx, job.job_id, ev.currentTarget);
          if (r !== FAIL && ctx.gen === S.gen) await reload();
        }}}) : null));
  }

  async function viewAnalysis(ctx) {
    const {container, route} = ctx;
    if (!route.sub) {
      replace(container, App.emptyEl('No analysis job is open. Use Analyze on an artifact of the Sources page to start one; its progress appears here.'));
      try { await App.loadHead(); } catch (e) { if (e instanceof App.ApiError) App.readFailed(container, e, () => App.reloadView()); }
      return;
    }
    let last = null, failed = false;
    async function tick(first) {
      if (ctx.gen !== S.gen) return;
      let job;
      try {
        job = await api('GET', '/v2/jobs/' + enc(route.sub) + '?workspace_id=' + enc(ctx.ws));
      } catch (e) {
        if (ctx.gen !== S.gen) return;
        if (!(e instanceof App.ApiError)) throw e;
        if (last) { failed = true; draw(); return; }
        App.readFailed(container, e, () => App.reloadView()); return;
      }
      if (ctx.gen !== S.gen) return;
      last = job; failed = false;
      draw();
      if (!TERMINAL.includes(jobState(job))) App.schedule(() => tick(false), 1500);
    }
    function draw() {
      replace(container, jobPanel(ctx, last, jobState(last), failed, () => tick(false)));
    }
    try { await App.loadHead(); } catch (e) {
      if (ctx.gen !== S.gen) return;
      if (e instanceof App.ApiError) { App.readFailed(container, e, () => App.reloadView()); return; }
      throw e;
    }
    replace(container, App.loadingEl('job'));
    await tick(true);
  }

  // ------------------------------------------------------------------ questions
  function provenanceOf(claim, confirmed) {
    if (confirmed.has(claim.candidate_id)) return 'human-confirmed';
    if (claim.applicability === 'UNKNOWN') return 'unknown';
    return 'inferred';
  }
  function claimRow(ctx, claim, prov, qid, reload) {
    const value = claim.value === null || claim.value === undefined ? '' : ' = ' + str(claim.value) + (claim.unit ? ' ' + claim.unit : '');
    const kids = [
      el('div', {}, el('span', {class: 'pill', testid: 'provenance-badge', text: prov}), ' ',
        el('strong', {text: claim.subject}), ' / ', el('code', {text: claim.predicate}), value,
        claim.modality ? el('span', {class: 'muted', text: ' (' + claim.modality + ')'}) : null),
      el('div', {class: 'muted'}, 'Source: ', el('code', {text: claim.logical_path || claim.artifact_id || 'unknown file'}),
        claim.source_revision_id ? ' @ ' + claim.source_revision_id : '',
        claim.scope && Object.keys(claim.scope).length ? ' - scope ' + str(claim.scope) : '')];
    if (prov === 'unknown' && claim.applicability_reason) kids.push(el('div', {class: 'muted', text: 'Why unknown: ' + claim.applicability_reason}));
    for (const q of claim.evidence || []) kids.push(el('blockquote', {testid: 'claim-quote', text: q.quote}));
    if (claim.evidence_redacted) kids.push(el('div', {class: 'muted', text: 'Source quotes are hidden for your role.'}));
    if (qid && can('knowledge.confirm') && prov !== 'human-confirmed') {
      kids.push(el('div', {class: 'row'}, el('button', {type: 'button', text: 'Confirm', on: {click: async ev => {
        const rationale = (S.drafts['rationale:' + qid] || '').trim();
        const r = await act(() => api('POST', '/v2/knowledge/confirm', Object.assign(
          {workspace_id: ctx.ws, claim_id: claim.candidate_id}, rationale ? {rationale} : {})), {button: ev.currentTarget, stale: true});
        if (r === FAIL || ctx.gen !== S.gen) return;
        App.say('Decision recorded: ' + claim.predicate + ' confirmed by you.');
        await reload();
      }}})));
    }
    return el('div', {testid: 'claim', data: {'claim-id': claim.candidate_id, provenance: prov}}, kids);
  }

  /* The stored prompt is generic ("Human confirmation required ..."); a person needs to see WHICH rule it is about. */
  function bizPrompt(item) {
    const c = (item.candidates || [])[0];
    if (c && /^Human confirmation required/.test(item.prompt || '')) {
      return 'Confirm this rule: ' + App.fmt.ruleTitle(c.subject, c.predicate) + ' = ' + App.fmt.valueText(c.value, c.unit, c.unit) + (c.logical_path ? ' (' + App.fmt.basename(c.logical_path) + ')' : '');
    }
    return item.prompt || item.question_id;
  }
  function queueItem(ctx, item, data, reload) {
    const qid = item.question_id;
    const open = M.open.has(qid);
    const answer = data.answersByQ[qid];
    const head = el('button', {type: 'button', class: 'qi-head', 'aria-expanded': String(open)},
      el('span', {testid: 'queue-item-prompt'}, App.both(bizPrompt(item), item.prompt || qid)),
      App.pill(item.status, ({PENDING: 'Waiting for a person', ANSWERED: 'Answered - not yet a decision'})[item.status] || item.status, {testid: 'queue-item-status'}),
      answer ? el('span', {}, el('span', {testid: 'queue-item-answer', text: answer.answer_text}),
        el('span', {class: 'muted'}, ' answered by ', el('span', {testid: 'answer-author', text: answer.actor_id}))) : null);
    const article = el('article', {testid: 'queue-item', data: {'question-id': qid, open: String(open)}}, head);
    article.addEventListener('click', ev => {
      if (M.open.has(qid)) return;
      if (article.querySelector('.qi-detail') && article.querySelector('.qi-detail').contains(ev.target)) return;
      M.open.add(qid); M.focusQ = qid;
      reload(false);
    });
    if (!open) return article;
    const detail = el('div', {class: 'qi-detail'});
    if (can('knowledge.confirm')) {
      const rt = App.bindDraft(el('textarea', {id: 'rat-' + qid, rows: '2'}), 'rationale:' + qid);
      detail.append(el('div', {}, el('label', {for: 'rat-' + qid, text: 'Rationale'}), rt,
        el('p', {class: 'muted', text: 'Use Confirm on a claim below to record your decision with this rationale.'})));
    }
    const claimsBox = el('div', {});
    for (const cand of item.candidates || []) {
      const claim = data.claimsById[cand.candidate_id] || Object.assign({}, cand, {evidence_redacted: !cand.evidence});
      claimsBox.append(claimRow(ctx, claim, provenanceOf(claim, data.confirmed), qid, reload));
    }
    if (!(item.candidates || []).length) claimsBox.append(el('p', {class: 'muted', text: 'No candidate claims are attached to this question.'}));
    detail.append(claimsBox);
    if (can('knowledge.answer')) {
      const ta = App.bindDraft(el('textarea', {id: 'ans-' + qid, rows: '3'}), 'answer:' + qid);
      detail.append(el('div', {},
        el('label', {for: 'ans-' + qid, text: 'Your answer'}), ta,
        el('div', {class: 'row'}, el('button', {type: 'button', text: 'Submit answer', on: {click: async ev => {
          const text = ta.value.trim();
          if (!text) { App.say('Write an answer first.'); return; }
          const r = await act(() => api('POST', '/v2/hitl/answer', {workspace_id: ctx.ws, question_id: qid, answer: text,
            idempotency_key: uuid(), expected_version: item.version}), {button: ev.currentTarget, stale: true});
          if (r === FAIL || ctx.gen !== S.gen) return;
          S.drafts['answer:' + qid] = '';
          App.say('Answer recorded (version ' + (r.version_after || r.version || '') + '). It is a draft until a knowledge owner confirms.');
          await reload();
        }}}))));
    }
    detail.append(el('div', {class: 'row'}, el('button', {type: 'button', class: 'secondary', text: 'Hide details', on: {click: ev => {
      ev.stopPropagation(); M.open.delete(qid); reload(false);
    }}})));
    article.append(detail);
    return article;
  }

  async function loadQnA(ctx) {
    const [queue, overview] = await Promise.all([
      api('GET', '/v2/hitl/queue?workspace_id=' + enc(ctx.ws)),
      api('GET', '/v2/knowledge/overview?workspace_id=' + enc(ctx.ws))]);
    const claimsById = {}; for (const c of overview.claims || []) claimsById[c.candidate_id] = c;
    const answersByQ = {}; for (const a of overview.answers || []) answersByQ[a.question_id] = a;
    const confirmed = new Set((overview.decisions || []).filter(d => d.active && d.action === 'confirm').map(d => d.target));
    const seen = new Set(); const items = [];
    for (const it of queue.items || []) { if (!seen.has(it.question_id)) { seen.add(it.question_id); items.push(it); } }
    return {items, claimsById, answersByQ, confirmed, overview};
  }

  async function viewQuestions(ctx) {
    const {container} = ctx;
    replace(container, App.loadingEl('questions'));
    let data;
    async function load() {
      try {
        await App.loadHead();
        data = await loadQnA(ctx);
      } catch (e) {
        if (ctx.gen !== S.gen) return false;
        if (!(e instanceof App.ApiError)) throw e;
        App.readFailed(container, e, () => App.reloadView()); return false;
      }
      return ctx.gen === S.gen;
    }
    async function draw() {
      const list = el('div', {});
      const attached = new Set();
      for (const item of data.items) {
        for (const c of item.candidates || []) attached.add(c.candidate_id);
        list.append(queueItem(ctx, item, data, reload));
      }
      const loose = (data.overview.claims || []).filter(c => !attached.has(c.candidate_id));
      replace(container,
        el('h2', {text: 'Questions for a human'}),
        data.items.length ? el('p', {class: 'muted', testid: 'questions-note', text: data.items.length + ' question(s) are open. Each one asks a person to confirm what the AI read, or to answer. Confirming records your decision; it does not edit any file.'}) : null,
        data.items.length ? list : App.emptyEl('No question is waiting for a human in this workspace.'),
        loose.length ? el('section', {class: 'card'}, el('h3', {text: 'Claims without a question'}),
          el('p', {class: 'muted', text: 'These were extracted but need no confirmation question; their provenance is shown as the server records it.'}),
          loose.map(c => claimRow(ctx, c, provenanceOf(c, data.confirmed), null, reload))) : null);
    }
    async function reload(refetch) {
      if (refetch !== false) { if (!(await load())) return; }
      await draw();
      if (M.focusQ) {
        const btn = document.querySelector('[data-testid=queue-item][data-question-id="' + CSS.escape(M.focusQ) + '"] .qi-head');
        M.focusQ = null;
        if (btn) btn.focus();
      }
    }
    if (await load()) await draw();
  }

  // ------------------------------------------------------------------ knowledge
  function findingRow(ctx, f, reload) {
    const resolved = !!f.decision;
    const picked = {id: null};
    const kids = [
      el('div', {}, el('strong', {text: f.kind}), ' ', el('span', {class: 'pill', text: f.severity || 'n/a'}), ' ',
        el('span', {class: 'muted', text: f.reason || ''}))];
    const resolveBtn = can('conflict.resolve') && !resolved && f.kind === 'CONFLICT'
      ? el('button', {type: 'button', text: 'Resolve conflict', disabled: true, on: {click: async ev => {
        const r = await act(() => api('POST', '/v2/conflict/resolve', {workspace_id: ctx.ws, conflict_id: f.finding_id,
          authoritative_claim_id: picked.id}), {button: ev.currentTarget, stale: true});
        if (r === FAIL || ctx.gen !== S.gen) return;
        App.say('Conflict resolved: the authoritative claim is recorded as your decision.');
        await reload();
      }}}) : null;
    for (const c of f.claims || []) {
      const label = c.subject + ' / ' + c.predicate + ' = ' + str(c.value) + (c.unit ? ' ' + c.unit : '') + ' (' + (c.logical_path || c.artifact_id || 'unknown file') + ')';
      const radio = can('conflict.resolve') && !resolved && f.kind === 'CONFLICT'
        ? el('input', {type: 'radio', name: 'auth-' + f.finding_id, value: c.candidate_id, on: {change: () => { picked.id = c.candidate_id; resolveBtn.disabled = false; }}}) : null;
      kids.push(el('label', {testid: 'finding-side', data: {'claim-id': c.candidate_id}}, radio, label));
    }
    if (resolved) {
      kids.push(el('p', {class: 'muted', text: 'Resolved by ' + f.decision.actor_id + '; authoritative claim ' + (f.decision.authoritative_claim_id || 'n/a') + '.'}));
    }
    if (resolveBtn) kids.push(el('div', {class: 'row'}, resolveBtn));
    return el('article', {class: 'card', testid: 'finding-row', data: {'finding-id': f.finding_id, state: resolved ? 'RESOLVED' : 'OPEN'}}, kids);
  }

  async function viewKnowledge(ctx) {
    const {container} = ctx;
    replace(container, App.loadingEl('knowledge'));
    let head, history, overview;
    async function load() {
      try {
        head = await App.loadHead();
        [history, overview] = await Promise.all([
          api('GET', '/v2/knowledge/history?workspace_id=' + enc(ctx.ws)),
          api('GET', '/v2/knowledge/overview?workspace_id=' + enc(ctx.ws))]);
      } catch (e) {
        if (ctx.gen !== S.gen) return false;
        if (!(e instanceof App.ApiError)) throw e;
        App.readFailed(container, e, () => App.reloadView()); return false;
      }
      return ctx.gen === S.gen;
    }
    async function reload() { if (await load()) draw(); }
    function draw() {
      const revs = history.revisions || [];
      const byId = {}; for (const r of revs) byId[r.revision_id] = r;
      const opts = sel => [el('option', {value: '', text: 'Select a revision'})].concat(
        revs.map(r => el('option', {value: r.revision_id, text: r.revision_id, selected: r.revision_id === sel})));
      const result = el('div', {testid: 'km-compare-result'});
      function cmp() {
        const a = byId[M.cmpFrom], b = byId[M.cmpTo];
        if (!a || !b) { replace(result, el('p', {class: 'muted', text: 'Choose two revisions to compare.'})); return; }
        const line = (label, r) => el('p', {}, label + ': ', el('code', {text: r.revision_id}),
          ' - source revision ', el('code', {text: r.source_revision_id}), ' - decision epoch ' + r.decision_epoch);
        replace(result, line('From', a), line('To', b),
          el('p', {class: 'muted', text: a.source_revision_id === b.source_revision_id ? 'Same source revision.' : 'Different source revisions.'}));
      }
      const from = el('select', {id: 'cmp-from', testid: 'km-compare-from', on: {change: e => { M.cmpFrom = e.target.value; cmp(); }}}, opts(M.cmpFrom));
      const to = el('select', {id: 'cmp-to', testid: 'km-compare-to', on: {change: e => { M.cmpTo = e.target.value; cmp(); }}}, opts(M.cmpTo));
      from.value = M.cmpFrom; to.value = M.cmpTo;
      cmp();
      const accept = can('knowledge.confirm') ? el('section', {class: 'card', testid: 'km-accept-panel'},
        el('h2', {text: 'Accept knowledge'}),
        head.source_revision_id
          ? el('div', {},
              el('p', {}, 'Accepting pins the knowledge to source revision ', el('code', {text: head.source_revision_id}),
                ' at decision epoch ', el('code', {text: String(head.decision_epoch)}), '.'),
              el('p', {class: 'muted', text: 'The request carries exactly these values. If the workspace moved since this page loaded, the server refuses and nothing is published.'}),
              el('div', {class: 'row'}, el('button', {type: 'button', text: 'Accept knowledge', on: {click: async ev => {
                const r = await act(() => api('POST', '/v2/knowledge/accept', {
                  workspace_id: ctx.ws, knowledge_revision_id: 'km-' + uuid().slice(0, 12),
                  source_revision_id: head.source_revision_id, decision_epoch: head.decision_epoch,
                  expected_version: head.version, idempotency_key: uuid()}), {button: ev.currentTarget, stale: true});
                if (r === FAIL || ctx.gen !== S.gen) return;
                App.say('Knowledge revision accepted.');
                await reload();
              }}})))
          : el('p', {class: 'muted', text: 'There is no source revision to accept knowledge for yet.'})) : null;
      const revList = revs.length ? revs.map(r => el('div', {testid: 'km-revision', data: {
        'revision-id': r.revision_id, current: String(r.revision_id === history.current_knowledge_revision_id)}},
        el('code', {text: r.revision_id}), ' - source ', el('code', {text: r.source_revision_id}), ' - epoch ' + r.decision_epoch,
        r.revision_id === history.current_knowledge_revision_id ? el('span', {class: 'pill', text: 'current'}) : null))
        : [el('p', {class: 'muted', text: 'No knowledge revision has been accepted yet.'})];
      const findings = (overview.findings || []).map(f => findingRow(ctx, f, reload));
      // Three different things people mix up: what the AI read, what people decided, and the accepted knowledge version.
      const claimsN = (overview.claims || []).length, decided = (overview.decisions || []).filter(x => x.active && x.action === 'confirm').length;
      const explain = el('section', {class: 'card', testid: 'knowledge-explain'},
        el('h2', {text: 'Where the knowledge stands'}),
        el('ul', {},
          el('li', {testid: 'km-read'}, el('strong', {text: claimsN + ' rule(s) read by the AI from your files. '}), decided + ' confirmed by a person, ' + Math.max(claimsN - decided, 0) + ' waiting. ',
            el('a', {href: App.hashFor(S.ws, 'explorer'), text: 'See them in the Explorer'}), claimsN ? '' : ' (nothing has been read yet: analyse a file first)'),
          el('li', {testid: 'km-decisions'}, el('strong', {text: decided + ' decision(s) by people. '}), 'A decision is one person confirming one rule or answer. It changes no document.'),
          el('li', {testid: 'km-accepted'}, el('strong', {text: head.knowledge_revision_id ? 'A knowledge version is accepted. ' : 'No knowledge version is accepted yet. '}),
            'Accepting a version is a separate step: it says "this confirmed set is what the system answers from". Confirming rules does not accept a version by itself.')),
        App.T(el('p', {class: 'muted', text: 'knowledge revision: ' + (head.knowledge_revision_id || 'none') + ' - decision epoch ' + head.decision_epoch})));
      replace(container,
        el('h2', {text: 'Knowledge'}),
        explain,
        accept,
        el('section', {class: 'card'}, el('h2', {text: 'Knowledge revisions'}), revList,
          el('div', {class: 'grid2'},
            el('div', {}, el('label', {for: 'cmp-from', text: 'Compare from'}), from),
            el('div', {}, el('label', {for: 'cmp-to', text: 'Compare to'}), to)), result),
        el('section', {}, el('h2', {text: 'Findings'}),
          findings.length ? findings : el('p', {class: 'muted', testid: 'no-findings', text: 'No findings are recorded for this workspace. This only means no finding check has reported anything; it is not proof that the sources agree. Run the checks first (or open the Explorer to see what was and was not analysed).'})));
    }
    if (await load()) draw();
  }

  App.register('analysis', viewAnalysis, {reset});
  App.register('questions', viewQuestions);
  App.register('knowledge', viewKnowledge);
})();
