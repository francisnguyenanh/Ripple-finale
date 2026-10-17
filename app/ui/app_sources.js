'use strict';
/* Sources view: import (manifest -> acknowledgement -> commit), revision coverage, artifacts,
 * evidence, export, manual refresh. Every number and state is read from the server. */
(function () {
  const {el, replace, api, act, can, enc, uuid, str, FAIL, S} = Object.assign({}, App, {S: App.S});
  const M = {prep: null, ack: false, refresh: null, refreshAck: false, ev: null, polls: 0};
  function reset() { M.prep = null; M.ack = false; M.refresh = null; M.refreshAck = false; M.ev = null; M.polls = 0; }

  async function fileEntries(files) {
    const out = [];
    for (const file of files) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      out.push({path: file.webkitRelativePath || file.name, content_b64: btoa(binary)});
    }
    return out;
  }
  const pill = (text, state) => el('span', {class: 'pill', data: {state: state || text}, text});

  // ------------------------------------------------------------------ import panel
  function importPanel(ctx) {
    const manifest = el('div', {id: 'import-manifest'});
    const fileInput = el('input', {id: 'import-files', type: 'file', multiple: true});
    const prepare = el('button', {type: 'button', text: 'Prepare import', on: {click: async ev => {
      const files = Array.from(fileInput.files || []);
      if (!files.length) { App.say('Choose one or more files first.'); return; }
      const r = await act(async () => api('POST', '/v2/sources/imports', {
        workspace_id: ctx.ws, idempotency_key: uuid(), files: await fileEntries(files)}), {button: ev.currentTarget});
      if (r === FAIL || ctx.gen !== S.gen) return;
      M.prep = r; M.ack = false;
      renderManifest(manifest, ctx);
      App.say('Manifest ready: review it, then commit the import.');
    }}});
    renderManifest(manifest, ctx);
    return el('section', {class: 'card', 'aria-labelledby': 'imp-h'},
      el('h2', {id: 'imp-h', text: 'Import source files'}),
      el('p', {class: 'muted', text: 'Files are checked first; nothing becomes current until you commit the manifest below.'}),
      el('label', {for: 'import-files', text: 'Upload source files'}), fileInput,
      el('div', {class: 'row'}, prepare), manifest);
  }

  function renderManifest(box, ctx) {
    const prep = M.prep;
    if (!prep) { replace(box); return; }
    const skips = prep.acknowledgement_required || [];
    const commit = el('button', {type: 'button', text: 'Commit import', disabled: skips.length > 0 && !M.ack, on: {click: async ev => {
      const r = await act(() => api('POST', '/v2/sources/imports/' + enc(prep.import_id) + '/commit', {
        workspace_id: ctx.ws, expected_version: prep.base_version,
        acknowledged_skips: skips.length && M.ack ? skips : []}), {button: ev.currentTarget});
      if (r === FAIL || ctx.gen !== S.gen) return;
      M.prep = null; M.ack = false;
      App.say('Import committed.');
      await App.reloadView();
    }}});
    const kids = [el('h3', {text: 'Import manifest'})];
    for (const it of prep.items || []) {
      kids.push(el('div', {testid: 'import-item', data: {'logical-path': it.logical_path, disposition: it.disposition}},
        el('code', {text: it.logical_path}), ' ', pill(it.disposition), ' ',
        el('span', {class: 'muted', text: it.byte_size + ' bytes'}),
        it.reason ? el('span', {text: ' - ' + it.reason}) : null));
    }
    if (skips.length) {
      kids.push(el('p', {class: 'muted', text: 'Skipped files will not be part of the revision.'}),
        el('label', {},
          el('input', {type: 'checkbox', testid: 'import-ack', checked: M.ack, on: {change: ev => { M.ack = ev.target.checked; commit.disabled = !M.ack; }}}),
          'I acknowledge the skipped files'));
    }
    kids.push(el('div', {class: 'row'}, commit));
    replace(box, kids);
  }

  // ------------------------------------------------------------------ revision, coverage, artifacts
  function coverageBusy(cov) {
    return !!cov && ['pending', 'running', 'queued', 'not_started'].includes(cov.status);
  }
  function renderCoverage(cov) {
    if (!cov) {
      return el('div', {}, el('p', {testid: 'coverage-analysis', data: {value: 'none'},
        text: 'Analysis coverage: not available for this revision (no import record).'}));
    }
    const skipped = cov.skipped || [];
    const kids = [
      el('p', {testid: 'coverage-admission', data: {value: String(cov.admission_complete)},
        text: 'Admission: ' + (cov.admission_complete ? 'complete (nothing skipped)' : 'incomplete (' + skipped.length + ' file(s) skipped)')}),
      el('p', {testid: 'coverage-analysis', data: {value: cov.status},
        text: 'Analysis (parse): ' + cov.status + (cov.complete ? ' - complete' : ' - incomplete')}),
      el('p', {testid: 'coverage-index', data: {value: cov.index_status},
        text: 'Index: ' + cov.index_status + (cov.index_reason ? ' (' + cov.index_reason + ')' : '')}),
      el('p', {testid: 'coverage-semantic', data: {value: cov.semantic_stage}, text: 'Semantic stage: ' + cov.semantic_stage})];
    if (skipped.length) kids.push(el('p', {class: 'muted', text: 'Skipped: ' + skipped.map(s => s.logical_path + ' (' + s.reason + ')').join(', ')}));
    if (!cov.complete || !cov.admission_complete) {
      kids.push(el('div', {testid: 'state-partial', text: 'Coverage is partial: not every file was admitted and analysed. Do not read absent results as "nothing found".'}));
    }
    return el('div', {}, kids);
  }

  async function downloadArtifact(ctx, rev, art) {
    const r = await act(() => api('GET', '/v2/sources/revisions/' + enc(rev) + '/artifacts/' + enc(art.artifact_id) + '/export?workspace_id=' + enc(ctx.ws)));
    if (r === FAIL) return;
    const bytes = Uint8Array.from(atob(r.content_b64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], {type: r.media_type || 'application/octet-stream'}));
    const a = el('a', {href: url, download: (art.logical_path || art.artifact_id).split('/').pop()});
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  App.downloadArtifact = downloadArtifact;
  function exportLink(ctx, rev, art, testid) {
    const run = () => downloadArtifact(ctx, rev, art);
    return el('a', {role: 'link', tabindex: '0', text: 'Export', testid,
      on: {click: run, keydown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); run(); } }}});
  }
  App.exportLink = exportLink;

  async function analyze(ctx, art, btn) {
    const jobId = 'ui-extract-' + uuid().slice(0, 16);
    const r = await act(() => api('POST', '/v2/jobs', {workspace_id: ctx.ws, kind: 'extraction', job_id: jobId,
      payload: {artifact_id: art.artifact_id, source_role: art.source_role || 'unknown'}}), {button: btn});
    if (r === FAIL) return;
    App.go('analysis', r.job_id || jobId);
  }

  async function showEvidence(ctx, rev, art) {
    M.ev = {artifact: art, rev, observations: null, selected: null, error: null};
    renderEvidence(ctx);
    const r = await act(() => api('GET', '/v2/sources/revisions/' + enc(rev) + '/artifacts/' + enc(art.artifact_id) + '/observations?workspace_id=' + enc(ctx.ws)));
    if (ctx.gen !== S.gen || !M.ev || M.ev.artifact !== art) return;
    if (r === FAIL) { M.ev = null; renderEvidence(ctx); return; }
    M.ev.observations = r.observations || [];
    renderEvidence(ctx);
  }
  async function pickObservation(ctx, obs) {
    const ev = M.ev; if (!ev) return;
    const r = await act(() => api('GET', '/v2/sources/revisions/' + enc(ev.rev) + '/evidence/' + enc(obs.observation_id) + '?workspace_id=' + enc(ctx.ws)));
    if (ctx.gen !== S.gen || M.ev !== ev) return;
    if (r === FAIL) return;
    ev.selected = r;
    renderEvidence(ctx);
  }
  function renderEvidence(ctx) {
    const box = document.getElementById('evidence-box');
    if (!box) return;
    const ev = M.ev;
    if (!ev) { replace(box); return; }
    const kids = [el('h2', {text: 'Evidence: ' + (ev.artifact.logical_path || ev.artifact.artifact_id)})];
    if (ev.observations === null) kids.push(App.loadingEl('observations'));
    else if (!ev.observations.length) kids.push(el('p', {class: 'muted', text: 'This artifact has no observations.'}));
    else {
      const list = el('div', {class: 'row'});
      for (const obs of ev.observations) {
        list.append(el('button', {type: 'button', class: 'secondary', testid: 'observation-item',
          text: obs.kind + ' @ ' + str(obs.locator), on: {click: () => pickObservation(ctx, obs)}}));
      }
      kids.push(list);
    }
    if (ev.selected) {
      const s = ev.selected;
      const loc = s.locator && typeof s.locator === 'object'
        ? Object.entries(s.locator).map(([k, v]) => k + ': ' + (typeof v === 'object' ? str(v) : String(v))).join('; ') : str(s.locator);
      kids.push(el('section', {class: 'card', testid: 'evidence-panel', data: {provenance: 'observed'}},
        el('p', {}, 'File: ', el('code', {testid: 'evidence-file', text: ev.artifact.logical_path || ev.artifact.artifact_id})),
        el('p', {}, 'Locator: ', el('code', {testid: 'evidence-locator', text: loc})),
        el('p', {}, 'Revision: ', el('code', {testid: 'evidence-revision', text: s.source_revision_id})),
        el('p', {}, 'Value: ', el('code', {testid: 'evidence-value', text: str(s.raw_value)})),
        el('p', {class: 'muted', text: 'Provenance: observed (read by the parser from the stored file, not inferred).'})));
    }
    replace(box, kids);
  }

  function artifactRows(ctx, rev, view) {
    const box = el('div', {});
    for (const art of view.artifacts || []) {
      const row = el('article', {class: 'card', testid: 'artifact-row', data: {'logical-path': art.logical_path, 'artifact-id': art.artifact_id}},
        el('h3', {}, App.both(App.fmt.basename(art.logical_path), art.logical_path)),
        el('p', {class: 'muted'}, App.B(el('span', {text: (art.byte_size >= 1024 ? Math.round(art.byte_size / 102.4) / 10 + ' KB' : art.byte_size + ' bytes') + ' - ' + (({'application/yaml': 'YAML settings file', 'text/markdown': 'Markdown document', 'text/x-python': 'Python code', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel workbook'})[art.media_type] || art.media_type) + ' - in the folder ' + (art.logical_path.split('/').slice(0, -1).join('/') || '(top)')})),
          App.T(el('span', {text: art.byte_size + ' bytes - ' + art.media_type + ' - role: ' + art.source_role}))),
        el('div', {class: 'row'},
          can('source.import') ? el('button', {type: 'button', text: 'Analyze', on: {click: ev => analyze(ctx, art, ev.currentTarget)}}) : null,
          el('button', {type: 'button', class: 'secondary', text: 'Evidence', on: {click: () => showEvidence(ctx, rev, art)}}),
          exportLink(ctx, rev, art)));
      box.append(row);
    }
    return box;
  }

  async function loadRevision(ctx, box, head, first) {
    if (!head.source_revision_id) { replace(box, App.emptyEl('No source has been imported into this workspace yet.')); return; }
    if (first) replace(box, App.loadingEl('source revision'));
    let view;
    try {
      view = await api('GET', '/v2/sources/revisions/' + enc(head.source_revision_id) + '?workspace_id=' + enc(ctx.ws));
    } catch (e) {
      if (ctx.gen !== S.gen) return;
      if (!(e instanceof App.ApiError)) throw e;
      App.readFailed(box, e, () => App.reloadView()); return;
    }
    if (ctx.gen !== S.gen) return;
    replace(box,
      el('section', {class: 'card'},
        el('h2', {text: 'Files in this workspace'}),
        el('p', {class: 'muted', testid: 'sources-note', text: 'Reading a file (parsing) is not the same as the AI extracting rules from it: open the Explorer to see which files have rules and which are only read. "Role: unknown" in the technical view only means nobody has classified what kind of source the file is; the file is imported.'}),
        el('p', {}, App.T(el('span', {}, 'Revision: ', el('code', {testid: 'revision-id', text: view.revision_id}),
          view.trigger ? el('span', {class: 'muted', text: ' (' + view.trigger + ')'}) : null))),
        renderCoverage(view.coverage)),
      artifactRows(ctx, view.revision_id, view));
    renderEvidence(ctx);
    if (coverageBusy(view.coverage) && M.polls < 60) {
      M.polls += 1;
      App.schedule(async () => {
        let h; try { h = await App.loadHead(); } catch (_) { return; }
        if (ctx.gen === S.gen) await loadRevision(ctx, box, h, false);
      }, 2000);
    }
  }

  // ------------------------------------------------------------------ manual refresh
  function refreshPanel(ctx) {
    const box = el('div', {id: 'refresh-box'});
    const files = el('input', {id: 'refresh-files', type: 'file', multiple: true, testid: 'field-refresh-files'});
    const prepare = el('button', {type: 'button', text: 'Prepare refresh', testid: 'action-refresh-prepare', on: {click: async ev => {
      const list = Array.from(files.files || []);
      if (!list.length) { App.say('Choose the whole changed source first.'); return; }
      const r = await act(async () => api('POST', '/v2/sources/refresh/prepare', {
        workspace_id: ctx.ws, idempotency_key: uuid(), files: await fileEntries(list)}), {button: ev.currentTarget});
      if (r === FAIL || ctx.gen !== S.gen) return;
      M.refresh = {view: r, status: null, committed: false}; M.refreshAck = false;
      renderRefresh(box, ctx);
    }}});
    renderRefresh(box, ctx);
    return el('section', {class: 'card'},
      el('h2', {text: 'Manual source refresh'}),
      el('p', {class: 'muted', text: 'Upload the whole changed source, review the diff, then commit it. A person starts every refresh; Ripple does not watch a repository, and a refresh never approves anything.'}),
      el('label', {for: 'refresh-files', text: 'Changed source files'}), files,
      el('div', {class: 'row'}, prepare), box);
  }

  function renderRefresh(box, ctx) {
    const st = M.refresh;
    if (!st) { replace(box); return; }
    const v = st.view;
    const d = v.diff || {};
    const deleted = (d.deleted || []).map(x => x.logical_path);
    const kids = [el('h3', {text: 'Refresh diff'})];
    const row = (kind, item, text) => el('div', {testid: 'refresh-diff', data: {kind, 'artifact-id': item.artifact_id || ''}},
      el('span', {class: 'pill', data: {kind}, text: kind}), ' ', el('code', {text}));
    for (const x of d.added || []) kids.push(row('added', x, x.logical_path));
    for (const x of d.changed || []) kids.push(row('changed', x, x.logical_path));
    for (const x of d.deleted || []) kids.push(row('deleted', x, x.logical_path));
    for (const x of d.renamed || []) kids.push(row('renamed', x, x.from_path + ' -> ' + x.to_path));
    kids.push(el('p', {class: 'muted', text: 'Unchanged: ' + (d.unchanged_count || 0) + (v.noop ? ' - no change: nothing would be published.' : '')}));
    const prov = (v.provenance && v.provenance.kind) || 'external_observation';
    kids.push(el('p', {testid: 'refresh-provenance', data: {value: prov},
      text: 'Provenance: ' + prov + ' (an observation of source changed outside Ripple; it is not an approval)'}));
    const commit = el('button', {type: 'button', text: 'Commit refresh', testid: 'action-refresh-commit',
      disabled: st.committed || (deleted.length > 0 && !M.refreshAck), on: {click: async ev => {
        const r = await act(() => api('POST', '/v2/sources/refresh/commit', {
          workspace_id: ctx.ws, refresh_id: v.refresh_id, expected_version: v.base.version,
          expected_refresh_digest: v.refresh_digest,
          acknowledged_deletions: deleted.length && M.refreshAck ? deleted : []}), {button: ev.currentTarget});
        if (r === FAIL || ctx.gen !== S.gen) return;
        st.committed = true;
        await refreshStatus(ctx, box);
        try { await App.loadHead(); } catch (_) { /* reloaded below */ }
        await loadRevisionAfterRefresh(ctx);
      }}});
    if (!v.noop) {
      if (deleted.length) {
        kids.push(el('label', {}, el('input', {type: 'checkbox', testid: 'refresh-ack', checked: M.refreshAck,
          on: {change: ev => { M.refreshAck = ev.target.checked; commit.disabled = st.committed || !M.refreshAck; }}}),
          'I acknowledge the deleted artifacts listed above'));
      }
      kids.push(el('div', {class: 'row'}, commit));
    }
    kids.push(el('p', {testid: 'refresh-status', data: {state: (st.status && st.status.status) || v.status || 'PREPARED'},
      text: 'Refresh status: ' + ((st.status && st.status.status) || v.status || 'PREPARED')}));
    if (st.status) {
      const s = st.status;
      kids.push(el('h3', {text: 'What became stale'}),
        el('p', {class: 'muted', text: 'Policy: ' + s.stale_policy + '. Reanalysis: ' + s.reanalysis.state + (s.pending_worker ? ' (worker still processing)' : '')}));
      for (const r of s.stale_reasons || []) {
        kids.push(el('div', {testid: 'stale-reason-item', data: {kind: r.kind, ref: r.ref}}, r.kind + ': ' + r.ref + ' - ' + r.reason + ' (next: ' + r.next_owner + ')'));
        if (r.kind === 'approval_stale') kids.push(el('div', {testid: 'stale-approval-item', data: {'job-id': r.ref}, text: 'Approval ' + r.ref + ' is stale and cannot be approved as shown.'}));
      }
      if (!(s.stale_reasons || []).length) kids.push(el('p', {class: 'muted', text: 'No stale dependents recorded.'}));
    }
    replace(box, kids);
  }
  async function refreshStatus(ctx, box) {
    const st = M.refresh; if (!st) return;
    try {
      st.status = await api('GET', '/v2/sources/refresh/' + enc(st.view.refresh_id) + '?workspace_id=' + enc(ctx.ws));
    } catch (e) { if (e instanceof App.ApiError) { App.actionFailed(e); return; } throw e; }
    if (ctx.gen !== S.gen) return;
    renderRefresh(box, ctx);
  }
  async function loadRevisionAfterRefresh(ctx) {
    const revBox = document.getElementById('revision-box');
    if (!revBox || ctx.gen !== S.gen) return;
    try { await loadRevision(ctx, revBox, S.head, false); } catch (_) { /* state shown by reload */ }
    const st = M.refresh;
    if (st && st.status && st.status.pending_worker && M.polls < 60) {
      M.polls += 1;
      App.schedule(async () => { await refreshStatus(ctx, document.getElementById('refresh-box')); await loadRevisionAfterRefresh(ctx); }, 2000);
    }
  }

  // ------------------------------------------------------------------ view
  async function viewSources(ctx) {
    const {container} = ctx;
    M.polls = 0;
    replace(container, App.loadingEl('source revision'));
    let head;
    try { head = await App.loadHead(); } catch (e) {
      if (ctx.gen !== S.gen) return;
      if (!(e instanceof App.ApiError)) throw e;
      App.readFailed(container, e, () => App.reloadView()); return;
    }
    if (ctx.gen !== S.gen) return;
    const revBox = el('div', {id: 'revision-box'});
    // The inventory is the page; adding or refreshing files comes after it (it is a permissioned action, not the first thing seen).
    replace(container,
      revBox,
      el('div', {id: 'evidence-box'}),
      can('source.import') ? importPanel(ctx) : null,
      can('source.import') ? refreshPanel(ctx) : null);
    await loadRevision(ctx, revBox, head, true);
    const st = M.refresh;
    if (st && st.committed && ctx.gen === S.gen) await refreshStatus(ctx, document.getElementById('refresh-box'));
  }

  App.register('sources', viewSources, {reset});
})();
