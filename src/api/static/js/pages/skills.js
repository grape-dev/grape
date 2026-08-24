let _skillDetailVisible = false;
let _skillTab = 'official';  // official / repo / user

function _currentRepo() {
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';

  // 官方技能页签：先异步检查 configs/skill-description.json 是否有新增官方技能，有则同步后再加载
  if (_skillTab === 'official') {
    API.post('/skills/sync-official', {}).then(syncRes => {
      if (syncRes && syncRes.added > 0) {
        if (app && app.showToast) app.showToast(syncRes.message || '发现新增官方技能，已同步');
      }
      _loadSkills();
    }).catch(() => {
      _loadSkills();
    });
  } else {
    _loadSkills();
  }
}

function _loadSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const repo = _currentRepo();
  const qs = repo ? '?repo=' + encodeURIComponent(repo) : '';
  API.get('/skills' + qs).then(d => {
    const skills = d.skills || [];
    const META = _AGENT_TYPES || {};
    const LABELS = {vod:'VOD Agent',issue:'Issue Agent',coding:'Coding Agent',qa:'QA Agent',sre:'SRE Agent',chat:'Chat Agent'};
    const SCOPE_NAMES = {official:'官方技能', repo:'仓库技能', user:'个人技能'};

    // 页签
    let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <input placeholder="搜索技能..." id="skillSearch" onkeyup="filterSkills(this)" style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:220px">`;

    ['official','repo','user'].forEach(tab => {
      const active = _skillTab === tab ? 'background:var(--a);color:#fff' : '';
      html += `<button onclick="_skillTab='${tab}';renderSkills()" style="padding:7px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active}">${SCOPE_NAMES[tab] || tab}</button>`;
    });

    // 新增技能按钮（仓库/个人页签）
    if (_skillTab === 'repo' || _skillTab === 'user') {
      html += `<button onclick="showSkillUpload()" style="padding:7px 14px;border:1px solid var(--a);border-radius:var(--rd);font-size:12px;cursor:pointer;color:var(--a);margin-left:auto">＋ 新增技能</button>`;
    }
    html += `</div>`;

    // 仓库技能需当前仓库；无仓库时提示
    if (_skillTab === 'repo' && !repo) {
      html += '<div class="ts t2" style="padding:12px">未选择仓库，请先在仓库页面选择当前仓库。</div>';
      el.innerHTML = html;
      return;
    }

    // 按 Agent 分组
    const groups = {};
    skills.forEach(s => {
      if (s.scope !== _skillTab) return;  // 只显示当前页签
      const at = s.agent_type || '__uncategorized__';
      if (!groups[at]) groups[at] = [];
      groups[at].push(s);
    });

    Object.keys(groups).forEach(at => {
      const meta = META[at] || {};
      const label = meta.display_name || LABELS[at] || at;
      const color = meta.color || 'var(--t2)';
      const ico = meta.icon || '◈';
      const groupSkills = groups[at];
      if (!groupSkills || groupSkills.length === 0) return;

      html += `<div class="skill-group" style="margin-bottom:18px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px;color:${color}">${ico} ${label} Skills</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">`;
      groupSkills.forEach(s => {
        const tagClass = s.scope === 'official' ? 'tg-c' : s.scope === 'repo' ? 'tg-i' : 'tg-d';
        // 华为云技能包 → 文件夹导航到独立页面
        if (s.name === 'huawei-cloud') {
          html += `<div class="cd" style="padding:14px;cursor:pointer" onclick="app.nav('skill','${s.id}')">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-size:20px">📁</span>
              <span style="font-size:13px;font-weight:600">${s.display_name || s.name}</span>
              <span class="tg ${tagClass}" style="margin-left:auto;font-size:9px">${SCOPE_NAMES[s.scope] || s.scope}</span>
            </div>
            <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-top:6px">${s.description}</div>
          </div>`;
          return;
        }
        html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${s.name}" data-agent="${at}" onclick="app.nav('skill','${s.id}')">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:${color}">${ico}</span>
            <span style="font-size:12px;font-weight:600">${s.display_name || s.name}</span>
            <span class="tg ${tagClass}" style="margin-left:auto;font-size:9px">${SCOPE_NAMES[s.scope] || s.scope}</span>
          </div>
          <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:8px">${s.description}</div>
          <div style="display:flex;gap:6px">
            <button class="bt bt-o bt-s" onclick="event.stopPropagation();app.nav('skill','${s.id}')">View</button>
            ${s.scope !== 'official' ? `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteSkill('${s.name}','${s.scope}')">Delete</button>` : ''}
            ${s.scope === 'user' && _currentRepo() ? `<button class="bt bt-o bt-s" onclick="event.stopPropagation();convertSkill('${s.name}')">转为仓库技能</button>` : ''}
          </div>
        </div>`;
      });
      html += `</div></div>`;
    });

    if (Object.keys(groups).length === 0) {
      html += '<div class="ts t2" style="padding:12px">当前页签暂无技能。</div>';
    }

    el.innerHTML = html;
  }).catch(() => {
    el.innerHTML = '<div class="error">加载技能列表失败</div>';
  });
}

function showSkillUpload() {
  const repo = _currentRepo();
  const scope = _skillTab;  // repo / user
  const AGENT_OPTS = [
    {v:'vod', label:'VOD Agent (♫ 声音分析)'},
    {v:'issue', label:'Issue Agent (⚠ 需求管理)'},
    {v:'coding', label:'Coding Agent (⚙ 代码实现)'},
    {v:'qa', label:'QA Agent (✔ 审查验证)'},
    {v:'sre', label:'SRE Agent (♡ 构建部署)'},
    {v:'chat', label:'Chat Agent (☎ 对话助手)'},
  ];
  const modal = document.createElement('div');
  modal.id = 'skillUploadModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:480px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">上传技能 (ZIP)</h3>
    <div class="srow" style="margin-bottom:10px"><label>类型</label><span>${scope === 'repo' ? '仓库技能' : '个人技能'}</span></div>
    ${scope === 'repo' ? `<div class="srow" style="margin-bottom:10px"><label>仓库</label><span>${repo}</span></div>` : ''}
    <div class="srow" style="margin-bottom:10px"><label>技能类型 *</label>
      <select id="skillAgentType" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">
        <option value="">-- 请选择技能所属 Agent --</option>
        ${AGENT_OPTS.map(o => `<option value="${o.v}">${o.label}</option>`).join('')}
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">选择技能 ZIP 文件（目录名 = 技能名，内含 SKILL.md）</label>
      <input type="file" id="skillFile" accept=".zip" style="font-size:12px">
    </div>
    <div class="srow" style="margin-bottom:12px"><label></label>
      <span style="font-size:11px;color:var(--t2)">格式要求：<a href="javascript:void(0)" onclick="downloadSkillTemplate()" style="color:var(--a);cursor:pointer">📄 下载技能模板 ZIP</a></span>
    </div>
    <div id="skillUploadProgress" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:var(--t2);margin-bottom:4px">上传中... <span id="skillUploadPct">0%</span></div>
      <div style="background:var(--bg);border-radius:8px;height:8px;overflow:hidden">
        <div id="skillUploadBar" style="background:var(--a);height:100%;width:0%;transition:width .2s"></div>
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('skillUploadModal').remove()">取消</button>
      <button class="bt bt-p bt-s" onclick="doSkillUpload()">上传</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
}

function downloadSkillTemplate() {
  // OBS 模式：接口返回 {url, expires_in} 临时签名 URL（点击时动态签发），直连 OBS 下载；
  // local 模式：接口返回文件流，API.get 解析失败时回退原 window.open 逻辑
  API.get('/skills/template?token=' + encodeURIComponent(localStorage.getItem('token') || '')).then(r => {
    if (r && r.url) {
      window.open(r.url, '_blank');
      return;
    }
    window.open('/api/skills/template?token=' + encodeURIComponent(localStorage.getItem('token') || ''), '_blank');
  }).catch(() => {
    window.open('/api/skills/template?token=' + encodeURIComponent(localStorage.getItem('token') || ''), '_blank');
  });
}

function _setSkillUploadProgress(pct, done) {
  const prog = document.getElementById('skillUploadProgress');
  const bar = document.getElementById('skillUploadBar');
  const pctEl = document.getElementById('skillUploadPct');
  if (!prog || !bar || !pctEl) return;
  prog.style.display = done ? 'none' : 'block';
  bar.style.width = pct + '%';
  pctEl.textContent = pct + '%';
}

function doSkillUpload() {
  const fileInput = document.getElementById('skillFile');
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    if (app && app.showToast) app.showToast('请选择技能 ZIP 文件');
    return;
  }
  const agentType = document.getElementById('skillAgentType')?.value || '';
  if (!agentType) {
    if (app && app.showToast) app.showToast('请选择技能类型（所属 Agent）');
    return;
  }
  const scope = _skillTab;
  const repo = _currentRepo();
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('scope', scope);
  fd.append('agent_type', agentType);
  if (scope === 'repo' && repo) fd.append('repo', repo);

  _setSkillUploadProgress(0, false);
  API.upload('/skills/upload', fd, (pct) => _setSkillUploadProgress(pct, false)).then(r => {
    _setSkillUploadProgress(100, true);
    document.getElementById('skillUploadModal')?.remove();
    if (app && app.showToast) app.showToast(r.message || '上传成功');
    renderSkills();
  }).catch(e => {
    _setSkillUploadProgress(0, true);
    if (e && e.errors && e.errors.length) {
      if (app && app.showToast) app.showToast('Skill 校验失败（' + e.errors.length + ' 项）：\n' + e.errors.join('\n'));
    } else if (app && app.showToast) {
      app.showToast('上传失败: ' + e.message);
    }
  });
}

function deleteSkill(name, scope) {
  if (!confirm('确定删除技能 ' + name + ' 吗？')) return;
  const repo = _currentRepo();
  let qs = '?scope=' + scope;
  if (scope === 'repo' && repo) qs += '&repo=' + encodeURIComponent(repo);
  API.del('/skills/' + encodeURIComponent(name) + qs).then(r => {
    if (app && app.showToast) app.showToast(r.message || '已删除');
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast('删除失败: ' + e.message);
  });
}

function convertSkill(skillName) {
  const repo = _currentRepo();
  if (!repo) {
    if (app && app.showToast) app.showToast('请先选择当前仓库');
    return;
  }
  if (!confirm('将个人技能「' + skillName + '」复制到仓库 ' + repo + '？\n\n原个人技能保留，复制后可在该仓库的 DevOps/Chat Agent 中配置。')) return;
  API.post('/skills/convert', {skill_name: skillName, repo}).then(r => {
    if (app && app.showToast) app.showToast(r.message || '转换成功');
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast('转换失败: ' + e.message);
  });
}

function renderSkillDetail(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;

  // 华为云技能包 → 展示子技能卡片页面
  if (data.name === 'huawei-cloud') {
    renderHuaweiCloudPackage(el, data);
    return;
  }

  const META = _AGENT_TYPES || {};
  const meta = META[data.agent_type] || {};
  const color = meta.color || 'var(--t2)';
  const ico = meta.icon || '◈';
  const SCOPE_NAMES = {official:'官方技能', repo:'仓库技能', user:'个人技能'};
  const tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';

  const content = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');
  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="app.nav('skills')" style="margin-bottom:12px">← 返回 Skills Library</button>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
      <div class="cd" style="padding:16px 20px;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:8px;min-width:0">
            <span style="color:${color};font-size:22px">${ico}</span>
            <div>
              <h3 style="font-size:15px;font-weight:600">${data.display_name || data.name}</h3>
              <span class="tg ${tagClass}" style="font-size:9px">${SCOPE_NAMES[data.scope] || data.scope}</span>
            </div>
          </div>
          ${data.is_obs ? `<button class="bt bt-o bt-s" onclick="downloadSkillZip('${data.id}')" title="下载技能包 zip">⬇ 下载技能</button>` : ''}
        </div>
        <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description}</div>
        <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
        <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${content || '(无详细内容)'}</pre>
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">Skill Info</h3>
        <div class="srow"><label>Name</label><span>${data.name}</span></div>
        <div class="srow"><label>Agent Type</label><span>${data.agent_type}</span></div>
        <div class="srow"><label>Type</label><span>${SCOPE_NAMES[data.scope] || data.scope}</span></div>
        <div class="srow"><label>Description</label><span class="t2">${data.description || '—'}</span></div>
      </div>
    </div>`;
}

// ===== 华为云技能包：独立页面展示子技能卡片 =====
function renderHuaweiCloudPackage(el, data) {
  const SCOPE_NAMES = {official:'官方技能', repo:'仓库技能', user:'个人技能'};
  const tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';

  el.innerHTML = `
      <button class="bt bt-o bt-s" onclick="app.nav('skills')" style="margin-bottom:12px">← 返回 Skills Library</button>
      <div class="cd" style="padding:16px 20px;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:28px">📁</span>
          <div>
            <h3 style="font-size:16px;font-weight:600;margin:0">${data.display_name || data.name}</h3>
            <div style="font-size:12px;color:var(--t2);margin-top:4px">${data.description}</div>
          </div>
          <span class="tg ${tagClass}" style="margin-left:auto;font-size:10px">${SCOPE_NAMES[data.scope] || data.scope}</span>
          <button class="bt bt-o bt-s" onclick="togglePkgSkillMd(${data.id})" style="font-size:11px">📖 查看 SKIL.md</button>
        </div>
      </div>
      <div id="subSkillsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
        <div style="padding:20px;text-align:center;color:var(--t3);font-size:13px">⏳ 加载子技能...</div>
      </div>`;

  // 加载子技能列表
  API.get('/skills/' + data.id + '/sub').then(d => {
    var subs = d.sub_skills || [];
    var grid = document.getElementById('subSkillsGrid');
    if (!grid) return;
    if (!subs.length) {
      grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3)">（暂无子技能）</div>';
      return;
    }
    var html = '';
    subs.forEach(function(sub) {
      html += '<div class="cd" style="padding:14px;cursor:pointer;display:flex;flex-direction:column" onclick="loadSubSkillDetail(' + data.id + ',\'' + sub.name + '\')">'
        + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
        + '<span style="font-size:16px">📄</span>'
        + '<span style="font-size:13px;font-weight:600">' + (sub.display_name || sub.name) + '</span>'
        + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-bottom:6px">' + sub.name + '</div>'
        + '<div style="font-size:11px;color:var(--t2);line-height:1.5;flex:1">' + (sub.description || '').substring(0, 200) + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-top:6px">' + sub.file_count + ' 个文件</div>'
        + '</div>';
    });
    grid.innerHTML = html;
  }).catch(function() {
    var grid = document.getElementById('subSkillsGrid');
    if (grid) grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--tr)">加载失败</div>';
  });
}

// ===== 加载并展示子技能详情（替换当前页面内容）=====
function loadSubSkillDetail(pkgId, subName) {
  var el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';

  API.get('/skills/package-sub/' + pkgId + '/' + subName).then(function(data) {
    var META = typeof _AGENT_TYPES !== 'undefined' ? _AGENT_TYPES : {};
    var meta = META[data.agent_type] || {};
    var color = meta.color || 'var(--t2)';
    var ico = meta.icon || '◈';
    var SCOPE_NAMES = {official:'官方技能', repo:'仓库技能', user:'个人技能'};
    var tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';
    // 转义模板字面量特殊字符
    var safeContent = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');

    el.innerHTML = `
      <button class="bt bt-o bt-s" onclick="renderHuaweiCloudPackage(document.getElementById('pageContent'), {id:${pkgId},name:'huawei-cloud',display_name:'华为云技能包',description:'',agent_type:'sre',scope:'official'})" style="margin-bottom:12px">← 返回华为云技能包</button>
      <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
        <div class="cd" style="padding:16px 20px;overflow-y:auto">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px">
            <div style="display:flex;align-items:center;gap:8px;min-width:0">
              <span style="color:${color};font-size:22px">${ico}</span>
              <div>
                <h3 style="font-size:15px;font-weight:600">${data.display_name || data.name}</h3>
                <span class="tg ${tagClass}" style="font-size:9px">${SCOPE_NAMES[data.scope] || data.scope}</span>
              </div>
            </div>
          </div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description}</div>
          <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
          <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${safeContent || '(无详细内容)'}</pre>
        </div>
        <div class="cd" style="padding:16px 20px;font-size:12px">
          <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">Skill Info</h3>
          <div class="srow"><label>Name</label><span>${data.name}</span></div>
          <div class="srow"><label>Agent Type</label><span>${data.agent_type}</span></div>
          <div class="srow"><label>Type</label><span>${SCOPE_NAMES[data.scope] || data.scope}</span></div>
          <div class="srow"><label>Description</label><span class="t2">${data.description || '—'}</span></div>
        </div>
      </div>`;
  }).catch(function(e) {
    el.innerHTML = '<div class="error">加载失败: ' + (e.message || '') + '</div>';
  });
}

function downloadSkillZip(id) {
  // 下载技能包 zip（按技能 id 精确查询）：OBS 模式 → {url, expires_in} 临时签名 URL
  // （点击动态签发）→ 直连 OBS 下载；local 模式 → zip 文件流 → blob 触发下载
  const token = localStorage.getItem('token') || '';
  fetch('/api/skills/by-id/' + encodeURIComponent(id) + '/download', {
    headers: {'Authorization': 'Bearer ' + token}
  }).then(r => {
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      return r.json().then(d => {
        if (d && d.url) { window.open(d.url, '_blank'); return; }
        throw new Error('no url');
      });
    }
    return r.blob().then(b => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = 'skill-' + id + '.zip';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    });
  }).catch(() => showToast('下载技能失败，请稍后重试'));
}

function filterSkills(input) {
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.skill-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const agent = (card.dataset.agent || '').toLowerCase();
    card.style.display = (name.includes(q) || agent.includes(q)) ? '' : 'none';
  });
}

