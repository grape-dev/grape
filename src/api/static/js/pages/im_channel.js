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
    nameKey: 'platform_feishu', code: 'Feishu',
    color: '#3370FF', bg: '#EBF1FF', bgLight: '#F5F8FF', gradient: 'linear-gradient(135deg,#3370FF,#5B8EFF)',
    descKey: 'desc_feishu',
    icon: '<img src="/static/img/feishu.svg" width="26" height="26" style="object-fit:contain">'
  },
  wecom: {
    nameKey: 'platform_wecom', code: 'WeCom',
    color: '#07C160', bg: '#E8F8EE', bgLight: '#F4FCF6', gradient: 'linear-gradient(135deg,#07C160,#2DD879)',
    descKey: 'desc_wecom',
    icon: '<img src="/static/img/wecom.svg" width="26" height="26" style="object-fit:contain">'
  },
  dingtalk: {
    nameKey: 'platform_dingtalk', code: 'DingTalk',
    color: '#1677FF', bg: '#E6F1FF', bgLight: '#F2F8FF', gradient: 'linear-gradient(135deg,#1677FF,#3D8BFF)',
    descKey: 'desc_dingtalk',
    icon: '<img src="/static/img/dingtalk.svg" width="26" height="26" style="object-fit:contain">'
  },
  // Issue #1309：Slack（Socket Mode 长连接 + Web API 双令牌）
  slack: {
    nameKey: 'platform_slack', code: 'Slack',
    color: '#4A154B', bg: '#F6EFF7', bgLight: '#FAF5FB', gradient: 'linear-gradient(135deg,#4A154B,#E01E5A)',
    descKey: 'desc_slack',
    icon: '<img src="/static/img/slack.svg" width="26" height="26" style="object-fit:contain">'
  },
  // Issue #1613：Telegram（单 Bot Token + getUpdates 长轮询，零公网）
  telegram: {
    nameKey: 'platform_telegram', code: 'Telegram',
    color: '#0088CC', bg: '#E6F4FA', bgLight: '#F2FAFD', gradient: 'linear-gradient(135deg,#0088CC,#29B6E8)',
    descKey: 'desc_telegram',
    icon: '<img src="/static/img/telegram.svg" width="26" height="26" style="object-fit:contain">'
  },
  // Issue #1728：微信 ilink Bot（ClawBot）——扫码绑定 + 长轮询
  wechat: {
    nameKey: 'platform_wechat', code: 'WeChat',
    color: '#07C160', bg: '#E8F8EE', bgLight: '#F4FCF6', gradient: 'linear-gradient(135deg,#07C160,#2DD879)',
    descKey: 'desc_wechat',
    icon: '<img src="/static/img/wechat.svg" width="26" height="26" style="object-fit:contain">'
  }
};

/** Slack App Manifest（Issue #1309 接入引导）：粘贴到 api.slack.com/apps 的
 *  Create New App → From an app manifest，可一次配齐 scopes / 事件 / Socket Mode。
 *  仅前端展示与复制，运行时不依赖；改清单需同步 bump index.html 的 ?v=。 */
const SLACK_APP_MANIFEST = JSON.stringify({
  display_information: {
    name: 'grape-devops',
    description: 'Grape DevOps chat agent (Socket Mode)',
    background_color: '#2c1f33'
  },
  features: {
    bot_user: { display_name: 'grape', always_online: true },
    app_home: { home_tab_enabled: false, messages_tab_enabled: true, messages_tab_read_only_enabled: false }
  },
  oauth_config: {
    scopes: {
      bot: [
        'app_mentions:read', 'channels:history', 'channels:read', 'groups:history',
        'groups:read', 'im:history', 'im:read', 'im:write', 'mpim:history',
        'mpim:read', 'chat:write', 'files:read', 'files:write', 'reactions:write',
        'users:read'
      ]
    }
  },
  settings: {
    event_subscriptions: {
      bot_events: ['app_mention', 'message.channels', 'message.groups', 'message.im', 'message.mpim']
    },
    interactivity: { is_enabled: true },
    org_deploy_enabled: false,
    socket_mode_enabled: true,
    token_rotation_enabled: false
  }
}, null, 2);

function copySlackManifest() {
  const txt = SLACK_APP_MANIFEST;
  const done = () => app.showToast(_imt('copy_ok'));
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done).catch(() => _copyFallback(txt, done));
  } else {
    _copyFallback(txt, done);
  }
}

