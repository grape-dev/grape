// Project Knowledge 页面（docs/new-version2/knowledge-board-design.md §三）
let _knowledgeTab = 'project';  // project / agent
let _knowledgeDetail = null;   // 详情数据

function _currentRepo() {
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

function _repoQS() {
  const repo = _currentRepo();
  return repo ? '?repo=' + encodeURIComponent(repo) : '';
}

function renderKnowledge() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">' + I18N.t('knowledge.loading') + '</div>';
  const repo = _currentRepo();
  if (!repo) {
    el.innerHTML = '<div class="ts t2" style="padding:12px">' + I18N.t('knowledge.no_repo_selected') + '</div>';
    return;
  }
  API.get('/knowledge' + _repoQS()).then(d => {
    _renderKnowledgeBoard(el, d);
  }).catch(e => {
    el.innerHTML = '<div class="error">' + I18N.t('knowledge.load_project_failed', {msg: e.message}) + '</div>';
  });
}

function _renderKnowledgeBoard(el, d) {
  const META = _AGENT_TYPES || {};
  const LABELS = {vod:'VOD Agent',issue:'Issue Agent',coding:'Coding Agent',qa:'QA Agent',sre:'SRE Agent',chat:'Chat Agent'};
  const repo = _currentRepo();

  let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <input placeholder="${I18N.t('knowledge.search_placeholder')}" id="knowledgeSearch" onkeyup="filterKnowledge(this)" style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:220px">`;

  ['project','agent'].forEach(tab => {
    const active = _knowledgeTab === tab ? 'background:var(--a);color:#fff' : '';
    const label = tab === 'project' ? I18N.t('knowledge.tab_project') : I18N.t('knowledge.tab_agent');
    html += `<button onclick="_knowledgeTab='${tab}';renderKnowledge()" style="padding:7px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active}">${label}</button>`;
  });

  html += `<button onclick="showKnowledgeUpload()" style="padding:7px 14px;border:1px solid var(--a);border-radius:var(--rd);font-size:12px;cursor:pointer;color:var(--a);margin-left:auto">${I18N.t('knowledge.upload_btn')}</button>`;
  html += `</div>`;

  if (_knowledgeTab === 'project') {
    html += _renderProjectTab(d);
  } else {
    html += _renderAgentTab(d);
  }

  el.innerHTML = html;
}

