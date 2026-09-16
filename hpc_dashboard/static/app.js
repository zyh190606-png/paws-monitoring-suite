const state = { token: '', snapshot: null, accounts: [], selectedAccount: '', selectedJob: '', search: '', selectedRuns: new Set(), scope: 'standard', fileLists: new Map(), fileAudits: new Map(), customFiles: new Map(), downloadRoot: '', downloadPoll: null };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['X-PAWS-Token'] = state.token;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Number(seconds)); const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); const m = Math.floor((s % 3600) / 60);
  return `${d ? d + '天 ' : ''}${h}时${m}分`;
}
function fmtDateTime(value) { if(!value)return '—'; const d=new Date(value); if(Number.isNaN(d.getTime()))return '—'; return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d); }
function fmtBytes(bytes) { if (bytes == null) return '—'; const units=['B','KB','MB','GB','TB']; let n=Number(bytes),i=0; while(n>=1024&&i<units.length-1){n/=1024;i++;} return `${n.toFixed(i?1:0)} ${units[i]}`; }
function runToken(run) { return `${run.account}|${run.run_key}`; }
function toast(title, detail='', level='ok', life=6500) { const el=document.createElement('div'); el.className=`toast ${level}`; el.innerHTML=`<b>${escapeHtml(title)}</b><span>${escapeHtml(detail)}</span>`; $('toastStack').appendChild(el); setTimeout(()=>el.remove(),life); }

async function bootstrap() {
  const data = await api('/api/bootstrap'); state.token=data.token; state.accounts=data.accounts||[]; state.downloadRoot=data.download_root; $('downloadRoot').textContent=data.download_root;
  $('daysInput').value=String(data.recent_days); bindEvents(); await refreshAll();
}

function bindEvents() {
  $('refreshBtn').addEventListener('click', refreshAll);
  $('refreshCasesBtn').addEventListener('click', refreshCurrentCases);
  $('clearAccount').addEventListener('click',()=>{state.selectedAccount='';state.selectedJob='';render();});
  $('manageAccountsBtn').addEventListener('click', openAccountDialog);
  $('markAlertsReadBtn').addEventListener('click', markVisibleAlertsRead);
  $('addAccountBtn').addEventListener('click', addAccount);
  $('accountKeyFile').addEventListener('change', e=>{ $('accountKeyFileName').textContent=e.target.files[0]?.name||'未选择文件'; });
  $('searchInput').addEventListener('input',e=>{state.search=e.target.value.toLowerCase().trim();renderCases();});
  $('downloadBtn').addEventListener('click',openDownloadDialog);
  document.querySelectorAll('.scope-tabs button').forEach(btn=>btn.addEventListener('click',()=>setScope(btn.dataset.scope)));
  $('confirmDownload').addEventListener('click',startDownload);
}

async function refreshAll() {
  await performRefresh(null);
}

async function refreshCurrentCases() {
  const accounts=[...new Set((state.snapshot?.runs||[]).map(run=>run.account))];
  if(!accounts.length){toast('当前没有可刷新的 Case','请先执行一次全部账号刷新','warning');return;}
  await performRefresh(accounts);
}