/** 老浏览器无 Clipboard API 时的兜底：临时 textarea + execCommand。 */
function _copyFallback(txt, done) {
  const ta = document.createElement('textarea');
  ta.value = txt;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); }
  catch (e) { app.showToast(_imt('copy_fail')); }
  document.body.removeChild(ta);
}

function loadImChannelConfig() {
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  API.get('/chat/config' + _qs).then(d => {
    _imConfigData = d;
    // 主人 2026-11-10：进页面默认选中飞书；只在首次进入时定位，保存后刷新不把选择跳走
    if (!_imChannelLoaded) _imCurrentProvider = 'feishu';
    renderImChannelBody(d);
    _imChannelLoaded = true;
    _imRefreshStatus();
  }).catch(() => {
    const body = document.getElementById('imChannelBody');
    if (body) body.innerHTML = `<div style="text-align:center;color:var(--r);padding:80px 0">${I18N.t('common.load_failed')}</div>`;
  });
}

function _imt(key, vars) { return I18N.t('chat.im_channel.' + key, vars); }

/** 凭据字段占位提示：各渠道口令名不同（飞书 App ID / 钉钉 AppKey / 企微 Bot ID / Slack 双令牌）。 */
function _imCredHint(defaultName) {
  const f = defaultName === 'App ID' ? 'app_id' : 'secret';
  const k = `${_imCurrentProvider}_${f}`;
  if (['dingtalk_app_id','dingtalk_secret','wecom_app_id','wecom_secret',
       'telegram_app_id','wechat_app_id','wechat_secret'].indexOf(k) >= 0) return _imt('hint_' + k);
  if (['slack_app_id','slack_secret'].indexOf(k) >= 0) return _imt('label_' + k);
  return defaultName;
}

/** 凭据字段标题（Issue #1309：Slack 双令牌，一栏 Bot Token、一栏 App Token）。 */
function _imCredLabel(defaultName) {
  const f = defaultName === 'App ID' ? 'app_id' : 'secret';
  const k = `${_imCurrentProvider}_${f}`;
  if (_imCurrentProvider === 'wecom' || _imCurrentProvider === 'slack'
      || _imCurrentProvider === 'telegram' || _imCurrentProvider === 'wechat') {
    return _imt('label_' + k);
  }
  if (_imCurrentProvider === 'dingtalk') return _imt('label_' + k);
  return I18N.t(defaultName === 'App ID' ? 'chat.app_id' : 'chat.app_secret');
}

/** 各渠道「首次接入」引导：平台入口 + 4 步可照做 + 一句关键提醒。
 *  主人 2026-11-10：飞书/企微/钉钉原先完全没有引导，Slack/Telegram/微信的说明太碎看不懂，统一重写。 */
const _IM_GUIDES = {
  feishu: {
    url: 'https://open.feishu.cn/app', urlLabelKey: 'guide_url_feishu',
    steps: ['guide_feishu_s1', 'guide_feishu_s2', 'guide_feishu_s3', 'guide_feishu_s4'],
    tipKey: 'guide_feishu_tip'
  },
  wecom: {
    url: 'https://work.weixin.qq.com/', urlLabelKey: 'guide_url_wecom',
    steps: ['guide_wecom_s1', 'guide_wecom_s2', 'guide_wecom_s3', 'guide_wecom_s4'],
    tipKey: 'guide_wecom_tip'
  },
  dingtalk: {
    url: 'https://open-dev.dingtalk.com/', urlLabelKey: 'guide_url_dingtalk',
    steps: ['guide_dingtalk_s1', 'guide_dingtalk_s2', 'guide_dingtalk_s3', 'guide_dingtalk_s4'],
    tipKey: 'guide_dingtalk_tip'
  },
  slack: {
    url: 'https://api.slack.com/apps', urlLabelKey: 'guide_url_slack', extra: 'manifest',
    steps: ['guide_slack_s1', 'guide_slack_s2', 'guide_slack_s3', 'guide_slack_s4'],
    tipKey: 'guide_slack_tip'
  },
  telegram: {
    url: 'https://t.me/BotFather', urlLabelKey: 'guide_url_telegram',
    steps: ['guide_telegram_s1', 'guide_telegram_s2', 'guide_telegram_s3', 'guide_telegram_s4'],
    tipKey: 'guide_telegram_tip'
  },
  wechat: {
    url: '', urlLabelKey: '',
    steps: ['guide_wechat_s1', 'guide_wechat_s2', 'guide_wechat_s3', 'guide_wechat_s4'],
    tipKey: 'guide_wechat_tip'
  }
};