// ===== 华为云技能包 文件夹展开/折叠 =====
function toggleSkillFolder(skillId, headerEl) {
  const container = headerEl.closest('.cd');
  if (!container) return;
  const subEl = container.querySelector('.sub-skills');
  const arrow = container.querySelector('.folder-arrow');
  if (!subEl) return;

  if (subEl.style.display !== 'none') {
    subEl.style.display = 'none';
    if (arrow) arrow.textContent = '▶';
    return;
  }

  subEl.style.display = 'block';
  if (arrow) arrow.textContent = '▼';

  // 已加载过的直接显示
  if (subEl.dataset.loaded) return;
  subEl.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:8px">加载子技能...</div>';

  API.get('/skills/' + skillId + '/sub').then(d => {
    const subs = d.sub_skills || [];
    if (!subs.length) {
      subEl.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:8px">（无子技能）</div>';
      return;
    }
    var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">';
    subs.forEach(function(sub) {
      html += '<div class="cd sub-skill-card" style="padding:12px;cursor:pointer" onclick="showSubSkillContent(' + skillId + ',\'' + sub.name + '\',\'' + _escQuotes(sub.display_name) + '\')">'
        + '<div style="font-size:12px;font-weight:600;margin-bottom:3px">📄 ' + (sub.display_name || sub.name) + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-bottom:4px">' + sub.name + '</div>'
        + '<div style="font-size:11px;color:var(--t2);line-height:1.4">' + (sub.description || '').substring(0, 150) + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-top:4px">' + sub.file_count + ' 个文件</div>'
        + '</div>';
    });
    html += '</div>';
    subEl.innerHTML = html;
    subEl.dataset.loaded = '1';
  }).catch(function() {
    subEl.innerHTML = '<div style="font-size:11px;color:var(--tr);padding:8px">加载失败</div>';
  });
}

