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
  el.innerHTML = '<div class="loading">加载中...</div>';
  const repo = _currentRepo();
  if (!repo) {
    el.innerHTML = '<div class="ts t2" style="padding:12px">未选择仓库，请先在仓库页面选择当前仓库。</div>';
    return;
  }
  API.get('/knowledge' + _repoQS()).then(d => {
    _renderKnowledgeBoard(el, d);
  }).catch(e => {
    el.innerHTML = '<div class="error">加载项目知识失败: ' + e.message + '</div>';
  });
}

function _renderKnowledgeBoard(el, d) {
  const META = _AGENT_TYPES || {};
  const LABELS = {vod:'VOD Agent',issue:'Issue Agent',coding:'Coding Agent',qa:'QA Agent',sre:'SRE Agent',chat:'Chat Agent'};
  const repo = _currentRepo();

  let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <input placeholder="搜索知识..." id="knowledgeSearch" onkeyup="filterKnowledge(this)" style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:220px">`;

  ['project','agent'].forEach(tab => {
    const active = _knowledgeTab === tab ? 'background:var(--a);color:#fff' : '';
    const label = tab === 'project' ? '项目级' : 'Agent 维度';
    html += `<button onclick="_knowledgeTab='${tab}';renderKnowledge()" style="padding:7px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active}">${label}</button>`;
  });

  html += `<button onclick="showKnowledgeUpload()" style="padding:7px 14px;border:1px solid var(--a);border-radius:var(--rd);font-size:12px;cursor:pointer;color:var(--a);margin-left:auto">＋ 上传知识</button>`;
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
  const degraded = meta.degraded ? '<span class="tr" style="font-size:10px">⚠ 降级生成</span>' : '';
  const updated = meta.last_updated ? (meta.last_updated || '').replace('T',' ').substring(0,19) : '—';

  let html = '<div class="grp-title" style="font-size:13px;font-weight:600;margin:14px 0 10px;color:var(--t)">📦 项目知识基线（DevOps Agent 自动生成）</div>';
  if (degraded) html += `<div class="ts" style="padding:6px 10px;margin-bottom:8px;font-size:11px;color:var(--or)">${degraded} ${meta.note || ''} · 更新 ${updated}</div>`;
  else if (updated && updated !== '—') html += `<div class="ts t3" style="padding:2px 0 8px;font-size:11px">更新 ${updated} · commit ${meta.last_commit_sha || '-'}</div>`;

  const files = d.project.files || [];
  if (files.length === 0) {
    html += '<div class="ts t2" style="padding:12px">暂无项目知识基线（project-info 未生成或降级）。</div>';
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
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${f.obs_key || f.name}')">View</button>
      </div>`;
    });
    html += '</div>';
  }

  // 项目级知识沉淀（knowledge.md）
  const krows = d.project.knowledge || [];
  if (krows.length) {
    html += '<div class="grp-title" style="font-size:13px;font-weight:600;margin:16px 0 10px">📄 项目知识沉淀</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">';
    krows.forEach(k => {
      const preview = (k.content || '').split('\n').filter(l => l.trim()).slice(-3).map(l => l.replace(/^- \[[^\]]*\]\s*/, '')).join(' · ');
      html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="知识沉淀" onclick="loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:var(--pk)">📄</span>
          <span style="font-size:12px;font-weight:600">knowledge.md</span>
          <span class="tg tg-d" style="margin-left:auto;font-size:9px">project</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:6px">${preview ? escapeHtml(preview).substring(0,120) : '(空)'}</div>
        <div style="font-size:10px;color:var(--t3)">${k.created_by || ''} · ${fmtLocalTime(k.created_at).substring(0,16)} · seq ${k.source_seq || 0}</div>
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">View</button>
      </div>`;
    });
    html += '</div>';
  }

  // 成员上传文档
  const docs = d.project.member_docs || [];
  html += '<div class="grp-title" style="font-size:13px;font-weight:600;margin:16px 0 10px">👥 成员上传文档</div>';
  if (docs.length === 0) {
    html += '<div class="ts t2" style="padding:12px">暂无成员上传文档，点击「＋ 上传知识」添加。</div>';
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
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();loadKnowledgeContent('${doc.obs_key || doc.path}')">View</button>
          ${canDel ? `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteKnowledgeDoc('${doc.path}')">Delete</button>` : ''}
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
    return '<div class="ts t2" style="padding:12px">暂无 Agent 维度知识沉淀（来自 Issue 对话的 accepted 知识）。</div>';
  }

  let html = '';
  order.forEach(at => {
    const meta = META[at] || {};
    const label = meta.display_name || LABELS[at] || at;
    const color = meta.color || 'var(--t2)';
    const ico = meta.icon || '◈';
    const entries = agents[at] || [];
    html += `<div class="skill-group" style="margin-bottom:18px">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:10px;color:${color}">${ico} ${label} · ${entries.length} 条知识</h3>`;
    entries.forEach(k => {
      const preview = (k.content || '').split('\n').filter(l => l.trim()).slice(-2).map(l => l.replace(/^- \[[^\]]*\]\s*/, '')).join(' · ');
      const scopeLabel = k.scope === 'member_agent' ? '成员上传' : (k.scope === 'issue_agent' ? 'Agent 级' : 'Issue 级');
      const issueTag = (k.scope !== 'member_agent' && k.issue_num) ? ' · Issue #' + k.issue_num : '';
      html += `<div class="cd skill-card" style="padding:14px;cursor:pointer;margin-bottom:8px" data-name="${label}" onclick="loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:${color}">${ico}</span>
          <span style="font-size:12px;font-weight:600">${scopeLabel}${issueTag}</span>
          <span class="tg ${k.scope === 'member_agent' ? 'tg-i' : 'tg-d'}" style="margin-left:auto;font-size:9px">${k.scope === 'member_agent' ? 'member' : k.scope}</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4">${preview ? escapeHtml(preview).substring(0,140) : '(空)'}</div>
<div style="font-size:10px;color:var(--t3);margin-top:4px">${k.scope === 'member_agent' ? '成员上传 · 定向该 Agent' : 'seq ' + (k.source_seq || 0) + ' · ' + fmtLocalTime(k.created_at).substring(0,16)}</div>
        <button class="bt bt-o bt-s" style="margin-top:8px" onclick="event.stopPropagation();loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')">View</button>
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
  el.innerHTML = '<div class="loading">加载中...</div>';
  // 如果 key 不是 project-info/ 开头，视为 obs_key 直接读 OBS
  const isPath = key && (key.startsWith('project-info/') || key.startsWith('/'));
  const params = isPath
    ? 'file=' + encodeURIComponent(key)
    : 'obs_key=' + encodeURIComponent(key);
  API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&' + params).then(d => {
    renderKnowledgeDetail(d);
  }).catch(e => {
    el.innerHTML = '<div class="error">加载知识详情失败: ' + e.message + '</div>';
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
    <button class="bt bt-o bt-s" onclick="app.nav('knowledge')" style="margin-bottom:12px">← 返回 Project Knowledge</button>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
      <div class="cd" style="padding:16px 20px;overflow-y:auto">
        <h3 style="font-size:15px;font-weight:600;margin-bottom:12px">📄 ${fileLabel}</h3>
        ${rendered || '<div class="ts t2">(空)</div>'}
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">Knowledge Info</h3>
        <div class="srow"><label>File</label><span>${fileLabel}</span></div>
        <div class="srow"><label>Repo</label><span>${d.repo_key || ''}</span></div>
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
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">上传项目知识</h3>
    <div class="srow" style="margin-bottom:10px"><label>仓库</label><span>${repo}</span></div>
    <div class="srow" style="margin-bottom:10px"><label>维度 *</label>
      <span style="display:flex;gap:8px;font-size:12px">
        <label style="cursor:pointer"><input type="radio" name="kScope" value="project" checked onchange="toggleKnowledgeAgentSel()"> 项目级</label>
        <label style="cursor:pointer"><input type="radio" name="kScope" value="agent" onchange="toggleKnowledgeAgentSel()"> Agent 维度</label>
      </span>
    </div>
    <div class="srow" style="margin-bottom:10px" id="knowledgeAgentRow" style="display:none"><label>Agent *</label>
      <select id="knowledgeAgentType" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">
        <option value="">-- 选择 Agent --</option>
        <option value="vod">VOD Agent</option>
        <option value="issue">Issue Agent</option>
        <option value="coding">Coding Agent</option>
        <option value="qa">QA Agent</option>
        <option value="sre">SRE Agent</option>
        <option value="chat">Chat Agent</option>
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">选择知识文档（.md / .txt / .json，≤10MB）</label>
      <input type="file" id="knowledgeFile" accept=".md,.txt,.json" style="font-size:12px">
    </div>
    <div id="knowledgeUploadProgress" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:var(--t2);margin-bottom:4px">上传中... <span id="knowledgeUploadPct">0%</span></div>
      <div style="background:var(--bg);border-radius:8px;height:8px;overflow:hidden">
        <div id="knowledgeUploadBar" style="background:var(--a);height:100%;width:0%;transition:width .2s"></div>
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('knowledgeUploadModal').remove()">取消</button>
      <button class="bt bt-p bt-s" onclick="doKnowledgeUpload()">上传</button>
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
    if (app && app.showToast) app.showToast('请选择知识文档');
    return;
  }
  const scope = document.querySelector('input[name="kScope"]:checked')?.value || 'project';
  let agentType = '';
  if (scope === 'agent') {
    agentType = document.getElementById('knowledgeAgentType')?.value || '';
    if (!agentType) {
      if (app && app.showToast) app.showToast('请选择 Agent');
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
    if (app && app.showToast) app.showToast(r.message || '上传成功');
    renderKnowledge();
  }).catch(e => {
    if (prog) prog.style.display = 'none';
    if (e && e.errors && e.errors.length) {
      if (app && app.showToast) app.showToast('校验失败：\n' + e.errors.join('\n'));
    } else if (app && app.showToast) {
      app.showToast('上传失败: ' + e.message);
    }
  });
}

function deleteKnowledgeDoc(path) {
  if (!confirm('确定删除该知识文档吗？')) return;
  API.del('/knowledge?repo=' + encodeURIComponent(_currentRepo()) + '&file=' + encodeURIComponent(path)).then(r => {
    if (app && app.showToast) app.showToast(r.message || '已删除');
    renderKnowledge();
  }).catch(e => {
    if (app && app.showToast) app.showToast('删除失败: ' + e.message);
  });
}