function _imGuide(cur) {
  const g = _IM_GUIDES[_imCurrentProvider];
  if (!g) return '';
  const ol = g.steps.map(s => `<li>${_imt(s)}</li>`).join('');
  const link = g.url
    ? `<a href="${g.url}" target="_blank" rel="noopener" class="bt bt-o" style="padding:6px 12px;font-size:10.5px;border-radius:9px;border-width:1.5px;display:inline-flex;align-items:center;gap:5px;font-weight:600;flex-shrink:0;text-decoration:none">${_imt(g.urlLabelKey)}</a>`
    : '';
  const manifest = g.extra === 'manifest' ? `
    <button class="bt bt-o" onclick="copySlackManifest()" style="padding:6px 12px;font-size:10.5px;border-radius:9px;border-width:1.5px;display:inline-flex;align-items:center;gap:5px;font-weight:600;flex-shrink:0">${_imt('guide_manifest_btn')}</button>
    <details style="margin-top:8px">
      <summary style="cursor:pointer;font-size:10px;color:var(--t3);user-select:none">${_imt('guide_manifest_show')}</summary>
      <textarea readonly onclick="this.select()" spellcheck="false" style="width:100%;height:140px;margin-top:6px;font-size:10px;line-height:1.5;font-family:ui-monospace,Menlo,monospace;border-radius:8px;border:1px solid var(--b);background:var(--c);color:var(--t2);padding:8px;resize:vertical;outline:none">${SLACK_APP_MANIFEST}</textarea>
    </details>` : '';
  return `<div style="margin-bottom:16px;padding:14px 16px;border-radius:12px;background:${cur.bgLight};border:1px solid ${cur.color}22">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <div style="font-size:11px;font-weight:700;color:${cur.color};display:flex;align-items:center;gap:6px">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
        ${_imt('guide_title', {n: g.steps.length})}
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${manifest}${link}</div>
    </div>
    <ol style="margin:0;padding-left:16px;font-size:10.5px;color:var(--t3);line-height:1.9">${ol}</ol>
    <div style="margin-top:8px;padding-top:8px;border-top:1px dashed ${cur.color}33;font-size:10px;color:var(--t3);line-height:1.7">💡 ${_imt(g.tipKey)}</div>
  </div>`;
}