async function performRefresh(accounts) {
  const activeBtn=accounts?$('refreshCasesBtn'):$('refreshBtn');
  $('refreshBtn').disabled=true;$('refreshCasesBtn').disabled=true;activeBtn.classList.add('refreshing');
  $('loadingTitle').textContent=accounts?`正在刷新 ${accounts.length} 个有 Case 的账号`:'正在并行查询全部账号';
  $('loadingDetail').textContent=accounts?'仅更新现有 Case · 其他账号保留上次快照':'SSH · SQUEUE · SACCT · PAWS STDOUT';
  $('loadingVeil').classList.remove('hidden');
  try {
    const manual=$('manualJobs').value.split(/[,，\s]+/).filter(Boolean);
    let storedPrevious=null; try{storedPrevious=JSON.parse(localStorage.getItem('pawsLastSnapshot')||'null')}catch(_err){}
    const previous=state.snapshot||storedPrevious; state.snapshot=await api('/api/refresh',{method:'POST',body:JSON.stringify({recent_days:Number($('daysInput').value),manual_jobs:manual,accounts})});
    notifyChanges(previous,state.snapshot); state.accounts=state.snapshot.managed_accounts||state.accounts; localStorage.setItem('pawsLastSnapshot',JSON.stringify({runs:state.snapshot.runs.map(r=>({account:r.account,run_key:r.run_key,state:r.state,health_level:r.health_level,case_id:r.case_id,health_warnings:r.health_warnings}))})); pruneSelection(); render();
    toast(accounts?'当前 Case 已刷新':'全部账号刷新完成',accounts?`${state.snapshot.refreshed_accounts.length} 个有 Case 的账号已更新`:`${state.snapshot.summary.reachable_accounts}/${state.snapshot.summary.account_count} 个账号可达`);
  } catch(err) { toast('刷新失败',err.message,'critical',10000); }
  finally { activeBtn.classList.remove('refreshing');$('refreshBtn').disabled=false;$('refreshCasesBtn').disabled=!(state.snapshot?.runs||[]).length;$('loadingVeil').classList.add('hidden'); }
}

function notifyChanges(previous,current) {
  if (!previous || !previous.runs) return;
  const old=new Map(previous.runs.map(r=>[runToken(r),r]));
  current.runs.forEach(r=>{ const p=old.get(runToken(r)); if(!p)return; if(p.state!==r.state && r.state==='COMPLETED') toast('Case 已完成',`${r.account} · ${r.case_id}`,r.download_ready?'ok':'warning',10000); if(p.health_level!=='critical'&&r.health_level==='critical') toast('发现新的严重异常',`${r.case_id} · ${(r.health_warnings[0]||{}).message||''}`,'critical',12000); });
}
function pruneSelection(){ const valid=new Set(state.snapshot.runs.filter(r=>r.download_ready).map(runToken)); [...state.selectedRuns].forEach(k=>{if(!valid.has(k))state.selectedRuns.delete(k)}); }