function _escQuotes(s) {
  return String(s || '').replace(/'/g, '\\\'').replace(/"/g, '&quot;');
}

// ===== 子技能 SKILL.md 内容展示（模态框）=====
function showSubSkillContent(skillId, subName, displayName) {
  closeSubSkillModal();
  var ov = document.createElement('div');
  ov.id = 'subSkillModal';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = '<div style="width:min(860px,92vw);height:min(700px,88vh);background:var(--bg,#11151c);border:1px solid var(--b,#2a3140);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.5)">'
    + '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0">'
    + '<span style="font-size:13px;font-weight:600">📄 ' + (displayName || subName) + '</span>'
    + '<span style="font-size:10px;color:var(--t3)">' + subName + '</span>'
    + '<button onclick="closeSubSkillModal()" style="margin-left:auto;border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">✕ 关闭</button>'
    + '</div>'
    + '<div id="subSkillContent" style="flex:1;min-height:0;overflow-y:auto;padding:14px;font-size:13px;line-height:1.7;white-space:pre-wrap;font-family:monospace;color:var(--t)">⏳ 加载中...</div>'
    + '</div>';
  document.body.appendChild(ov);
  ov.addEventListener('click', function(e) { if (e.target === ov) closeSubSkillModal(); });

  API.get('/skills/' + skillId + '/sub/' + subName).then(function(data) {
    var contentEl = document.getElementById('subSkillContent');
    if (contentEl) {
      var md = data.content || '(无内容)';
      var html = '<div style="background:#0b0e14;color:#c2cad6;padding:14px;border-radius:8px;font-size:12px;font-family:monospace;line-height:1.7;white-space:pre-wrap">' + _escHtml(md) + '</div>';
      // 如果有 references 文件
      if (data.references && data.references.length) {
        html += '<h3 style="font-size:12px;font-weight:600;margin:14px 0 8px">📎 参考文档</h3>';
        data.references.forEach(function(ref) {
          html += '<div style="margin-bottom:8px;background:#0b0e14;border-radius:6px;padding:10px;border:1px solid var(--b,#2a3140)">'
            + '<div style="font-size:11px;font-weight:600;margin-bottom:4px;color:var(--a)">' + _escHtml(ref.name) + '</div>'
            + '<div style="font-size:11px;color:var(--t2);line-height:1.5;white-space:pre-wrap;font-family:monospace">' + _escHtml(ref.content) + '</div>'
            + '</div>';
        });
      }
      contentEl.innerHTML = html;
    }
  }).catch(function() {
    var contentEl = document.getElementById('subSkillContent');
    if (contentEl) contentEl.innerHTML = '<div style="color:var(--tr)">加载失败</div>';
  });
}

function _escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function closeSubSkillModal() {
  var ov = document.getElementById('subSkillModal');
  if (ov) ov.remove();
}