function renderImChannelBody(d) {
  const body = document.getElementById('imChannelBody');
  if (!body) return;
  const platforms = d.im || {};
  const cur = _IM_PLATFORMS[_imCurrentProvider];
  const curConfig = platforms[_imCurrentProvider] || {};
  const isConfigured = !!curConfig.app_id;
  const curName = _imt(cur.nameKey);

  const platformCards = _IM_ORDER.map(key => {
    const p = _IM_PLATFORMS[key];
    if (!p) return '';
    const config = platforms[key] || {};
    const isActive = _imCurrentProvider === key;
    const dispName = _imt(p.nameKey);
    return `<div ${p.disabled ? '' : `onclick="selectImProvider('${key}')"`} style="padding:18px;border-radius:16px;border:2px solid ${isActive ? p.color : 'var(--b)'};background:${isActive ? p.bg : 'var(--c)'};transition:all .25s cubic-bezier(.25,.1,.25,1);position:relative;overflow:hidden;box-shadow:${isActive ? '0 8px 24px ' + p.color + '22' : 'var(--sh)'};${p.disabled ? 'opacity:.55;cursor:not-allowed' : 'cursor:pointer'}">
      ${isActive && !p.disabled ? `<div style="position:absolute;top:0;left:0;right:0;height:3px;background:${p.gradient}"></div>` : ''}
      ${p.disabled ? `<div style="position:absolute;top:10px;right:10px;padding:2px 8px;border-radius:10px;background:var(--bg);color:var(--t3);font-size:9px;font-weight:600;border:1px solid var(--b)">${_imt('coming_soon')}</div>` : ''}
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="width:44px;height:44px;border-radius:12px;background:${p.bgLight};display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:transform .25s;${p.disabled ? 'filter:grayscale(.6)' : ''}">${p.icon}</div>
        <div style="min-width:0;flex:1">
          <div style="font-size:14px;font-weight:700;color:${isActive && !p.disabled ? p.color : 'var(--t)'}">${dispName}</div>
          <div style="font-size:10px;color:var(--t3)">${p.code || ''}</div>
        </div>
      </div>
      <div style="font-size:10px;color:var(--t3);margin-bottom:10px;line-height:1.4">${_imt(p.descKey)}</div>
      <div style="display:flex;align-items:center">
        ${p.disabled ? `<span style="font-size:10px;color:var(--t3)">—</span>` : _imStateBadge(key, config)}
      </div>
    </div>`;
  }).join('');

  const _lbl = 'font-size:11px;color:var(--t2);display:block;margin-bottom:6px;font-weight:600;letter-spacing:.2px';
  const _inp = `width:100%;padding:11px 14px;border:1.5px solid var(--b);border-radius:10px;font-size:13px;background:var(--c);outline:none;transition:all .2s`;

  body.innerHTML = `
    <!-- top banner -->
    <div style="background:linear-gradient(135deg,${cur.bg},${cur.bgLight});border-radius:16px;padding:20px 24px;margin-bottom:20px;display:flex;align-items:center;gap:16px;border:1px solid ${cur.color}22">
      <div style="width:48px;height:48px;border-radius:14px;background:${cur.gradient};display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0;box-shadow:0 6px 16px ${cur.color}44">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      </div>
      <div style="flex:1">
        <h2 style="font-size:18px;font-weight:800;margin:0;letter-spacing:-.4px">${_imt('title')}</h2>
        <p style="font-size:12px;color:var(--t3);margin:4px 0 0">${_imt('subtitle')}</p>
      </div>
      ${isConfigured ? `<div style="padding:7px 16px;border-radius:20px;background:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;gap:6px;box-shadow:0 2px 8px #1ca44c1a">${_imStateBadge(_imCurrentProvider, curConfig)}</div>` : ''}
    </div>

    <!-- main columns -->
    <div style="display:flex;gap:20px;align-items:flex-start">

      <!-- left: platform picker -->
      <div style="flex:0 0 300px;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:11px;font-weight:700;color:var(--t3);margin-bottom:2px;letter-spacing:.5px;text-transform:uppercase">${_imt('provider')}</div>
        ${platformCards}
      </div>

      <!-- right: config form -->
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px">

        <!-- platform config card -->
        <div class="cd" style="padding:22px 24px;border-radius:16px;box-shadow:0 4px 16px rgba(0,0,0,.05)">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px;padding-bottom:16px;border-bottom:2px solid var(--bg)">
            <div style="width:36px;height:36px;border-radius:10px;background:${cur.bgLight};display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid ${cur.color}22">${cur.icon}</div>
            <div style="flex:1">
              <div style="font-size:14px;font-weight:700">${curName} ${_imt('config')}</div>
              <div style="font-size:11px;color:var(--t3)">${_imt(cur.descKey)}</div>
            </div>
            ${isConfigured ? `<div style="display:flex;align-items:center">${_imStateBadge(_imCurrentProvider, curConfig)}</div>` : ''}
          </div>

          ${_imGuide(cur)}
          <div style="display:flex;gap:14px;margin-bottom:18px">
            <div style="flex:1">
              <label style="${_lbl}">${_imCredLabel('App ID')}</label>
              <input id="cfgImAppId" type="${(_imCurrentProvider === 'slack' || _imCurrentProvider === 'telegram') ? 'password' : 'text'}" placeholder="${_imt(cur.nameKey)} ${_imCredHint('App ID')}" value="${curConfig.app_id || ''}" style="${_inp}" onfocus="this.style.borderColor='${cur.color}';this.style.boxShadow='0 0 0 3px ${cur.color}1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">
            </div>
            ${_imCurrentProvider === 'telegram' ? '' : `
            <div style="flex:1">
              <label style="${_lbl}">${_imCredLabel('App Secret')}</label>
              <input id="cfgImSecret" type="password" placeholder="${_imt(cur.nameKey)} ${_imCredHint('App Secret')}" value="${curConfig.app_secret_exists ? '********' : ''}" style="${_inp}" onfocus="this.style.borderColor='${cur.color}';this.style.boxShadow='0 0 0 3px ${cur.color}1a'" onblur="this.style.borderColor='var(--b)';this.style.boxShadow='none'">
            </div>`}
          </div>

          <div style="display:flex;gap:10px">
            ${_imCurrentProvider === 'feishu' ? `<button class="bt bt-o" onclick="startFeishuOnboard('chat_bot')" style="padding:10px 20px;font-size:12px;border-radius:10px;border-width:1.5px;display:flex;align-items:center;gap:6px;font-weight:600">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 7h10M7 12h6M7 17h4"/></svg>
              ${I18N.t('chat.scan_config')}
            </button>` : ''}
            ${_imCurrentProvider === 'wechat' ? `<button class="bt bt-o" onclick="startWechatLogin()" style="padding:10px 20px;font-size:12px;border-radius:10px;border-width:1.5px;display:flex;align-items:center;gap:6px;font-weight:600">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 7h10M7 12h6M7 17h4"/></svg>
              ${_imt('btn_scan_wechat')}
            </button>` : ''}
            <button class="bt bt-p" onclick="saveImChannelConfig()" style="padding:10px 20px;font-size:12px;border-radius:10px;display:${_imCurrentProvider === 'wechat' ? 'none' : 'flex'};align-items:center;gap:6px;font-weight:600;box-shadow:0 4px 12px var(--a)33">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
              ${I18N.t('common.save')}
            </button>
            ${isConfigured ? `<button class="bt bt-r" onclick="unbindImChannelConfig()" style="padding:10px 20px;font-size:12px;border-radius:10px;display:flex;align-items:center;gap:6px;font-weight:600">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 15l6-6"/><path d="M11 6l1-1a4.24 4.24 0 016 6l-1 1"/><path d="M13 18l-1 1a4.24 4.24 0 01-6-6l1-1"/></svg>
              ${_imt('unbind')}
            </button>` : ''}
          </div>
        </div>

        <!-- help card -->
        <div style="padding:14px 18px;font-size:11px;color:var(--t3);line-height:1.7;border-radius:12px;border:1px solid ${cur.color}22;background:${cur.bgLight};display:flex;gap:10px;align-items:flex-start">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="${cur.color}" stroke-width="2" style="flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
          <div>
            <div style="font-weight:700;margin-bottom:3px;color:var(--t2)">${_imt('help_title')}</div>
            <div>${_imt('help_desc')}</div>
          </div>
        </div>
      </div>
    </div>`;
}

