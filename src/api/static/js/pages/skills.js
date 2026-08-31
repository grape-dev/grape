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
  el.innerHTML = '<div class="loading">' + I18N.t('skills.loading') + '</div>';

  // 官方技能页签：先异步检查 configs/skill-description.json 是否有新增官方技能，有则同步后再加载
  if (_skillTab === 'official') {
    API.post('/skills/sync-official', {}).then(syncRes => {
      if (syncRes && syncRes.added > 0) {
        if (app && app.showToast) app.showToast(syncRes.message || I18N.t('skills.toast.sync_found'));
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
    const SCOPE_NAMES = {official:I18N.t('skills.scope.official'), repo:I18N.t('skills.scope.repo'), user:I18N.t('skills.scope.user')};

    // 页签
    let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <input placeholder="${I18N.t('skills.search.placeholder')}" id="skillSearch" onkeyup="filterSkills(this)" style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:220px">`;

    ['official','repo','user'].forEach(tab => {
      const active = _skillTab === tab ? 'background:var(--a);color:#fff' : '';
      html += `<button onclick="_skillTab='${tab}';renderSkills()" style="padding:7px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active}">${SCOPE_NAMES[tab] || tab}</button>`;
    });

    // 新增技能按钮（仓库/个人页签）
    if (_skillTab === 'repo' || _skillTab === 'user') {
      html += `<button onclick="showSkillUpload()" style="padding:7px 14px;border:1px solid var(--a);border-radius:var(--rd);font-size:12px;cursor:pointer;color:var(--a);margin-left:auto">${I18N.t('skills.action.add_skill')}</button>`;
    }
    html += `</div>`;

    // 仓库技能需当前仓库；无仓库时提示
    if (_skillTab === 'repo' && !repo) {
      html += '<div class="ts t2" style="padding:12px">' + I18N.t('skills.empty.no_repo_selected') + '</div>';
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
            <button class="bt bt-o bt-s" onclick="event.stopPropagation();app.nav('skill','${s.id}')">${I18N.t('skills.action.view')}</button>
            ${s.scope !== 'official' ? `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteSkill('${s.name}','${s.scope}')">${I18N.t('skills.action.delete')}</button>` : ''}
            ${s.scope === 'user' && _currentRepo() ? `<button class="bt bt-o bt-s" onclick="event.stopPropagation();convertSkill('${s.name}')">${I18N.t('skills.action.convert_to_repo')}</button>` : ''}
          </div>
        </div>`;
      });
      html += `</div></div>`;
    });

    if (Object.keys(groups).length === 0) {
      html += '<div class="ts t2" style="padding:12px">' + I18N.t('skills.empty.no_skills') + '</div>';
    }

    el.innerHTML = html;
  }).catch(() => {
    el.innerHTML = '<div class="error">' + I18N.t('skills.error.load_list_failed') + '</div>';
  });
}

function showSkillUpload() {
  const repo = _currentRepo();
  const scope = _skillTab;  // repo / user
  const AGENT_OPTS = [
    {v:'vod', label:I18N.t('skills.agent_type.vod')},
    {v:'issue', label:I18N.t('skills.agent_type.issue')},
    {v:'coding', label:I18N.t('skills.agent_type.coding')},
    {v:'qa', label:I18N.t('skills.agent_type.qa')},
    {v:'sre', label:I18N.t('skills.agent_type.sre')},
    {v:'chat', label:I18N.t('skills.agent_type.chat')},
  ];
  const modal = document.createElement('div');
  modal.id = 'skillUploadModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:480px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">${I18N.t('skills.upload.title')}</h3>
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.type')}</label><span>${scope === 'repo' ? I18N.t('skills.scope.repo') : I18N.t('skills.scope.user')}</span></div>
    ${scope === 'repo' ? `<div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.repo')}</label><span>${repo}</span></div>` : ''}
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.agent_type')}</label>
      <select id="skillAgentType" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">
        <option value="">${I18N.t('skills.upload.select_agent')}</option>
        ${AGENT_OPTS.map(o => `<option value="${o.v}">${o.label}</option>`).join('')}
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">${I18N.t('skills.upload.file_label')}</label>
      <input type="file" id="skillFile" accept=".zip" style="font-size:12px">
    </div>
    <div class="srow" style="margin-bottom:12px"><label></label>
      <span style="font-size:11px;color:var(--t2)">${I18N.t('skills.upload.format_req')}<a href="javascript:void(0)" onclick="downloadSkillTemplate()" style="color:var(--a);cursor:pointer">${I18N.t('skills.action.download_template')}</a></span>
    </div>
    <div id="skillUploadProgress" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:var(--t2);margin-bottom:4px">${I18N.t('skills.upload.uploading')} <span id="skillUploadPct">0%</span></div>
      <div style="background:var(--bg);border-radius:8px;height:8px;overflow:hidden">
        <div id="skillUploadBar" style="background:var(--a);height:100%;width:0%;transition:width .2s"></div>
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('skillUploadModal').remove()">${I18N.t('skills.action.cancel')}</button>
      <button class="bt bt-p bt-s" onclick="doSkillUpload()">${I18N.t('skills.action.upload')}</button>
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
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_zip'));
    return;
  }
  const agentType = document.getElementById('skillAgentType')?.value || '';
  if (!agentType) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_agent_type'));
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
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.upload_success'));
    renderSkills();
  }).catch(e => {
    _setSkillUploadProgress(0, true);
    if (e && e.errors && e.errors.length) {
      if (app && app.showToast) app.showToast(I18N.t('skills.toast.validation_failed', {count: e.errors.length, errors: e.errors.join('\n')}));
    } else if (app && app.showToast) {
      app.showToast(I18N.t('skills.toast.upload_failed', {msg: e.message}));
    }
  });
}

function deleteSkill(name, scope) {
  if (!confirm(I18N.t('skills.confirm.delete_skill', {name: name}))) return;
  const repo = _currentRepo();
  let qs = '?scope=' + scope;
  if (scope === 'repo' && repo) qs += '&repo=' + encodeURIComponent(repo);
  API.del('/skills/' + encodeURIComponent(name) + qs).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.deleted'));
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.delete_failed', {msg: e.message}));
  });
}

