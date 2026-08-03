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
        html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${s.name}" data-agent="${at}" onclick="app.nav('skill','${s.name}')">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:${color}">${ico}</span>
            <span style="font-size:12px;font-weight:600">${s.display_name || s.name}</span>
            <span class="tg ${tagClass}" style="margin-left:auto;font-size:9px">${SCOPE_NAMES[s.scope] || s.scope}</span>
          </div>
          <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:8px">${s.description}</div>
          <div style="display:flex;gap:6px">
            <button class="bt bt-o bt-s" onclick="event.stopPropagation();app.nav('skill','${s.name}')">View</button>
            ${s.scope !== 'official' ? `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteSkill('${s.name}','${s.scope}')">Delete</button>` : ''}
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
  const modal = document.createElement('div');
  modal.id = 'skillUploadModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:440px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">上传技能 (ZIP)</h3>
    <div class="srow" style="margin-bottom:10px"><label>类型</label><span>${scope === 'repo' ? '仓库技能' : '个人技能'}</span></div>
    ${scope === 'repo' ? `<div class="srow" style="margin-bottom:10px"><label>仓库</label><span>${repo}</span></div>` : ''}
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">选择技能 ZIP 文件（目录名 = 技能名，内含 SKILL.md）</label>
      <input type="file" id="skillFile" accept=".zip" style="font-size:12px">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('skillUploadModal').remove()">取消</button>
      <button class="bt bt-p bt-s" onclick="doSkillUpload()">上传</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
}

function doSkillUpload() {
  const fileInput = document.getElementById('skillFile');
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    if (window.app && app.showToast) app.showToast('请选择技能 ZIP 文件');
    return;
  }
  const scope = _skillTab;
  const repo = _currentRepo();
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('scope', scope);
  if (scope === 'repo' && repo) fd.append('repo', repo);

  API.upload('/skills/upload', fd).then(r => {
    document.getElementById('skillUploadModal')?.remove();
    if (window.app && app.showToast) app.showToast(r.message || '上传成功');
    renderSkills();
  }).catch(e => {
    if (window.app && app.showToast) app.showToast('上传失败: ' + e.message);
  });
}

function deleteSkill(name, scope) {
  if (!confirm('确定删除技能 ' + name + ' 吗？')) return;
  const repo = _currentRepo();
  let qs = '?scope=' + scope;
  if (scope === 'repo' && repo) qs += '&repo=' + encodeURIComponent(repo);
  API.del('/skills/' + encodeURIComponent(name) + qs).then(r => {
    if (window.app && app.showToast) app.showToast(r.message || '已删除');
    renderSkills();
  }).catch(e => {
    if (window.app && app.showToast) app.showToast('删除失败: ' + e.message);
  });
}

function renderSkillDetail(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
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
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <span style="color:${color};font-size:22px">${ico}</span>
          <div>
            <h3 style="font-size:15px;font-weight:600">${data.display_name || data.name}</h3>
            <span class="tg ${tagClass}" style="font-size:9px">${SCOPE_NAMES[data.scope] || data.scope}</span>
          </div>
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

function filterSkills(input) {
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.skill-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const agent = (card.dataset.agent || '').toLowerCase();
    card.style.display = (name.includes(q) || agent.includes(q)) ? '' : 'none';
  });
}