/** 卡片展示顺序（主人 2026-11-10 定）：飞书打头（进入即默认选中），微信排在企微上面。 */
const _IM_ORDER = ['feishu', 'wechat', 'wecom', 'dingtalk', 'slack', 'telegram'];

/** 真连接状态（来自 /api/im/installations 的 Supervisor 活渠道），channel_type → {connected,status} */
let _imLiveByType = {};

/** 卡片徽标：已连接（引擎真的连着）＞已配置·未连接＞已配置·已停用＞未接入。 */
function _imStateBadge(key, config) {
  const live = _imLiveByType[key] || null;
  let txt, color, bg;
  if (live && live.connected) { txt = _imt('badge_connected'); color = 'var(--g)'; bg = '#e8f8ee'; }
  else if (live && live.status === 'active') { txt = _imt('badge_configured_offline'); color = 'var(--y)'; bg = '#fdf6e8'; }
  else if (live && live.status === 'disabled') { txt = _imt('badge_disabled'); color = 'var(--t3)'; bg = 'var(--bg)'; }
  else if (config && config.app_id) { txt = _imt('badge_configured'); color = 'var(--t3)'; bg = 'var(--bg)'; }
  else { txt = _imt('badge_none'); color = 'var(--t3)'; bg = 'var(--bg)'; }
  const dot = (live && live.connected)
    ? '<span style="width:5px;height:5px;border-radius:50%;background:var(--g)"></span>' : '';
  return `<span style="font-size:10px;color:${color};font-weight:600;display:inline-flex;align-items:center;gap:4px;padding:3px 8px;background:${bg};border-radius:10px">${dot}${txt}</span>`;
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
  // Issue #956：新载荷形态 im={provider, app_id, app_secret}（后端兼容旧子 dict 形态）
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  const _body = {im: {provider, app_id: appId, app_secret: secret}};
  // 失败只 toast 不重载：已录入/已存凭证保留在表单中，不因保存失败被清空
  API.put('/chat/config' + _qs, _body).then(() => {
    app.showToast(I18N.t('chat.config.saved'));
    loadImChannelConfig();
  }).catch(e => app.showToast(I18N.t('chat.config.save_failed', {msg: e && e.message ? e.message : ''})));
}

