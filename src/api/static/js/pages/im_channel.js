/** IM 渠道配置页 */
function renderImChannel() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div id="imChannelBody" style="padding:0">
    <div style="text-align:center;color:var(--t3);padding:80px 0">${I18N.t('common.loading')}</div>
  </div>`;
  loadImChannelConfig();
}

let _imChannelLoaded = false;
let _imCurrentProvider = 'feishu';
let _imConfigData = null;

const _IM_PLATFORMS = {
  feishu: {
    name: 'Feishu', nameZh: '飞书',
    color: '#3370FF', bg: '#EBF1FF', bgLight: '#F5F8FF', gradient: 'linear-gradient(135deg,#3370FF,#5B8EFF)',
    desc: 'Lark / Feishu Open Platform',
    icon: '<img src="/static/img/feishu.svg" width="26" height="26" style="object-fit:contain">'
  },
  wecom: {
    name: 'WeCom', nameZh: '企业微信',
    color: '#07C160', bg: '#E8F8EE', bgLight: '#F4FCF6', gradient: 'linear-gradient(135deg,#07C160,#2DD879)',
    desc: 'WeCom / Enterprise WeChat',
    icon: '<img src="/static/img/wecom.svg" width="26" height="26" style="object-fit:contain">',
    disabled: true
  },
  dingtalk: {
    name: 'DingTalk', nameZh: '钉钉',
    color: '#1677FF', bg: '#E6F1FF', bgLight: '#F2F8FF', gradient: 'linear-gradient(135deg,#1677FF,#3D8BFF)',
    desc: 'DingTalk / DingTalk Open Platform',
    icon: '<img src="/static/img/dingtalk.svg" width="26" height="26" style="object-fit:contain">',
    disabled: true
  }
};

function loadImChannelConfig() {
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  API.get('/chat/config' + _qs).then(d => {
    _imConfigData = d;
    const platforms = d.im || {};
    if (platforms.dingtalk?.app_id) _imCurrentProvider = 'dingtalk';
    else if (platforms.wecom?.app_id) _imCurrentProvider = 'wecom';
    else _imCurrentProvider = 'feishu';
    renderImChannelBody(d);
    _imChannelLoaded = true;
  }).catch(() => {
    const body = document.getElementById('imChannelBody');
    if (body) body.innerHTML = `<div style="text-align:center;color:var(--r);padding:80px 0">${I18N.t('common.load_failed')}</div>`;
  });
}

function _imt(key) { return I18N.t('chat.im_channel.' + key); }

function renderImChannelBody(d) {
  const body = document.getElementById('imChannelBody');
  if (!body) return;
  const platforms = d.im || {};
  const cur = _IM_PLATFORMS[_imCurrentProvider];
  const curConfig = platforms[_imCurrentProvider] || {};
  const isConfigured = !!curConfig.app_id;
  const curName = I18N.getLang() === 'zh' ? cur.nameZh : cur.name;
  const backend = d.backend || {};
  const backendsOpts = (d._backends || []).map(b =>
    `<option value="${b.name}" data-rid="${b.runtime_id || ''}" ${b.name === (backend.type || '').toLowerCase() || b.name.startsWith((backend.type || '').toLowerCase() + '-') ? 'selected' : ''}>${(b.display_name || b.name) + (b.scope === 'local' ? ' (' + I18N.t('chat.local_direct') + ')' : '')}</option>`
  ).join('') || '<option value="">Default</option>';
  _chatSkillScope = (d.skill_scope === 'repo') ? 'repo' : (d.skill_scope === 'user') ? 'user' : 'official';

  const platformCards = Object.entries(_IM_PLATFORMS).map(([key, p]) => {
    const config = platforms[key] || {};
    const isActive = _imCurrentProvider === key;
    const configured = !!config.app_id;
    const dispName = I18N.getLang() === 'zh' ? p.nameZh : p.name;
    return `<div ${p.disabled ? '' : `onclick="selectImProvider('${key}')"`} style="padding:18px;border-radius:16px;border:2px solid ${isActive ? p.color : 'var(--b)'};background:${isActive ? p.bg : 'var(--c)'};transition:all .25s cubic-bezier(.25,.1,.25,1);position:relative;overflow:hidden;box-shadow:${isActive ? '0 8px 24px ' + p.color + '22' : 'var(--sh)'};${p.disabled ? 'opacity:.55;cursor:not-allowed' : 'cursor:pointer'}">
      ${isActive && !p.disabled ? `<div style="position:absolute;top:0;left:0;right:0;height:3px;background:${p.gradient}"></div>` : ''}
      ${p.disabled ? `<div style="position:absolute;top:10px;right:10px;padding:2px 8px;border-radius:10px;background:var(--bg);color:var(--t3);font-size:9px;font-weight:600;border:1px solid var(--b)">${_imt('coming_soon')}</div>` : ''}
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="width:44px;height:44px;border-radius:12px;background:${p.bgLight};display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:transform .25s;${p.disabled ? 'filter:grayscale(.6)' : ''}">${p.icon}</div>
        <div style="min-width:0;flex:1">
          <div style="font-size:14px;font-weight:700;color:${isActive && !p.disabled ? p.color : 'var(--t)'}">${dispName}</div>
          <div style="font-size:10px;color:var(--t3)">${p.name}</div>
        </div>
      </div>
      <div style="font-size:10px;color:var(--t3);margin-bottom:10px;line-height:1.4">${p.desc}</div>
      <div style="display:flex;align-items:center">
        ${p.disabled ? `<span style="font-size:10px;color:var(--t3)">—</span>` : configured
          ? `<span style="font-size:10px;color:var(--g);font-weight:600;display:flex;align-items:center;gap:4px;padding:3px 8px;background:#e8f8ee;border-radius:10px"><span style="width:5px;height:5px;border-radius:50%;background:var(--g)"></span>${_imt('connected')}</span>`
          : `<span style="font-size:10px;color:var(--t3);padding:3px 8px;background:var(--bg);border-radius:10px">${_imt('not_connected')}</span>`}
      </div>
    </div>`;
  }).join('');

  const _lbl = 'font-size:11px;color:var(--t2);display:block;margin-bottom:6px;font-weight:600;letter-spacing:.2px';
  const _inp = `width:100%;padding:11px 14px;border:1.5px solid var(--b);border-radius:10px;font-size:13px;background:var(--c);outline:none;transition:all .2s`;
  const _sel = `width:100%;padding:11px 14px;border:1.5px solid var(--b);border-radius:10px;font-size:13px;background:var(--c);outline:none;transition:all .2s;cursor:pointer`;

  body.innerHTML = `
    <!-- 顶部 Banner -->
    <div style="background:linear-gradient(135deg,${cur.bg},${cur.bgLight});border-radius:16px;padding:20px 24px;margin-bottom:20px;display:flex;align-items:center;gap:16px;border:1px solid ${cur.color}22">
      <div style="width:48px;height:48px;border-radius:14px;background:${cur.gradient};display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0;box-shadow:0 6px 16px ${cur.color}44">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      </div>
      <div style="flex:1">
        <h2 style="font-size:18px;font-weight:800;margin:0;letter-spacing:-.4px">${_imt('title')}</h2>
        <p style="font-size:12px;color:var(--t3);margin:4px 0 0">${_imt('subtitle')}</p>
      </div>
      ${isConfigured ? `<div style="padding:7px 16px;border-radius:20px;background:#fff;color:var(--g);font-size:11px;font-weight:700;display:flex;align-items:center;gap:6px;box-shadow:0 2px 8px #1ca44c1a"><span style="width:7px;height:7px;border-radius:50%;background:var(--g);box-shadow:0 0 0 3px #1ca44c33"></span>${_imt('active_status')}</div>` : ''}
    </div>

    <!-- 主体分栏 -->
    <div style="display:flex;gap:20px;align-items:flex-start">

      <!-- 左栏：平台选择 -->
      <div style="flex:0 0 300px;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:11px;font-weight:700;color:var(--t3);margin-bottom:2px;letter-spacing:.5px;text-transform:uppercase">${_imt('provider')}</div>
        ${platformCards}
      </div>

      <!-- 右栏：配置表单 -->
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px">

        <!-- IM 平台配置卡片 -->
        <div class="cd" style="padding:22px 24px;border-radius:16px;box-shadow:0 4px 16px rgba(0,0,0,.05)">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px;padding-bottom:16px;border-bottom:2px solid var(--bg)">
            <div style="width:36px;height:36px;border-radius:10px;background:${cur.bgLight};display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid ${cur.color}22">${cur.icon}</div>
            <div style="flex:1">
              <div style="font-size:14px;font-weight:700">${curName} ${_imt('config')}</div>
              <div style="font-size:11px;color:var(--t3)">${cur.desc}</div>
            </div>
            ${isConfigured ? `<div style="font-size:10px;color:var(--g);background:#e8f8ee;padding:4px 10px;border-radius:12px;font-weight:600;display:flex;align-items:center;gap:3px"><span style="width:5px;height:5px;border-radius:50%;background:var(--g)"></span>${_imt('connected')}</div>` : ''}
          </div>

          <div style="display:flex;gap:14px;margin-bottom:18px">
            <div style="flex:1">
              <label style="${_lbl}">${I18N.t('chat.app_id')}</label>
              <input id="cfgImAppId" placeholder="${cur.name} App ID" value="${curConfig.app_id || ''}" style="${_inp}" onfocus="this.style.borderColor='${cur.color}';this.style.boxShadow='0 0 0 3px ${cur.color}1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">
            </div>
            <div style="flex:1">
              <label style="${_lbl}">${I18N.t('chat.app_secret')}</label>
              <input id="cfgImSecret" type="password" placeholder="${cur.name} App Secret" value="${curConfig.app_secret_exists ? '********' : ''}" style="${_inp}" onfocus="this.style.borderColor='${cur.color}';this.style.boxShadow='0 0 0 3px ${cur.color}1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">
            </div>
          </div>

          <div style="display:flex;gap:10px">
            <button class="bt bt-o" onclick="startFeishuOnboard('chat_bot')" style="padding:10px 20px;font-size:12px;border-radius:10px;border-width:1.5px;display:flex;align-items:center;gap:6px;font-weight:600">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 7h10M7 12h6M7 17h4"/></svg>
              ${I18N.t('chat.scan_config')}
            </button>
            <button class="bt bt-p" onclick="saveImChannelConfig()" style="padding:10px 20px;font-size:12px;border-radius:10px;display:flex;align-items:center;gap:6px;font-weight:600;box-shadow:0 4px 12px var(--a)33">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
              ${I18N.t('common.save')}
            </button>
          </div>
        </div>

        <!-- 模型与技能配置 -->
        <div class="cd" style="padding:22px 24px;border-radius:16px;box-shadow:0 4px 16px rgba(0,0,0,.05)">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px;padding-bottom:16px;border-bottom:2px solid var(--bg)">
            <div style="width:36px;height:36px;border-radius:10px;background:var(--bg);display:flex;align-items:center;justify-content:center;flex-shrink:0">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--t2)" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </div>
            <div>
              <div style="font-size:14px;font-weight:700">${I18N.t('chat.engine_skill_title')}</div>
              <div style="font-size:11px;color:var(--t3)">${I18N.t('chat.engine_skill_desc')}</div>
            </div>
          </div>

          <div style="margin-bottom:16px">
            <label style="${_lbl}">${I18N.t('chat.engine_label')}</label>
            <select id="cfgBackend" style="${_sel}" onfocus="this.style.borderColor='var(--a)';this.style.boxShadow='0 0 0 3px var(--a)1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">${backendsOpts}</select>
          </div>

          <div>
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
              <label style="font-size:11px;color:var(--t2);font-weight:600;flex-shrink:0;letter-spacing:.2px">${I18N.t('chat.skill_label')}</label>
              <select id="cfgSkillScope" onchange="_switchChatSkillScope(this.value)" style="flex:0 0 78px;max-width:78px;padding:6px 8px;border:1.5px solid var(--b);border-radius:8px;font-size:11px;background:var(--bg);outline:none;cursor:pointer;transition:all .2s" onfocus="this.style.borderColor='var(--a)'" onblur="this.style.borderColor='var(--b)'">
                <option value="official">${I18N.t('chat.scope_short.global')}</option>
                <option value="repo">${I18N.t('chat.scope_short.repo')}</option>
                <option value="user">${I18N.t('chat.scope_short.user')}</option>
              </select>
            </div>
            <select id="cfgSkill" data-skill-id="${d.skill_id || 0}" style="${_sel}" onfocus="this.style.borderColor='var(--a)';this.style.boxShadow='0 0 0 3px var(--a)1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">
              <option value="0">${I18N.t('chat.skill.default_official')}</option>
            </select>
          </div>
        </div>

        <!-- 说明卡片 -->
        <div style="padding:14px 18px;font-size:11px;color:var(--t3);line-height:1.7;border-radius:12px;border:1px solid ${cur.color}22;background:${cur.bgLight};display:flex;gap:10px;align-items:flex-start">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="${cur.color}" stroke-width="2" style="flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
          <div>
            <div style="font-weight:700;margin-bottom:3px;color:var(--t2)">${_imt('help_title')}</div>
            <div>${_imt('help_desc')}</div>
          </div>
        </div>
      </div>
    </div>`;

  const scopeSel = document.getElementById('cfgSkillScope');
  if (scopeSel) scopeSel.value = _chatSkillScope;
  _loadChatSkillOptions();
}

function selectImProvider(provider) {
  _imCurrentProvider = provider;
  if (_imConfigData) renderImChannelBody(_imConfigData);
}

function saveImChannelConfig() {
  const provider = _imCurrentProvider;
  const appId = document.getElementById('cfgImAppId')?.value || '';
  const rawSecret = document.getElementById('cfgImSecret')?.value || '';
  const secret = rawSecret === '********' ? '' : rawSecret;
  const im = {};
  im[provider] = {app_id: appId, app_secret: secret};
  const _bkSel = document.getElementById('cfgBackend');
  const backend = _bkSel?.value || '';
  const rid = _bkSel?.selectedOptions?.[0]?.dataset?.rid || '';
  const skillVal = document.getElementById('cfgSkill')?.value || '0';
  const skillId = parseInt(skillVal) || 0;
  if (_chatSkillScope !== 'official' && !skillId) {
    if (app && app.showToast) app.showToast(I18N.t('chat.skill.select_required'));
    return;
  }
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  const _body = {backend, skill_id: skillId, im};
  if (rid) _body.runtime_id = parseInt(rid, 10);
  API.put('/chat/config' + _qs, _body).then(() => {
    app.showToast(I18N.t('chat.config.saved'));
    loadImChannelConfig();
  }).catch(e => app.showToast(I18N.t('chat.config.save_failed', {msg: e.message})));
}