function render(){ renderFreshness();renderSummary();renderAccounts();renderAlerts();renderJobs();renderCases(); }
function renderFreshness(){ const s=state.snapshot;$('freshDot').classList.toggle('live',!!s);$('freshLabel').textContent=s?(s.refresh_scope==='case_accounts'?'当前 Case 已更新':'全量快照有效'):'尚未采样';$('freshTime').textContent=s?new Date(s.generated_at).toLocaleString('zh-CN'):'—';$('refreshCasesBtn').disabled=!(s?.runs||[]).length; }
function renderSummary(){ const s=state.snapshot?.summary||{}; const metrics=[['账号在线',`${s.reachable_accounts??0}/${s.account_count??0}`,'SSH并行探测','good'],['活跃任务',s.running_count??0,'RUNNING cases','good'],['近期完成',s.completed_count??0,'最近查询窗口',''],['可安全下载',s.download_ready_count??0,'六项健康门通过','good'],['未读异常',s.critical_count??0,'已读后从这里移除',s.critical_count?'bad':''],['识别 Case',s.case_count??0,`${s.job_count??0} 个Job`,'']]; $('summaryGrid').innerHTML=metrics.map((m,i)=>`<article class="metric ${m[3]}" data-index="0${i+1}"><span>${m[0]}</span><strong>${m[1]}</strong><small>${m[2]}</small></article>`).join(''); }
function renderAccounts(){ const accounts=state.snapshot?.accounts||[]; $('accountRail').innerHTML=accounts.map(a=>{const failLabel=a.error_code==='QUERY_TIMEOUT'?'查询超时':a.error_code==='AUTH_FAILED'?'认证失败':a.error_code==='NOT_REFRESHED'?'尚未查询':'连接失败';const detail=a.reachable?`${a.latency_ms} ms · ${a.source}${a.attempts>1?' · 重试成功':''}`:failLabel;const capacity=a.capacity_status==='known'?`余量 ${a.capacity_remaining} / ${a.capacity_limit}`:a.capacity_status==='read_only'?'历史账号 · 只监控':'余量未知 · 账本不可用';return `<button class="account-chip ${a.reachable?'online':''} ${state.selectedAccount===a.username?'active':''}" data-account="${a.username}" title="${escapeHtml(a.error||detail)}"><b>${a.username}</b><small><i></i>${detail}</small><small class="account-capacity">${capacity}</small></button>`}).join(''); document.querySelectorAll('.account-chip').forEach(el=>el.onclick=()=>{state.selectedAccount=state.selectedAccount===el.dataset.account?'':el.dataset.account;state.selectedJob='';render();}); }
function filteredAlerts(){ return (state.snapshot?.alerts||[]).filter(a=>!state.selectedAccount||a.account===state.selectedAccount); }
function renderAlerts(){ const alerts=filteredAlerts();$('alertPanel').classList.toggle('hidden',!alerts.length);$('alertCount').textContent=`${alerts.length} 未读`; $('markAlertsReadBtn').disabled=!alerts.length; $('alertList').innerHTML=alerts.slice(0,12).map(a=>`<div class="alert-item ${a.level}"><i></i><div><b>${escapeHtml(a.message)}</b><small>${escapeHtml(a.account)} · ${escapeHtml(a.case_id)}</small></div><div class="alert-meta"><small>${escapeHtml(a.code)}</small><button type="button" class="alert-read-btn" data-alert-id="${a.alert_id}" title="标记这条告警为已读">✓ 已读</button></div></div>`).join(''); document.querySelectorAll('.alert-read-btn').forEach(btn=>btn.onclick=()=>markAlertsRead([btn.dataset.alertId])); }
async function markAlertsRead(alertIds){ try { state.snapshot=await api('/api/alerts/read',{method:'POST',body:JSON.stringify({alert_ids:alertIds})}); render(); toast('告警已标记为已读','当前红色告警已从面板隐藏'); } catch(err){ toast('无法标记告警',err.message,'critical',9000); } }
function markVisibleAlertsRead(){ markAlertsRead(filteredAlerts().map(alert=>alert.alert_id)); }
function openAccountDialog(){ renderAccountManager(); $('accountDialog').showModal(); }
function renderAccountManager(){ $('accountList').innerHTML=state.accounts.length?state.accounts.map(a=>`<div class="managed-account"><div><b>${escapeHtml(a.username)}</b><small>${escapeHtml(a.source)} · ${a.managed?'面板导入副本':'自动发现凭据'}</small></div><button type="button" class="remove-account-btn" data-account="${escapeHtml(a.username)}">移除</button></div>`).join(''):'<div class="picker-message">暂无已配置账号</div>'; document.querySelectorAll('.remove-account-btn').forEach(btn=>btn.onclick=()=>removeAccount(btn.dataset.account)); }
async function addAccount(){ const username=$('accountUsername').value.trim(); const file=$('accountKeyFile').files[0]; if(!username||!file){toast('请填写账号并选择私钥文件','','warning');return;} if(file.size>1000000){toast('私钥文件过大','最大支持 1 MB','warning');return;} try { const keyContent=await file.text(); const data=await api('/api/accounts',{method:'POST',body:JSON.stringify({username,key_content:keyContent,filename:file.name})}); state.accounts=data.accounts||[]; $('accountUsername').value=''; $('accountKeyFile').value=''; $('accountKeyFileName').textContent='未选择文件'; $('accountDialog').close(); toast('账号已添加',`${username} 已加入监控列表`); await refreshAll(); } catch(err){toast('添加账号失败',err.message,'critical',10000);} }
async function removeAccount(username){ const account=state.accounts.find(a=>a.username===username); const text=account?.managed?'将删除面板导入的密钥副本。':'只从面板移除，不会删除原始密钥。'; if(!window.confirm(`确定移除 ${username}？\n${text}`))return; try { const data=await api('/api/accounts',{method:'DELETE',body:JSON.stringify({username})}); state.accounts=data.accounts||[]; if(state.selectedAccount===username){state.selectedAccount='';state.selectedJob='';} renderAccountManager(); toast('账号已移除',`${username} 不再参与后续监控`); await refreshAll(); } catch(err){toast('移除账号失败',err.message,'critical',10000);} }
function visibleJobs(){ return (state.snapshot?.jobs||[]).filter(j=>(!state.selectedAccount||j.account===state.selectedAccount)); }
function renderJobs(){ const jobs=visibleJobs(); $('jobCards').innerHTML=jobs.length?jobs.map(j=>{ const level=j.critical_count?'critical':j.warning_count?'warning':'';const states=Object.entries(j.states).map(([k,v])=>`${k} ${v}`).join(' · ');return `<article class="job-card ${level} ${state.selectedJob===j.job_id?'active':''}" data-job="${j.job_id}" data-account="${j.account}"><div class="job-top"><b>#${j.job_id}</b><span>${j.account}</span></div><h3>${escapeHtml(j.job_name||'PAWS array')}</h3><p>${escapeHtml(j.batch)}</p><div class="job-meter"><i style="width:${j.mean_progress_pct??0}%"></i></div><div class="job-foot"><span>${states}</span><span>${j.mean_progress_pct==null?'—':j.mean_progress_pct+'%'}</span></div></article>`}).join(''):'<div class="empty-state">所选账号在查询窗口内没有识别到任务</div>'; document.querySelectorAll('.job-card').forEach(el=>el.onclick=()=>{state.selectedAccount=el.dataset.account;state.selectedJob=state.selectedJob===el.dataset.job?'':el.dataset.job;render();}); }