/** 解绑当前渠道（Issue #294，#956 provider 化）：置空 app_id 走既有 PUT 语义，后端按渠道同步清理。 */
function unbindImChannelConfig() {
  app.showConfirm(_imt('unbind_confirm'), function() {
    const provider = _imCurrentProvider;
    const _repo = app.currentRepo();
    const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
    const _body = {im: {provider, app_id: '', app_secret: ''}};
    API.put('/chat/config' + _qs, _body).then(() => {
      app.showToast(_imt('unbind_success'));
      loadImChannelConfig();
    }).catch(e => app.showToast(_imt('unbind_failed', {msg: e && e.message ? e.message : ''})));
  }, {danger: true});
}

/* ============================================================
 * Issue #825：新 IM 引擎面板
 * 数据源是 channel_installation + Supervisor（旧 legacy 双轨表只做租约/迁移兼容）：
 *   - 总开关 on/off（写 data/im_engine_mode，5 秒内生效，不用重启）
 *   - 已连接渠道数 / 安装统计
 *   - 每条安装的引擎侧启停（active / disabled）
 * 配置的保存/解绑仍走原接口（A2 M2 起直写 channel_installation，不再双写），这里只做"引擎视角"的观测与控制。
 * ============================================================ */

function _imEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function _imRepoFullName() {
  const r = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  return r ? r.fullName : '';
}

/** 拉真实连接状态，只用于卡片徽标（正式界面不展示引擎运维面板）。 */
function _imRefreshStatus() {
  const repo = _imRepoFullName();
  const qs = repo ? '?repo=' + encodeURIComponent(repo) : '';
  API.get('/im/installations' + qs)
    .then(list => {
      const changed = _imApplyLive(list || {});
      if (changed && _imConfigData) renderImChannelBody(_imConfigData);
    })
    .catch(() => { /* status read failed: badges fall back to "not connected" */ });
}

/** 把 /api/im/installations 的活渠道状态落到卡片徽标；返回是否有变化（变了才重绘画）。 */
function _imApplyLive(list) {
  const map = {};
  (list.installations || []).forEach(it => {
    if (!it || it.status === 'revoked') return;
    map[it.channel_type] = { connected: !!it.connected, status: it.status };
  });
  const changed = JSON.stringify(map) !== JSON.stringify(_imLiveByType);
  _imLiveByType = map;
  return changed;
}

// ===== 微信 ilink 扫码绑定（Issue #1728 §5.1 / M4）=====
let _wechatLoginId = null;
let _wechatLoginTimer = null;
let _wechatVerifyCode = '';

