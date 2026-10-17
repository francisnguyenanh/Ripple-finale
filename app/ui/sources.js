const el = id => document.getElementById(id);
const show = id => { el(id).hidden = false; };
let workspaceId = '';
let currentRevisionId = '';

async function call(path, {method = 'GET', body} = {}) {
  const opts = {method, headers: {'X-Ripple-Judge': '1'}, credentials: 'same-origin'};
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const response = await fetch(path, opts);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function readableStatus(coverage) {
  if (!coverage) return 'no ingest job yet (admission only)';
  const lines = [
    `parse/analysis stage: ${coverage.status}`,
    `index build stage: ${coverage.index_status}${coverage.index_reason ? ' -- ' + coverage.index_reason : ''}`,
    `semantic (provider) stage: ${coverage.semantic_stage}`,
    `admission complete: ${coverage.admission_complete}`,
  ];
  if (coverage.skipped && coverage.skipped.length) {
    lines.push(`skipped: ${coverage.skipped.map(s => s.logical_path).join(', ')}`);
  }
  return lines.join('\n');
}

async function loadEvidence(revisionId, observationId) {
  el('status').textContent = 'Loading evidence…';
  try {
    const evidence = await call(
      `/v2/sources/revisions/${revisionId}/evidence/${observationId}?workspace_id=${encodeURIComponent(workspaceId)}`);
    el('evidenceDetail').textContent = JSON.stringify(evidence, null, 2);
    el('status').textContent = '';
  } catch (error) { el('status').textContent = error.message; }
}

async function listObservations(revisionId, artifact) {
  el('status').textContent = 'Listing observations…';
  try {
    const result = await call(
      `/v2/sources/revisions/${revisionId}/artifacts/${artifact.artifact_id}/observations?workspace_id=${encodeURIComponent(workspaceId)}`);
    const container = el('observationList');
    container.replaceChildren();
    const heading = document.createElement('h3');
    heading.textContent = `${artifact.logical_path} -- ${result.observations.length} observation(s)`;
    container.append(heading);
    for (const obs of result.observations) {
      const btn = document.createElement('button');
      btn.className = 'secondary';
      btn.textContent = `${obs.kind} @ ${JSON.stringify(obs.locator)}`;
      btn.addEventListener('click', () => loadEvidence(revisionId, obs.observation_id));
      container.append(btn);
    }
    if (!result.observations.length) container.append(document.createTextNode('No observations for this artifact.'));
    el('status').textContent = '';
  } catch (error) { el('status').textContent = error.message; }
}

function renderArtifacts(revisionId, artifacts) {
  const container = el('artifacts');
  container.replaceChildren();
  for (const artifact of artifacts) {
    const article = document.createElement('article');
    const title = document.createElement('h3');
    title.textContent = artifact.logical_path;
    const detail = document.createElement('pre');
    detail.textContent = `artifact_id: ${artifact.artifact_id}\nmedia_type: ${artifact.media_type}\nbyte_size: ${artifact.byte_size}\nblob_hash: ${artifact.blob_hash}`;
    const evidenceBtn = document.createElement('button');
    evidenceBtn.textContent = 'List evidence';
    evidenceBtn.addEventListener('click', () => listObservations(revisionId, artifact));
    const exportBtn = document.createElement('button');
    exportBtn.textContent = 'Export bytes';
    exportBtn.addEventListener('click', async () => {
      el('status').textContent = 'Exporting…';
      try {
        const result = await call(
          `/v2/sources/revisions/${revisionId}/artifacts/${artifact.artifact_id}/export?workspace_id=${encodeURIComponent(workspaceId)}`);
        const bytes = Uint8Array.from(atob(result.content_b64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], {type: result.media_type || 'application/octet-stream'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = artifact.logical_path.split('/').pop();
        document.body.append(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        el('status').textContent = '';
      } catch (error) { el('status').textContent = error.message; }
    });
    article.append(title, detail, evidenceBtn, exportBtn);
    container.append(article);
  }
}

// ---- AI9-T03: manual source refresh (a person uploads, reviews the diff, commits) ----
let preparedRefresh = null;

async function fileEntries(files) {
  const entries = [];
  for (const file of files) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    entries.push({path: file.webkitRelativePath || file.name, content_b64: btoa(binary)});
  }
  return entries;
}

function describeDiff(view) {
  const d = view.diff;
  const lines = [`refresh ${view.refresh_id} (${view.noop ? 'no change: nothing will be published' : 'reviewable diff'})`,
    `provenance: ${view.provenance.kind} (an external observation, not a change approval)`,
    `added: ${d.added.map(a => a.logical_path).join(', ') || '-'}`,
    `changed: ${d.changed.map(c => c.logical_path).join(', ') || '-'}`,
    `deleted: ${d.deleted.map(x => x.logical_path).join(', ') || '-'}`,
    `renamed: ${d.renamed.map(r => `${r.from_path} -> ${r.to_path}`).join(', ') || '-'}`,
    `unchanged: ${d.unchanged_count}`];
  return lines.join('\n');
}

function describeStale(status) {
  const lines = [`status: ${status.status}${status.pending_worker ? ' (worker still processing)' : ''}`,
    `stale policy: ${status.stale_policy}`,
    `reanalysis: ${status.reanalysis.state}${status.reanalysis.non_convergence_reason ? ' -- ' + status.reanalysis.non_convergence_reason : ''}`];
  for (const reason of status.stale_reasons) {
    lines.push(`- ${reason.kind}: ${reason.ref} -- ${reason.reason} (next owner: ${reason.next_owner})`);
  }
  if (!status.stale_reasons.length) lines.push('no stale dependents recorded');
  lines.push(`decisions kept: ${status.decisions.kept.length}; invalidated: ${status.decisions.invalidated.length}`);
  if (status.conflicts_opened.length) lines.push(`conflicts opened: ${status.conflicts_opened.join(', ')}`);
  return lines.join('\n');
}

el('refreshPrepare').addEventListener('click', async () => {
  el('status').textContent = 'Preparing refresh…';
  try {
    const files = await fileEntries(el('refreshFiles').files);
    const key = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    preparedRefresh = await call('/v2/sources/refresh/prepare', {
      method: 'POST', body: {workspace_id: workspaceId, idempotency_key: key, files}});
    el('refreshDiff').textContent = describeDiff(preparedRefresh);
    const needsAck = preparedRefresh.diff.deleted.length > 0;
    el('refreshAckLabel').hidden = !needsAck;
    el('refreshAck').checked = false;
    el('refreshCommit').hidden = preparedRefresh.noop;
    el('status').textContent = '';
  } catch (error) { el('status').textContent = error.message; }
});

async function showRefreshStatus() {
  const status = await call(
    `/v2/sources/refresh/${preparedRefresh.refresh_id}?workspace_id=${encodeURIComponent(workspaceId)}`);
  el('staleReasons').textContent = describeStale(status);
  el('refreshStatus').hidden = false;
}

el('refreshCommit').addEventListener('click', async () => {
  if (!preparedRefresh) return;
  const deleted = preparedRefresh.diff.deleted.map(x => x.logical_path);
  if (deleted.length && !el('refreshAck').checked) { el('status').textContent = 'Acknowledge the deleted artifacts first.'; return; }
  el('status').textContent = 'Committing refresh…';
  try {
    const result = await call('/v2/sources/refresh/commit', {method: 'POST', body: {
      workspace_id: workspaceId, refresh_id: preparedRefresh.refresh_id,
      expected_version: preparedRefresh.base.version,
      expected_refresh_digest: preparedRefresh.refresh_digest, acknowledged_deletions: deleted}});
    el('refreshDiff').textContent += `\ncommitted: revision ${result.source_revision_id}${result.replayed ? ' (replayed)' : ''}`;
    await showRefreshStatus();
    el('status').textContent = '';
  } catch (error) { el('status').textContent = error.message; }
});

el('refreshStatus').addEventListener('click', async () => {
  try { await showRefreshStatus(); } catch (error) { el('status').textContent = error.message; }
});

el('login').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await call('/v2/auth/login', {method: 'POST',
      body: {account_id: el('account').value, credential: el('credential').value}});
    el('login').hidden = true;
    show('load');
    el('reset').hidden = false;
  } catch (error) { el('status').textContent = error.message; }
});

el('loadRevision').addEventListener('click', async () => {
  el('status').textContent = 'Loading…';
  try {
    workspaceId = el('workspaceId').value;
    currentRevisionId = el('revisionId').value;
    const view = await call(
      `/v2/sources/revisions/${currentRevisionId}?workspace_id=${encodeURIComponent(workspaceId)}`);
    el('coverage').textContent = readableStatus(view.coverage);
    renderArtifacts(currentRevisionId, view.artifacts);
    show('revision');
    show('evidence');
    show('refresh');
    el('status').textContent = '';
  } catch (error) { el('status').textContent = error.message; }
});

el('reset').addEventListener('click', () => {
  el('login').hidden = false;
  for (const name of ['load', 'revision', 'evidence', 'refresh']) el(name).hidden = true;
  preparedRefresh = null;
  el('refreshDiff').textContent = ''; el('staleReasons').textContent = '';
  el('reset').hidden = true;
  el('coverage').textContent = '';
  el('artifacts').replaceChildren();
  el('observationList').replaceChildren();
  el('evidenceDetail').textContent = '';
  el('status').textContent = '';
});