function visibleRuns(){ return (state.snapshot?.runs||[]).filter(r=>(!state.selectedAccount||r.account===state.selectedAccount)&&(!state.selectedJob||r.job_id===state.selectedJob)&&(!state.search||`${r.account} ${r.job_id} ${r.case_id} ${r.batch}`.toLowerCase().includes(state.search))); }
function healthText(run){ if(run.download_ready)return ['下载门已通过','可选择打包下载']; if(run.health_warnings.length)return [run.health_warnings[0].message,run.health_warnings.length>1?`另有${run.health_warnings.length-1}项`:'需检查']; if(run.state==='RUNNING')return ['运行健康','stdout持续监控']; if(run.state==='COMPLETED')return ['完成待核验','未通过全部下载门']; return ['暂无异常证据','状态信息有限']; }
function renderCases(){ const runs=visibleRuns();$('caseRows').innerHTML=runs.length?runs.map(r=>{ const pct=r.progress_pct;const ht=healthText(r);const checked=state.selectedRuns.has(runToken(r));return `<tr><td class="check-col"><input class="case-check" type="checkbox" data-run="${runToken(r)}" ${checked?'checked':''} ${r.download_ready?'':'disabled'} title="${r.download_ready?'选择下载':'完成健康门未通过'}"></td><td><span class="mono">${r.account}</span><span class="subline">#${r.job_id}_${r.task_id} · ${escapeHtml(r.partition||'—')}</span></td><td><span class="case-name" title="${escapeHtml(r.case_id)}">${escapeHtml(r.case_id)}</span><span class="subline">${escapeHtml(r.batch)}</span></td><td><span class="state-pill ${r.state}">${escapeHtml(r.state)}</span><span class="subline">${escapeHtml(r.elapsed||'—')}</span></td><td class="model-date"><b>${r.latest_date||'—'}</b><span class="subline">${r.latest_year?`${r.latest_year}年 · 第${r.latest_day}天`:'等待输出'}</span></td><td><div class="progress-cell"><div class="progress-track"><i style="width:${pct??0}%"></i></div><b>${pct==null?'—':pct+'%'}</b></div><span class="subline">${r.model_start||'?' } → ${r.model_end||'?'}</span></td><td><span class="mono expected-end" data-has-eta="${r.expected_end_at?'true':'false'}">${fmtDateTime(r.expected_end_at)}</span><span class="subline">剩余 ${fmtDuration(r.eta_seconds)} · 墙钟 ${fmtDuration(r.time_left_seconds)}</span></td><td><div class="health ${r.health_level}"><i></i><div><b class="${r.download_ready?'gate-ready':''}">${escapeHtml(ht[0])}</b><small>${escapeHtml(ht[1])}</small></div></div></td></tr>`}).join(''):'<tr><td colspan="8" class="empty-state">没有符合筛选条件的case</td></tr>'; document.querySelectorAll('.case-check').forEach(el=>el.onchange=()=>{el.checked?state.selectedRuns.add(el.dataset.run):state.selectedRuns.delete(el.dataset.run);updateSelection();}); updateSelection(); }
function updateSelection(){ const selected=[...state.selectedRuns];const accounts=new Set(selected.map(k=>k.split('|')[0]));$('selectionLabel').textContent=selected.length?`已选择 ${selected.length} 个 case${accounts.size>1?'（请保持同一账号）':''}`:'未选择可下载 case';$('downloadBtn').disabled=!selected.length||accounts.size>1; }