function convertSkill(skillName) {
  const repo = _currentRepo();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_repo_first'));
    return;
  }
  if (!confirm(I18N.t('skills.confirm.convert_skill', {name: skillName, repo: repo}))) return;
  API.post('/skills/convert', {skill_name: skillName, repo}).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.convert_success'));
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.convert_failed', {msg: e.message}));
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
  const SCOPE_NAMES = {official:I18N.t('skills.scope.official'), repo:I18N.t('skills.scope.repo'), user:I18N.t('skills.scope.user')};
  const tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';

  const content = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');
  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="app.nav('skills')" style="margin-bottom:12px">${I18N.t('skills.action.back_to_library')}</button>
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
          ${data.is_obs ? `<button class="bt bt-o bt-s" onclick="downloadSkillZip('${data.id}')" title="${I18N.t('skills.action.download_skill_title')}">${I18N.t('skills.action.download_skill')}</button>` : ''}
        </div>
        <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description}</div>
        <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
        <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${content || I18N.t('skills.empty.no_detail')}</pre>
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">${I18N.t('skills.info_title')}</h3>
        <div class="srow"><label>${I18N.t('skills.info_name')}</label><span>${data.name}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_agent_type')}</label><span>${data.agent_type}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_type')}</label><span>${SCOPE_NAMES[data.scope] || data.scope}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_description')}</label><span class="t2">${data.description || '—'}</span></div>
      </div>
    </div>`;
}

// ===== 华为云技能包：独立页面展示子技能卡片 =====
function renderHuaweiCloudPackage(el, data) {
  const SCOPE_NAMES = {official:I18N.t('skills.scope.official'), repo:I18N.t('skills.scope.repo'), user:I18N.t('skills.scope.user')};
  const tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';

  el.innerHTML = `
      <button class="bt bt-o bt-s" onclick="app.nav('skills')" style="margin-bottom:12px">${I18N.t('skills.action.back_to_library')}</button>
      <div class="cd" style="padding:16px 20px;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:28px">📁</span>
          <div>
            <h3 style="font-size:16px;font-weight:600;margin:0">${data.display_name || data.name}</h3>
            <div style="font-size:12px;color:var(--t2);margin-top:4px">${data.description}</div>
          </div>
          <span class="tg ${tagClass}" style="margin-left:auto;font-size:10px">${SCOPE_NAMES[data.scope] || data.scope}</span>
          <button class="bt bt-o bt-s" onclick="togglePkgSkillMd(${data.id})" style="font-size:11px">${I18N.t('skills.action.view_skill_md')}</button>
        </div>
      </div>
      <div id="subSkillsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
        <div style="padding:20px;text-align:center;color:var(--t3);font-size:13px">I18N.t('skills.huawei.loading_sub')</div>
      </div>`;

  // 加载子技能列表
  API.get('/skills/' + data.id + '/sub').then(d => {
    var subs = d.sub_skills || [];
    var grid = document.getElementById('subSkillsGrid');
    if (!grid) return;
    if (!subs.length) {
      grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3)">' + I18N.t('skills.huawei.no_sub_skill') + '</div>';
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
        + '<div style="font-size:10px;color:var(--t3);margin-top:6px">' + I18N.t('skills.huawei.files_count', {count: sub.file_count}) + '</div>'
        + '</div>';
    });
    grid.innerHTML = html;
  }).catch(function() {
    var grid = document.getElementById('subSkillsGrid');
    if (grid) grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--tr)">' + I18N.t('common.load_failed') + '</div>';
  });
}

// ===== 加载并展示子技能详情（替换当前页面内容）=====
function loadSubSkillDetail(pkgId, subName) {
  var el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">' + I18N.t('skills.loading') + '</div>';

  API.get('/skills/package-sub/' + pkgId + '/' + subName).then(function(data) {
    var META = typeof _AGENT_TYPES !== 'undefined' ? _AGENT_TYPES : {};
    var meta = META[data.agent_type] || {};
    var color = meta.color || 'var(--t2)';
    var ico = meta.icon || '◈';
    var SCOPE_NAMES = {official:I18N.t('skills.scope.official'), repo:I18N.t('skills.scope.repo'), user:I18N.t('skills.scope.user')};
    var tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';
    // 转义模板字面量特殊字符
    var safeContent = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');

    el.innerHTML = `
      <button class="bt bt-o bt-s" onclick="renderHuaweiCloudPackage(document.getElementById('pageContent'), {id:${pkgId},name:'huawei-cloud',display_name:'${I18N.t('skills.huawei.package_name')}',description:'',agent_type:'sre',scope:'official'})" style="margin-bottom:12px">← ${I18N.t('skills.huawei.back')}</button>
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
          <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${safeContent || I18N.t('skills.huawei.no_detail')}</pre>
        </div>
        <div class="cd" style="padding:16px 20px;font-size:12px">
          <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">${I18N.t('skills.info_title')}</h3>
          <div class="srow"><label>${I18N.t('skills.info_name')}</label><span>${data.name}</span></div>
          <div class="srow"><label>${I18N.t('skills.info_agent_type')}</label><span>${data.agent_type}</span></div>
          <div class="srow"><label>${I18N.t('skills.info_type')}</label><span>${SCOPE_NAMES[data.scope] || data.scope}</span></div>
          <div class="srow"><label>${I18N.t('skills.info_description')}</label><span class="t2">${data.description || '—'}</span></div>
        </div>
      </div>`;
  }).catch(function(e) {
    el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message || '') + '</div>';
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
  }).catch(() => showToast(I18N.t('skills.toast.download_failed')));
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
  subEl.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:8px">' + I18N.t('skills.huawei.loading_sub') + '</div>';

  API.get('/skills/' + skillId + '/sub').then(d => {
    const subs = d.sub_skills || [];
    if (!subs.length) {
      subEl.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:8px">' + I18N.t('skills.huawei.no_sub_skill') + '</div>';
      return;
    }
    var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">';
    subs.forEach(function(sub) {
      html += '<div class="cd sub-skill-card" style="padding:12px;cursor:pointer" onclick="showSubSkillContent(' + skillId + ',\'' + sub.name + '\',\'' + _escQuotes(sub.display_name) + '\')">'
        + '<div style="font-size:12px;font-weight:600;margin-bottom:3px">📄 ' + (sub.display_name || sub.name) + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-bottom:4px">' + sub.name + '</div>'
        + '<div style="font-size:11px;color:var(--t2);line-height:1.4">' + (sub.description || '').substring(0, 150) + '</div>'
        + '<div style="font-size:10px;color:var(--t3);margin-top:4px">' + I18N.t('skills.huawei.files_count', {count: sub.file_count}) + '</div>'
        + '</div>';
    });
    html += '</div>';
    subEl.innerHTML = html;
    subEl.dataset.loaded = '1';
  }).catch(function() {
    subEl.innerHTML = '<div style="font-size:11px;color:var(--tr);padding:8px">' + I18N.t('common.load_failed') + '</div>';
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
    + '<button onclick="closeSubSkillModal()" style="margin-left:auto;border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">✕ ' + I18N.t('common.close') + '</button>'
    + '</div>'
    + '<div id="subSkillContent" style="flex:1;min-height:0;overflow-y:auto;padding:14px;font-size:13px;line-height:1.7;white-space:pre-wrap;font-family:monospace;color:var(--t)">' + I18N.t('common.loading') + '</div>'
    + '</div>';
  document.body.appendChild(ov);
  ov.addEventListener('click', function(e) { if (e.target === ov) closeSubSkillModal(); });

  API.get('/skills/' + skillId + '/sub/' + subName).then(function(data) {
    var contentEl = document.getElementById('subSkillContent');
    if (contentEl) {
      var md = data.content || I18N.t('skills.huawei.no_content');
      var html = '<div style="background:#0b0e14;color:#c2cad6;padding:14px;border-radius:8px;font-size:12px;font-family:monospace;line-height:1.7;white-space:pre-wrap">' + _escHtml(md) + '</div>';
      // 如果有 references 文件
      if (data.references && data.references.length) {
        html += '<h3 style="font-size:12px;font-weight:600;margin:14px 0 8px">📎 ' + I18N.t('skills.huawei.reference_docs') + '</h3>';
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
    if (contentEl) contentEl.innerHTML = '<div style="color:var(--tr)">' + I18N.t('common.load_failed') + '</div>';
  });
}

function _escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function closeSubSkillModal() {
  var ov = document.getElementById('subSkillModal');
  if (ov) ov.remove();
}
