const el = id => document.getElementById(id);
let candidate, intent, shadow, changeset, approved=false;
const pendingRequests=new Map();
const show = id => { for (const name of ['conflict','intent','review','verify']) el(name).hidden = name !== id; };
const readable = value => String(value ?? 'Unknown').replaceAll('_',' ');
const amount = value => typeof value === 'number' ? value.toLocaleString('en-US') : readable(value);
function display(id, value) {
  let text=value;
  if(typeof value!=='string') {
    if(id==='candidate') text=[`Rule: ${readable(value.target)}`,`Current comparison value: ${amount(value.current_value)} ${value.unit}`,`Requested value: ${amount(value.desired_value)} ${value.unit}`,`Scope: ${value.scope || 'Please specify'}`,value.validation.ok?'Ready for your confirmation.':'Please clarify before confirming.',...value.validation.reasons,...value.injection_findings.map(x=>`Untrusted instruction detected: ${x}`),`Proposal source: ${value.provenance.model}`].join('\n');
    else if(id==='impact') {
      const d=value.preview.operational_diff;
      text=[`Risk: ${value.preview.risk_level}`,d?`Simulation: ${d.changed} of ${d.total} reference requests would be handled differently.`:'No behavioral simulation available.',...(d?.notes||[]),'',...['direct','indirect','unknown'].flatMap(tier=>[tier.toUpperCase(),...value.impact[tier].map(item=>`${item.identifier}\n  ${item.reason}`),''])].join('\n');
    } else if(id==='verification' && value.written) text=[`Applied to ${value.written.length} artifacts:`,...value.written,'',...value.notes,'Select Verify artifacts to check the edits.'].join('\n');
    else if(id==='verification') text=[value.passed?'Approved artifact edits verified.':'Verification needs attention.',value.reason,`Verified lineage version: ${value.lineage_version}`,...value.unresolved,...value.next_actions.map(a=>`${a.finding}: ${a.options.join(', ')}`)].join('\n');
    else if(id==='drift') text=value.changed?[`${value.artifact}: ${amount(value.from)} → ${amount(value.to)}`,`${value.alignment} · ${readable(value.decision)}`,value.reason,`Scope: ${value.scope.artifacts.length} of ${value.scope.total_artifacts} artifacts`,...value.scope.notes,'The verified lineage has not advanced. A person decides what to do next.'].join('\n'):value.note;
    else text=JSON.stringify(value,null,2);
  }
  el(id).textContent=text;
}
async function send(path, body) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Ripple-Judge':'1'}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok || result.ok === false) throw new Error(result.error || result.code || 'Request failed');
  return result.data || result;
}
async function rpc(operation,payload={}) {
  const key=JSON.stringify([operation,payload]);
  if(!pendingRequests.has(key))pendingRequests.set(key,crypto.randomUUID());
  const result=await send('/rpc',{operation,payload,request_id:pendingRequests.get(key)});
  pendingRequests.delete(key);return result;
}
function action(id, work) { el(id).addEventListener('click',async () => { el(id).disabled=true; el('status').textContent='Working…'; try { await work(); el('status').textContent=''; } catch(error) { el('status').textContent=error.message; } finally { el(id).disabled=false; } }); }
async function conflicts() {
  const result=await rpc('findings.list'); el('findings').replaceChildren();
  for (const finding of result.findings) {
    const article=document.createElement('article'); const title=document.createElement('h3'); title.textContent=readable(finding.target)+' · '+finding.kind;
    const detail=document.createElement('pre'); detail.textContent=(finding.representations||[]).map(r=>`${r.artifact} (${r.locator}): ${r.value===null?'no value stated':amount(r.value)}`).join('\n');
    article.append(title,detail);el('findings').append(article);
  }
  if (!result.findings.length) el('findings').textContent='No findings are available for this session.';
  show('conflict'); el('reset').hidden=false;
}
el('login').addEventListener('submit',async event=>{event.preventDefault();try{await send('/login',{code:el('code').value});el('code').value='';el('login').hidden=true;await conflicts();}catch(error){el('status').textContent=error.message;}});
action('begin',async()=>show('intent'));
action('parse',async()=>{candidate=null;el('confirm').disabled=true;el('prepare').disabled=true;const result=await rpc('intent.parse',{text:el('prompt').value});candidate=result;display('candidate',result);el('confirm').disabled=!result.validation.ok;});
el('prompt').addEventListener('input',()=>{candidate=null;el('confirm').disabled=true;el('prepare').disabled=true;});
action('confirm',async()=>{if(!candidate?.validation.ok)throw Error('Prepare a valid candidate first');intent=await rpc('intent.create',{candidate_id:candidate.candidate_id,content_hash:candidate.content_hash});shadow=await rpc('whatif.simulate',{target:candidate.semantic_id,proposed_value:candidate.desired_value});display('impact',{preview:shadow,impact:await rpc('impact.show',{shadow_id:shadow.shadow_id})});el('prepare').disabled=false;});
action('prepare',async()=>{approved=false;el('reject').disabled=false;changeset=await rpc('changeset.prepare',{intent_id:intent.intent_id,shadow_id:shadow.shadow_id});const review=await rpc('changeset.review',{changeset_id:changeset.changeset_id});display('reviewText',review.text);show('review');});
async function decide(decision){if(!el('reason').value.trim())throw Error('Please give a decision reason');await rpc('changeset.decide',{changeset_id:changeset.changeset_id,decision,reason:el('reason').value});}
action('approve',async()=>{if(!approved){await decide('approve');approved=true;el('reject').disabled=true;}const applied=await rpc('changeset.apply',{changeset_id:changeset.changeset_id});display('verification',applied);show('verify');});
action('reject',async()=>{await decide('reject');await conflicts();});
action('runVerify',async()=>display('verification',await rpc('verification.run',{changeset_id:changeset.changeset_id})));
action('external',async()=>display('drift',await rpc('scenario.external_edit',{artifact:'data/approval_matrix.xlsx',locator:'Thresholds!C2',value:Number(el('externalValue').value)})));
action('reset',async()=>{await rpc('session.reset');candidate=intent=shadow=changeset=null;approved=false;pendingRequests.clear();for(const id of ['candidate','impact','reviewText','verification','drift'])display(id,'');el('confirm').disabled=el('prepare').disabled=true;await conflicts();});
