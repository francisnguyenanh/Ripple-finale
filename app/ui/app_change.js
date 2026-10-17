'use strict';
/* Changes view (intent -> impact -> simulation -> proposal -> review -> approval -> apply ->
 * verification / reanalysis / knowledge -> result decision) and History view (revisions, diff,
 * restore preview/approval/apply, manual refresh results shared with Sources).
 *
 * Truthfulness rules kept here:
 *  - a decision request carries exactly the digest that was on screen (captured when the page was
 *    drawn); a stale page never re-reads a newer digest and approves it on the person's behalf;
 *  - "Completed" is written only when the server holds a human result decision;
 *  - blockers, limitations, manual work and reopened decisions are always shown, never hidden. */
(function () {
  const {el, replace, api, act, can, enc, uuid, str, FAIL, S} = Object.assign({}, App, {S: App.S});
  const M = {cache: {rev: {}, diff: {}}, staleShown: false, refusal: null};
  function reset() { M.cache = {rev: {}, diff: {}}; M.staleShown = false; M.refusal = null; }
  const jobState = j => App.jobState(j);
  const pill = (text, state) => el('span', {class: 'pill', data: {state: state || text}, text});

  async function revisionView(ctx, rev) {
    if (!rev) return null;
    if (!M.cache.rev[rev]) M.cache.rev[rev] = await api('GET', '/v2/sources/revisions/' + enc(rev) + '?workspace_id=' + enc(ctx.ws));
    return M.cache.rev[rev];
  }
  const getJob = (ctx, id) => id ? api('GET', '/v2/jobs/' + enc(id) + '?workspace_id=' + enc(ctx.ws)) : null;
  function parseValue(text) {
    const t = String(text).trim();
    if (/^-?\d+$/.test(t)) return parseInt(t, 10);
    if (/^-?\d*\.\d+$/.test(t)) return parseFloat(t);
    if (t === 'true') return true;
    if (t === 'false') return false;
    return t;
  }

  // ------------------------------------------------------------------ outcome wording
  function outcomeText(dto) {
    if (!dto) return 'No change has been applied yet.';
    const by = dto.completion && dto.completion.decision ? ' by ' + dto.completion.decision.actor_id : '';
    return ({
      STAGED: 'Queued: nothing is published yet.',
      APPROVED: 'Nothing was published: this change carries manual work only, or it has not been applied.',
      SOURCE_PUBLISHED: 'Published to the source. Verification and reanalysis are still pending.',
      REANALYSING: 'Published. The AI reanalysis is still running; this is not a finished result.',
      MACHINE_VERIFIED: 'Machine checks passed. The knowledge candidate still needs its owner and a human result decision.',
      AWAITING_ACCEPTANCE: 'Waiting for the human result decision.',
      COMPLETED: 'Completed' + by + ' (a human result decision is on record).',
      FAILED: 'Failed: this change needs recovery.',
      RECOVERY_REQUIRED: 'Rejected by a human: recovery is required.',
      STALE: 'Stale: the source moved after this change. Do not rely on it.',
    })[dto.state] || ('State: ' + dto.state);
  }

  // ------------------------------------------------------------------ change section (shared)
  function changeLive(cs) {
    if (!cs) return false;
    if (!App.TERMINAL_JOB.includes(jobState(cs.applyJob))) return true;
    return ['STAGED', 'SOURCE_PUBLISHED', 'REANALYSING', 'MACHINE_VERIFIED', 'AWAITING_ACCEPTANCE'].includes(cs.dto.state);
  }

  async function fetchChange(ctx, applyId) {
    const ws = ctx.ws;
    const [applyJob, dto] = await Promise.all([
      getJob(ctx, applyId), api('GET', '/v2/changes/' + enc(applyId) + '?workspace_id=' + enc(ws))]);
    const ids = [].concat(dto.reanalysis.job_ids || [], dto.reanalysis.invalidation_job_ids || [], dto.reanalysis.downstream_job_ids || []);
    const uniq = Array.from(new Set(ids));
    const [jobs, rev] = await Promise.all([
      Promise.all(uniq.map(id => getJob(ctx, id))),
      dto.published_revision_id ? revisionView(ctx, dto.published_revision_id) : null]);
    return {applyJob, dto, reJobs: jobs, rev};
  }

  function resultPermission(dto) { return dto.subject === 'RESTORE' ? 'source.restore' : 'change.accept_result'; }

  function changeSection(ctx, cs, hooks) {
    const dto = cs.dto, decided = !!(dto.completion && dto.completion.decision);
    const canDecide = can(resultPermission(dto)) && !decided;
    const head = S.head;
    const kids = [el('h2', {text: 'Change result'})];
    kids.push(el('p', {}, 'Apply job: ', el('span', {class: 'pill', testid: 'state-apply', data: {'job-id': dto.apply_job_id, state: jobState(cs.applyJob)}, text: jobState(cs.applyJob)}),
      ' ', el('code', {text: dto.apply_job_id})));
    kids.push(el('p', {}, 'Change state: ', el('span', {class: 'pill', testid: 'state-change', data: {state: dto.state}, text: dto.state})));
    kids.push(el('p', {}, 'Machine verification: ', el('span', {class: 'pill', testid: 'state-verification',
      data: {state: dto.verification ? dto.verification.status : 'NOT_STARTED'}, text: dto.verification ? dto.verification.status : 'NOT_STARTED'})));
    kids.push(el('p', {}, 'AI reanalysis: ', el('span', {class: 'pill', testid: 'state-reanalysis', data: {state: dto.reanalysis.status}, text: dto.reanalysis.status}),
      dto.reanalysis.reason ? el('span', {class: 'muted', text: ' - ' + dto.reanalysis.reason}) : null));
    for (const job of cs.reJobs) {
      if (!job) continue;
      const st = jobState(job);
      kids.push(el('div', {testid: 'reanalysis-job', data: {'job-id': job.job_id, state: st}},
        pill(st), ' ', el('span', {text: job.kind + ' '}), el('code', {text: job.job_id}),
        job.execution_reason && st === 'FAILED' ? el('span', {class: 'muted', text: ' - ' + job.execution_reason}) : null,
        st === 'FAILED' && can('source.import') ? el('button', {type: 'button', class: 'secondary', text: 'Retry', on: {click: async ev => {
          const r = await App.retryJob(ctx, job.job_id, ev.currentTarget);
          if (r !== FAIL && ctx.gen === S.gen) await hooks.reload();
        }}}) : null));
    }
    kids.push(el('p', {}, 'Knowledge: ', el('span', {class: 'pill', testid: 'state-knowledge', data: {state: dto.knowledge.status}, text: dto.knowledge.status}),
      el('span', {class: 'muted', text: ' - retrieval index ' + dto.knowledge.retrieval_index_status})));
    kids.push(el('p', {}, 'Completion: ', el('span', {class: 'pill', testid: 'state-completion', data: {state: dto.completion.status}, text: dto.completion.status})));
    kids.push(el('p', {testid: 'state-outcome', text: outcomeText(dto)}));
    const lin = dto.lineage || {};
    kids.push(el('div', {class: 'grid2'},
      el('p', {}, 'Source before: ', el('code', {testid: 'source-before', text: lin.parent_source_revision_id || 'none'})),
      el('p', {}, 'Source after: ', el('code', {testid: 'source-after', text: lin.source_revision_id || 'not published'})),
      el('p', {}, 'Knowledge before: ', el('code', {testid: 'km-prior', text: lin.prior_knowledge_revision_id || 'none'})),
      el('p', {}, 'Knowledge candidate: ', el('code', {testid: 'km-candidate', text: lin.knowledge_candidate_revision_id || 'none'}))));
    kids.push(el('p', {class: 'muted', text: 'Global alignment: ' + (dto.alignment ? dto.alignment.global : 'NOT_ASSESSED') + ' (this page never claims the whole system is aligned).'}));
    if (dto.verification) {
      kids.push(el('p', {}, 'Verification report digest: ', el('code', {testid: 'report-digest', text: dto.verification.report_digest})));
    }
    const reopened = (dto.carry_forward && dto.carry_forward.reopened) || [];
    if (reopened.length) kids.push(el('h3', {text: 'Decisions the rebuild reopened'}));
    for (const item of reopened) {
      kids.push(el('div', {class: 'blocker', testid: 'reopened-decision-item'}, item.decision_id ? item.decision_id + ': ' : '', item.reason || str(item)));
    }
    // manual work
    for (const m of dto.manual_work || []) {
      const proof = m.proof;
      const box = el('div', {testid: 'manual-work-item', data: {index: m.index}}, el('strong', {text: 'Manual work #' + m.index + ': '}), m.reason || m.action || '');
      if (proof) box.append(el('p', {class: 'muted', text: 'Proof recorded by ' + proof.actor_id + ': ' + proof.evidence_ref}));
      else if (canDecide) {
        const input = App.bindDraft(el('input', {id: 'fld-proof-' + m.index, type: 'text', testid: 'field-work-proof', 'aria-label': 'Evidence of the manual work'}), 'proof:' + m.index);
        box.append(el('div', {class: 'row'}, input, el('button', {type: 'button', testid: 'action-submit-work-proof', text: 'Submit proof', on: {click: async ev => {
          const text = input.value.trim();
          if (!text) { App.say('Describe the evidence first.'); return; }
          const r = await act(() => api('POST', '/v2/changes/' + enc(dto.apply_job_id) + '/work-proof', {
            workspace_id: ctx.ws, work_index: m.index, evidence_ref: text}), {button: ev.currentTarget, stale: true});
          if (r === FAIL || ctx.gen !== S.gen) return;
          S.drafts['proof:' + m.index] = '';
          await hooks.reload();
        }}})));
      }
      kids.push(box);
    }
    // gate reasons
    if (!decided) {
      const reasons = (dto.completion.blocked_reasons || []);
      kids.push(el('div', {}, el('h3', {text: 'Why this is not complete yet'}),
        el('div', {testid: 'completion-reasons', text: reasons.length ? reasons.join(', ') : 'no blocking reasons reported'})));
    }
    const findingIds = [];
    for (const g of (dto.alignment && dto.alignment.residual) || []) if (g.kind === 'critical_gap') findingIds.push(g.finding_id);
    for (const g of (dto.result_view && dto.result_view.conflicts && dto.result_view.conflicts.residual) || []) findingIds.push(typeof g === 'string' ? g : g.finding_id);
    for (const f of (M.refusal && M.refusal.applyId === dto.apply_job_id ? M.refusal.findings : [])) findingIds.push(typeof f === 'string' ? f : (f.finding_id || str(f)));
    for (const id of Array.from(new Set(findingIds.filter(Boolean)))) kids.push(el('div', {class: 'blocker', testid: 'finding-item', text: 'Open finding: ' + id}));
    // exports
    if (cs.rev && dto.published_revision_id) {
      const scope = (dto.result_view && dto.result_view.scope) || [];
      const arts = (cs.rev.artifacts || []).filter(a => !scope.length || scope.includes(a.artifact_id));
      if (arts.length) kids.push(el('h3', {text: 'Exported files (published revision)'}));
      for (const art of arts) {
        const run = () => App.downloadArtifact(ctx, cs.rev.revision_id, art);
        kids.push(el('div', {}, el('a', {role: 'link', tabindex: '0', testid: 'export-link', data: {'logical-path': art.logical_path},
          text: 'Export ' + art.logical_path, on: {click: run, keydown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); run(); } }}})));
      }
    }
    // actions
    const actions = el('div', {class: 'row'});
    if (can('knowledge.confirm') && dto.knowledge.candidate_revision_id && !dto.knowledge.accepted && !decided && dto.state !== 'STALE') {
      actions.append(el('button', {type: 'button', testid: 'action-accept-km', text: 'Accept knowledge candidate', on: {click: async ev => {
        const h = head;
        const r = await act(() => api('POST', '/v2/knowledge/accept', {
          workspace_id: ctx.ws, knowledge_revision_id: dto.knowledge.candidate_revision_id,
          source_revision_id: h.source_revision_id, decision_epoch: h.decision_epoch,
          expected_version: h.version, idempotency_key: uuid()}), {button: ev.currentTarget, stale: true});
        if (r === FAIL || ctx.gen !== S.gen) return;
        App.say('Knowledge candidate accepted.');
        await hooks.reload();
      }}}));
      kids.push(el('p', {class: 'muted', text: 'Accepting pins the candidate to source revision ' + (head && head.source_revision_id) + ' at decision epoch ' + (head && head.decision_epoch) + '.'}));
    }
    if (canDecide) {
      const digest = dto.verification ? dto.verification.report_digest : null;
      const reason = App.bindDraft(el('textarea', {id: 'fld-result-reason', rows: '2', testid: 'field-decision-reason', 'aria-label': 'Reason for the result decision'}), 'result-reason');
      kids.push(el('div', {}, el('label', {for: 'fld-result-reason', text: 'Result decision reason (optional)'}), reason));
      const decide = (action, label, testid, secondary) => el('button', {type: 'button', testid, class: secondary ? 'secondary' : '',
        text: label, disabled: !digest || (action === 'accept' && !dto.completion.eligible), on: {click: async ev => {
          const r = await act(() => api('POST', '/v2/changes/' + enc(dto.apply_job_id) + '/result-decision', {
            workspace_id: ctx.ws, action, reason: reason.value.trim() || ({accept: 'Accepted on the change page.',
              request_changes: 'Changes requested on the change page.', reject: 'Rejected on the change page.'})[action],
            expected_report_digest: digest, idempotency_key: uuid()}), {button: ev.currentTarget});
          if (r === FAIL) {
            const e = App.lastError;
            M.refusal = {applyId: dto.apply_job_id, findings: (e && e.body && e.body.findings) || []};
            if (M.refusal.findings.length) await hooks.redraw();
            return;
          }
          M.refusal = null;
          if (ctx.gen !== S.gen) return;
          App.say(action === 'accept' ? 'Result decision recorded.' : 'Your decision was recorded.');
          await hooks.reload();
        }}});
      actions.append(decide('accept', 'Accept result', 'action-accept-result'),
        decide('request_changes', 'Request changes', 'action-request-changes', true),
        decide('reject', 'Reject result', 'action-reject-result', true));
    }
    kids.push(actions);
    return el('section', {class: 'card', testid: 'flow-change'}, kids);
  }

  /* The apply job exists (its id is in the link) but its state could not be read yet: keep the change
   * screen with an honest placeholder instead of dropping it (never a success wording). */
  function unreadChange(applyId) {
    return el('section', {class: 'card', testid: 'flow-change'},
      el('h2', {text: 'Change result'}),
      el('p', {}, 'Apply job: ', el('span', {class: 'pill', testid: 'state-apply', data: {'job-id': applyId, state: 'UNKNOWN'}, text: 'UNKNOWN'}), ' ', el('code', {text: applyId})),
      el('p', {}, 'Change state: ', el('span', {class: 'pill', testid: 'state-change', data: {state: 'UNKNOWN'}, text: 'UNKNOWN'})),
      el('p', {testid: 'state-outcome', text: 'The state of this change has not been read from the server yet. Nothing is shown as finished.'}));
  }

  // ------------------------------------------------------------------ changes flow view
  const STAGES = ['impact', 'simulation', 'proposal', 'review', 'approval', 'apply'];
  const KINDS = [['yaml_typed_edit', 'YAML value edit'], ['xlsx_typed_edit', 'Workbook cell edit'],
                 ['markdown', 'Markdown text'], ['python', 'Python code']];

  async function viewChanges(ctx) {
    const {container} = ctx;
    const F = {ids: {}, data: null, failed: false, sig: '', extra: {claims: null, arts: null}, waitPolls: 0, lastNext: '', copyMsg: ''};
    for (const k of ['intent', 'draft', 'query', 'whatif', 'chat'].concat(STAGES)) F.ids[k] = ctx.route.params[k] || '';
    const hooks = {reload: () => refresh(false), redraw: async () => draw()};
    M.staleShown = false; M.refusal = null;
    if (ctx.userReload && F.ids.proposal && F.ids.approval) {
      // The person asked to reload a stale page: follow the newest approval prepared for this
      // proposal (a deliberate act; a page never jumps to a newer bundle by itself).
      try {
        const latest = await api('GET', '/v2/proposals/' + enc(F.ids.proposal) + '/approvals?workspace_id=' + enc(ctx.ws));
        if (latest.latest_job_id && latest.latest_job_id !== F.ids.approval) { F.ids.approval = latest.latest_job_id; App.setParams({approval: latest.latest_job_id}); }
      } catch (_) { /* keep the current approval; the read below reports any real failure */ }
    }

    function setId(kind, id) { F.ids[kind] = id; App.setParams({[kind]: id}); }
    async function createStage(kind, id, payload, btn) {
      const r = await act(() => api('POST', '/v2/jobs', {workspace_id: ctx.ws, kind, job_id: id, payload}), {button: btn, stale: true});
      return r;
    }

    async function fetchAll() {
      const head = await App.loadHead();
      const ws = ctx.ws, ids = F.ids;
      const whatif = ids.whatif ? await getJob(ctx, ids.whatif).catch(() => null) : null;
      let chat = null;
      if (ids.chat) {
        try { chat = await api('GET', '/v2/chat/' + enc(ids.chat) + '?workspace_id=' + enc(ws)); } catch (e) {
          if (!(e instanceof App.ApiError) || e.status === 401) throw e;
          chat = {unavailable: true};
        }
      }
      const [intent, impact, simulation, proposal, review, approval, apply, draft, query] = await Promise.all([
        ids.intent ? api('GET', '/v2/intent/' + enc(ids.intent) + '?workspace_id=' + enc(ws)) : null,
        getJob(ctx, ids.impact), getJob(ctx, ids.simulation), getJob(ctx, ids.proposal),
        getJob(ctx, ids.review), getJob(ctx, ids.approval), getJob(ctx, ids.apply),
        getJob(ctx, ids.draft), getJob(ctx, ids.query)]);
      const needClaims = !ids.intent && can('intent.create');
      const needArts = (!!simulation && jobState(simulation) === 'COMPLETED' || !!apply) && can('source.import');
      const proposalDone = proposal && jobState(proposal) === 'COMPLETED';
      const loopWanted = !!ids.draft && !!ids.apply && !!apply && jobState(apply) === 'COMPLETED';
      const draftDone = draft && jobState(draft) === 'COMPLETED';
      const gaps = draftDone ? await api('GET', '/v2/change/gaps?workspace_id=' + enc(ws) + '&draft_job_id=' + enc(ids.draft)).catch(() => null) : null;
      const suggest = {};
      if (gaps) for (const g of gaps.gaps) for (const jid of (g.suggestion_jobs || []).slice(-1)) suggest[g.gap_id] = await getJob(ctx, jid).catch(() => null);
      const [decisions, diff, change, claims, rev, contracts, loop] = await Promise.all([
        ids.approval ? api('GET', '/v2/jobs/' + enc(ids.approval) + '/decisions?workspace_id=' + enc(ws)) : null,
        proposalDone ? (M.cache.diff[ids.proposal] ? M.cache.diff[ids.proposal] : api('GET', '/v2/proposals/' + enc(ids.proposal) + '/diff?workspace_id=' + enc(ws))) : null,
        ids.apply ? fetchChange(ctx, ids.apply) : null,
        needClaims ? api('GET', '/v2/knowledge/overview?workspace_id=' + enc(ws)) : null,
        needArts && head.source_revision_id ? revisionView(ctx, head.source_revision_id) : null,
        proposalDone ? api('GET', '/v2/verification/contracts?workspace_id=' + enc(ws) + '&proposal_job_id=' + enc(ids.proposal)) : null,
        loopWanted ? api('GET', '/v2/change/loop-status?workspace_id=' + enc(ws) + '&draft_job_id=' + enc(ids.draft) + '&apply_job_id=' + enc(ids.apply)).catch(() => null) : null]);
      if (diff && proposalDone) M.cache.diff[ids.proposal] = diff;
      return {chat, head, intent, impact, simulation, proposal, review, approval, apply, draft, query, loop, decisions, diff, change, gaps, suggest, whatif,
              claims: claims ? claims.claims : null, arts: rev ? rev.artifacts : null,
              contracts: contracts ? contracts.contracts : null};
    }

    // In a sandbox workspace, a page with live jobs asks the bounded pump to run what is runnable (the same call as the
    // Sandbox page button). The server enforces the lease, the approval hold and the budget; a refusal is ignored here.
    let lastPump = 0;
    function autoPump() {
      const now = Date.now();
      if (now - lastPump < 3000) return;
      lastPump = now;
      api('POST', '/v2/sandbox/pump', {workspace_id: ctx.ws}).catch(() => {});
    }

    async function refresh(first) {
      let d;
      try { d = await fetchAll(); } catch (e) {
        if (ctx.gen !== S.gen) return;
        if (!(e instanceof App.ApiError)) throw e;
        if (e.status === 401) { App.signedOut(undefined, true); return; }
        if (first || !F.data) { App.readFailed(container, e, () => App.reloadView()); return; }
        F.failed = true; draw(); return;
      }
      if (ctx.gen !== S.gen) return;
      if (d.chat && d.chat.intent_id && !F.ids.intent) {       // the requester confirmed the chat: everybody opening it continues with the saved request
        setId('intent', d.chat.intent_id);
        try { d = await fetchAll(); } catch (_) { /* the next refresh reports it */ }
        if (ctx.gen !== S.gen) return;
      }
      F.data = d; F.failed = false;
      // A human checkpoint (someone else must act): look again every few seconds, a bounded number of times, so the person who is
      // waiting sees the next step without re-creating anything. Never more than WAIT_POLL_MAX checks per waiting state.
      const nx = nextAction(d), sig = nx.kind + '|' + nx.who + '|' + nx.text;
      if (sig !== F.lastNext) { F.lastNext = sig; F.waitPolls = 0; }
      draw();
      if (live(d)) { autoPump(); App.schedule(() => refresh(false), 2000); }
      else if (nx.kind === 'wait' && F.waitPolls < WAIT_POLL_MAX) { F.waitPolls += 1; App.schedule(() => refresh(false), 6000); }
    }
    const WAIT_POLL_MAX = 20;

    function live(d) {
      if (d.chat && d.chat.awaiting_ai) return true;
      const jobs = [d.draft, d.query, d.impact, d.simulation, d.proposal, d.review, d.approval, d.apply, d.whatif];
      if (jobs.some(j => j && !App.TERMINAL_JOB.includes(jobState(j)))) return true;
      return d.change ? changeLive(d.change) : false;
    }

    function approvalStale(d) {
      if (!d.approval || jobState(d.approval) !== 'COMPLETED') return false;
      const snap = (d.approval.execution_result || {}).snapshot || {};
      return snap.source_revision_id !== d.head.source_revision_id || snap.decision_epoch !== d.head.decision_epoch;
    }

    function stageBox(name, title, kids) {
      return el('section', {class: 'card', testid: 'flow-' + name}, el('h2', {text: title}), kids);
    }
    // What "not started" means depends on what is missing first (never a bare "NONE").
    const NONE_TEXT = {draft: 'Not started. The request must be confirmed first.', impact: 'Not started. It needs the AI draft first.',
      simulation: 'Not started. Run the impact analysis first.', proposal: 'Not started. Needs the simulation first.',
      review: 'Not started. Needs a proposal first.', approval: 'Not started. Needs a finished review first.',
      apply: 'Not started. Needs an approved change first.', query: 'Not started.', whatif: 'Not started.'};
    const noneText = name => {
      const d = F.data;
      if (name === 'draft' && d && d.intent && d.intent.status === 'CONFIRMED') return 'Not started.';
      return NONE_TEXT[name] || 'Not started.';
    };
    const stateText = (name, s) => s === 'NONE' ? noneText(name) : App.phrase.job(s);
    const stageState = (name, job) => el('p', {}, App.T(el('span', {text: 'Job: '})), App.pill(job ? jobState(job) : 'NONE', stateText(name, job ? jobState(job) : 'NONE'),
      {testid: 'state-' + name, data: {'job-id': job ? job.job_id : ''}}),
      job && job.execution_reason && jobState(job) === 'FAILED' ? el('span', {class: 'muted', text: ' - ' + job.execution_reason}) : null,
      job && jobState(job) === 'FAILED' && can('source.import') ? el('button', {type: 'button', class: 'secondary', text: 'Retry job', on: {click: async ev => {
        const r = await App.retryJob(ctx, job.job_id, ev.currentTarget);
        if (r !== FAIL && ctx.gen === S.gen) await refresh(false);
      }}}) : null);
    const done = job => !!job && jobState(job) === 'COMPLETED';

    // ---- free-text chat: words -> (AI questions) -> checked understanding -> the person confirms -> an ordinary saved request.
    // Nothing here approves, drafts or applies; the AI reply itself needs a different person to allow it.
    const chatNum = v => { try { return Number(v).toLocaleString('en-US'); } catch (_) { return str(v); } };
    const chatShow = (k, v) => {
      if (v === null || v === undefined || v === '') return null;
      if (Array.isArray(v)) return v.length ? v.join(', ') : null;
      if (typeof v === 'object') { const e = Object.entries(v); return e.length ? e.map(([a, b]) => a.replace(/_/g, ' ') + ': ' + b).join('; ') : null; }
      return (k === 'old_value' || k === 'new_value') ? chatNum(v) : str(v);
    };
    const CHAT_FIELDS = [['subject', 'What is changing'], ['scope', 'Applies to'], ['unit', 'What the amount measures'], ['currency', 'Currency'],
      ['old_value', 'Current value'], ['new_value', 'New value'], ['applies_to', 'Cases it should cover']];
    async function chatCall(btn, path, body) {
      const r = await act(() => api('POST', '/v2/chat' + path, Object.assign({workspace_id: ctx.ws}, body)), {button: btn});
      return (r === FAIL || ctx.gen !== S.gen) ? null : r;
    }
    function chatPanel(d) {
      const C = d.chat, box = el('div', {class: 'card chat', testid: 'chat-panel',
        data: {state: !C ? 'new' : C.unavailable ? 'unavailable' : (C.status + (C.awaiting_ai ? '-ai' : C.awaiting_allowance ? '-allow' : ''))}});
      box.append(el('h3', {text: 'Describe the change in your own words'}),
        el('p', {class: 'muted', text: 'Write it the way you would tell a colleague. The AI helps turn it into a checked request. It never changes a document, and nothing starts until you confirm below and an administrator confirms the request.'}));
      box.append(el('div', {class: 'nextact', testid: 'chat-scope-note'},
        el('strong', {text: 'What happens with your request'}),
        el('span', {text: 'Ripple sets up and applies changes to the NUMBER of an existing rule (a limit, a threshold). A request about scope, approvers, conditions or exceptions is only NOTED: it is written down here and nothing is carried out for it.'}),
        el('p', {class: 'muted', text: 'Steps that need ANOTHER person: allowing the AI to reply (an administrator), confirming the request (an administrator), allowing the AI draft (a second person), approving the change (an approver). You cannot do these for yourself. In this test the AI is SIMULATED.'})));
      if (C && C.unavailable) {
        box.append(el('p', {class: 'blocker', testid: 'chat-unavailable', text: 'This conversation is not available to your account (it may belong to someone else, or it does not exist). Nothing was changed.'}));
        return [box];
      }
      if (!C) {
        const ta = App.bindDraft(el('textarea', {id: 'chat-input', rows: '3', testid: 'chat-input', 'aria-label': 'Tell Ripple what you want to change'}), 'chat-input');
        box.append(ta, el('div', {class: 'row'}, el('button', {type: 'button', testid: 'chat-start', text: 'Send', on: {click: async ev => {
          const text = ta.value.trim();
          if (!text) { App.say('Write what you want to change first.'); return; }
          const r = await chatCall(ev.currentTarget, '/start', {text});
          if (!r) return;
          S.drafts['chat-input'] = '';
          F.ids.chat = r.conversation_id; App.setParams({chat: r.conversation_id});
          await refresh(false);
        }}})));
        return [box];
      }
      const mineChat = C.you_are_creator, open = C.status === 'OPEN';
      const list = el('div', {class: 'turns', testid: 'chat-turns'});
      for (const t of C.turns) {
        const pending = t.role === 'assistant' && !t.text;
        const who = t.role === 'user' ? (t.by === S.account ? 'You' : t.by) : t.role === 'assistant' ? 'AI' : t.role === 'edit' ? 'You (edited the details)' : 'Note';
        list.append(el('div', {class: 'turn ' + t.role, testid: 'chat-turn', data: {role: t.role, seq: String(t.seq), pending: pending ? 'true' : 'false'}},
          el('strong', {text: who + ': '}), el('span', {text: pending ? 'The AI is replying...' : t.text})));
      }
      box.append(list);
      for (const h of C.safety_notes || []) box.append(el('p', {class: 'muted', testid: 'chat-safety-note', data: {code: h.code}}, h.business, App.T(el('span', {text: ' [' + h.code + ']'}))));
      const u = C.understanding;
      if (u) {
        const uc = el('div', {class: 'reqcard', testid: 'chat-understanding', data: {state: u.status, version: String(C.version)}},
          el('p', {class: 'big', text: u.status === 'READY' ? 'This is what I understood' : u.status === 'CLARIFY' ? 'I still need some details' : u.status === 'UNSUPPORTED' ? 'This is not something I can set up here' : 'No usable reply'}));
        if (u.fields) {
          const dl = el('dl', {class: 'kv', testid: 'chat-fields'});
          for (const [k, label] of CHAT_FIELDS) {
            const v = chatShow(k, u.fields[k]);
            const src = u.sources && u.sources[k];
            dl.append(el('dt', {text: label}), el('dd', {testid: 'chat-field-' + k, data: {missing: v === null ? 'true' : 'false'}, text: v === null ? 'not known yet' : v}));
            if (src) dl.append(el('dt', {class: 'muted', text: ''}), el('dd', {class: 'muted', testid: 'chat-source-' + k, text: src.quote ? 'from your words: "' + src.quote + '"' : 'from the stored rule'}));
          }
          uc.append(dl);
        }
        for (const q of u.questions || []) uc.append(el('p', {class: 'blocker', testid: 'chat-question', text: str(q)}));
        if ((u.changes || []).length) {
          const ul = el('ul', {testid: 'chat-changes'});
          for (const ch of u.changes) ul.append(el('li', {data: {kind: ch.kind, supported: ch.supported ? 'true' : 'false'},
            text: ch.summary + ' - ' + (ch.supported ? 'a number change Ripple can prepare' : 'recorded only, not applied automatically') + (ch.quote ? ' (your words: "' + ch.quote + '")' : ' (not in your words)')}));
          uc.append(el('p', {class: 'muted', text: 'What I read in your message:'}), ul);
        }
        if ((u.out_of_scope || []).length) uc.append(el('p', {class: 'blocker', testid: 'chat-out-of-scope', text: 'Not covered by this request: ' + u.out_of_scope.join('; ')}));
        if (u.limits_note) uc.append(el('p', {class: 'muted', testid: 'chat-limits-note', text: u.limits_note}));
        for (const h of (u.safety || []).filter(x => !(C.safety_notes || []).some(y => y.code === x.code))) uc.append(el('p', {class: 'muted', testid: 'chat-safety', data: {code: h.code}}, h.business, App.T(el('span', {text: ' [' + h.code + ']'}))));
        if ((u.assumptions || []).length) uc.append(el('p', {class: 'muted', testid: 'chat-assumptions', text: 'Assumed, please check: ' + u.assumptions.join('; ')}));
        uc.append(App.T(el('div', {}, App.techField('understanding_version', C.version), App.techField('source', u.source || ''), App.techField('conversation_id', C.conversation_id),
          App.techField('pinned_source_revision_id', C.pinned_source_revision_id || ''), App.techField('pinned_decision_epoch', C.pinned_decision_epoch))));
        box.append(uc);
      }
      const Z = C.authorization || {};
      const zbox = el('div', {class: 'nextact ' + (Z.live ? '' : 'wait'), testid: 'chat-authorization', data: {live: Z.live ? 'true' : 'false'}},
        el('strong', {text: Z.live ? 'AI permission: ' + Z.replies_left + ' more ' + (Z.replies_left === 1 ? 'reply' : 'replies') + ' can run without asking again'
          : (Z.revoked_at ? 'AI permission withdrawn' : 'No AI permission right now')}),
        el('span', {text: Z.live ? 'Allowed by ' + Z.granted_by + ' until ' + new Date(Z.expires_at).toLocaleTimeString() + '. ' + Z.covers
          : 'A different person with permission has to allow it (it uses the paid model). Replies used so far: ' + C.ai_used + '.'}),
        el('p', {class: 'muted', testid: 'chat-not-covered', text: 'This does NOT cover: ' + (Z.does_not_cover || []).join(', ') + '. Those stay separate steps with their own people.'}),
        App.T(el('div', {}, App.techField('replies_used/allowed', C.ai_used + '/' + C.ai_allowance + ' (max ' + C.limits.max_ai_turns + ')'),
          App.techField('reserved_usd_per_reply_worst_case', Z.reserved_usd_per_reply || 'n/a'), App.techField('worst_case_usd_left', Z.worst_case_usd_left || 'n/a'),
          App.techField('expires_at', Z.expires_at || ''), App.techField('revoked_by', Z.revoked_by || ''))));
      if (Z.live && open && (C.can_allow || mineChat || can('knowledge.confirm'))) zbox.append(el('button', {type: 'button', class: 'secondary', testid: 'chat-revoke', text: 'Withdraw this permission', on: {click: async ev => {
        if (await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/revoke', {})) await refresh(false);
      }}}));
      box.append(zbox);
      if (C.awaiting_allowance && open) {
        box.append(el('div', {class: 'nextact wait', testid: 'chat-awaiting-allowance'}, el('strong', {text: 'The AI has not been asked yet'}),
          el('span', {text: 'A different person with permission (for example an administrator) must allow the AI to reply, because it uses the paid model. Nothing was sent to the AI.'})));
        if (C.can_allow) box.append(el('div', {class: 'row'},
          el('button', {type: 'button', testid: 'chat-allow', text: 'Allow the AI to reply', on: {click: async ev => { if (await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/allow', {turns: 1})) await refresh(false); }}}),
          el('button', {type: 'button', class: 'secondary', testid: 'chat-allow-3', text: 'Allow 3 replies', on: {click: async ev => { if (await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/allow', {turns: 3})) await refresh(false); }}})));
      }
      if (mineChat && open && !C.awaiting_ai) {
        const ta = App.bindDraft(el('textarea', {id: 'chat-reply', rows: '2', testid: 'chat-reply', 'aria-label': 'Your answer'}), 'chat-reply');
        box.append(el('label', {for: 'chat-reply', text: u && u.status === 'CLARIFY' ? 'Your answer' : 'Add something'}), ta,
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'chat-send', text: 'Send', on: {click: async ev => {
            const text = ta.value.trim();
            if (!text) { App.say('Write your answer first.'); return; }
            const r = await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/message', {text});
            if (!r) return;
            S.drafts['chat-reply'] = ''; await refresh(false);
          }}})));
        // edit the details yourself (a new version; no AI call)
        if (S.drafts['ce-ver'] !== String(C.conversation_id + ':' + C.version)) {
          const f = (u && u.fields) || {}, [sk, sv] = Object.entries(f.scope || {})[0] || ['', ''];
          Object.assign(S.drafts, {'ce-subject': f.subject || '', 'ce-scope-key': sk, 'ce-scope-val': str(sv), 'ce-unit': f.unit || '', 'ce-currency': f.currency || '',
            'ce-old': f.old_value === null || f.old_value === undefined ? '' : str(f.old_value), 'ce-new': f.new_value === null || f.new_value === undefined ? '' : str(f.new_value),
            'ce-applies': (f.applies_to || []).join(', '), 'ce-ver': String(C.conversation_id + ':' + C.version)});
        }
        const cf = (id, label) => [el('label', {for: id, text: label}), App.bindDraft(el('input', {id, type: 'text', testid: id}), id)];
        const det = el('details', {testid: 'chat-edit'}, el('summary', {text: 'Edit the details myself'}),
          cf('ce-subject', 'What is changing'), cf('ce-scope-key', 'Applies to - kind of scope'), cf('ce-scope-val', 'Applies to - which one'),
          cf('ce-unit', 'What the amount measures'), cf('ce-currency', 'Currency'), cf('ce-old', 'Current value'), cf('ce-new', 'New value'),
          cf('ce-applies', 'Cases it should cover (comma separated)'),
          el('div', {class: 'row'}, el('button', {type: 'button', class: 'secondary', testid: 'chat-edit-apply', text: 'Check my edits', on: {click: async ev => {
            const v = k => (S.drafts[k] || '').trim();
            const fields = {subject: v('ce-subject'), unit: v('ce-unit') || null, currency: v('ce-currency'), old_value: v('ce-old'), new_value: v('ce-new'),
              applies_to: v('ce-applies').split(',').map(x => x.trim()).filter(Boolean)};
            if (v('ce-scope-key') && v('ce-scope-val')) fields.scope = {[v('ce-scope-key')]: v('ce-scope-val')};
            if (await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/edit', {fields})) { S.drafts['ce-ver'] = ''; S.drafts['chat-edit-open'] = '1'; await refresh(false); }
          }}})));
        if (S.drafts['chat-edit-open']) det.open = true;
        det.querySelector('summary').addEventListener('click', () => { S.drafts['chat-edit-open'] = det.open ? '' : '1'; });
        box.append(det);
      }
      if (mineChat && open) {
        const acts = el('div', {class: 'row'});
        const ackNeeded = !!(u && ((u.assumptions || []).length || (u.out_of_scope || []).length));
        const ack = ackNeeded ? el('input', {type: 'checkbox', id: 'chat-ack', testid: 'chat-ack'}) : null;
        if (ack) acts.append(el('label', {for: 'chat-ack'}, ack, ' I have checked what the AI assumed and what is left out of this request'));
        if (u && u.status === 'READY') acts.append(el('button', {type: 'button', testid: 'chat-confirm', disabled: !!C.awaiting_ai, text: 'Yes, this is what I mean', on: {click: async ev => {
          if (ack && !ack.checked) { App.say('Tick the box to confirm you checked the assumptions and what is left out.'); return; }
          const r = await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/confirm', {version: C.version, acknowledged: !!(ack && ack.checked)});
          if (!r) return;
          if (F.ids.intent && F.ids.intent !== r.intent_id) {
            const dd = F.data || {}, steps = [];
            const old = dd.intent;
            steps.push({name: 'The previous request (' + (old ? App.phrase.intent(old.status === 'PARSED' ? 'DRAFT' : old.status).toLowerCase() : 'saved') + ')', state: 'OUT OF DATE'});
            for (const [key, label] of [['draft', 'AI draft'], ['impact', 'Impact analysis'], ['simulation', 'Simulation'], ['proposal', 'Proposal'], ['review', 'Review'], ['approval', 'Approval'], ['apply', 'Applied change'], ['query', 'Question on the new revision'], ['whatif', 'What-if']]) {
              if (F.ids[key]) steps.push({name: label + ' (' + (dd[key] ? jobState(dd[key]).toLowerCase() : 'started') + ')', state: 'OUT OF DATE'});
            }
            S.drafts['superseded'] = JSON.stringify({steps, applied: !!F.ids.apply});
            for (const k of ['draft', 'query', 'whatif'].concat(STAGES)) { F.ids[k] = ''; }
            App.setParams({draft: '', query: '', whatif: '', impact: '', simulation: '', proposal: '', review: '', approval: '', apply: ''});
          }
          setId('intent', r.intent_id); await refresh(false);
        }}}));
        acts.append(el('button', {type: 'button', class: 'secondary', testid: 'chat-cancel', text: 'Cancel this conversation', on: {click: async ev => {
          if (await chatCall(ev.currentTarget, '/' + enc(C.conversation_id) + '/cancel', {})) await refresh(false);
        }}}));
        box.append(acts, el('p', {class: 'muted', text: 'Confirming here only saves your request. It is not an approval, and an administrator still has to confirm the request before anything is analysed.'}));
      }
      if (!mineChat) box.append(el('p', {class: 'muted', testid: 'chat-not-yours', text: 'This conversation belongs to ' + C.created_by + '. Only they can answer, edit or confirm it.'}));
      if (!open) box.append(el('p', {class: 'muted', testid: 'chat-closed', text: 'This conversation is ' + C.status.toLowerCase() + '.'}));
      return [box];
    }

    function intentCard(d) {
      const kids = [];
      if (!F.ids.intent) {
        if (!can('intent.create')) { kids.push(el('p', {class: 'muted', text: 'You cannot start a change in this workspace.'})); return stageBox('intent', '1. Request and confirmation', kids); }
        kids.push(...chatPanel(d));
        const advStart = kids.length;
        const ruleId = ctx.route.params.rule, rc = (d.claims || []).find(c => c.candidate_id === ruleId);
        if (rc && S.drafts['prefilled-from'] !== ruleId) {
          const [sk, sv] = Object.entries(rc.scope || {})[0] || ['', ''];
          const cur = /^[A-Z]{3}$/.test(rc.unit || '');
          Object.assign(S.drafts, {'sf-subject': rc.subject || '', 'sf-scope-key': sk, 'sf-scope-val': str(sv), 'sf-unit': cur ? '' : (rc.unit || ''),
            'sf-currency': cur ? rc.unit : '', 'sf-old': rc.value === null || rc.value === undefined ? '' : str(rc.value), 'prefilled-from': ruleId});
        }
        if (rc) kids.push(el('p', {class: 'muted', testid: 'prefill-note', text: 'Prefilled from the rule you picked in the Explorer (' + (rc.logical_path || rc.artifact_id) + '). Check every field and type the new value yourself; nothing is submitted or confirmed until you parse the request and a person with confirm rights confirms the intent.'}));
        const sf = (id, label, key, ph) => [el('label', {for: id, text: label}), App.bindDraft(el('input', {id, type: 'text', placeholder: ph || '', testid: id}), key)];
        // Start from a rule (so nobody has to type a rule name or a scope key).
        if (!rc && (d.claims || []).length) {
          const pick = el('select', {id: 'start-rule', testid: 'start-rule', 'aria-label': 'Start from a rule'}, el('option', {value: '', text: 'Choose a rule to change...'}));
          for (const c of d.claims) pick.append(el('option', {value: c.candidate_id, text: App.fmt.ruleTitle(c.subject, c.predicate) + ' - ' + App.fmt.valueText(c.value, c.unit, c.unit) + ' (' + App.fmt.basename(c.logical_path) + ')'}));
          pick.addEventListener('change', () => { if (pick.value) { App.setParams({rule: pick.value}); ctx.route.params.rule = pick.value; refresh(false); } });
          kids.push(el('div', {class: 'nextact', testid: 'start-from-rule'}, el('strong', {text: 'Start from a rule'}), el('label', {for: 'start-rule', text: 'Which rule do you want to change?'}), pick,
            el('p', {class: 'muted', text: 'The form below is then filled in for you. You only type the new value and check the rest. (You can also start from a rule in the Explorer.)'})));
        }
        if (rc) kids.push(el('div', {class: 'reqcard', testid: 'prefill-summary'}, el('p', {class: 'big', text: App.fmt.ruleTitle(rc.subject, rc.predicate) + ': ' + App.fmt.valueText(rc.value, rc.unit, rc.unit)}),
          el('p', {class: 'muted', text: 'Applies to: ' + App.fmt.scopeText(rc.scope) + '. Source: ' + App.fmt.basename(rc.logical_path) + '.'})));
        kids.push(el('h3', {text: 'Your request (exact values are kept as typed)'}),
          sf('sf-subject', 'What is changing', 'sf-subject', 'for example: Manager approval limit'),
          sf('sf-scope-key', 'Applies to - kind of scope (kept from the rule)', 'sf-scope-key', 'for example: order_type'),
          sf('sf-scope-val', 'Applies to - which one', 'sf-scope-val', 'for example: standard'),
          sf('sf-unit', 'What the amount measures', 'sf-unit', 'for example: net goods amount'),
          sf('sf-currency', 'Currency', 'sf-currency', 'JPY'),
          sf('sf-old', 'Current value (as you know it)', 'sf-old', '1,000,000'),
          sf('sf-new', 'New value you want', 'sf-new', '500,000'),
          sf('sf-applies', 'Which orders you expect this to cover (comma separated)', 'sf-applies', 'standard, emergency'),
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-parse-structured', text: 'Save the request', on: {click: async ev => {
            const v = k => (S.drafts[k] || '').trim();
            const form = {subject: v('sf-subject'), unit: v('sf-unit'), currency: v('sf-currency'), old_value: v('sf-old'), new_value: v('sf-new'),
              applies_to: v('sf-applies').split(',').map(x => x.trim()).filter(Boolean)};
            if (v('sf-scope-key') && v('sf-scope-val')) form.scope = {[v('sf-scope-key')]: v('sf-scope-val')};
            if (rc && S.drafts['prefilled-from'] === rc.candidate_id) form.target_candidate_id = rc.candidate_id;
            const r = await act(() => api('POST', '/v2/intent/parse', {workspace_id: ctx.ws, structured_form: form}), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return;
            setId('intent', r.intent_id);
            await refresh(false);
          }}})),
          el('h3', {text: 'Or describe the change in free text (older path)'}));
        const text = App.bindDraft(el('textarea', {id: 'fld-intent-text', rows: '3'}), 'intent-text');
        const sel = el('select', {id: 'fld-intent-target', testid: 'field-intent-target'}, el('option', {value: '', text: 'Select the rule to change'}));
        for (const c of d.claims || []) {
          sel.append(el('option', {value: c.candidate_id, text: c.subject + ' / ' + c.predicate + (c.value !== null && c.value !== undefined ? ' = ' + str(c.value) : '')}));
        }
        App.bindDraft(sel, 'intent-target');
        kids.push(el('label', {for: 'fld-intent-text', text: 'Describe the change'}), text,
          el('label', {for: 'fld-intent-target', text: 'Rule this change applies to'}), sel,
          (d.claims || []).length ? null : el('p', {class: 'muted', text: 'No extracted rule exists yet. Analyse a source and confirm a rule first.'}),
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-parse-intent', text: 'Parse intent', on: {click: async ev => {
            const raw = text.value.trim();
            if (!raw) { App.say('Describe the change first.'); return; }
            const r = await act(() => api('POST', '/v2/intent/parse', Object.assign({workspace_id: ctx.ws, raw_request: raw},
              sel.value ? {target_hint: sel.value} : {})), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return;
            S.drafts['intent-text'] = ''; S.drafts['intent-target'] = '';
            setId('intent', r.intent_id);
            await refresh(false);
          }}})));
        const moved = kids.splice(advStart);
        const adv = el('details', {class: 'advanced', testid: 'advanced-input'}, el('summary', {text: 'Advanced input: fill in the form yourself'}),
          el('p', {class: 'muted', text: 'The same checks apply. Use this if you prefer exact fields, or to start from a rule.'}), moved);
        if (rc || S.drafts['adv-open']) adv.open = true;
        adv.querySelector('summary').addEventListener('click', () => { S.drafts['adv-open'] = adv.open ? '' : '1'; });   // recorded synchronously: a redraw right after the click must keep it open
        kids.push(adv);
        return stageBox('intent', '1. Request and confirmation', kids);
      }
      const it = d.intent;
      if (d.chat && !d.chat.unavailable && d.chat.status === 'OPEN' && d.chat.intent_id) kids.push(...chatPanel(d));      // revising a confirmed request
      const shown = it.status === 'PARSED' ? 'DRAFT' : it.status;
      kids.push(el('p', {}, App.T(el('span', {text: 'Status: '})), App.pill(shown, App.phrase.intent(shown), {testid: 'state-intent', data: {'server-status': it.status}})),
        App.T(el('p', {class: 'muted', text: 'Request: ' + it.raw_request})),
        App.T(el('p', {class: 'muted'}, 'Target rule: ', el('code', {text: it.target || 'none grounded'}), it.value !== null && it.value !== undefined ? ' - value ' + str(it.value) : '')));
      for (const q of it.questions || []) kids.push(el('p', {class: 'blocker', text: str(q)}));
      if (it.status === 'PARSED' && can('knowledge.confirm')) {
        kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-confirm-intent', text: 'Confirm intent', on: {click: async ev => {
          const r = await act(() => api('POST', '/v2/intent/confirm', {workspace_id: ctx.ws, intent_id: it.intent_id}), {button: ev.currentTarget, stale: true});
          if (r !== FAIL && ctx.gen === S.gen) await refresh(false);
        }}})));
      } else if (it.status === 'PARSED') kids.push(el('p', {class: 'muted', text: 'A knowledge owner must confirm this intent before analysis.'}));
      return stageBox('intent', '1. Request and confirmation', kids);
    }

    const draftResult = d => (d && d.draft && jobState(d.draft) === 'COMPLETED') ? (d.draft.execution_result || null) : null;
    // A job that spends model budget waits for a second person: show that phase, not just the execution state.
    const phase = job => !job ? 'NONE' : (App.TERMINAL_JOB.includes(job.execution_status) ? job.execution_status
      : (job.status === 'PENDING_APPROVAL' || job.status === 'APPROVED') ? job.status : jobState(job));
    const phaseState = (name, job) => el('p', {}, App.T(el('span', {text: 'Job: '})), App.pill(phase(job), stateText(name, phase(job)),
      {testid: 'state-' + name, data: {'job-id': job ? job.job_id : ''}}),
      job && job.execution_reason && jobState(job) === 'FAILED' ? el('span', {class: 'muted', text: ' - ' + job.execution_reason}) : null);
    const approveJobBtn = (job, label, testid) => (job && job.status === 'PENDING_APPROVAL' && can('source.import'))
      ? el('button', {type: 'button', class: 'secondary', testid, text: label, on: {click: async ev => {
          const r = await act(() => api('POST', '/v2/jobs/' + enc(job.job_id) + '/approve', {workspace_id: ctx.ws}), {button: ev.currentTarget, stale: true});
          if (r !== FAIL && ctx.gen === S.gen) await refresh(false);
        }}}) : null;

    function gapPanel(d) {
      const G = d.gaps;
      if (!G || !G.completed) return [];
      const kids = [el('h3', {text: 'Open points (GAP) - answer, comment or ask for a suggestion (' + G.gaps.length + ')'}),
        el('p', {class: 'muted', text: 'A comment and a GenAI suggestion are NOT authoritative. Only a confirmed answer (a person with confirm rights) is given to the next draft, as a stated assumption. Nothing here edits a source or closes a finding.'})];
      kids.push(el('p', {class: 'muted', testid: 'gap-wish-note', text: 'If your answer asks for behaviour the system does not have today (for example emergency orders following the new limit), it is recorded as what you want, not as a fact. Making the system do it needs a code change, which is planned for the final product.'}));
      if (G.stale) kids.push(el('p', {class: 'blocker', testid: 'draft-stale', text: 'STALE: ' + (G.stale_reasons || []).join('; ') + '. Use "Generate GenAI draft" again to include the confirmed answers.'}));
      if ((G.clarifications_used || []).length) kids.push(el('p', {class: 'muted', testid: 'clarifications-used', text: 'This draft used ' + G.clarifications_used.length + ' confirmed answer(s): ' + G.clarifications_used.map(u => u.actor_id + ' (epoch ' + u.epoch + ')').join(', ')}));
      const arts = Array.from(new Set(((draftResult(d) || {}).items || []).map(x => x.artifact_id).filter(Boolean)));
      for (const g of G.gaps) {
        const key = 'gap-' + g.key;
        const box = el('div', {class: 'card', testid: 'gap-item', data: {'gap-key': g.key, state: g.state, kind: g.kind}},
          el('p', {}, el('span', {class: 'pill', text: g.kind}), ' ', App.pill(g.state, App.phrase.gap(g.state), {testid: 'gap-state'}), ' ', g.text));
        for (const c of g.comments || []) box.append(el('p', {class: 'muted', testid: 'gap-comment', text: 'Comment (not authoritative) by ' + c.actor_id + ': ' + c.text}));
        if (g.confirmation) box.append(el('p', {testid: 'gap-confirmation', text: 'CONFIRMED by ' + g.confirmation.actor_id + ' (epoch ' + g.confirmation.epoch + '): ' + g.confirmation.answer + (g.confirmation.rationale ? ' - why: ' + g.confirmation.rationale : '')}));
        const sj = d.suggest && d.suggest[g.gap_id];
        if (sj) {
          const sr = sj.execution_result || {};
          box.append(el('p', {class: 'muted', testid: 'gap-suggestion', data: {state: jobState(sj)}, text: 'GenAI suggestion (not confirmed; job ' + jobState(sj) + '): ' + (jobState(sj) === 'COMPLETED' ? (sr.status + ' - ' + (sr.answer || sr.question_for_human || '')) : 'waiting')}),
            (sr.evidence_spans || []).map(sp => el('blockquote', {class: 'muted', text: sp.artifact_path + ': ' + sp.quote})),
            approveJobBtn(sj, 'Approve this model call (a different person than the requester)', 'action-approve-suggest'));
        }
        if (!g.confirmation) {
          const text = App.bindDraft(el('textarea', {rows: '2', testid: 'gap-text', placeholder: 'Your answer or comment'}), key);
          const why = App.bindDraft(el('input', {type: 'text', testid: 'gap-why', placeholder: 'Why (recorded with your name)'}), key + '-why');
          const row = el('div', {class: 'row'});
          if (can('knowledge.answer')) row.append(el('button', {type: 'button', testid: 'gap-comment-save', text: 'Save as comment (not authoritative)', on: {click: async ev => {
            const t = (S.drafts[key] || '').trim(); if (!t) { App.say('Write the comment first.'); return; }
            const r = await act(() => api('POST', '/v2/hitl/answer', {workspace_id: ctx.ws, question_id: g.gap_id, answer: t, idempotency_key: uuid(), expected_version: (g.comments || []).length || null}), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return; S.drafts[key] = ''; await refresh(false);
          }}}));
          if (can('source.import') && arts.length) row.append(el('button', {type: 'button', testid: 'gap-suggest', text: 'Ask GenAI to suggest (model call, needs approval)', on: {click: async ev => {
            const id = 'ui-suggest-' + uuid().slice(0, 16);
            const r = await createStage('gap.answer', id, {question: g.text, question_id: 'suggest:' + g.gap_id, artifact_ids: arts}, ev.currentTarget);
            if (r === FAIL || ctx.gen !== S.gen) return; await refresh(false);
          }}}));
          if (can('knowledge.confirm')) row.append(el('button', {type: 'button', testid: 'gap-confirm', text: 'Confirm this answer as a decision', on: {click: async ev => {
            const t = (S.drafts[key] || '').trim(); if (!t) { App.say('Write the answer to confirm first.'); return; }
            const r = await act(() => api('POST', '/v2/change/gap/confirm', {workspace_id: ctx.ws, draft_job_id: F.ids.draft, gap_key: g.key, answer: t, rationale: (S.drafts[key + '-why'] || '').trim()}), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return; S.drafts[key] = ''; S.drafts[key + '-why'] = '';
            App.say('Decision recorded by you. This draft is now stale; the source is unchanged.'); await refresh(false);
          }}}));
          box.append(text, why, row);
        }
        kids.push(box);
      }
      return kids;
    }

    const WHATIF_KEYS = [
      ['route', 'routing_rules.standard_manager_threshold_jpy', 'Manager approval threshold (JPY)'],
      ['route', 'routing_rules.capex_finance_threshold_jpy', 'CAPEX finance threshold (JPY)'],
      ['match', 'matching_rules.price_tolerance_fixed_jpy', 'Invoice price tolerance, fixed (JPY)'],
      ['match', 'matching_rules.price_tolerance_percent', 'Invoice price tolerance, percent'],
      ['match', 'matching_rules.payment_term_days', 'Payment terms (days)']];

    function whatIfCard(d) {
      if (!can('source.import')) return null;
      const sel = el('select', {id: 'wi-key', testid: 'whatif-key'}, WHATIF_KEYS.map(([c, k, label]) => el('option', {value: c + '|' + k, text: label})));
      App.bindDraft(sel, 'wi-key');
      const val = App.bindDraft(el('input', {id: 'wi-value', type: 'text', testid: 'whatif-value', placeholder: 'integer, e.g. 750000'}), 'wi-value');
      const kids = [el('p', {class: 'nextact', testid: 'whatif-note', text: 'Simulation only. Nothing is changed, no proposal is created and no document is edited. It runs the Harbor system itself (deterministic, no AI call) with a value you type, and shows what would change.'}),
        el('label', {for: 'wi-key', text: 'Rule'}), sel, el('label', {for: 'wi-value', text: 'Proposed value'}), val,
        el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-run-whatif', text: 'Run what-if simulation', on: {click: async ev => {
          const [c, k] = (sel.value || WHATIF_KEYS[0][0] + '|' + WHATIF_KEYS[0][1]).split('|');
          const raw = (S.drafts['wi-value'] || '').trim().replace(/[ ,]/g, '');
          if (!/^\d+$/.test(raw)) { App.say('Enter a whole number.'); return; }
          const id = 'ui-whatif-' + uuid().slice(0, 16);
          const r = await createStage('simulation', id, {adapter: 'harbor_cli', case: c, edits: [{path: k, value: parseInt(raw, 10)}]}, ev.currentTarget);
          if (r === FAIL || ctx.gen !== S.gen) return;
          F.ids.whatif = id; App.setParams({whatif: id}); await refresh(false);
        }}}))];
      const w = d.whatif;
      if (w) {
        kids.push(phaseState('whatif', w));
        if (jobState(w) === 'COMPLETED') {
          const res = w.execution_result || {};
          kids.push(res.harbor && res.status === 'EXECUTED' ? simulationReport(res.harbor) : el('p', {class: 'blocker', testid: 'whatif-status', text: 'Simulation ' + (res.status || 'unknown') + (res.reason ? ': ' + res.reason : '')}));
        }
      }
      return stageBox('whatif', 'Try a value (what-if, simulation only)', kids);
    }

    // The four groups a person needs to tell apart. Nothing here is a causal claim: graph relations are proposed identity links,
    // an item is "impacted" only where the SERVER re-checked a citation, and everything else is a question or a disclosed gap.
    function docsList(d, r, impact) {
      const L = (tag, text) => el('span', {class: 'pill', text: tag, title: text});
      const items = r.items || [];
      const where = x => x.logical_path + ' ' + (JSON.stringify(x.locator && (x.locator.key_path || x.locator.cell || x.locator.symbol || x.locator.line_start)) || '');
      const link = x => (x.candidate_ids || []).slice(0, 1).map(id => el('a', {href: App.hashFor(S.ws, 'explorer', null, {node: id}), text: ' (rule in the Explorer)'}));
      const quote = x => x.citation && x.citation.quote ? el('blockquote', {class: 'muted', text: x.citation.quote}) : null;
      const group = (title, tid, note, rows) => el('div', {class: 'card', testid: tid, data: {count: rows.length}},
        el('h4', {text: title + ' (' + rows.length + ')'}), el('p', {class: 'muted', text: note}),
        rows.length ? el('ul', {}, rows.map(x => el('li', {}, x))) : el('p', {class: 'muted', text: 'none'}));
      const evid = items.filter(x => x.status === 'PROPOSED' || x.status === 'NO_OP');
      const confirm = items.filter(x => ['AMBIGUITY', 'MANUAL_CHANGE', 'UNSUPPORTED'].includes(x.status));
      const unchanged = items.filter(x => x.status === 'MANUAL_CHANGE' || x.status === 'PROPOSED');
      const uncovered = [].concat((r.unaddressed || []).map(u => u.logical_path + ' ' + (u.location || '') + ': not classified by the model'),
        (r.not_covered || []).map(n => (n.logical_path || n.artifact_id) + ': ' + n.reason),
        impact ? (impact.unknown || []).map(u => 'rule ' + u.candidate_id + ': UNKNOWN (no relation evidence)') : [],
        impact ? (impact.excluded || []).map(u => 'rule ' + u.candidate_id + ': excluded - ' + u.reason) : []);
      const gapOpen = ((d.gaps && d.gaps.gaps) || []).filter(g => g.state !== 'confirmed').length;
      return el('div', {testid: 'preview-docs'},
        el('p', {class: 'muted', text: 'Links between rules are proposed identity relations from the graph, not causal dependencies. A document appears under "impacted" only where a citation was re-checked by the server.'}),
        group('Impacted, with evidence', 'preview-docs-evidence', 'The server re-checked the quoted text in the stored source.',
          evid.map(x => [L('SERVER', 'citation re-checked'), ' ', el('code', {text: where(x)}), ' - ' + x.status + (x.status === 'PROPOSED' ? ' (the source is unchanged until you apply)' : ' (already at the new value)'), quote(x), link(x)])),
        group('Needs your confirmation', 'preview-docs-confirm', 'Ambiguous, manual or unsupported items and open points: you decide; nothing is applied for them.',
          confirm.map(x => [el('code', {text: where(x)}), ' - ' + x.status + (x.reason ? ': ' + x.reason : ''), quote(x), link(x)]).concat(gapOpen ? [[gapOpen + ' open point(s) in the GAP list below']] : [])),
        group('Source not changed by this proposal', 'preview-docs-unchanged', 'These files still say what they said; a proposal edits one YAML value, text and workbook edits stay manual.',
          unchanged.map(x => [el('code', {text: where(x)}), x.status === 'PROPOSED' ? ' - will change only after apply and approval' : ' - stays as it is: a person edits it', link(x)])),
        group('Not covered / unknown', 'preview-docs-uncovered', 'Sources or rules nothing here could examine. UNKNOWN is not "unaffected".', uncovered.map(t => [t])));
    }

    function previewCard(d) {
      const r = draftResult(d);
      if (!r) return null;
      const L = (tag, text) => el('span', {class: 'pill', text: tag, title: text});
      const edits = r.edits || [];
      const impact = d.impact && jobState(d.impact) === 'COMPLETED' ? (d.impact.execution_result || {}) : null;
      const sim = d.simulation && jobState(d.simulation) === 'COMPLETED' ? ((d.simulation.execution_result || {}).coverage || {}) : null;
      const kids = [el('p', {class: 'muted', text: 'Nothing below is applied. Each line says who produced it: MODEL (GenAI reading of the evidence), SERVER (deterministic, checked against stored state), NOT RUN / MISSING.'}),
        el('h3', {text: 'Current vs proposed'}),
        edits.length ? el('ul', {}, edits.map(e => el('li', {}, L('SERVER', 'value read from the stored observation; proposed value from your confirmed intent'), ' ', el('code', {text: e.logical_path + ' ' + (e.proposal_edit ? e.proposal_edit.path : '')}), ': ', el('strong', {text: str(e.old) + ' -> ' + str(e.new)})))) : el('p', {class: 'muted', text: 'No automatic YAML edit was proposed (everything is manual, ambiguous or unaddressed).'}),
        el('h3', {text: 'What the model concluded'}),
        el('p', {}, L('MODEL', 'not a verified result'), ' ', r.summary || ''),
        el('h3', {text: 'Related documents and sources'}),
        docsList(d, r, impact),
        impact ? el('p', {}, L('SERVER', 'from stored relations and a text scan; UNKNOWN = not covered'), ' direct ' + (impact.direct || []).length + ', indirect ' + (impact.indirect || []).length + ', unknown ' + (impact.unknown || []).length + ', excluded ' + (impact.excluded || []).length + '. ',
          el('a', {href: App.hashFor(S.ws, 'explorer'), text: 'Open the Explorer'}), ' to see the rules and their evidence.') : el('p', {class: 'muted', text: 'Impact analysis not run yet (step 3).'}),
        el('h3', {text: 'Behaviour before/after'}),
        el('p', {testid: 'preview-simulation', data: {state: sim && sim.submitted ? 'RAN' : 'UNAVAILABLE'}}, L(sim && sim.submitted ? 'SERVER' : 'NOT RUN', 'simulation'), ' ',
          sim && sim.submitted ? ('Harbor itself was run over ' + sim.submitted + ' public requisitions' + ((d.simulation.execution_result || {}).harbor ? ': ' + (d.simulation.execution_result.harbor.changed_count) + ' change their route (details in step 3).' : '.')) : 'No behaviour simulation exists for this change (only the scalar thresholds of the Harbor approval config can be simulated). The model analysis above does NOT replace it.'),
        el('h3', {text: 'Still open'}),
        el('p', {testid: 'preview-open', text: ((d.gaps && d.gaps.gaps) ? d.gaps.gaps.filter(g => g.state !== 'confirmed').length : 0) + ' open point(s); ' + (r.not_covered || []).length + ' source(s) not covered (for example code and emergency); ' + (r.unaddressed || []).length + ' evidence item(s) not classified.'})];
      return stageBox('preview', '2b. Preview before apply', kids);
    }

    function draftCard(d) {
      const confirmed = d.intent && d.intent.status === 'CONFIRMED';
      const kids = [el('p', {class: 'muted', text: 'A model analyses your confirmed request against the stored evidence and drafts the impact and the proposed edits with citations. The server checks every citation and builds the typed edits itself; nothing is applied here.'}),
        phaseState('draft', d.draft)];
      const running = d.draft && !App.TERMINAL_JOB.includes(phase(d.draft));
      if (can('source.import')) kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-run-draft', text: 'Generate GenAI draft',
        disabled: !confirmed || running, on: {click: async ev => {
          const id = 'ui-draft-' + uuid().slice(0, 16);
          const r = await act(() => api('POST', '/v2/jobs', {workspace_id: ctx.ws, kind: 'change.draft', job_id: id, payload: {intent_id: F.ids.intent}}), {button: ev.currentTarget, stale: true});
          if (r === FAIL || ctx.gen !== S.gen) return;
          F.ids.impact = F.ids.simulation = F.ids.proposal = F.ids.review = F.ids.approval = F.ids.apply = ''; F.ids.query = '';
          App.setParams({draft: id, impact: '', simulation: '', proposal: '', review: '', approval: '', apply: '', query: ''}); F.ids.draft = id;
          await refresh(false);
        }}}), approveJobBtn(d.draft, 'Approve this model call (a different person than the requester)', 'action-approve-draft')));
      if (d.draft && d.draft.status === 'PENDING_APPROVAL') kids.push(el('p', {class: 'muted', text: 'This job spends model budget, so a second person must approve it before the worker runs it.'}));
      const r = draftResult(d);
      if (!r) return stageBox('draft', '2. AI draft with citations', kids);
      const u = d.draft.execution_usage || {};
      kids.push(el('p', {}, App.both('Result: ', 'Draft status: '), App.pill(r.status, App.phrase.draft(r.status), {testid: 'draft-status'}),
          el('span', {class: 'muted', text: ' - model ' + (d.draft.execution_model_id || 'n/a') + ' - tokens in ' + str(u.input_tokens) + ' / out ' + str(u.output_tokens) + ' - hash ' + String(r.draft_hash || '').slice(0, 12)})),
        el('p', {class: 'muted', testid: 'draft-claim', text: r.claim}),
        el('p', {testid: 'draft-summary', text: r.summary || (r.outcome ? 'No model call: ' + r.outcome + ' - ' + (r.reasons || []).join(', ') : '')}));
      for (const q of r.questions_for_human || []) kids.push(el('p', {class: 'blocker', text: 'Question: ' + str(q)}));
      for (const m of r.mismatches || []) kids.push(el('p', {class: 'blocker', text: 'The model read ' + m.field + ' as ' + str(m.model) + '; your confirmed value ' + str(m.intent) + ' is used.'}));
      const rows = (r.items || []).map(x => el('div', {class: 'card', testid: 'draft-item', data: {status: x.status, ref: x.ref}},
        el('p', {}, el('span', {class: 'pill', text: x.status}), ' ', el('code', {text: x.logical_path}), el('span', {class: 'muted', text: ' ' + (JSON.stringify(x.locator && (x.locator.key_path || x.locator.cell || x.locator.symbol || x.locator.line_start)) || '')})),
        el('p', {class: 'muted', text: 'Model said: ' + x.model_action + (x.reason ? ' - server: ' + x.reason : '') + ' - ' + x.rationale}),
        x.status === 'PROPOSED' ? el('p', {}, 'Edit: ', el('code', {text: x.proposal_edit.path + ': ' + str(x.proposal_edit.expected_current) + ' -> ' + str(x.proposal_edit.proposed)})) : null,
        el('blockquote', {class: 'muted', testid: 'draft-citation', text: x.citation.quote})));
      kids.push(el('h3', {text: 'Affected locations (' + rows.length + ')'}), rows, gapPanel(d));
      const list = (title, items, fmt, tid) => items && items.length ? [el('h3', {text: title + ' (' + items.length + ')'}), items.map(i => el('p', {class: 'blocker', testid: tid, text: fmt(i)}))] : [];
      kids.push(list('Not classified by the model (unaddressed)', r.unaddressed, i => i.logical_path + ' ' + (i.location || '') + ': ' + i.text, 'draft-unaddressed'),
        list('Unsupported or rejected citations', (r.unsupported || []).concat(r.rejected || []), i => JSON.stringify(i), 'draft-unsupported'),
        list('Not covered', r.not_covered, i => (i.logical_path || '') + ' ' + i.reason, 'draft-not-covered'),
        list('Stored findings kept open and unchanged (grouped; each stays open)', Object.entries((r.open_items || []).reduce((m, i) => { const k = (i.type || 'finding') + ': ' + str(i.reason); m[k] = (m[k] || 0) + 1; return m; }, {})), ([k, n]) => k + '  x' + n, 'draft-open-item'));
      return stageBox('draft', '2. AI draft with citations', kids);
    }

    function loopCard(d) {
      if (!d.loop) return null;
      const l = d.loop;
      const kids = [el('p', {}, 'Overall: ', App.pill(l.status, App.phrase.loop(l.status), {testid: 'loop-status'}),
          el('span', {class: 'muted', text: ' - ' + (l.reasons || []).join(', ')})),
        el('p', {class: 'muted', text: l.note}),
        el('p', {class: 'muted', testid: 'loop-stages', text: 'Verification ' + str(l.stages.verification) + ' - knowledge rebuild ' + str(l.stages.knowledge_rebuild) +
          ' - re-extraction ' + l.stages.reanalysis.completed + '/' + l.stages.reanalysis.total + ' - knowledge blockers ' + str(l.stages.knowledge_blockers)})];
      const res = l.residual_counterevidence || [];
      kids.push(el('h3', {text: 'Sources still not at the new value (' + res.length + ')'}),
        res.map(x => el('p', {class: 'blocker', testid: 'loop-residual', data: {state: x.current_state}, text: x.logical_path + ' - ' + x.draft_status + ' - now: ' + x.current_state})));
      return stageBox('loop', '8. Where this change stands', kids);
    }

    function queryCard(d) {
      if (!F.ids.apply || !(d.apply && jobState(d.apply) === 'COMPLETED')) return null;
      const kids = [el('p', {class: 'muted', text: 'Ask again on the new revision. The answer must cite the new source text; a claim the new revision does not support is withheld.'}), phaseState('query', d.query)];
      if (can('source.import')) {
        const q = App.bindDraft(el('input', {id: 'fld-query-q', type: 'text', testid: 'field-query'}), 'query-q');
        const sel = el('select', {id: 'fld-query-art', testid: 'field-query-artifact', multiple: 'multiple'});
        for (const a of d.arts || []) sel.append(el('option', {value: a.artifact_id, text: a.logical_path}));
        kids.push(el('label', {for: 'fld-query-q', text: 'Question'}), q, el('label', {for: 'fld-query-art', text: 'Sources to search (hold Ctrl to pick several)'}), sel,
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-run-query', text: 'Ask on the new revision', on: {click: async ev => {
            const ids = Array.from(sel.selectedOptions).map(o => o.value), qt = (S.drafts['query-q'] || '').trim();
            if (!qt || !ids.length) { App.say('Enter a question and pick at least one source.'); return; }
            const id = 'ui-query-' + uuid().slice(0, 16);
            const r = await act(() => api('POST', '/v2/jobs', {workspace_id: ctx.ws, kind: 'gap.answer', job_id: id, payload: {question: qt, question_id: id, artifact_ids: ids}}), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return;
            F.ids.query = id; App.setParams({query: id}); await refresh(false);
          }}}), approveJobBtn(d.query, 'Approve this model call', 'action-approve-query')));
      }
      if (d.query && jobState(d.query) === 'COMPLETED') {
        const r = d.query.execution_result || {};
        kids.push(el('p', {}, 'Result: ', el('span', {class: 'pill', testid: 'query-status', data: {state: r.status}, text: r.status}), ' ', el('strong', {testid: 'query-answer', text: r.answer || ''})),
          r.question_for_human ? el('p', {class: 'blocker', text: r.question_for_human}) : null,
          (r.evidence_spans || []).map(sp => el('blockquote', {class: 'muted', testid: 'query-citation', text: sp.artifact_path + ' @ ' + sp.source_revision_id + ': ' + sp.quote})));
      }
      return stageBox('query', '9. Ask again on the new revision', kids);
    }


    const REASONS = {
      no_confirmed_relation_evidence_bounded_investigation_not_run: 'No relation evidence covers this rule and the target - UNKNOWN (not "unaffected"). Compute relation coverage and re-run.',
      candidate_not_found: 'Not a rule of this workspace (or not visible to you).',
      no_target_candidate_resolved: 'No target rule could be resolved.',
    };
    function impactReport(res) {
      const box = el('div', {testid: 'impact-report'});
      const labels = {};
      const lab = id => labels[id] ? labels[id].label + (labels[id].logical_path ? ' (' + labels[id].logical_path + ')' : '') : id;
      const cite = ev => (ev || []).map(e => el('div', {class: 'muted', text: 'cites ' + str(e.locator) + (e.quote || e.text ? ': ' + (e.quote || e.text) : '')}));
      function section(title, items, render, testid) {
        return el('div', {testid}, el('h4', {text: title + ' (' + items.length + ')'}),
          items.length ? el('ul', {}, items.map(it => el('li', {}, render(it)))) : el('p', {class: 'muted', text: 'none'}));
      }
      function draw() {
        const cov = res.coverage || {};
        replace(box,
          el('p', {class: 'muted', text: 'Paths come from stored relations and a deterministic text scan of the pinned revision. A graph projection is not a causal dependency; UNKNOWN means not covered, not unaffected.'}),
          section('Direct', res.direct || [], x => [el('strong', {text: lab(x.candidate_id)}), ' path: ' + (x.path || []).join(' \u2192 ')].concat(cite(x.evidence)), 'impact-direct'),
          section('Indirect (file level)', res.indirect || [], x => [el('strong', {text: x.logical_path}), ' rule: ' + x.rule + ' path: ' + (x.path || []).join(' \u2192 ')].concat(cite(x.evidence)), 'impact-indirect'),
          (res.indirect_model || []).length ? section('Indirect (model judgement, re-validated)', res.indirect_model, x => [el('strong', {text: x.logical_path}), ' quote: ' + x.evidence_quote], 'impact-indirect-model') : null,
          section('Unknown', res.unknown || [], x => [el('strong', {text: lab(x.candidate_id)}), ' - ' + (REASONS[x.reason] || x.reason)], 'impact-unknown'),
          section('Excluded', res.excluded || [], x => [el('strong', {text: lab(x.candidate_id)}), ' - ' + (REASONS[x.reason] || x.reason)], 'impact-excluded'),
          (res.existing_conflicts || []).length ? section('Existing findings carried forward', res.existing_conflicts, x => (x.kind || 'finding') + ': ' + (x.reason || ''), 'impact-findings') : null,
          el('p', {testid: 'impact-coverage', class: cov.impact_complete ? 'muted' : 'blocker', data: {traversal: String(!!cov.traversal_complete), impact: String(!!cov.impact_complete)}, text: cov.impact_complete ? 'Impact fully resolved (no UNKNOWN rule).' : (cov.traversal_complete ? 'Traversal finished, but the impact is NOT fully resolved: ' : 'Traversal INCOMPLETE: ') + [
            (cov.omitted_relation_jobs || []).length && 'relation jobs unusable', (cov.unresolved_candidate_ids || []).length && 'unresolved rules',
            cov.truncated && 'context truncated', cov.omitted_scan_observations && 'scan omitted ' + cov.omitted_scan_observations + ' observations',
            (res.unknown || []).length && (res.unknown || []).length + ' rule(s) UNKNOWN'].filter(Boolean).join('; ')}));
      }
      draw();
      if (res.target_candidate_id) api('GET', '/v2/change/impact-context?workspace_id=' + enc(ctx.ws) + '&target_candidate_id=' + enc(res.target_candidate_id))
        .then(cx => { Object.assign(labels, cx.candidates || {}); draw(); }).catch(() => {});
      return box;
    }

    function simulationReport(h) {
      const counts = o => Object.entries(o || {}).map(([k, v]) => k + ' ' + v).join(', ');
      const rows = (h.changed || []).slice(0, 60);
      return el('div', {testid: 'sim-report', data: {changed: h.changed_count, unexplained: h.unexplained_changes}},
        el('h4', {}, el('span', {class: 'pill', text: 'DETERMINISTIC'}), ' Harbor itself, before vs after'),
        el('p', {class: 'muted', testid: 'sim-integrity', text: 'Adapter check: the seed config reproduced Harbor\'s shipped output (' + ((h.integrity || {}).rows) + '/' + ((h.integrity || {}).rows) + ' rows ' + ((h.integrity || {}).seed_equals_shipped ? 'identical' : 'DIFFERENT') + '). Baseline config: ' + ((h.baseline_source || {}).logical_path || (h.baseline_source || {}).kind) + (((h.baseline_source || {}).source_revision_id) ? ' @ ' + h.baseline_source.source_revision_id : '') + '.'}),
        el('p', {}, 'Edit(s): ' + (h.edits || []).map(e => e.path + ' ' + e.from + ' -> ' + e.to + (e.no_op ? ' (no change)' : '')).join('; ')),
        el('p', {testid: 'sim-summary', text: h.transactions + ' public ' + (h.case === 'match' ? 'invoices' : 'requisitions') + ' simulated. Before: ' + counts(h.by_route_before) + '. After: ' + counts(h.by_route_after) + '. Changed: ' + h.changed_count + (h.unexplained_changes ? ' (' + h.unexplained_changes + ' NOT explained by the edited keys)' : ' (all explained by the edited keys)') + '.'}),
        h.changed_count ? el('table', {testid: 'sim-changed'}, el('thead', {}, el('tr', {}, [h.case === 'match' ? 'Invoice' : 'Request', 'Amount JPY', 'Detail', 'Before', 'After', 'Cause (config key, code)'].map(t => el('th', {text: t})))),
          el('tbody', {}, rows.map(c => el('tr', {data: {request: c.id}}, [c.id, String(c.amount_jpy), c.label, c.before.outcome + ' (' + c.before.reason + (c.before.due_date ? ', due ' + c.before.due_date : '') + ')', c.after.outcome + ' (' + c.after.reason + (c.after.due_date ? ', due ' + c.after.due_date : '') + ')',
            (c.cause || []).map(x => x.config_key + ' ' + x.from + '->' + x.to + (x.code_ref ? ' [' + x.code_ref.file + ':' + x.code_ref.line + ']' : '')).join('; ') || 'not explained'].map(t => el('td', {text: t})))))) : el('p', {class: 'muted', text: 'No requisition changes its route under this edit.'}),
        h.snapshot ? el('p', {class: 'muted', testid: 'sim-snapshot', text: h.snapshot.statement + '. Code ' + h.snapshot.code_sha256.slice(0, 10) + ', input ' + h.snapshot.input_sha256.slice(0, 10) + ' (' + h.snapshot.input_files.join(', ') + '), config ' + h.snapshot.config_baseline_sha256.slice(0, 10) + ' vs ' + h.snapshot.config_proposed_sha256.slice(0, 10) + '.'}) : null,
        (h.not_controlled || []).length ? el('div', {testid: 'sim-not-controlled'}, el('h4', {text: 'Config does not control this behaviour'}), el('ul', {}, h.not_controlled.map(n => el('li', {text: n.behaviour + ': ' + n.proof + ' [' + n.evidence.file + ':' + n.evidence.line + '  ' + n.evidence.text + ']'})))) : null,
        (h.no_change_edits || []).length ? el('div', {testid: 'sim-no-change'}, el('h4', {text: 'No change in this sample (the key IS read by the code - not inert)'}), el('ul', {}, h.no_change_edits.map(n => el('li', {text: n.path + ' ' + n.from + ' -> ' + n.to + ': consumed at ' + n.consumers.map(c => c.file + ':' + c.line).join(', ')})))) : null,
        (h.inert_edits || []).length ? el('div', {testid: 'sim-inert'}, el('h4', {text: 'Edits the code does not read (traced)'}), el('ul', {}, h.inert_edits.map(i => el('li', {text: i.path + ' ' + i.from + ' -> ' + i.to + ': ' + i.reason + (i.code_ref ? ' [' + i.code_ref.file + ':' + i.code_ref.line + ']' : '')})))) : null,
        h.changed_truncated || h.changed_count > rows.length ? el('p', {class: 'muted', text: 'Showing ' + rows.length + ' of ' + h.changed_count + ' changed requisitions.'}) : null,
        el('h4', {text: 'Limits of this simulation'}), el('ul', {}, (h.limits || []).map(t => el('li', {text: t}))),
        el('p', {class: 'muted', testid: 'sim-runner', text: (h.runner || {}).statement}));
    }

    function analysisCard(d) {
      const confirmed = d.intent && d.intent.status === 'CONFIRMED';
      const kids = [];
      const btn = (label, testid, enabled, click) => el('button', {type: 'button', testid, text: label, disabled: !enabled, on: {click}});
      const mayRun = can('source.import');
      kids.push(stageState('impact', d.impact));
      const impactTarget = () => {
        const dr = draftResult(d);
        const draftCand = dr && (dr.edits || []).flatMap(e => e.candidate_ids || [])[0];
        return draftCand || (App.parseHash().params || {}).rule || d.intent.target;
      };
      if (mayRun) kids.push(el('div', {class: 'row'},
        btn('Compute relation coverage (no model call)', 'action-run-relations', confirmed && !!impactTarget(), async ev => {
          const cx = await act(() => api('GET', '/v2/change/impact-context?workspace_id=' + enc(ctx.ws) + '&target_candidate_id=' + enc(impactTarget())), {button: ev.currentTarget});
          if (cx === FAIL || ctx.gen !== S.gen) return;
          const id = 'ui-relation-' + uuid().slice(0, 16);
          const r = await createStage('relation', id, {candidate_ids: cx.candidate_ids}, ev.currentTarget);
          if (r === FAIL || ctx.gen !== S.gen) return;
          App.say('Relation job ' + id + ' created. When it completes, run impact analysis so it uses the new relation evidence. Without it, other rules stay UNKNOWN.');
        }),
        btn('Run impact analysis', 'action-run-impact', confirmed && !(d.impact && !App.TERMINAL_JOB.includes(jobState(d.impact))), async ev => {
          const target = impactTarget();
          let cx = null;
          if (target) {
            try { cx = await api('GET', '/v2/change/impact-context?workspace_id=' + enc(ctx.ws) + '&target_candidate_id=' + enc(target)); } catch (e) {
              if (!(e instanceof App.ApiError) || e.status !== 404) { await act(() => { throw e; }); return; }
              App.say('No extracted rule matches this request, so the impact analysis runs without a target rule: related rules stay UNKNOWN.');
            }
            if (ctx.gen !== S.gen) return;
          }
          const id = 'ui-impact-' + uuid().slice(0, 16);
          const payload = cx ? {candidate_ids: cx.candidate_ids, target_candidate_id: target, relation_job_ids: cx.relation_job_ids,
            existing_finding_ids: cx.existing_finding_ids, expected_decision_epoch: cx.expected_decision_epoch, context_truncated: cx.context_truncated}
            : {candidate_ids: [], existing_finding_ids: []};
          const r = await createStage('impact', id, payload, ev.currentTarget);
          if (r === FAIL || ctx.gen !== S.gen) return;
          F.ids.simulation = ''; F.ids.proposal = ''; F.ids.review = ''; F.ids.approval = ''; F.ids.apply = '';
          App.setParams({impact: id, simulation: '', proposal: '', review: '', approval: '', apply: ''}); F.ids.impact = id;
          await refresh(false);
        })));
      if (done(d.impact)) kids.push(impactReport(d.impact.execution_result || {}));
      kids.push(stageState('simulation', d.simulation));
      if (mayRun) kids.push(el('div', {class: 'row'}, btn('Run simulation', 'action-run-simulation', done(d.impact) && !(d.simulation && !App.TERMINAL_JOB.includes(jobState(d.simulation))), async ev => {
        const id = 'ui-simulation-' + uuid().slice(0, 16);
        // The Harbor adapter simulates the scalar YAML edits the (server-checked) draft proposes for the approval config.
        // Without such an edit there is nothing Harbor can run: an empty run is recorded and shown as UNAVAILABLE.
        const dr = draftResult(d);
        const SIM = {route: ['routing_rules.standard_manager_threshold_jpy', 'routing_rules.capex_finance_threshold_jpy'],
          match: ['matching_rules.price_tolerance_fixed_jpy', 'matching_rules.price_tolerance_percent', 'matching_rules.payment_term_days']};
        const cfgEdits = ((dr && dr.edits) || []).filter(e => /config\/approval\.yaml$/.test(e.logical_path || '') && e.proposal_edit && Number.isInteger(e.proposal_edit.proposed));
        const simCase = cfgEdits.some(e => SIM.route.includes(e.proposal_edit.path)) ? 'route' : (cfgEdits.some(e => SIM.match.includes(e.proposal_edit.path)) ? 'match' : null);
        const simEdits = simCase ? cfgEdits.filter(e => SIM[simCase].includes(e.proposal_edit.path))
          .map(e => ({path: e.proposal_edit.path, value: e.proposal_edit.proposed, expected_current: Number.isInteger(e.proposal_edit.expected_current) ? e.proposal_edit.expected_current : undefined})) : [];
        const payload = simEdits.length ? {adapter: 'harbor_cli', case: simCase, edits: simEdits} : {};
        const r = await createStage('simulation', id, payload, ev.currentTarget);
        if (r === FAIL || ctx.gen !== S.gen) return;
        F.ids.proposal = ''; F.ids.review = ''; F.ids.approval = ''; F.ids.apply = '';
        App.setParams({simulation: id, proposal: '', review: '', approval: '', apply: ''}); F.ids.simulation = id;
        await refresh(false);
      })));
      if (done(d.simulation)) {
        const res = d.simulation.execution_result || {};
        const cov = res.coverage || {};
        kids.push(el('p', {testid: 'sim-denominator', text: 'Denominator (records submitted): ' + (cov.submitted === undefined ? 'not reported' : cov.submitted) + ' - ' + str(cov)}),
          (cov.submitted === undefined || cov.submitted === 0) ? el('p', {class: 'blocker', testid: 'sim-unavailable', text: 'Simulation UNAVAILABLE for this change: no behaviour corpus or adapter is connected, so nothing was simulated. This is NOT a "no impact" result.'}) : null,
          el('p', {testid: 'sim-mode', text: 'Mode: ' + (res.status || 'unknown')}),
          el('p', {testid: 'sim-limits', text: 'Limits: ' + (res.reason || 'none reported')}));
        if (res.harbor && res.status === 'EXECUTED') kids.push(simulationReport(res.harbor));
      }
      return stageBox('analysis', '3. Impact and simulation', kids);
    }

    function proposalCard(d) {
      const kids = [stageState('proposal', d.proposal)];
      const dr = draftResult(d);
      const proposed = dr ? (dr.edits || []).filter(e => e.status === 'PROPOSED') : [];
      if (d.gaps && d.gaps.stale) kids.push(el('p', {class: 'blocker', testid: 'proposal-draft-stale', text: 'This draft is stale (' + (d.gaps.stale_reasons || []).join('; ') + '). Generate a new draft before preparing a proposal from it.'}));
      if (can('source.import') && done(d.simulation) && proposed.length && !(d.gaps && d.gaps.stale)) {
        const chosen = new Set(proposed.map(e => e.ref));
        const boxes = proposed.map(e => {
          const cb = el('input', {type: 'checkbox', checked: 'checked', testid: 'proposal-accept-' + e.ref, on: {change: ev => { if (ev.target.checked) chosen.add(e.ref); else chosen.delete(e.ref); }}});
          return el('label', {}, cb, ' ', el('code', {text: e.logical_path + ' ' + e.proposal_edit.path + ': ' + str(e.proposal_edit.expected_current) + ' -> ' + str(e.proposal_edit.proposed)}));
        });
        kids.push(el('h3', {text: 'Proposal from the GenAI draft - nothing to retype'}), boxes,
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-prepare-proposal-from-draft', text: 'Prepare proposal from draft', on: {click: async ev => {
            const refs = proposed.filter(e => chosen.has(e.ref));
            if (!refs.length) { App.say('Keep at least one proposed edit.'); return; }
            const target = refs[0].artifact_id;
            if (refs.some(e => e.artifact_id !== target)) { App.say('One proposal covers one artifact; keep edits of a single file.'); return; }
            const id = 'ui-proposal-' + uuid().slice(0, 16);
            const r = await createStage('proposal', id, {intent_id: F.ids.intent, impact_job_id: F.ids.impact, simulation_job_id: F.ids.simulation,
              target_artifact_id: target, edit_kind: 'yaml_typed_edit', draft_job_id: F.ids.draft, accepted_refs: refs.map(e => e.ref)}, ev.currentTarget);
            if (r === FAIL || ctx.gen !== S.gen) return;
            F.ids.review = ''; F.ids.approval = ''; F.ids.apply = '';
            App.setParams({proposal: id, review: '', approval: '', apply: ''}); F.ids.proposal = id;
            await refresh(false);
          }}})));
        const bind = d.proposal && done(d.proposal) ? (d.proposal.execution_result || {}).draft_binding : null;
        if (bind && (bind.left_for_humans || []).length) {
          kids.push(el('h3', {text: 'Left for people (not part of this proposal)'}),
            bind.left_for_humans.map(x => el('p', {class: 'blocker', testid: 'left-for-humans', text: x.logical_path + ' - ' + x.status + (x.reason ? ' - ' + x.reason : '')})));
        }
      }
      if (can('source.import') && done(d.simulation) && !proposed.length) {

        const kindSel = el('select', {id: 'fld-prop-kind', testid: 'field-proposal-kind', on: {change: ev => { S.drafts['prop-kind'] = ev.target.value; draw(); }}},
          KINDS.map(([v, l]) => el('option', {value: v, text: l})));
        kindSel.value = S.drafts['prop-kind'] || 'yaml_typed_edit';
        const artSel = el('select', {id: 'fld-prop-art', testid: 'field-proposal-artifact'}, el('option', {value: '', text: 'Select the artifact to change'}));
        for (const a of d.arts || []) artSel.append(el('option', {value: a.artifact_id, text: a.logical_path}));
        App.bindDraft(artSel, 'prop-art');
        const kind = kindSel.value;
        const field = (id, label, testid, key) => [el('label', {for: id, text: label}), App.bindDraft(el('input', {id, type: 'text', testid}), key)];
        const fields = [];
        if (kind === 'yaml_typed_edit') fields.push(field('fld-prop-path', 'Key path (dotted)', 'field-proposal-path', 'prop-path'));
        if (kind === 'xlsx_typed_edit') fields.push(field('fld-prop-sheet', 'Sheet name', 'field-proposal-sheet', 'prop-sheet'), field('fld-prop-cell', 'Cell (for example B2)', 'field-proposal-cell', 'prop-cell'));
        if (kind === 'yaml_typed_edit' || kind === 'xlsx_typed_edit') {
          fields.push(field('fld-prop-expected', 'Expected current value', 'field-proposal-expected', 'prop-expected'),
                      field('fld-prop-value', 'New value', 'field-proposal-value', 'prop-value'));
        }
        kids.push(el('label', {for: 'fld-prop-art', text: 'Artifact'}), artSel,
          el('label', {for: 'fld-prop-kind', text: 'Kind of edit'}), kindSel, fields,
          el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-prepare-proposal', text: 'Prepare proposal', on: {click: async ev => {
            if (!artSel.value) { App.say('Select the artifact first.'); return; }
            const payload = {intent_id: F.ids.intent, impact_job_id: F.ids.impact, simulation_job_id: F.ids.simulation,
              target_artifact_id: artSel.value, edit_kind: kind};
            const d0 = S.drafts;
            if (kind === 'yaml_typed_edit') payload.edits = [{path: (d0['prop-path'] || '').trim(), expected_current: parseValue(d0['prop-expected'] || ''), proposed: parseValue(d0['prop-value'] || '')}];
            if (kind === 'xlsx_typed_edit') payload.edits = [{sheet: (d0['prop-sheet'] || '').trim(), cell: (d0['prop-cell'] || '').trim(), expected_current: parseValue(d0['prop-expected'] || ''), proposed: parseValue(d0['prop-value'] || '')}];
            const id = 'ui-proposal-' + uuid().slice(0, 16);
            const r = await createStage('proposal', id, payload, ev.currentTarget);
            if (r === FAIL || ctx.gen !== S.gen) return;
            F.ids.review = ''; F.ids.approval = ''; F.ids.apply = '';
            App.setParams({proposal: id, review: '', approval: '', apply: ''}); F.ids.proposal = id;
            await refresh(false);
          }}})));
      }
      if (done(d.proposal)) {
        const pr = d.proposal.execution_result || {};
        kids.push(el('p', {class: 'muted', text: 'Classification: ' + pr.classification + ' - readiness: ' + pr.readiness + ' - kind: ' + pr.edit_kind}));
        kids.push(el('p', {}, 'After hash: ', el('code', {testid: 'diff-after-hash', text: pr.after_blob_hash || 'unchanged'}),
          el('span', {class: 'muted', text: ' - before hash ' + (pr.before_blob_hash || 'n/a')})));
        if (d.diff) {
          const view = el('div', {testid: 'diff-view'});
          if (!d.diff.lines.length) view.append(el('p', {class: 'muted', text: d.diff.is_no_op ? 'This proposal changes nothing (no-op).' : 'No machine-produced text diff: a person must author this change.'}));
          for (const line of d.diff.lines) {
            if (line.kind === 'hunk') view.append(el('div', {testid: 'diff-hunk', text: line.text}));
            else view.append(el('div', {testid: 'diff-line', data: {kind: line.kind}, text: line.text}));
          }
          if (d.diff.truncated) view.append(el('p', {class: 'muted', text: 'The diff is truncated at ' + d.diff.lines.length + ' lines.'}));
          kids.push(el('h3', {text: 'Exact diff of ' + (d.diff.logical_path || d.diff.target_artifact_id)}), view);
        }
      }
      return stageBox('proposal', '4. Proposal', kids);
    }

    // Operator-supplied acceptance value (W1 D-4). The form is deliberately BLANK: nothing is
    // prefilled from the proposal, so the expected value is never copied from the change under review.
    function contractCard(d) {
      const kids = [el('h3', {text: 'Check after the change (expected result, stated independently by an approver)'}), App.B(el('p', {class: 'muted', text: 'After the change is applied the system reads the file again and compares it with the value you state here. You state it yourself from the confirmed request; it is never filled in for you.'}))];
      const list = d.contracts || [];
      for (const c of list) {
        kids.push(el('p', {testid: 'contract-row', data: {'contract-id': c.contract_id}},
          el('code', {text: c.logical_path + ' @ ' + c.locator}), ' = ', el('code', {text: str(c.expected)}),
          el('span', {class: 'muted', text: ' - set by ' + c.author_account_id + ' - sha256 ' + String(c.contract_sha256 || '').slice(0, 12)})));
      }
      if (!list.length) kids.push(el('p', {class: 'muted', testid: 'contract-none', text: 'No contract exists for this proposal. Without one the acceptance check stays UNAVAILABLE and the review stays blocked.'}));
      if (!done(d.proposal)) return kids;
      if (!can('verification.contract.set')) {
        kids.push(el('p', {class: 'muted', text: 'An approver or admin who did not request this change states the expected value.'}));
        return kids;
      }
      const field = (id, label, testid, key) => [el('label', {for: id, text: label}), App.bindDraft(el('input', {id, type: 'text', testid}), key)];
      const kindSel = el('select', {id: 'fld-contract-kind', testid: 'field-contract-kind'},
        el('option', {value: 'yaml_value_equals', text: 'YAML value equals'}), el('option', {value: 'xlsx_cell_equals', text: 'Workbook cell equals'}));
      App.bindDraft(kindSel, 'contract-kind');
      kids.push(el('label', {for: 'fld-contract-kind', text: 'Kind'}), kindSel,
        field('fld-contract-path', 'File (path as shown in Sources)', 'field-contract-path', 'contract-path'),
        field('fld-contract-locator', 'Where in the file (key, or Sheet!Cell for a workbook)', 'field-contract-locator', 'contract-locator'),
        field('fld-contract-expected', 'Value that must be there after the change', 'field-contract-expected', 'contract-expected'),
        field('fld-contract-note', 'Where this expectation comes from (for the record)', 'field-contract-note', 'contract-note'),
        el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-set-contract', text: 'Save contract', on: {click: async ev => {
          const dr = S.drafts;
          const body = {workspace_id: ctx.ws, proposal_job_id: F.ids.proposal, kind: dr['contract-kind'] || 'yaml_value_equals',
            logical_path: (dr['contract-path'] || '').trim(), locator: (dr['contract-locator'] || '').trim(),
            expected: parseValue(dr['contract-expected'] || '')};
          const note = (dr['contract-note'] || '').trim();
          if (note) body.provenance = {note};
          if (!body.logical_path || !body.locator || !(dr['contract-expected'] || '').trim()) { App.say('Fill in the path, the key or cell and the expected value.'); return; }
          const r = await act(() => api('POST', '/v2/verification/contracts', body), {button: ev.currentTarget});
          if (r === FAIL || ctx.gen !== S.gen) return;
          for (const k of ['contract-path', 'contract-locator', 'contract-expected', 'contract-note']) S.drafts[k] = '';
          App.say('Contract saved. Run the review to use it.');
          await refresh(false);
        }}})));
      return kids;
    }

    /* What was confirmed, in plain words, for the person who must state the expected result independently. Reads the intent only. */
    function confirmedRequestPanel(d) {
      const f = fmtForm(d.intent);
      return el('div', {class: 'nextact', testid: 'confirmed-request'}, el('strong', {text: 'The confirmed request'}),
        el('span', {text: f.title + ': from ' + f.from + ' to ' + f.to + '. Applies to: ' + f.scope + '. '}),
        el('span', {class: 'muted', text: 'Type the expected result below from this request, not from the proposal or from any AI text.'}));
    }

    function reviewCard(d) {
      const kids = [stageState('review', d.review)];
      if (done(d.proposal) && d.intent) kids.push(confirmedRequestPanel(d));
      if (done(d.proposal)) kids.push(...contractCard(d));
      if (can('source.import')) kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-run-review', text: 'Run review',
        disabled: !done(d.proposal) || !!(d.review && !App.TERMINAL_JOB.includes(jobState(d.review))), on: {click: async ev => {
          const id = 'ui-review-' + uuid().slice(0, 16);
          const payload = {proposal_job_id: F.ids.proposal};
          const named = (d.contracts || []);
          if (named.length === 1) payload.verification_contract_id = named[0].contract_id;
          const r = await createStage('review', id, payload, ev.currentTarget);
          if (r === FAIL || ctx.gen !== S.gen) return;
          F.ids.approval = ''; F.ids.apply = '';
          App.setParams({review: id, approval: '', apply: ''}); F.ids.review = id;
          await refresh(false);
        }}})));
      if (done(d.review)) {
        const rr = d.review.execution_result || {};
        kids.push(el('h3', {text: 'Roles (evaluated separately)'}));
        for (const [name, role] of Object.entries(rr.roles || {})) kids.push(el('div', {testid: 'review-role', data: {role: name, state: role.status}}, name + ': ', pill(role.status)));
        kids.push(el('h3', {text: 'Tool checks'}));
        for (const [name, chk] of Object.entries(rr.checks || {})) {
          kids.push(el('div', {testid: 'tool-check', data: {check: name, state: chk.status}}, name + ': ', pill(chk.status), chk.detail ? el('span', {class: 'muted', text: ' - ' + chk.detail}) : null));
        }
        kids.push(el('p', {testid: 'review-disclosure', text: rr.model_correlation_disclosure || 'No disclosure about model independence was reported.'}));
      }
      return stageBox('review', '4. Review', kids);
    }

    function limitations(d) {
      const out = [];
      const pr = (d.proposal && d.proposal.execution_result) || {};
      const rr = (d.review && d.review.execution_result) || {};
      const sr = (d.simulation && d.simulation.execution_result) || {};
      if (pr.edit_kind === 'xlsx_typed_edit') out.push('Workbook edit: cached formula values are not recalculated. Open the exported workbook in a spreadsheet application to recalculate before relying on formula results.');
      if (sr.reason) out.push('Simulation limit: ' + sr.reason + (sr.status ? ' (mode ' + sr.status + ')' : ''));
      for (const [name, chk] of Object.entries(rr.checks || {})) {
        if (chk.status === 'UNAVAILABLE' || chk.status === 'NOT_RUN') out.push('Check ' + name + ' is ' + chk.status + (chk.detail ? ': ' + chk.detail : ''));
      }
      return out;
    }

    function approvalCard(d) {
      const kids = [stageState('approval', d.approval)];
      if (can('source.import')) kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-prepare-approval', text: 'Prepare approval bundle',
        disabled: !done(d.review) || !!(d.approval && !App.TERMINAL_JOB.includes(jobState(d.approval))), on: {click: async ev => {
          const id = 'ui-approval-' + uuid().slice(0, 16);
          const r = await createStage('approval', id, {proposal_job_id: F.ids.proposal, review_job_id: F.ids.review}, ev.currentTarget);
          if (r === FAIL || ctx.gen !== S.gen) return;
          F.ids.apply = '';
          App.setParams({approval: id, apply: ''}); F.ids.approval = id;
          M.staleShown = false;
          await refresh(false);
        }}})));
      if (!done(d.approval)) return stageBox('approval', '5. Approval', kids);
      const ar = d.approval.execution_result || {};
      const dec = (d.decisions && d.decisions.decisions) || [];
      const last = dec.length ? dec[dec.length - 1] : null;
      const decState = last ? ({approve: 'APPROVED', reject: 'REJECTED', request_changes: 'CHANGES_REQUESTED'})[last.action] || 'NONE' : 'NONE';
      const stale = approvalStale(d);
      const pr = (d.proposal && d.proposal.execution_result) || {};
      kids.push(el('p', {}, 'Approval digest: ', el('code', {testid: 'approval-digest', text: ar.approval_digest})),
        el('p', {class: 'muted', text: 'Readiness: ' + ar.readiness + ' - required action: ' + ar.required_action}));
      // manual work required BEFORE approving (only while nothing was applied yet)
      if (!F.ids.apply) {
        for (const step of pr.verification_plan || []) {
          if (step.action === 'human_authoring_required') kids.push(el('div', {class: 'blocker', testid: 'manual-work-item'}, 'Manual work required: ', step.reason || 'a person must author this edit'));
        }
      }
      const sim = d.simulation && jobState(d.simulation) === 'COMPLETED' ? (d.simulation.execution_result || {}) : null;
      const hb = sim && sim.harbor && sim.status === 'EXECUTED' ? sim.harbor : null;
      kids.push(el('div', {class: 'nextact', testid: 'decision-brief'}, el('strong', {text: 'What you are deciding'}),
        el('span', {text: (d.intent ? (() => { const f = fmtForm(d.intent); return f.title + ': ' + f.from + ' → ' + f.to + '. '; })() : '') +
          'Approving records your decision with your name and reason; it does not change any file by itself. Only the exact edit in the diff above can be applied afterwards. ' +
          (hb ? 'In the simulation, ' + hb.changed_count + ' of ' + hb.transactions + ' sample cases would change their outcome. ' : 'No behaviour simulation backs this change. ') +
          ((pr.verification_plan || []).filter(x => x.action === 'human_authoring_required').length ? (pr.verification_plan || []).filter(x => x.action === 'human_authoring_required').length + ' related item(s) are left for people and stay unresolved.' : '')})));
      const lims = limitations(d);
      if (lims.length) kids.push(el('h3', {text: 'Limitations'}));
      for (const l of lims) kids.push(el('div', {class: 'limit', testid: 'limitation-item', text: l}));
      const blockers = ar.blocked_reasons || [];
      kids.push(el('h3', {text: blockers.length ? 'Blockers (' + blockers.length + ') - shown before you decide' : 'No blockers reported'}));
      for (const b of blockers) {
        kids.push(el('div', {class: 'blocker', testid: 'blocker-item'}, (b.name || b.kind || 'blocker') + ': ' + str(b.reason !== undefined ? b.reason : (b.detail !== undefined ? b.detail : ''))));
      }
      if (blockers.length) kids.push(el('p', {class: 'muted', text: 'The server records an approval together with its blockers; approving does not clear them.'}));
      kids.push(el('p', {}, 'Decision: ', App.pill(decState, ({NONE: 'Waiting for the approver to decide', APPROVED: 'Approved', REJECTED: 'Rejected', CHANGES_REQUESTED: 'Changes requested'})[decState] || decState, {testid: 'state-approval-decision'}),
        last ? el('span', {class: 'muted', text: ' by ' + last.actor_id + (last.reason ? ' - ' + last.reason : '')}) : null));
      if (stale) {
        kids.push(el('p', {class: 'blocker', text: 'This approval bundle was computed before the workspace changed. It cannot be approved as shown; prepare a new bundle.'}));
        if (!M.staleShown) { M.staleShown = true; App.showStale('This approval bundle is out of date (the source or decision epoch changed after it was computed).'); }
      }
      if (can('change.approve') && decState === 'NONE') {
        const digest = ar.approval_digest;   // exactly what is on screen; never re-read at click time
        const jobId = d.approval.job_id;
        const reason = App.bindDraft(el('textarea', {id: 'fld-decision-reason', rows: '2', testid: 'field-decision-reason'}), 'decision-reason');
        const decide = (action, label, testid, secondary) => el('button', {type: 'button', testid, class: secondary ? 'secondary' : '', text: label, disabled: stale, on: {click: async ev => {
          const text = reason.value.trim();
          if (!text) { App.say('A reason is required for this decision.'); return; }
          const r = await act(() => api('POST', '/v2/jobs/' + enc(jobId) + '/decision', {
            workspace_id: ctx.ws, action, reason: text, expected_approval_digest: digest, idempotency_key: uuid()}),
            {button: ev.currentTarget, stale: true});
          if (r === FAIL || ctx.gen !== S.gen) return;
          S.drafts['decision-reason'] = '';
          App.say('Your ' + action + ' decision was recorded on approval ' + jobId + '.');
          await refresh(false);
        }}});
        kids.push(el('label', {for: 'fld-decision-reason', text: 'Decision reason'}), reason,
          el('div', {class: 'row'}, decide('approve', 'Approve', 'action-approve'), decide('reject', 'Reject', 'action-reject', true)));
      }
      return stageBox('approval', '5. Approval', kids);
    }

    function applyCard(d) {
      const kids = [];
      const dec = (d.decisions && d.decisions.decisions) || [];
      const approved = dec.length && dec[dec.length - 1].action === 'approve';
      if (!F.ids.apply) {
        if (can('source.import')) {
          const ar = (d.approval && d.approval.execution_result) || {};
          kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-apply', text: 'Apply approved change',
            disabled: !(approved && done(d.approval)), on: {click: async ev => {
              const id = 'ui-apply-' + uuid().slice(0, 16);
              const r = await createStage('apply', id, {approval_job_id: d.approval.job_id, expected_approval_digest: ar.approval_digest}, ev.currentTarget);
              if (r === FAIL || ctx.gen !== S.gen) return;
              F.ids.apply = id; App.setParams({apply: id});
              await refresh(false);
            }}})));
        }
        kids.push(el('p', {class: 'muted', text: approved ? 'The approval is recorded. Applying publishes the change to the source.' : 'Apply becomes available after a human approval is recorded.'}));
      }
      return stageBox('apply', '6. Apply', kids);
    }

    // ---------------------------------------------------------------- request card, "what next" and stepper
    const fmtForm = it => {
      const sf = (it && it.structured_form) || {};
      return {title: App.fmt.humanize(sf.subject || it.target || 'Change request'),
        from: App.fmt.valueText(sf.old_value, sf.unit, sf.currency), to: App.fmt.valueText(sf.new_value, sf.unit, sf.currency),
        scope: App.fmt.scopeText(sf.scope), applies: (sf.applies_to || []).join(', ') || 'not stated', unit: sf.unit || ''};
    };

    function requestCard(d) {
      const it = d.intent;
      const link = App.shareLink();
      const msg = el('p', {class: 'muted', testid: 'copy-status', role: 'status', 'aria-live': 'polite', text: F.copyMsg});
      const box = el('input', {type: 'text', readonly: true, value: link, testid: 'request-link', 'aria-label': 'Link to this request (read-only, select to copy)'});
      box.addEventListener('focus', () => box.select());
      const btn = el('button', {type: 'button', testid: 'copy-request-link', disabled: !it && !(d.chat && !d.chat.unavailable), text: 'Copy request link', on: {click: async () => {
        const url = App.shareLink();
        box.value = url;
        if (await App.copyText(url)) F.copyMsg = 'Copied. Open it in the browser profile that is signed in as the next person (for example the administrator).';
        else F.copyMsg = 'Your browser did not allow automatic copying. Select the link in the box and press Ctrl+C (Cmd+C on a Mac) to copy it.';
        msg.textContent = F.copyMsg;
        box.focus(); box.select();
      }}});
      const kids = [];
      if (!it) {
        const hasChat = d.chat && !d.chat.unavailable;
        kids.push(el('p', {class: 'muted', text: hasChat ? 'Your conversation is saved. Copy the link to hand it to a person who can allow the AI to reply.' : 'Nothing is saved yet. After you save the request, a link appears here that you can hand to the next person.'}),
          el('div', {class: 'linkbox'}, btn, hasChat ? box : null), hasChat ? msg : null);
        return el('section', {class: 'card reqcard', testid: 'request-card', data: {saved: 'false'}}, el('h2', {text: 'Your request'}), kids);
      }
      const f = fmtForm(it);
      kids.push(el('p', {class: 'big', testid: 'request-summary', text: f.title + ': ' + f.from + '  \u2192  ' + f.to}),
        el('p', {class: 'muted', text: 'Applies to: ' + f.scope + '. Orders you expect it to cover: ' + f.applies + '.'}),
        el('p', {}, App.pill(it.status === 'PARSED' ? 'DRAFT' : it.status, App.phrase.intent(it.status === 'PARSED' ? 'DRAFT' : it.status), {testid: 'request-status'})),
        App.T(el('div', {}, App.techField('intent_id', it.intent_id), App.techField('digest', it.digest || ''), App.techField('structured_form', it.structured_form || {}))),
        el('div', {class: 'linkbox'}, btn, box), msg,
        (d.chat && d.chat.you_are_creator && d.chat.status === 'CONFIRMED' && d.chat.intent_id === it.intent_id)
          ? el('div', {class: 'row'}, el('button', {type: 'button', class: 'secondary', testid: 'chat-reopen', text: 'Change this request', on: {click: async ev => {
              if (await chatCall(ev.currentTarget, '/' + enc(d.chat.conversation_id) + '/reopen', {})) await refresh(false);
            }}}), el('span', {class: 'muted', text: ' The saved request stays as it is until you confirm the new one. Work already done for it becomes out of date.'})) : null,
        el('p', {class: 'muted', text: 'The link gives no extra rights: whoever opens it can only do what their own account is allowed to do. It contains no password.'}));
      return el('section', {class: 'card reqcard', testid: 'request-card', data: {saved: 'true'}}, el('h2', {text: 'Your request'}), kids);
    }

    function supersededCard(d) {
      if (!S.drafts['superseded']) return null;
      let v; try { v = JSON.parse(S.drafts['superseded']); } catch (_) { return null; }
      const ul = el('ul', {});
      for (const st of v.steps) ul.append(el('li', {}, st.name + ' - ', App.pill('STALE', st.state, {testid: 'superseded-state'})));
      return el('section', {class: 'card', testid: 'superseded-card'}, el('h3', {text: 'You changed the request: earlier work is out of date'}),
        el('p', {class: 'muted', text: 'These belong to the previous version of the request. They are NOT reused for the new one: an administrator has to confirm the new request, then the steps are done again from step 2.'}),
        ul, v.applied ? el('p', {class: 'blocker', text: 'A change was already applied for the previous request. The new request does not undo it; it starts from the current sources.'}) : null);
    }

    const iAsked = job => !!(job && job.requested_by && job.requested_by === S.account);
    /* Who has to do what next, computed from the real state; "you" only when this account may really do it. */
    function nextAction(d) {
      const it = d.intent, A = (who, text, kind, extra) => ({who, text, kind: kind || 'act', extra});
      if (it && d.chat && !d.chat.unavailable && d.chat.status === 'OPEN' && d.chat.intent_id === it.intent_id)
        return A('You', 'You are changing the request. Edit the details or answer in the chat, then confirm the new understanding. Until then the saved request below stays as it is.');
      if (!it && d.chat && !d.chat.unavailable) {
        const C = d.chat;
        if (C.status === 'CANCELLED') return A('You', 'This conversation was cancelled. Start a new request.', 'stop');
        if (C.awaiting_ai) return A('The AI', 'is replying. This page updates by itself.', 'wait');
        if (C.awaiting_allowance) return C.can_allow ? A('You', 'Allow the AI to reply ("Allow the AI to reply"). It uses the paid model; nothing is sent until you do.')
          : A('A different person with permission (for example an administrator)', 'must allow the AI to reply. Copy the request link above and send it.', 'wait');
        if (!C.you_are_creator) return A('The requester', 'must answer the AI or confirm the understanding.', 'wait');
        if (C.understanding && C.understanding.status === 'UNSUPPORTED') return A('You', 'This is not something Ripple can set up automatically (see the note in the chat). Nothing was started. You can reword it as a number change or stop here.', 'stop');
        if (C.understanding && C.understanding.status === 'READY') return A('You', 'Check the understanding in the chat and confirm it ("Yes, this is what I mean").');
        return A('You', 'Answer the AI questions in the chat, or edit the details yourself.');
      }
      if (!it) return A('You', 'Fill in the request below (or start from a rule in the Explorer) and save it.');
      if (it.status === 'NEEDS_CLARIFICATION') return A('You', 'The request could not be saved yet: ' + (it.questions || []).join(' ') + ' Correct it and save again.', 'stop');
      if (it.status === 'PARSED') return can('knowledge.confirm')
        ? A('You (administrator rights)', 'Confirm the request. Confirming records your decision; it changes no document.')
        : A('An administrator', 'must confirm the request. Copy the request link above and open it in the browser profile signed in as the administrator.', 'wait');
      if (it.status !== 'CONFIRMED') return A('You', 'This request is ' + App.phrase.intent(it.status).toLowerCase() + '. Save a new one.', 'stop');
      const dr = d.draft, G = d.gaps;
      if (!dr) return can('source.import') ? A('You', 'Ask for the AI draft ("Generate GenAI draft"). A second person must then allow the AI run.') : A('A requester', 'must ask for the AI draft.', 'wait');
      const ph = phase(dr);
      if (ph === 'PENDING_APPROVAL') return iAsked(dr)
        ? A('A second person (administrator)', 'must allow this AI run. You asked for it, so you cannot allow it yourself: copy the link above and send it.', 'wait')
        : (can('source.import') ? A('You', 'Allow the AI run ("Approve this model call") so the draft can be produced.') : A('A person with permission', 'must allow the AI run.', 'wait'));
      if (jobState(dr) === 'FAILED') return A('You', 'The draft failed (' + (dr.execution_reason || 'no reason recorded') + '). Ask for a new draft.', 'stop');
      if (!done(dr)) return A('The system', 'is producing the draft. This page updates by itself.', 'wait');
      if (G && G.stale) return A('You', 'A decision was recorded after this draft, so it is out of date. Ask for a new draft: it will use the decision.', 'stop');
      if (!d.impact) return A('You', 'Run the impact analysis ("Compute relation coverage" first, then "Run impact analysis").');
      if (!done(d.impact)) return A('The system', 'is running the impact analysis.', 'wait');
      if (!d.simulation) return A('You', 'Run the simulation to see what would change before anything is proposed.');
      if (!done(d.simulation)) return A('The system', 'is running the simulation.', 'wait');
      if (!d.proposal) return A('You', 'Prepare the proposal from the draft: it shows the exact before/after of the file. Nothing is applied yet.');
      if (!done(d.proposal)) return A('The system', 'is preparing the proposal.', 'wait');
      if (!(d.contracts || []).length) return can('verification.contract.set') && !iAsked(d.proposal)
        ? A('You (approver)', 'State the check after the change: the value that must be in the file once it is applied. Type it from the confirmed request above, not from the proposal.')
        : A('An approver (not the requester)', 'must state the check after the change. Copy the link above and send it.', 'wait');
      if (!d.review) return can('source.import') ? A('You', 'Run the review.') : A('A requester', 'must run the review.', 'wait');
      if (!done(d.review)) return A('The system', 'is running the review.', 'wait');
      if (!d.approval) return can('source.import') ? A('You', 'Prepare the approval bundle: it is what the approver will read and decide on.') : A('A requester', 'must prepare the approval bundle.', 'wait');
      if (!done(d.approval)) return A('The system', 'is preparing the approval bundle.', 'wait');
      const dec = (d.decisions && d.decisions.decisions) || [], last = dec.length ? dec[dec.length - 1] : null;
      if (!last) return can('change.approve') && !iAsked(d.proposal)
        ? A('You (approver)', 'Read the before/after, the limits and what stays unresolved, then approve or reject with a reason. Approving applies nothing by itself.')
        : A('An approver (not the requester)', 'must approve or reject the change. Copy the link above and send it.', 'wait');
      if (last.action !== 'approve') return A('You', 'The change was ' + (last.action === 'reject' ? 'rejected' : 'sent back for changes') + ' by ' + last.actor_id + (last.reason ? ' ("' + last.reason + '")' : '') + '. Prepare a new proposal if you still want it.', 'stop');
      if (!F.ids.apply) return can('source.import') ? A('You', 'Apply the approved change. This is the first step that changes a document.') : A('A requester', 'must apply the approved change.', 'wait');
      if (d.apply && jobState(d.apply) === 'FAILED') return A('You', 'Applying failed (' + (d.apply.execution_reason || 'no reason recorded') + '). Nothing was marked as updated.', 'stop');
      if (!d.apply || !done(d.apply) || (d.loop && d.loop.status !== 'COMPLETE' && !(d.loop.stages && d.loop.stages.reanalysis && d.loop.stages.reanalysis.completed === d.loop.stages.reanalysis.total && d.loop.stages.verification === 'PASSED')))
        return A('The system', 'is applying the change and re-checking the documents. This page updates by itself.', 'wait');
      return A('You', 'Read "Where this change stands": what was updated and what still needs a person. You can then ask a question on the new revision.', 'act');
    }

    function nextCard(d) {
      const n = nextAction(d);
      const exhausted = n.kind === 'wait' && F.waitPolls >= WAIT_POLL_MAX;
      return el('div', {class: 'nextact ' + (n.kind === 'wait' ? 'wait' : (n.kind === 'stop' ? 'stop' : '')), testid: 'next-action', data: {who: n.who, kind: n.kind}},
        el('strong', {text: (n.kind === 'wait' ? 'Waiting for: ' : 'Next: ') + n.who}), el('span', {text: n.text}),
        n.kind === 'wait' ? el('p', {class: 'muted', testid: 'wait-note', text: exhausted ? 'Automatic checking has stopped. Press "Check now" to look again.' : 'This page checks by itself every few seconds; you do not need to create the request again.'}) : null,
        n.kind === 'wait' ? el('button', {type: 'button', class: 'secondary', testid: 'check-now', text: 'Check now', on: {click: () => { F.waitPolls = 0; refresh(false); }}}) : null);
    }

    const STEPS = [['request', 'Request', 'flow-intent'], ['confirm', 'Confirm', 'flow-intent'], ['analyse', 'Analyse', 'flow-draft'], ['simulate', 'Simulate', 'flow-analysis'],
      ['approve', 'Check and approve', 'flow-review'], ['apply', 'Apply', 'flow-apply'], ['verify', 'Verify and update', 'flow-loop']];
    function stepper(d) {
      const it = d.intent, dec = (d.decisions && d.decisions.decisions) || [], last = dec.length ? dec[dec.length - 1] : null;
      const doneBy = {request: !!it, confirm: !!it && it.status === 'CONFIRMED', analyse: done(d.draft) && done(d.impact) && !(d.gaps && d.gaps.stale),
        simulate: done(d.simulation), approve: !!last && last.action === 'approve', apply: !!d.apply && done(d.apply),
        verify: !!d.loop && d.loop.status !== undefined && !!d.loop.stages && d.loop.stages.reanalysis.completed === d.loop.stages.reanalysis.total && d.loop.stages.verification === 'PASSED'};
      const blockedBy = {confirm: it && it.status === 'NEEDS_CLARIFICATION', analyse: (d.draft && jobState(d.draft) === 'FAILED') || (d.gaps && d.gaps.stale),
        approve: !!last && last.action !== 'approve', apply: d.apply && jobState(d.apply) === 'FAILED'};
      let currentSet = false;
      const items = STEPS.map(([key, label, target], i) => {
        let st = doneBy[key] ? 'done' : 'future';
        if (!doneBy[key] && !currentSet) { st = blockedBy[key] ? 'blocked' : 'current'; currentSet = true; }
        const mark = st === 'done' ? '\u2713' : (st === 'current' ? '\u25B6' : (st === 'blocked' ? '!' : '\u2022'));
        const word = {done: 'done', current: 'now', blocked: 'blocked', future: 'later'}[st];
        const a = el('a', {href: '#' + target, 'aria-label': label + ': ' + word, text: ''}, el('span', {class: 'mark', text: mark}), label, el('span', {class: 'muted', text: ' (' + word + ')'}));
        a.addEventListener('click', ev => { ev.preventDefault(); const t = document.querySelector('[data-testid=' + target + ']'); if (t) { t.scrollIntoView({block: 'start'}); } });
        return el('li', {data: {'step-state': st, step: key}, 'aria-current': st === 'current' ? 'step' : null}, a);
      });
      return el('ol', {class: 'stepper', testid: 'stepper', 'aria-label': 'Progress of this change'}, items);
    }

    function draw() {
      const d = F.data; if (!d) return;
      const active = document.activeElement;
      const focusId = active && active.id && container.contains(active) ? active.id : null;
      const pos = focusId && active.selectionStart !== undefined ? [active.selectionStart, active.selectionEnd] : null;
      const nodes = [el('h2', {text: 'Change flow'}),
        F.failed ? App.refreshFailedEl(() => refresh(false)) : null,
        requestCard(d), supersededCard(d), stepper(d), nextCard(d),
        d.intent ? null : whatIfCard(d), intentCard(d), draftCard(d), previewCard(d), analysisCard(d), proposalCard(d), reviewCard(d), approvalCard(d), applyCard(d), loopCard(d), queryCard(d), d.intent ? whatIfCard(d) : null];
      if (d.change) nodes.push(changeSection(ctx, d.change, hooks));
      else if (F.ids.apply) nodes.push(unreadChange(F.ids.apply));
      replace(container, nodes);
      if (focusId) {
        const again = document.getElementById(focusId);
        if (again) { again.focus(); if (pos && again.setSelectionRange) try { again.setSelectionRange(pos[0], pos[1]); } catch (_) { /* not a text field */ } }
      }
    }

    replace(container, App.loadingEl('change flow'));
    await refresh(true);
  }

  // ------------------------------------------------------------------ history view
  async function viewHistory(ctx) {
    const {container} = ctx;
    const H = {ids: {restore: ctx.route.params.restore || '', approval: ctx.route.params.approval || '', apply: ctx.route.params.apply || ''},
      data: null, preview: null, from: '', to: '', diff: null, failed: false, approved: null};
    const hooks = {reload: () => refresh(false), redraw: async () => draw()};
    M.refusal = null;

    async function fetchAll() {
      const head = await App.loadHead();
      const ws = ctx.ws;
      const hist = await api('GET', '/v2/history/' + enc(ws));
      let approved = null, change = null, preview = null;
      const activity = await api('GET', '/v2/audit/' + enc(ws)).then(a => a.events || []).catch(() => []);
      if (H.ids.approval) {
        const audit = {events: activity};
        const ev = (audit.events || []).filter(e => e.event_type === 'RESTORE_APPROVED' && e.payload && e.payload.restore_approval_id === H.ids.approval).pop();
        if (ev) approved = {id: H.ids.approval, digest: ev.payload.approval_digest, target: ev.payload.target_revision_id, actor: ev.actor_id};
      }
      if (H.ids.apply) change = await fetchChange(ctx, H.ids.apply);
      return {head, hist, approved, change, preview, activity};
    }
    async function refresh(first) {
      let d;
      try { d = await fetchAll(); } catch (e) {
        if (ctx.gen !== S.gen) return;
        if (!(e instanceof App.ApiError)) throw e;
        if (e.status === 401) { App.signedOut(undefined, true); return; }
        if (first || !H.data) { App.readFailed(container, e, () => App.reloadView()); return; }
        H.failed = true; draw(); return;
      }
      if (ctx.gen !== S.gen) return;
      H.data = d; H.failed = false; H.approved = d.approved;
      if (H.ids.restore && !H.preview && !H.approved && can('source.restore') && !H.previewAsked) { H.previewAsked = true; await preview(H.ids.restore, true); }
      draw();
      if (d.change && changeLive(d.change)) App.schedule(() => refresh(false), 2000);
    }
    async function preview(target, silent) {
      const r = await act(() => api('POST', '/v2/restore/preview', {workspace_id: ctx.ws, target_revision_id: target}));
      if (r === FAIL || ctx.gen !== S.gen) return;
      H.preview = r; H.ids.restore = target; H.ids.approval = ''; H.approved = null;
      App.setParams({restore: target, approval: ''});
      if (!silent) draw();
    }
    async function loadDiff() {
      if (!H.from || !H.to || H.from === H.to) { H.diff = null; draw(); return; }
      const r = await act(() => api('GET', '/v2/history/' + enc(ctx.ws) + '/diff?from_revision_id=' + enc(H.from) + '&to_revision_id=' + enc(H.to)));
      if (r === FAIL || ctx.gen !== S.gen) return;
      H.diff = r; draw();
    }

    function revisionList(d) {
      const rows = (d.hist.revisions || []).slice().reverse().map(rev => el('div', {testid: 'revision-row', class: 'card', data: {
        'revision-id': rev.revision_id, trigger: rev.trigger || 'None', current: String(!!rev.is_current), 'restored-from': rev.restored_from || ''}},
        el('strong', {text: 'Version ' + rev.sequence + ' '}), App.T(el('code', {text: rev.revision_id})), ' ', App.B(el('span', {text: ({import: 'created by an import', apply: 'created by an approved change', restore: 'created by a restore', refresh: 'created by a refresh'})[rev.trigger] || 'created'})), App.T(pill(rev.trigger || 'unknown', 'trigger')),
        rev.is_current ? el('span', {class: 'pill', text: 'current'}) : null,
        rev.restored_from ? el('span', {class: 'muted', text: ' restored from ' + rev.restored_from}) : null,
        el('span', {class: 'muted', text: ' - ' + rev.created_at}),
        can('source.restore') && !rev.is_current ? el('button', {type: 'button', class: 'secondary', testid: 'action-restore-preview', text: 'Preview restore',
          on: {click: () => preview(rev.revision_id, false)}}) : null));
      return el('section', {}, el('h2', {text: 'Source revisions'}), rows.length ? rows : App.emptyEl('This workspace has no source revisions yet.'));
    }
    function diffCard(d) {
      const revs = d.hist.revisions || [];
      if (revs.length < 2) return el('section', {class: 'card', testid: 'history-no-compare'}, el('h2', {text: 'Compare versions'}), el('p', {class: 'muted', text: 'There is only one version of the files so far, so there is nothing to compare yet. After a change is applied, the before and after appear here.'}));
      const opts = sel => [el('option', {value: '', text: 'Select a revision'})].concat(revs.map(r => el('option', {value: r.revision_id, text: r.revision_id})));
      const from = el('select', {id: 'hist-from', testid: 'history-diff-from', on: {change: ev => { H.from = ev.target.value; loadDiff(); }}}, opts());
      const to = el('select', {id: 'hist-to', testid: 'history-diff-to', on: {change: ev => { H.to = ev.target.value; loadDiff(); }}}, opts());
      from.value = H.from; to.value = H.to;
      const out = el('div', {testid: 'history-diff'});
      if (H.diff) {
        for (const kind of ['added', 'removed', 'changed']) for (const it of H.diff[kind] || []) {
          out.append(el('div', {testid: 'diff-' + kind, data: {'artifact-id': it.artifact_id}}, kind + ': ', el('code', {text: it.artifact_id})));
        }
        out.append(el('p', {class: 'muted', text: 'Unchanged artifacts: ' + H.diff.unchanged_count + (H.diff.truncated ? ' (list truncated)' : '')}));
      } else out.append(el('p', {class: 'muted', text: 'Choose two different revisions to see what changed.'}));
      return el('section', {class: 'card'}, el('h2', {text: 'Compare revisions'}),
        el('div', {class: 'grid2'}, el('div', {}, el('label', {for: 'hist-from', text: 'Compare from'}), from), el('div', {}, el('label', {for: 'hist-to', text: 'Compare to'}), to)), out);
    }
    const ACT = {SOURCE_IMPORT_PREPARED: 'prepared an import of files', SOURCE_PUBLISHED: 'made a new version of the files current', INTENT_PARSED: 'saved a change request',
      INTENT_CONFIRMED: 'confirmed a change request', DECISION_RECORDED: 'recorded a decision (no file was changed)', SANDBOX_LEASE_ACQUIRED: 'took the writing session',
      SANDBOX_LEASE_RELEASED: 'released the writing session', SANDBOX_LEASE_RENEWED: 'extended the writing session', RESTORE_APPROVED: 'approved a restore to an earlier version',
      HITL_ANSWER_RECORDED: 'wrote a comment or answer (not a decision)', 'job.approved': 'allowed a job to run'};
    function activityCard(d) {
      const ev = (d.activity || []).filter(e => e.event_type !== 'JOB_QUEUED' && e.event_type !== 'membership.grant').slice(-40).reverse();
      return el('section', {class: 'card', testid: 'history-activity'}, el('h2', {text: 'What happened, and who did it'}),
        el('p', {class: 'muted', text: 'Most recent first. A decision is recorded here but changes no file; only an applied change makes a new version of the files.'}),
        ev.length ? el('ul', {}, ev.map(e => el('li', {data: {event: e.event_type}},
          el('strong', {text: e.actor_id || 'the system'}), ' ', App.both(ACT[e.event_type] || App.fmt.humanize(e.event_type).toLowerCase(), e.event_type), ' ',
          el('span', {class: 'muted', text: e.created_at || ''}),
          App.T(el('div', {class: 'muted', text: JSON.stringify(e.payload || {}).slice(0, 300)}))))) : el('p', {class: 'muted', text: 'Nothing has happened yet.'}));
    }
    function restoreCard(d) {
      const p = H.preview, approved = d.approved;
      if (!p && !approved) return null;
      const kids = [el('h2', {text: 'Restore'})];
      if (p && !approved) {
        kids.push(el('p', {}, 'Current revision: ', el('code', {testid: 'restore-current', text: p.current.source_revision_id})),
          el('p', {}, 'Target revision: ', el('code', {testid: 'restore-target', text: p.target.source_revision_id})),
          el('p', {}, 'Preview digest: ', el('code', {testid: 'restore-digest', text: p.preview_digest || 'none (nothing to restore)'})));
        const delta = el('div', {testid: 'restore-delta'});
        for (const kind of ['added', 'removed', 'changed']) for (const it of (p.delta && p.delta[kind]) || []) {
          delta.append(el('div', {testid: 'delta-item', data: {kind, 'artifact-id': it.artifact_id}}, kind + ' ', el('code', {text: it.artifact_id})));
        }
        kids.push(el('h3', {text: 'What restoring changes'}), delta,
          el('p', {class: 'muted', text: 'Unchanged artifacts: ' + ((p.delta && p.delta.unchanged_count) || 0)}));
        for (const id of p.affected_decision_ids || []) kids.push(el('div', {testid: 'affected-decision-item', text: 'Decision kept in the ledger: ' + id}));
        for (const id of p.stale_knowledge_revision_ids || []) kids.push(el('div', {testid: 'stale-km-item', text: 'Knowledge revision that will be stale: ' + id}));
        kids.push(el('p', {class: 'blocker', testid: 'restore-loss-warning', text: p.loss_warning || 'Restoring can discard work; review the changes above.'}));
        kids.push(el('p', {}, 'Status: ', el('span', {class: 'pill', testid: 'restore-status', data: {state: 'PREVIEWED'}, text: 'PREVIEWED'})));
        if (can('source.restore') && !p.noop) {
          const reason = App.bindDraft(el('textarea', {id: 'fld-restore-reason', rows: '2', testid: 'field-restore-reason'}), 'restore-reason');
          const digest = p.preview_digest, target = p.target.source_revision_id;
          kids.push(el('label', {for: 'fld-restore-reason', text: 'Reason for the restore'}), reason,
            el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-restore-approve', text: 'Approve restore', on: {click: async ev => {
              const text = reason.value.trim();
              if (!text) { App.say('A reason is required to approve a restore.'); return; }
              const r = await act(() => api('POST', '/v2/restore/approve', {workspace_id: ctx.ws, target_revision_id: target,
                expected_preview_digest: digest, reason: text, idempotency_key: uuid()}), {button: ev.currentTarget});
              if (r === FAIL || ctx.gen !== S.gen) return;
              S.drafts['restore-reason'] = '';
              H.ids.approval = r.restore_approval_id; H.ids.restore = target;
              App.setParams({restore: target, approval: r.restore_approval_id});
              App.say('Restore approved. A different restore operator applies it from this link.');
              await refresh(false);
            }}})));
        }
      } else if (approved) {
        kids.push(el('p', {}, 'Target revision: ', el('code', {testid: 'restore-target', text: approved.target})),
          el('p', {class: 'muted', text: 'Approval ' + approved.id + ' by ' + approved.actor + ' (digest ' + approved.digest + ').'}),
          el('p', {}, 'Status: ', el('span', {class: 'pill', testid: 'restore-status', data: {state: 'APPROVED'}, text: 'APPROVED'})));
        if (can('source.restore') && !H.ids.apply) {
          kids.push(el('div', {class: 'row'}, el('button', {type: 'button', testid: 'action-restore-apply', text: 'Apply restore', on: {click: async ev => {
            const id = 'ui-restore-' + uuid().slice(0, 16);
            const r = await act(() => api('POST', '/v2/jobs', {workspace_id: ctx.ws, kind: 'apply', job_id: id,
              payload: {restore_approval_id: approved.id, expected_approval_digest: approved.digest}}), {button: ev.currentTarget, stale: true});
            if (r === FAIL || ctx.gen !== S.gen) return;
            H.ids.apply = id; App.setParams({apply: id});
            await refresh(false);
          }}})));
        }
      }
      return el('section', {class: 'card', testid: 'flow-restore'}, kids);
    }

    function draw() {
      const d = H.data; if (!d) return;
      const nodes = [el('h2', {text: 'History'}), H.failed ? App.refreshFailedEl(() => refresh(false)) : null,
        activityCard(d), revisionList(d), diffCard(d), restoreCard(d)];
      if (d.change) nodes.push(changeSection(ctx, d.change, hooks));
      const active = document.activeElement;
      const focusId = active && active.id && container.contains(active) ? active.id : null;
      replace(container, nodes);
      if (focusId) { const again = document.getElementById(focusId); if (again) again.focus(); }
    }

    replace(container, App.loadingEl('history'));
    await refresh(true);
  }

  App.register('changes', viewChanges, {reset});
  App.register('history', viewHistory);
})();