function _renderProjectTab(d) {
  const meta = d.project.meta || {};
  const degraded = meta.degraded ? '<span class="tr" style="font-size:10px">' + I18N.t('knowledge.degraded') + '</span>' : '';
  const updated = meta.last_updated ? (meta.last_updated || '').replace('T',' ').substring(0,19) : '—';

  let html = '<div class="grp-title" style="font-size:13px;font-weight:600;margin:14px 0 10px;color:var(--t)">' + I18N.t('knowledge.project_baseline_title') + '</div>';
  if (degraded) html += `<div class="ts" style="padding:6px 10px;margin-bottom:8px;font-size:11px;color:var(--or)">${degraded} ${meta.note || ''} · ${I18N.t('knowledge.updated_commit', {updated: updated, sha: (meta.last_commit_sha || '-')})}</div>`;
  else if (updated && updated !== '—') html += `<div class="ts t3" style="padding:2px 0 8px;font-size:11px">${I18N.t('knowledge.updated_commit', {updated: updated, sha: (meta.last_commit_sha || '-')})}</div>`;

  const files = d.project.files || [];
  if (files.length === 0) {
    html += '<div class="ts t2" style="padding:12px">' + I18N.t('knowledge.no_baseline') + '</div>';
  } else {
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">';
    files.forEach(f => {
html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${f.label}" onclick="event.stopPropagation();loadKnowledgeContent('${f.obs_key || f.name}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:var(--a)">${f.label === 'metadata' ? '🗂' : '📄'}</span>
          <span style="font-size:12px;font-weight:600">${f.label}</span>
          <span class="tg tg-c" style="margin-left:auto;font-size:9px">project-info</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4">${f.name.split('/').pop()}</div>
        <div style="font-size:10px;color:var(--t3);margin-top:4px">${f.updated || ''}</div>
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${f.obs_key || f.name}')">${I18N.t('knowledge.view')}</button>
      </div>`;
    });
    html += '</div>';
  }

  // 项目级知识沉淀（knowledge.md）
  const krows = d.project.knowledge || [];
  if (krows.length) {
    html += '<div class="grp-title" style="font-size:13px;font-weight:600;margin:16px 0 10px">' + I18N.t('knowledge.knowledge_sediment_title') + '</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">';
    krows.forEach(k => {
      const preview = (k.content || '').split('\n').filter(l => l.trim()).slice(-3).map(l => l.replace(/^- \[[^\]]*\]\s*/, '')).join(' · ');
      html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${I18N.t('knowledge.knowledge_sediment_title')}" onclick="loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:var(--pk)">📄</span>
          <span style="font-size:12px;font-weight:600">knowledge.md</span>
          <span class="tg tg-d" style="margin-left:auto;font-size:9px">project</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:6px">${preview ? escapeHtml(preview).substring(0,120) : I18N.t('knowledge.empty')}</div>
        <div style="font-size:10px;color:var(--t3)">${k.created_by || ''} · ${fmtLocalTime(k.created_at).substring(0,16)} · seq ${k.source_seq || 0}</div>
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">${I18N.t('knowledge.view')}</button>
      </div>`;
    });
    html += '</div>';
  }

  // 成员上传文档
  const docs = d.project.member_docs || [];
  html += '<div class="grp-title" style="font-size:13px;font-weight:600;margin:16px 0 10px">' + I18N.t('knowledge.member_docs_title') + '</div>';
  if (docs.length === 0) {
    html += '<div class="ts t2" style="padding:12px">' + I18N.t('knowledge.no_member_docs') + '</div>';
  } else {
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">';
    docs.forEach(doc => {
      const me = (() => { try { return JSON.parse(localStorage.getItem('user')||'{}').username || ''; } catch { return ''; } })();
      const canDel = !doc.owner || doc.owner === me;
      html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${doc.name}" onclick="loadKnowledgeContent('${doc.obs_key || doc.path}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:var(--a)">📄</span>
          <span style="font-size:12px;font-weight:600">${doc.name}</span>
          <span class="tg tg-i" style="margin-left:auto;font-size:9px">member</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4">@${doc.owner} · ${doc.size} bytes</div>
        <div style="font-size:10px;color:var(--t3);margin-top:4px">${doc.updated_at || ''}</div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();loadKnowledgeContent('${doc.obs_key || doc.path}')">${I18N.t('knowledge.view')}</button>
          ${canDel ? `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteKnowledgeDoc('${doc.path}')">${I18N.t('knowledge.delete')}</button>` : ''}
        </div>
      </div>`;
    });
    html += '</div>';
  }
  return html;
}

function _renderAgentTab(d) {
  const META = _AGENT_TYPES || {};
  const LABELS = {vod:'VOD Agent',issue:'Issue Agent',coding:'Coding Agent',qa:'QA Agent',sre:'SRE Agent',chat:'Chat Agent'};
  const agents = d.agents || {};
  const types = ['vod','issue','coding','qa','sre','chat'];
  const present = Object.keys(agents).filter(k => k && (agents[k] || []).length);
  const order = types.filter(t => present.includes(t)).concat(present.filter(k => !types.includes(k)));

  if (order.length === 0) {
    return '<div class="ts t2" style="padding:12px">' + I18N.t('knowledge.no_agent_knowledge') + '</div>';
  }

  let html = '';
  order.forEach(at => {
    const meta = META[at] || {};
    const label = meta.display_name || LABELS[at] || at;
    const color = meta.color || 'var(--t2)';
    const ico = meta.icon || '◈';
    const entries = agents[at] || [];
    html += `<div class="skill-group" style="margin-bottom:18px">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:10px;color:${color}">${ico} ${I18N.t('knowledge.agent_knowledge_count', {label: label, count: entries.length})}</h3>`;
    entries.forEach(k => {
      const preview = (k.content || '').split('\n').filter(l => l.trim()).slice(-2).map(l => l.replace(/^- \[[^\]]*\]\s*/, '')).join(' · ');
      const scopeLabel = k.scope === 'member_agent' ? I18N.t('knowledge.scope_member') : (k.scope === 'issue_agent' ? I18N.t('knowledge.scope_issue_agent') : I18N.t('knowledge.scope_issue'));
      const issueTag = (k.scope !== 'member_agent' && k.issue_num) ? ' · Issue #' + k.issue_num : '';
      html += `<div class="cd skill-card" style="padding:14px;cursor:pointer;margin-bottom:8px" data-name="${label}" onclick="loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:${color}">${ico}</span>
          <span style="font-size:12px;font-weight:600">${scopeLabel}${issueTag}</span>
          <span class="tg ${k.scope === 'member_agent' ? 'tg-i' : 'tg-d'}" style="margin-left:auto;font-size:9px">${k.scope === 'member_agent' ? 'member' : k.scope}</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4">${preview ? escapeHtml(preview).substring(0,140) : I18N.t('knowledge.empty')}</div>
<div style="font-size:10px;color:var(--t3);margin-top:4px">${k.scope === 'member_agent' ? '' + I18N.t('knowledge.scope_member_directed') + '' : 'seq ' + (k.source_seq || 0) + ' · ' + fmtLocalTime(k.created_at).substring(0,16)}</div>
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">${I18N.t('knowledge.view')}</button>
      </div>`;
    });
    html += '</div>';
  });
  return html;
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function filterKnowledge(input) {
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.skill-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const txt = (card.textContent || '').toLowerCase();
    card.style.display = (name.includes(q) || txt.includes(q)) ? '' : 'none';
  });
}

function loadKnowledgeContent(key) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">' + I18N.t('knowledge.loading') + '</div>';
  // 统一传 obs_key 和 file 两个参数，后端优先用 obs_key，查不到回退 file
  // obs_key 和 file 可能是同一个值（非 OBS 模式下 obs_key 为空，前端传的是 path）
  // 后端逻辑：有 obs_key 且非空 → 读 OBS；否则 → 走 _read_file
  // 但 obs_key 格式为 project-info/gitcode:xxx/file.md 时也能当 file 用（_read_file 会查 DB）
  // 所以直接传 obs_key 参数即可——后端 obs_key 为空时会回退 file 参数
  API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&obs_key=' + encodeURIComponent(key)).then(d => {
    renderKnowledgeDetail(d);
  }).catch(e => {
    el.innerHTML = '<div class="error">' + I18N.t('knowledge.load_detail_failed', {msg: e.message}) + '</div>';
  });
}