function _wechatQrSrc(content) {
  const raw = (content || '').trim();
  if (!raw) return '';
  // 只有 data URI 或长 base64 才是图片本身；微信 ilink 返回的 https://liteapp.weixin.qq.com/…
  // 是一个 text/html 的二维码页面，塞进 <img> 只会破图，必须本地用 QRCode 库编码成二维码。
  if (raw.startsWith('data:image/')) return raw;
  if (raw.length > 120 && !/^https?:\/\//i.test(raw)) return 'data:image/png;base64,' + raw;
  return '';
}

let _wechatLastQr = '';

function _wechatPaintQr(content) {
  const box = document.getElementById('wechatQrBox');
  if (!box) return;
  const raw = (content || '').trim();
  if (!raw) return;
  const src = _wechatQrSrc(raw);
  if (src) {
    box.innerHTML = `<img src="${_imEsc(src)}" style="width:200px;height:200px;object-fit:contain">`;
    return;
  }
  if (raw === _wechatLastQr && box.querySelector('canvas, img')) return;
  _wechatLastQr = raw;
  box.innerHTML = '';
  if (typeof QRCode === 'function') {
    const holder = document.createElement('div');
    box.appendChild(holder);
    new QRCode(holder, { text: raw, width: 200, height: 200, correctLevel: QRCode.CorrectLevel.M });
  } else {
    box.innerHTML = `<div style="font-size:11px;color:#888;word-break:break-all;padding:10px">${_imEsc(raw)}</div>`;
  }
}

function startWechatLogin() {
  API.post('/chat/im/wechat/login/start', {}).then(res => {
    _wechatLoginId = res.login_id;
    _wechatVerifyCode = '';
    _wechatLastQr = '';
    _showWechatLoginModal(res.qrcode_img_content, res.expires_in);
    _pollWechatLogin();
  }).catch(e => app.showToast(_imt('err_qr_failed') + (e && e.message)));
}

function _showWechatLoginModal(qrContent, expiresIn) {
  let modal = document.getElementById('wechatLoginModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'wechatLoginModal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center';
    document.body.appendChild(modal);
  }
  modal.innerHTML = `<div style="background:#fff;border-radius:16px;padding:24px;width:340px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.2)">
    <div style="font-size:15px;font-weight:800;margin-bottom:6px">${_imt('wechat_modal_title')}</div>
    <div style="font-size:11px;color:#888;margin-bottom:14px;line-height:1.6">${_imt('wechat_modal_desc', {sec: expiresIn || 600})}</div>
    <div id="wechatQrBox" style="min-height:180px;display:flex;align-items:center;justify-content:center;margin-bottom:14px"></div>
    <div id="wechatPinBox" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:#888;margin-bottom:6px">${_imt('wechat_pin_hint')}</div>
      <input id="wechatPinInput" inputmode="numeric" style="width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:9px;font-size:14px;text-align:center;outline:none" placeholder="${_imt('wechat_pin_placeholder')}">
    </div>
    <div id="wechatLoginStatus" style="font-size:11px;color:#888;margin-bottom:12px">${_imt('wechat_wait')}</div>
    <div style="display:flex;gap:10px;justify-content:center">
      <button class="bt bt-p" onclick="_submitWechatPin()" id="wechatPinSubmit" style="display:none;padding:8px 18px;border-radius:9px">${_imt('wechat_pin_submit')}</button>
      <button class="bt bt-o" onclick="closeWechatLogin()" style="padding:8px 18px;border-radius:9px">${_imt('wechat_unbind_btn')}</button>
    </div>
  </div>`;
  _wechatPaintQr(qrContent);
}

function _submitWechatPin() {
  const input = document.getElementById('wechatPinInput');
  if (!input || !input.value.trim()) return;
  _wechatVerifyCode = input.value.trim();
  _pollWechatLogin();
}

function _pollWechatLogin() {
  if (!_wechatLoginId) return;
  clearTimeout(_wechatLoginTimer);
  const qs = '/chat/im/wechat/login/status?id=' + encodeURIComponent(_wechatLoginId)
    + (_wechatVerifyCode ? '&verify_code=' + encodeURIComponent(_wechatVerifyCode) : '');
  API.get(qs).then(res => {
    const statusEl = document.getElementById('wechatLoginStatus');
    const pinBox = document.getElementById('wechatPinBox');
    const pinBtn = document.getElementById('wechatPinSubmit');
    if (statusEl) statusEl.textContent = ({wait: _imt('wechat_wait'), scaned: _imt('wechat_scaned'), need_verifycode: _imt('wechat_need_pin'), confirmed: _imt('wechat_confirmed'), expired: _imt('wechat_expired')})[res.status] || res.status || '';
    if (res.status === 'need_verifycode') {
      if (pinBox) pinBox.style.display = 'block';
      if (pinBtn) pinBtn.style.display = 'inline-block';
    } else {
      if (pinBox) pinBox.style.display = 'none';
      if (pinBtn) pinBtn.style.display = 'none';
    }
    if (res.qrcode_img_content) _wechatPaintQr(res.qrcode_img_content);
    if (res.status === 'confirmed') {
      _wechatVerifyCode = '';
      closeWechatLogin();
      app.showToast(_imt('wechat_success_toast'));
      setTimeout(() => { if (typeof loadImChannelConfig === 'function') loadImChannelConfig(); }, 1500);
      return;
    }
    if (res.status === 'expired' || res.status === 'verify_code_blocked') {
      const msg = document.getElementById('wechatLoginStatus');
      if (msg) msg.textContent = res.message || _imt('wechat_expired_toast');
      return;
    }
    _wechatLoginTimer = setTimeout(_pollWechatLogin, 2000);
  }).catch(() => {
    _wechatLoginTimer = setTimeout(_pollWechatLogin, 3000);
  });
}

function closeWechatLogin() {
  clearTimeout(_wechatLoginTimer);
  _wechatLoginTimer = null;
  _wechatLoginId = null;
  _wechatVerifyCode = '';
  const modal = document.getElementById('wechatLoginModal');
  if (modal) modal.remove();
}