async function openDownloadDialog(){ const runs=(state.snapshot?.runs||[]).filter(r=>state.selectedRuns.has(runToken(r)));if(!runs.length)return;state.fileLists.clear();state.fileAudits.clear();state.customFiles.clear();$('selectedCaseSummary').innerHTML=`<b>${runs[0].account} · Job #${runs[0].job_id}</b>　${runs.map(r=>escapeHtml(r.case_id)).join('　·　')}`;$('packageLabel').value=`paws_${runs[0].job_id}_${runs.length}case_selected`;setScope('standard');$('downloadDialog').showModal();$('filePicker').innerHTML='<div class="picker-message">正在读取远端文件清单…</div>';try{await Promise.all(runs.map(async r=>{const data=await api(`/api/files?account=${encodeURIComponent(r.account)}&run_key=${encodeURIComponent(r.run_key)}`);state.fileLists.set(runToken(r),data.files);state.fileAudits.set(runToken(r),data.audit);}));renderFilePicker();}catch(err){$('filePicker').innerHTML=`<div class="picker-message">${escapeHtml(err.message)}</div>`;} }
function setScope(scope){state.scope=scope;document.querySelectorAll('.scope-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.scope===scope));const help={standard:'分析输出、运行日志与 provenance；默认排除静态运行资源。',full:'打包所选 case 的完整 work 与运行元数据，文件可能很大。',custom:'逐个勾选远端具体文件；保留 case 目录结构。'};$('scopeHelp').textContent=help[scope];renderFilePicker();}
function fileSelectionKey(token,path){return `${token}\n${path}`;}
function renderFilePicker(){ if(!state.fileLists.size)return;const custom=state.scope==='custom';const incomplete=state.scope==='standard'&&[...state.fileAudits.values()].some(a=>a&&!a.standard_complete);let total=0,count=0,html='';for(const [token,files] of state.fileLists){const caseId=(state.snapshot.runs.find(r=>runToken(r)===token)||{}).case_id||token;if(custom&&![...state.customFiles.keys()].some(k=>k.startsWith(`${token}\n`))){files.filter(f=>f.standard).forEach(f=>state.customFiles.set(fileSelectionKey(token,f.path),true));}const chosen=state.scope==='standard'?files.filter(f=>f.standard):custom?files.filter(f=>state.customFiles.get(fileSelectionKey(token,f.path))):files;count+=chosen.length;total+=chosen.reduce((s,f)=>s+f.size_bytes,0);if(custom){html+=`<section class="file-group"><h3>${escapeHtml(caseId)} · ${files.length} files</h3>${files.map(f=>{const key=fileSelectionKey(token,f.path);return `<label class="file-row"><input type="checkbox" class="file-check" data-token="${escapeHtml(token)}" data-path="${escapeHtml(f.path)}" ${state.customFiles.get(key)?'checked':''}><span>${escapeHtml(f.path)}</span><small>${escapeHtml(f.role)} · ${fmtBytes(f.size_bytes)}</small></label>`;}).join('')}</section>`;}}if(!custom){const problems=[...state.fileAudits.entries()].filter(([,a])=>a&&!a.standard_complete);const warning=problems.length?`<div class="picker-warning"><b>标准结果不完整，已禁止确认</b><span>${problems.length} 个case缺少必要结果；${escapeHtml(problems[0][1].messages.join('；'))}</span><small>文件名与case显示名不一致时，面板会按实际输出前缀归类；cRec.txt不能替代Recorder。</small></div>`:'';html=`${warning}<div class="picker-message">将打包 <b>${count}</b> 个文件，未压缩总量约 <b>${fmtBytes(total)}</b></div>`;}$('confirmDownload').disabled=(custom?count===0:incomplete);$('filePicker').innerHTML=html;if(custom){document.querySelectorAll('.file-check').forEach(el=>el.onchange=()=>{state.customFiles.set(fileSelectionKey(el.dataset.token,el.dataset.path),el.checked);renderFilePicker();});}}

async function startDownload(){const runs=(state.snapshot?.runs||[]).filter(r=>state.selectedRuns.has(runToken(r)));if(!runs.length)return;const label=$('packageLabel').value.trim();if(!label){toast('请输入本地文件夹名称','','warning');return;}if(!window.confirm(`确认对 ${runs.length} 个已完成 case 执行远端打包，并下载到 ${state.downloadRoot}？`))return;const selections=runs.map(r=>({run_key:r.run_key,files:state.scope==='custom'?[...state.fileLists.get(runToken(r))||[]].filter(f=>state.customFiles.get(fileSelectionKey(runToken(r),f.path))).map(f=>f.path):[]}));try{const task=await api('/api/downloads',{method:'POST',body:JSON.stringify({confirm:true,account:runs[0].account,label,scope:state.scope,selections})});$('downloadDialog').close();state.selectedRuns.clear();renderCases();toast('下载任务已开始',task.label);startDownloadPolling();}catch(err){toast('无法开始下载',err.message,'critical',10000);}}
async function pollDownloads(){try{const data=await api('/api/downloads');renderDownloads(data.tasks);if(!data.tasks.some(t=>['queued','running'].includes(t.status))){clearInterval(state.downloadPoll);state.downloadPoll=null;}}catch(err){console.error(err)}}
function startDownloadPolling(){if(!state.downloadPoll)state.downloadPoll=setInterval(pollDownloads,1500);pollDownloads();}
function renderDownloads(tasks){$('downloadSection').classList.toggle('hidden',!tasks.length);$('downloadTasks').innerHTML=tasks.map(t=>`<article class="download-task ${t.status}"><div><b>${escapeHtml(t.label)}</b><small>${escapeHtml(t.account)} · ${t.cases.length} cases</small></div><div><span>${escapeHtml(t.phase)}</span><div class="progress-track"><i style="width:${t.progress_pct||0}%"></i></div><small>${t.error?escapeHtml(t.error):`${fmtBytes(t.local_bytes)} / ${fmtBytes(t.archive_bytes)}`}</small></div><div class="task-status">${t.status==='completed'?'校验通过':t.status==='failed'?'失败':`${t.progress_pct||0}%`}<small>${t.local_path?escapeHtml(t.local_path):''}</small></div></article>`).join(''); }

bootstrap().catch(err=>toast('面板初始化失败',err.message,'critical',15000));