function renderKnowledgeDetail(d) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const fileLabel = d.file || d.obs_key || '';
  const isJson = fileLabel.toLowerCase().endsWith('.json');
  const content = d.content || '';
  const rendered = isJson
    ? '<pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">' + escapeHtml(content) + '</pre>'
    : renderMarkdown(content);

  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="app.nav('knowledge')" style="margin-bottom:12px">${I18N.t('knowledge.back_to_knowledge')}</button>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
      <div class="cd" style="padding:16px 20px;overflow-y:auto">
        <h3 style="font-size:15px;font-weight:600;margin-bottom:12px">📄 ${fileLabel}</h3>
        ${rendered || '<div class="ts t2">' + I18N.t('knowledge.empty') + '</div>'}
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">${I18N.t('knowledge.info_title')}</h3>
        <div class="srow"><label>${I18N.t('knowledge.info_file')}</label><span>${fileLabel}</span></div>
        <div class="srow"><label>${I18N.t('knowledge.info_repo')}</label><span>${d.repo_key || ''}</span></div>
      </div>
    </div>`;
}

// ===== 上传 / 删除 =====

function showKnowledgeUpload() {
  const repo = _currentRepo();
  const modal = document.createElement('div');
  modal.id = 'knowledgeUploadModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:460px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">${I18N.t('knowledge.upload_modal_title')}</h3>
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('knowledge.upload_repo')}</label><span>${repo}</span></div>
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('knowledge.upload_scope')}</label>
      <span style="display:flex;gap:8px;font-size:12px">
        <label style="cursor:pointer"><input type="radio" name="kScope" value="project" checked onchange="toggleKnowledgeAgentSel()"> ${I18N.t('knowledge.tab_project')}</label>
        <label style="cursor:pointer"><input type="radio" name="kScope" value="agent" onchange="toggleKnowledgeAgentSel()"> ${I18N.t('knowledge.tab_agent')}</label>
      </span>
    </div>
    <div class="srow" style="margin-bottom:10px" id="knowledgeAgentRow" style="display:none"><label>${I18N.t('knowledge.agent_label')}</label>
      <select id="knowledgeAgentType" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">
        <option value="">${I18N.t('knowledge.select_agent')}</option>
        <option value="vod">VOD Agent</option>
        <option value="issue">Issue Agent</option>
        <option value="coding">Coding Agent</option>
        <option value="qa">QA Agent</option>
        <option value="sre">SRE Agent</option>
        <option value="chat">Chat Agent</option>
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">${I18N.t('knowledge.select_file')}</label>
      <input type="file" id="knowledgeFile" accept=".md,.txt,.json" style="font-size:12px">
    </div>
    <div id="knowledgeUploadProgress" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:var(--t2);margin-bottom:4px">${I18N.t('knowledge.uploading')} <span id="knowledgeUploadPct">0%</span></div>
      <div style="background:var(--bg);border-radius:8px;height:8px;overflow:hidden">
        <div id="knowledgeUploadBar" style="background:var(--a);height:100%;width:0%;transition:width .2s"></div>
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('knowledgeUploadModal').remove()">${I18N.t('knowledge.cancel')}</button>
      <button class="bt bt-p bt-s" onclick="doKnowledgeUpload()">${I18N.t('knowledge.upload_btn_confirm')}</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  toggleKnowledgeAgentSel();
}

function toggleKnowledgeAgentSel() {
  const sel = document.querySelector('input[name="kScope"]:checked');
  const row = document.getElementById('knowledgeAgentRow');
  if (row) row.style.display = (sel && sel.value === 'agent') ? 'flex' : 'none';
}

function doKnowledgeUpload() {
  const fileInput = document.getElementById('knowledgeFile');
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    if (app && app.showToast) app.showToast(I18N.t('knowledge.please_select_file'));
    return;
  }
  const scope = document.querySelector('input[name="kScope"]:checked')?.value || 'project';
  let agentType = '';
  if (scope === 'agent') {
    agentType = document.getElementById('knowledgeAgentType')?.value || '';
    if (!agentType) {
      if (app && app.showToast) app.showToast(I18N.t('knowledge.please_select_agent'));
      return;
    }
  }
  const repo = _currentRepo();
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('repo', repo);
  fd.append('scope', scope);
  if (agentType) fd.append('agent_type', agentType);

  const prog = document.getElementById('knowledgeUploadProgress');
  const bar = document.getElementById('knowledgeUploadBar');
  const pct = document.getElementById('knowledgeUploadPct');
  if (prog) prog.style.display = 'block';
  API.upload('/knowledge', fd, (p) => { if (bar) bar.style.width = p + '%'; if (pct) pct.textContent = p + '%'; }).then(r => {
    document.getElementById('knowledgeUploadModal')?.remove();
    if (app && app.showToast) app.showToast(r.message || I18N.t('knowledge.upload_success'));
    renderKnowledge();
  }).catch(e => {
    if (prog) prog.style.display = 'none';
    if (e && e.errors && e.errors.length) {
      if (app && app.showToast) app.showToast(I18N.t('knowledge.validation_failed', {errors: e.errors.join('\n')}));
    } else if (app && app.showToast) {
      app.showToast(I18N.t('knowledge.upload_failed', {msg: e.message}));
    }
  });
}

function deleteKnowledgeDoc(path) {
  if (!confirm(I18N.t('knowledge.delete_confirm'))) return;
  API.del('/knowledge?repo=' + encodeURIComponent(_currentRepo()) + '&file=' + encodeURIComponent(path)).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('knowledge.deleted'));
    renderKnowledge();
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('knowledge.delete_failed', {msg: e.message}));
  });
}
