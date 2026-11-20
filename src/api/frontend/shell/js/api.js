var _AGENT_TYPES = {};  // 全局 Agent 类型缓存（由 app.js 填充）

// 全局工具：UTC ISO 时间字符串（后端 normalize_event_time 归一化，带 +00:00 偏移）转本地展示。
// 支持 'YYYY-MM-DD HH:MM:SS'（视为 UTC，SQLite 语义兜底）、ISO 毫秒 + 时区偏移（±HH:MM / Z）。
function fmtLocalTime(utcStr) {
  if (!utcStr) return '';
  let s = String(utcStr).replace('T', ' ').replace('Z', '');
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-]\d{2}:\d{2})?/);
  if (!m) return utcStr;
  let ms = Date.UTC(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]);
  if (m[7]) {
    // 带偏移：把「本地假想时刻」还原成绝对 UTC 时间点（+08:00 减 8h，-05:00 加 5h）
    const sign = m[7][0] === '-' ? 1 : -1;
    const oh = parseInt(m[7].slice(1, 3), 10);
    const om = parseInt(m[7].slice(4, 6), 10);
    ms += sign * (oh * 3600 + om * 60) * 1000;
  }
  const d = new Date(ms);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 全局工具：任意时间输入（秒/毫秒时间戳、UTC ISO、带 ±HH:MM 偏移字符串）→ 固定东八区（UTC+8）
// 的 'YYYY-MM-DD HH:mm:ss'。Issue #1477：API Key「最近使用时间」展示口径——固定时区，跨环境一致。
// - 0/负值/空 → ''（占位由调用方兜底为 '—'，避免 0 被格式化成 1970-01-01）
// - 数字/纯数字字符串按量级自动判别秒（< 1e11）或毫秒
// - 字符串先按带偏移/UTC 语义换算为绝对时刻，再用 Intl 固定 Asia/Shanghai 取分量
// - 解析失败 → 原样返回（不破坏数据、不抛异常）
function fmtTimestamp8(v) {
  if (v === null || v === undefined || v === '') return '';
  if (typeof v === 'number' || (/^-?\d+$/.test(String(v).trim()) && String(v).trim() !== '')) {
    const n = Number(v);
    if (!isFinite(n) || n <= 0) return '';
    const ms = n < 1e11 ? n * 1000 : n;
    return _format8FromMs(ms);
  }
  const s = String(v).replace('T', ' ').replace('Z', '').trim();
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-]\d{2}:\d{2})?/);
  if (!m) return String(v);
  let ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  if (m[7]) {
    // 带偏移：把「本地假想时刻」还原成绝对 UTC 时间点（+08:00 减 8h，-05:00 加 5h）
    const sign = m[7][0] === '-' ? 1 : -1;
    const oh = parseInt(m[7].slice(1, 3), 10);
    const om = parseInt(m[7].slice(4, 6), 10);
    ms += sign * (oh * 3600 + om * 60) * 1000;
  }
  return _format8FromMs(ms);
}

// 绝对毫秒时间戳 → Asia/Shanghai 固定时区的 YYYY-MM-DD HH:mm:ss（Intl 不可用时按 UTC+8 偏移兜底）
function _format8FromMs(ms) {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(new Date(ms));
    const get = t => (parts.find(p => p.type === t) || {}).value || '';
    const day = `${get('year')}-${get('month')}-${get('day')}`;
    const hm = `${get('hour')}:${get('minute')}:${get('second')}`;
    if (day.includes('undefined') || hm.includes('undefined')) throw new Error('intl parts missing');
    // en-CA 24h 下 hour 可能为 "24"，归一为 "00"
    return `${day} ${hm === '24:00:00' ? '00:00:00' : hm}`;
  } catch (e) {
    // 兜底：无 Intl/时区数据环境直接按 UTC+8 偏移取分量
    const d = new Date(ms + 8 * 3600 * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  }
}

// 全局工具：Markdown 渲染（先转义 HTML 防 XSS，再由 marked 解析）
let _markedReady = false;
try {
  if (window.marked && typeof marked.parse === 'function') {
    marked.setOptions({breaks: true, gfm: true});
    _markedReady = true;
  }
} catch (e) { _markedReady = false; }

// 裸 URL 后紧跟 CJK/全角字符时在边界插入空格，让 GFM autolink 在此断开，
// 避免全角字符被收进链接再 percent-encode（URL 匹配限定 RFC3986 ASCII 字符集，
// 不会影响行内链接 [文字](URL)、图片 ![](…) 与英文半角括号）
const BARE_URL_CJK_BOUNDARY = /(^|[^A-Za-z0-9:/@.\-_~#?&=%+])(https?:\/\/[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+)(?=[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\u3000-\u303F])/g;

function renderMarkdown(text) {
  if (!text) return '';
  const safe = String(text)
    .replace(BARE_URL_CJK_BOUNDARY, '$1$2 ')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  try {
    return _markedReady ? marked.parse(safe) : safe;
  } catch (e) {
    return safe;
  }
}

const API = (() => {
  const BASE = '/api';
  let _refreshPromise = null;  // 并发 refresh 时所有请求等待同一个 Promise
  // Issue #3734: 会话失效广播去重——5 列看板并发 401 只广播一次，避免重复停机/重复提示。
  // 任一成功响应即复位（重新登录后再次失效仍能通知）。
  let _sessionExpiredNotified = false;
  function _notifySessionExpired() {
    if (_sessionExpiredNotified) return;
    _sessionExpiredNotified = true;
    try {
      if (typeof window !== 'undefined' && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent('grape:session-expired'));
      }
    } catch (e) {}
  }

  // ── Issue #3569: reauth 按平台跳转 + 防整页重载循环 ──
  // 旧实现收到 401 reauth_required 一律跳 /auth/login/gitcode：GitHub 令牌失效被送去 GitCode
  // 授权，回调后仍 401 → 整页重载 → 再 401，形成无限重载闭环（页面级 _reauthRedirecting 在
  // 整页重载后归零，挡不住）。这里用 sessionStorage（页签级、跨整页重载保留）记录
  // 「同一 provider 60 秒内已跳转过」，命中则停止跳转并渲染明确提示。
  var _REAUTH_MARK_KEY = 'grape_reauth_redirect';
  var _REAUTH_MARK_TTL_MS = 60 * 1000;
  var _REAUTH_PROVIDER_PATHS = {gitcode: '/auth/login/gitcode', github: '/auth/login/github'};
  var _REAUTH_PROVIDER_LABELS = {gitcode: 'GitCode', github: 'GitHub'};
  var _REAUTH_NOTICE_ID = 'oauthReauthNotice';

  function _reauthProviderLabel(provider) {
    return _REAUTH_PROVIDER_LABELS[provider] || provider;
  }

  // provider 归一化：只认 gitcode/github；缺失（旧后端无 provider 字段）→ 回退 GitCode；
  // 已知但无授权入口的平台（如 gitee）返回 ''，由调用方改为「只提示不跳转」——
  // 猜一个平台去跳会原样复制本缺陷（GitCode 修不了 GitHub/Gitee）。
  function _normalizeReauthProvider(provider) {
    var p = String(provider || '').trim().toLowerCase();
    if (!p) return 'gitcode';
    return _REAUTH_PROVIDER_PATHS[p] ? p : '';
  }

  function _readReauthMarks() {
    try {
      var m = JSON.parse(sessionStorage.getItem(_REAUTH_MARK_KEY) || 'null');
      return (m && typeof m === 'object') ? m : {};
    } catch (e) { return {}; }
  }

  function _setReauthMark(provider) {
    try {
      var m = _readReauthMarks();
      m[provider] = Date.now();
      sessionStorage.setItem(_REAUTH_MARK_KEY, JSON.stringify(m));
    } catch (e) { /* sessionStorage 不可用（隐私模式）→ 仅页面级去重兜底 */ }
  }

  function _clearReauthMark(provider) {
    try {
      if (!provider) { sessionStorage.removeItem(_REAUTH_MARK_KEY); return; }
      var m = _readReauthMarks();
      delete m[provider];
      if (Object.keys(m).length) sessionStorage.setItem(_REAUTH_MARK_KEY, JSON.stringify(m));
      else sessionStorage.removeItem(_REAUTH_MARK_KEY);
    } catch (e) {}
  }

  // 同一 provider 60 秒内已跳转过 → true（本轮 401 不得再跳，否则整页重载闭环）
  function _recentlyRedirected(provider) {
    var ts = Number(_readReauthMarks()[provider] || 0);
    return !!ts && (Date.now() - ts) < _REAUTH_MARK_TTL_MS;
  }

  // 重新绑定/重新登录成功（URL 带 bind_success=<provider>）→ 清除该平台标记，
  // 保证重新授权后下一次 401 仍能正常跳转（正向回归）。
  function _consumeBindSuccessMark() {
    try {
      var p = new URLSearchParams(window.location.search || '').get('bind_success');
      if (p) _clearReauthMark(String(p).trim().toLowerCase());
    } catch (e) {}
  }
  _consumeBindSuccessMark();

  function _reauthNoticeText(key, provider, fallback) {
    try {
      if (typeof I18N !== 'undefined' && I18N.t) {
        return I18N.t('app.error.' + key, {label: _reauthProviderLabel(provider)}, fallback);
      }
    } catch (e) {}
    return fallback;
  }

  // reauth 提示条：不整页跳转，只在当前页面渲染可读提示 + 账号设置入口（#account/profile）
  function _showReauthNotice(provider, textKey, fallbackText) {
    var msg = textKey
      ? _reauthNoticeText(textKey, provider, fallbackText)
      : _reauthNoticeText('reauth_expired', provider,
          _reauthProviderLabel(provider) + ' 授权已过期，请到 账号设置 → 第三方绑定 重新授权');
    var shown = false;
    try {
      var old = document.getElementById(_REAUTH_NOTICE_ID);
      if (old && old.parentNode) old.parentNode.removeChild(old);
      var bar = document.createElement('div');
      bar.id = _REAUTH_NOTICE_ID;
      bar.setAttribute('role', 'alert');
      bar.style.cssText = 'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:10001;'
        + 'background:#fff7e6;color:#7a4d00;border:1px solid #ffd591;border-radius:10px;padding:10px 14px;'
        + 'font-size:13px;line-height:1.4;box-shadow:0 8px 24px rgba(0,0,0,.12);display:flex;'
        + 'align-items:center;gap:10px;max-width:min(92vw,720px)';
      var span = document.createElement('span');
      span.textContent = msg;
      var go = document.createElement('a');
      go.href = '#account/profile';
      go.textContent = _reauthNoticeText('reauth_go_settings', provider, '去账号设置');
      go.style.cssText = 'color:#0a6cff;text-decoration:underline;white-space:nowrap';
      go.onclick = function (ev) {
        if (typeof app !== 'undefined' && app.nav) {
          ev.preventDefault();
          app.nav('account', 'profile');
        }
      };
      var close = document.createElement('button');
      close.type = 'button';
      close.textContent = '×';
      close.setAttribute('aria-label', 'close');
      close.style.cssText = 'background:transparent;border:0;font-size:16px;line-height:1;cursor:pointer;color:inherit';
      close.onclick = function () { if (bar.parentNode) bar.parentNode.removeChild(bar); };
      bar.appendChild(span);
      bar.appendChild(go);
      bar.appendChild(close);
      (document.body || document.documentElement).appendChild(bar);
      shown = true;
    } catch (e) { /* DOM 不可用 → 走 toast 兜底 */ }
    if (!shown) {
      try {
        if (typeof app !== 'undefined' && app.showToast) app.showToast(msg, '', 6000);
      } catch (e) {}
    }
  }

  // Issue #3569: 按 401 detail.provider 跳对应平台授权；
  // 目标平台未配置（400）/限流（429）/其他失败一律「只提示 + 退避」，不做整页导航。
  function _redirectToOAuth(provider) {
    var p = _normalizeReauthProvider(provider);
    if (!p) {
      // 未知平台没有已知授权入口 → 不猜平台，只提示去账号设置重新授权（不做整页导航）
      _showReauthNotice(String(provider || '').trim().toLowerCase());
      return;
    }
    if (_reauthRedirecting) return;   // 页面级并发去重
    if (_isOnLoginPage()) return;     // 已在登录页
    if (_recentlyRedirected(p)) {
      // 同一 provider 60 秒内已跳转过且本次仍 401 → 再跳只会整页重载循环，停止并提示
      _showReauthNotice(p);
      return;
    }
    _reauthRedirecting = true;
    API.get(_REAUTH_PROVIDER_PATHS[p]).then(function (d) {
      if (d && d.redirect_url) {
        _setReauthMark(p);            // 跳转前落标记（sessionStorage 跨整页重载保留）
        location.href = d.redirect_url;
        return;
      }
      // 无 redirect_url（后端未给出授权地址）→ 只提示，不做整页导航
      _showReauthNotice(p);
    }).catch(function (e) {
      var st = (e && e.status) || 0;
      if (st === 429) {
        // 429 只提示 + 退避：禁止新增整页导航（否则正是闭环加速器）
        _showReauthNotice(p, 'reauth_retry_later',
          _reauthProviderLabel(p) + ' 授权入口请求过于频繁，请稍后重试');
      } else if (st === 400) {
        // 目标平台 OAuth 未配置 → 只提示，不跳转
        _showReauthNotice(p, 'reauth_not_configured',
          _reauthProviderLabel(p) + ' OAuth 未配置，请联系管理员完成平台应用配置');
      } else {
        _showReauthNotice(p, 'reauth_redirect_failed',
          _reauthProviderLabel(p) + ' 授权跳转失败，请稍后重试');
      }
    });
  }

  // 多 Tab 同步——监听 localStorage token 变化，其他 Tab 刷新 token 后自动同步
  // 体验优化，不是安全屏障——同步失败有 401 + Promise 队列兜底
  if (typeof window !== 'undefined') {
    window.addEventListener('storage', (e) => {
      if (e.key === 'token') {
        // token 被其他 Tab 更新了——当前 Tab 的下次请求会自动用新 token（从 localStorage 读）
        // 如果新值为 null（其他 Tab logout 了）——跳登录页
        if (!e.newValue) {
          localStorage.removeItem('user');
          if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
        } else {
          // Issue #86: 其他 Tab 刷新了 token——当前 Tab 按新 token 的 exp 重设定时器
          // 否则旧定时器触发时发现新 token 还很充足，不刷新也不重设，定时器停摆
          scheduleProactiveRefresh();
        }
      }
      // R8: 其他标签页清除 repo_member_cache 或修改 user.bindings 时
      // 检查当前标签页的 currentRepo 是否仍有效
      if (e.key === 'repo_member_cache' || e.key === 'user') {
        // Issue #3569: 重新绑定成功（bindings 变化）→ 清除对应平台的 reauth 跳转标记，
        // 保证用户手动重新授权后下一次 401 仍能正常跳转（reload 回调之外的 popup 绑定路径）
        if (e.key === 'user' && e.newValue) {
          try {
            var _ub = JSON.parse(e.newValue).bindings || [];
            for (var _bi = 0; _bi < _ub.length; _bi++) {
              _clearReauthMark(String(_ub[_bi] || '').toLowerCase());
            }
          } catch (ex) {}
        }
        try {
          var cr = JSON.parse(sessionStorage.getItem('currentRepo') || 'null');
          if (cr && cr.repoKey) {
            // 从 localStorage.user.bindings 检查 currentRepo 的 provider 是否仍绑定
            var _u = JSON.parse(localStorage.getItem('user') || '{}');
            var _bindings = _u.bindings || [];
            // 提取 currentRepo 的 provider 前缀
            var _colonIdx = cr.repoKey.indexOf(':');
            if (_colonIdx > 0) {
              var _provider = cr.repoKey.substring(0, _colonIdx);
              if (_provider && _provider !== cr.repoKey && _bindings.indexOf(_provider) < 0) {
                // provider 已被其他标签页解绑 → 清理当前标签页状态
                sessionStorage.removeItem('currentRepo');
                if (typeof app !== 'undefined' && app._clearCachedReposData) app._clearCachedReposData();
                if (typeof app !== 'undefined' && app.goToRepos) app.goToRepos();
              }
            }
          }
        } catch(ex) {}
      }
    });
  }

  // 用 refresh_token 换新的 access_token（滚动续期）
  // refresh_token 从 HttpOnly cookie 自动发送——JS 不需要读
  // 并发 401 时所有请求等待同一个 Promise——避免重复 refresh
  function doRefresh() {
    if (_refreshPromise) return _refreshPromise;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    _refreshPromise = fetch(BASE + '/auth/refresh', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',  // CSRF 保护——跨站伪造无法加自定义 header
      },
      credentials: 'same-origin',  // 自动带 HttpOnly cookie 里的 refresh_token
      signal: controller.signal,
    }).then(r => {
      if (!r.ok) throw new Error('refresh failed');
      return r.json();
    }).then(d => {
      if (d.token) {
        localStorage.setItem('token', d.token);
        // refresh_token 在 HttpOnly cookie 里自动更新——JS 不需要接触
        return d.token;
      }
      throw new Error('no token in refresh response');
    }).catch(e => {
      // Issue #63: 区分错误类型——abort/网络错误不清 token，只有 refresh 真失败才跳登录
      if (e.name === 'AbortError') throw e;  // 浏览器 abort/超时——不清 token
      if (e instanceof TypeError) throw e;   // 网络错误——不清 token
      // refresh 真的失败（HTTP 401/403）——清 token 跳登录
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      // R11-2: Unified to app.showLogin() (DOM switch) instead of location.href='/login'
      // (整页刷新). This also resolves R7's language prefix issue — showLogin doesn't
      // involve URL navigation.
      if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      _notifySessionExpired();
      throw e;
    }).finally(() => {
      clearTimeout(timer);
      _refreshPromise = null;
    });
    return _refreshPromise;
  }

  // Issue #86: 主动刷新——解析 JWT exp，过期前 2 分钟主动刷新
  const _REFRESH_THRESHOLD_MS = 2 * 60 * 1000;  // 过期前 2 分钟
  let _proactiveTimer = null;

  function _parseTokenExp(token) {
    if (!token) return 0;
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      return (payload.exp || 0) * 1000;
    } catch (e) { return 0; }
  }

  function _getRemainingMs(token) {
    const exp = _parseTokenExp(token);
    if (!exp) return Infinity;  // 无法解析——不主动刷新，交给 401 兜底
    return exp - Date.now();
  }

  // 统一入口：检查 token 剩余有效期，不足阈值则刷新。返回可用的 token。
  // 定时器(A)和请求前(B)都调此函数，内部复用 _refreshPromise 单例，不会重复刷新。
  // 注意：JWT exp 按客户端本地时间算，时钟偏差可能导致 remaining 不准——401 被动刷新兜底。
  async function ensureTokenValid() {
    const token = localStorage.getItem('token');
    // 无 token（未登录/已退出）→ 不尝试 refresh，直接返回 null
    // refresh 依赖 refresh_token cookie，logout 已清；尝试 refresh 会触发 location.href='/login' 导航，
    // 干扰后续 pwdLogin 流程（doRefresh 的页面跳转和 pwdLogin 的 showApp 竞态）
    if (!token) return null;
    const remaining = _getRemainingMs(token);
    if (remaining > _REFRESH_THRESHOLD_MS) return token;
    // 剩余不足阈值——刷新（doRefresh 内部有 _refreshPromise 单例）
    try {
      return await doRefresh();
    } catch (e) {
      // R8-2: Network error (TypeError) / timeout (AbortError) — backend unreachable.
      // Retry won't help in 500ms. Return stale token; the subsequent request will
      // fail and 401兜底 handles it.
      if (e instanceof TypeError) return token;
      if (e.name === 'AbortError') return token;
      // HTTP error (refresh_token rejected) — retry once after short delay
      await new Promise(r => setTimeout(r, 500));
      try {
        return await doRefresh();
      } catch (e2) {
        return token;  // 重试仍失败——返回原 token，交给 401 兜底
      }
    }
  }

  // 定时器：到点调 ensureTokenValid，刷新成功后重新设定时器
  function scheduleProactiveRefresh() {
    if (_proactiveTimer) { clearTimeout(_proactiveTimer); _proactiveTimer = null; }
    const token = localStorage.getItem('token');
    if (!token) return;
    const remaining = _getRemainingMs(token);
    if (remaining <= 0) return;  // 已过期——等 401 被动刷新
    // 过期前 2 分钟触发（最少 30 秒后）
    const delay = Math.max(remaining - _REFRESH_THRESHOLD_MS, 30000);
    _proactiveTimer = setTimeout(async () => {
      try {
        const newToken = await ensureTokenValid();
        if (newToken) scheduleProactiveRefresh();  // 刷新成功——重设定时器
      } catch (e) {
        // 静默——401 被动刷新兜底
      }
    }, delay);
  }

  // 401 时尝试 refresh，成功后重发原请求
  function handle401(retryFn) {
    return doRefresh().then(() => retryFn()).catch(e => {
      // Issue #63: abort/网络错误不清 token（doRefresh 已处理），只有 refresh 真失败才跳登录
      if (e.name === 'AbortError' || e instanceof TypeError) throw e;
      // R11-2: doRefresh already cleared token + called showLogin.
      // No need to duplicate here — just throw the session_expired error.
      throw new Error(I18N.t('common.session_expired'));
    });
  }

  // Issue #297: 非 ok 响应可能是 HTML（如 nginx 413/502 错误页），r.json() 会抛
  // "Unexpected token '<'" 裸异常——先看 content-type，HTML / JSON 解析失败均降级为 null，
  // 由调用方用 request_failed({status}) 模板渲染用户可读提示
  function _safeErrJson(r) {
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('text/html') !== -1) return Promise.resolve(null);
    return r.json().catch(() => null);
  }

  function handleResp(r, retryFn) {
    if (!r.ok) {
      if (r.status === 401 && retryFn) {
        // 尝试 refresh（refresh_token 在 HttpOnly cookie 里自动带）
        // 但先检查是否是 OAuth 平台过期（不需要 JWT refresh，需要重新授权）
        return _safeErrJson(r).then(err => {
          if (err?.detail?.action === 'reauth_required') {
            // Issue #3569：按 detail.provider 跳对应平台授权；失败只提示不整页导航。
            // Issue #3563 的结构化错误继续透传，入口页可按 status/detail 分流。
            _redirectToOAuth(err?.detail?.provider);
            throw _reauthError(err.detail);
          }
          // R1 fix: credential 401 on public auth endpoints — user has no token
          // or is hitting a public auth endpoint (login/refresh/exchange/associate).
          // Skip refresh and surface the backend message directly.
          var reqUrl = r.url || '';
          var isPublicAuthEndpoint = /\/api\/auth\/(login|refresh|exchange)$/.test(reqUrl)
            || /\/api\/auth\/login\/[^/]+\/associate$/.test(reqUrl);
          if (!localStorage.getItem('token') || isPublicAuthEndpoint) {
            var detail = (err && err.detail) || {};
            var e = new Error(detail.message || I18N.t('common.request_failed', {status: 401}));
            e.status = 401;
            e.detail = detail;
            e.error_code = detail.error_code || '';
            throw e;
          }
          // Session 401 — JWT expired, attempt refresh
          return handle401(retryFn);
        });
      }
      if (r.status === 401) {
// 没有 retryFn——检查是否 reauth_required（按 provider 跳对应平台），否则直接跳登录
        return _safeErrJson(r).then(err => {
          if (err?.detail?.action === 'reauth_required') {
            // Issue #3569: 按 provider 跳对应平台授权（缺失/未知回退 GitCode）
            _redirectToOAuth(err?.detail?.provider);
            throw _reauthError(err.detail);
          }
          // P33 fix: don't clear token for public auth endpoint 401s
          var reqUrl = r.url || '';
          var isPublicAuthEndpoint = /\/api\/auth\/(login|refresh|exchange)$/.test(reqUrl)
            || /\/api\/auth\/login\/[^/]+\/associate$/.test(reqUrl);
          if (isPublicAuthEndpoint) {
            // Public auth endpoint 401 — show backend message, don't clear token
            var detail33 = (err && err.detail) || {};
            var e33 = new Error(detail33.message || I18N.t('common.request_failed', {status: 401}));
            e33.status = 401;
            e33.error_code = detail33.error_code || '';
            throw e33;
          }
          localStorage.removeItem('token');
          localStorage.removeItem('user');
          if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
          // Issue #3734: 会话失效广播——tasks 看板收到即停轮询并给出重新登录入口
          _notifySessionExpired();
          throw new Error(err?.detail?.message || I18N.t('common.session_expired'));
        });
      }
      // Issue #297: HTML 错误页（413/502 等）降级为 request_failed({status})，
      // 不再让 "Unexpected token '<'" 裸异常进入用户提示
      return _safeErrJson(r).then(err => {
        const detail = (err && err.detail) || {};
        const e = new Error(detail.message || I18N.t('common.request_failed', {status: r.status}));
        e.status = r.status;
        e.errors = detail.errors || null;
        e.detail = detail;
        e.error_code = detail.error_code || '';  // Issue #372: error_code for i18n mapping
        throw e;
      });
    }
    // Issue #3734: 会话恢复（任一成功响应）→ 复位失效广播去重标记
    _sessionExpiredNotified = false;
    // Issue #63: 响应是 HTML（如 nginx 502 页面或登录页重定向）时，不报 "Unexpected token"
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('text/html') !== -1) {
      // R8-1: 200 + HTML is not necessarily session expiry — it's more likely
      // an nginx/proxy misconfiguration. Use neutral server_error for all HTML responses.
      var e = new Error(I18N.t('common.server_error') || '服务器异常，请稍后重试');
      e.status = r.status;
      throw e;
    }
    return r.json();
  }

  // R7: Check if currently on any login page (both /login and /en/login)
  function _isOnLoginPage() {
    var p = location.pathname;
    return p === '/login' || p === '/en/login';
  }

  // Issue #52: OAuth token 过期且无法刷新时跳转授权页
  // R9-2: 页面级去重标记（整页重载后归零，跨页防循环见 Issue #3569 的 sessionStorage 标记）
  var _reauthRedirecting = false;

  // Issue #3563（Spec v2 B3）：401 reauth 抛出的错误携带 status/detail，
  // 供入口页（repos/templates）先判 status/action 再决定提示，不再直塞 check_failed 文案。
  function _reauthError(detail) {
    var e = new Error((detail && detail.message) || I18N.t('common.request_failed', {status: 401}));
    e.status = 401;
    e.detail = detail || {};
    e.reauth_required = true;
    return e;
  }

  // fetch 超时封装：30s 后 abort，防止后端 hang 导致前端永久 loading
  // 网络错误自动重试：fetch 报 TypeError（TCP 连接过期/网络抖动）时延迟 500ms 重试一次
  // Issue #63: 合并全局 nav/tab signal，切 tab/页面时主动取消请求；区分超时 abort 和外部 abort
  const _DEFAULT_TIMEOUT = 30000;
  // 网络错误(TypeError)重试：指数退避，最多重试 _MAX_NET_RETRIES 次；仅幂等方法重试，避免 POST/PUT/DELETE 重复提交副作用
  const _MAX_NET_RETRIES = 2;                 // 初次请求外再多重试 2 次（共 3 次尝试）
  const _NET_RETRY_DELAYS = [500, 1500];      // 第 1/2 次重试前的等待（ms）
  const _IDEMPOTENT_METHODS = ['GET', 'PUT', 'DELETE'];
  function _fetchWithTimeout(url, opts, timeout, _retried, _skipNavSignal) {
    const _t0 = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => { controller._timedOut = true; controller.abort(); }, timeout || _DEFAULT_TIMEOUT);
    // 合并全局 nav signal + tab signal（读操作合并，写操作跳过）
    if (!_skipNavSignal) {
      var navSignal = window._navSignal;
      var tabSignal = window._tabSignal;
      // 只跟随「活着的」取消信号：已 abort 的 signal 属于上一次导航，把它挂到新请求上会造成
      // 每个请求立即 abort → 视图（如 #tasks）永久停在「加载中」，且重试也无法恢复。
      if (navSignal && !navSignal.aborted) {
        navSignal.addEventListener('abort', function() { controller.abort(); });
      }
      if (tabSignal && !tabSignal.aborted) {
        tabSignal.addEventListener('abort', function() { controller.abort(); });
      }
    }
    opts = Object.assign({}, opts, { signal: controller.signal });
    return fetch(url, opts).finally(() => clearTimeout(timer)).then(r => {
      _reportLatency(url, opts.method || 'GET', r.status, Math.round(performance.now() - _t0));
      return r;
    }).catch(e => {
      _reportLatency(url, opts.method || 'GET', 0, Math.round(performance.now() - _t0));
      if (e.name === 'AbortError') {
        if (controller._timedOut) throw e;  // 超时 abort，正常显示错误
        e.silent = true; throw e;            // 外部取消（nav/tab/浏览器），静默
      }
      // 网络层错误（TypeError: Failed to fetch）——仅幂等方法重试（指数退避），非幂等直接抛
      if (e instanceof TypeError) {
        const method = (opts.method || 'GET').toUpperCase();
        const idempotent = _IDEMPOTENT_METHODS.indexOf(method) >= 0;
        const attempt = (_retried || 0);
        if (idempotent && attempt < _MAX_NET_RETRIES) {
          const delay = _NET_RETRY_DELAYS[Math.min(attempt, _NET_RETRY_DELAYS.length - 1)];
          return new Promise(r => setTimeout(r, delay))
            .then(() => _fetchWithTimeout(url, opts, timeout, attempt + 1, _skipNavSignal));
        }
      }
      throw e;
    });
  }

  // Issue #86: headers 改为 async——请求前先确保 token 有效（B 模式兜底）
  async function _makeHeaders() {
    const token = await ensureTokenValid();
    const h = {'Content-Type': 'application/json'};
    if (token) h['Authorization'] = 'Bearer ' + token;
    // 组织上下文：从 localStorage 读取当前组织 ID
    const orgId = localStorage.getItem('current_org_id');
    if (orgId) h['X-Org-ID'] = orgId;
    return h;
  }

  function _get(path, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {headers: h}, timeout, false, false).then(r => handleResp(r, () => _get(path, timeout))));
  }
  function _post(path, body, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {method: 'POST', headers: h, body: JSON.stringify(body)}, timeout, false, true).then(r => handleResp(r, () => _post(path, body, timeout))));
  }
  function _put(path, body, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {method: 'PUT', headers: h, body: JSON.stringify(body)}, timeout, false, true).then(r => handleResp(r, () => _put(path, body, timeout))));
  }
  function _patch(path, body, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {method: 'PATCH', headers: h, body: JSON.stringify(body)}, timeout, false, true).then(r => handleResp(r, () => _patch(path, body, timeout))));
  }
  function _del(path, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {method: 'DELETE', headers: h}, timeout, false, true).then(r => handleResp(r, () => _del(path, timeout))));
  }

  // 组织工作台统一错误映射：后端保留结构化 error_code，前端负责展示可读、可本地化的提示。
  function localizeOrgError(e) {
    e = e || {};
    var code = String(e.error_code || '').toUpperCase();
    var status = Number(e.status || 0);
    var key = {
      PERMISSION_DENIED: 'organizations.errors.permission_denied',
      REPO_ALREADY_BOUND: 'organizations.errors.repo_already_bound',
      ORG_REPO_PROTECTED: 'organizations.errors.org_repo_protected',
      ORG_REPO_QUOTA_EXCEEDED: 'organizations.errors.quota_exceeded',
      DISPATCH_IN_PROGRESS: 'organizations.errors.dispatch_in_progress',
      TARGET_RETRY_IN_PROGRESS: 'organizations.errors.dispatch_in_progress',
      INVALID_TARGET: 'organizations.errors.invalid_target',
      INVALID_REPO: 'organizations.errors.invalid_target',
      INVALID_ORG_CONTEXT: 'organizations.errors.invalid_target',
      NO_TARGET_SCENE: 'organizations.errors.invalid_target',
      // Issue #2236：组织上下文 / 知识库写操作错误码（#2150 后端契约）
      INVALID_CONTEXT_KIND: 'organizations.errors.invalid_kind',
      INVALID_NAME: 'organizations.errors.invalid_name',
      INVALID_CONTENT: 'organizations.errors.invalid_content',
      CONTENT_TOO_LARGE: 'organizations.errors.content_too_large',
      SENSITIVE_CONTENT_REJECTED: 'organizations.errors.sensitive_rejected',
      INVALID_STATUS_TRANSITION: 'organizations.errors.invalid_transition',
      PUBLISH_BUSY: 'organizations.errors.publish_busy',
    }[code];
    if (!key && status === 403) key = 'organizations.errors.forbidden';
    if (!key && status === 409) key = 'organizations.errors.conflict';
    if (!key && status === 404) key = 'organizations.errors.not_found';
    if (!key) return e.message || I18N.t('common.request_failed', {status: status});
    var localized = I18N.t(key);
    return (localized && localized !== key) ? localized : (e.message || localized || key);
  }

  function _orgCatch(promise) {
    return promise.catch(function(e) {
      e.message = localizeOrgError(e);
      throw e;
    });
  }

  function _orgRepoPath(repoKey) {
    return String(repoKey || '').split('/').map(encodeURIComponent).join('/');
  }

  var orgRepos = {
    list: function(orgId) {
      return _orgCatch(_get('/orgs/' + orgId + '/repos'));
    },
    bind: function(orgId, repoKey) {
      return _orgCatch(_post('/orgs/' + orgId + '/repos', {repo_key: repoKey || ''}));
    },
    unbind: function(orgId, repoKey) {
      return _orgCatch(_del('/orgs/' + orgId + '/repos/' + _orgRepoPath(repoKey) + '?confirm=true'));
    },
    transfer: function(orgId, repoKey, targetOrgId) {
      return _orgCatch(_post(
        '/orgs/' + orgId + '/repos/' + _orgRepoPath(repoKey) + '/transfer',
        {target_org_id: Number(targetOrgId || 0)}
      ));
    },
  };

  // Issue #2236：列表过滤参数按契约（context: kind/status；knowledge: kind/status/tag/q）拼接，
  // 空值不发送，避免后端把空串当作过滤条件。
  function _orgFilterQuery(filters) {
    var f = filters || {};
    var names = ['kind', 'status', 'tag', 'q'];
    var parts = [];
    for (var i = 0; i < names.length; i++) {
      var value = f[names[i]];
      if (value === undefined || value === null || value === '') continue;
      parts.push(names[i] + '=' + encodeURIComponent(String(value)));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  var orgContext = {
    list: function(orgId, filters) {
      return _orgCatch(_get('/orgs/' + orgId + '/context' + _orgFilterQuery(filters)));
    },
    get: function(orgId, ctxId) {
      return _orgCatch(_get('/orgs/' + orgId + '/context/' + ctxId));
    },
    create: function(orgId, payload) {
      return _orgCatch(_post('/orgs/' + orgId + '/context', payload || {}));
    },
    update: function(orgId, ctxId, payload) {
      return _orgCatch(_put('/orgs/' + orgId + '/context/' + ctxId, payload || {}));
    },
    publish: function(orgId, ctxId) {
      return _orgCatch(_post('/orgs/' + orgId + '/context/' + ctxId + '/publish', {}));
    },
    deprecate: function(orgId, ctxId) {
      return _orgCatch(_post('/orgs/' + orgId + '/context/' + ctxId + '/deprecate', {}));
    },
    remove: function(orgId, ctxId) {
      return _orgCatch(_del('/orgs/' + orgId + '/context/' + ctxId));
    },
  };

  var orgKnowledge = {
    list: function(orgId, filters) {
      return _orgCatch(_get('/orgs/' + orgId + '/knowledge' + _orgFilterQuery(filters)));
    },
    get: function(orgId, knowledgeId) {
      return _orgCatch(_get('/orgs/' + orgId + '/knowledge/' + knowledgeId));
    },
    create: function(orgId, payload) {
      return _orgCatch(_post('/orgs/' + orgId + '/knowledge', payload || {}));
    },
    update: function(orgId, knowledgeId, payload) {
      return _orgCatch(_put('/orgs/' + orgId + '/knowledge/' + knowledgeId, payload || {}));
    },
    publish: function(orgId, knowledgeId) {
      return _orgCatch(_post('/orgs/' + orgId + '/knowledge/' + knowledgeId + '/publish', {}));
    },
    deprecate: function(orgId, knowledgeId) {
      return _orgCatch(_post('/orgs/' + orgId + '/knowledge/' + knowledgeId + '/deprecate', {}));
    },
    archive: function(orgId, knowledgeId) {
      return _orgCatch(_post('/orgs/' + orgId + '/knowledge/' + knowledgeId + '/archive', {}));
    },
    remove: function(orgId, knowledgeId) {
      return _orgCatch(_del('/orgs/' + orgId + '/knowledge/' + knowledgeId));
    },
  };

  var orgDispatches = {
    list: function(orgId, limit) {
      var n = Number(limit || 50);
      return _orgCatch(_get('/orgs/' + orgId + '/dispatches?limit=' + n));
    },
    detail: function(orgId, dispatchId) {
      return _orgCatch(_get('/orgs/' + orgId + '/dispatches/' + dispatchId));
    },
    create: function(orgId, payload) {
      return _orgCatch(_post('/orgs/' + orgId + '/dispatches', payload || {}));
    },
    cancel: function(orgId, dispatchId) {
      return _orgCatch(_post('/orgs/' + orgId + '/dispatches/' + dispatchId + '/cancel', {}));
    },
    retryTarget: function(orgId, dispatchId, repoKey) {
      return _orgCatch(_post(
        '/orgs/' + orgId + '/dispatches/' + dispatchId + '/targets/' + _orgRepoPath(repoKey) + '/retry',
        {}
      ));
    },
  };

  // ===== 方案B：前端 API 时延埋点 + 批量上报 =====
  // Worker 轮询路径不上报（高频内部请求，非用户页面加载）
  var _LATENCY_SKIP_PREFIXES = ['/runtime/runtimes/', '/runtime/heartbeat', '/runtime/tasks/', '/latency/report'];
  var _latencyBuffer = [];
  var _latencyTimer = null;
  var _LATENCY_BATCH_SIZE = 10;
  var _LATENCY_FLUSH_INTERVAL = 30000;
  var _LATENCY_THRESHOLD_MS = 200;  // 只上报耗时 ≥ 200ms 的请求，避免数据量爆炸

  function _reportLatency(url, method, status, durationMs) {
    try {
      if (durationMs < _LATENCY_THRESHOLD_MS) return;  // 快请求不上报
      var path = url.replace(BASE, '');
      // 跳过 Worker 轮询和上报自身
      for (var i = 0; i < _LATENCY_SKIP_PREFIXES.length; i++) {
        if (path.startsWith(_LATENCY_SKIP_PREFIXES[i])) return;
      }
      _latencyBuffer.push({ url: path, method: method, status: status, durationMs: durationMs, ts: Date.now() });
      if (_latencyBuffer.length >= _LATENCY_BATCH_SIZE) {
        _flushLatency();
      } else if (!_latencyTimer) {
        _latencyTimer = setTimeout(_flushLatency, _LATENCY_FLUSH_INTERVAL);
      }
    } catch(e) { /* 埋点失败不影响业务 */ }
  }

  function _flushLatency() {
    if (!_latencyBuffer.length) return;
    var batch = _latencyBuffer.splice(0);
    _latencyTimer = null;
    try {
      // sendBeacon：异步不阻塞页面，即使页面关闭也能发出
      var blob = new Blob([JSON.stringify({ entries: batch })], { type: 'application/json' });
      navigator.sendBeacon(BASE + '/latency/report', blob);
    } catch(e) { /* ignore */ }
  }

  // 页面卸载/隐藏时冲刷剩余数据
  if (typeof window !== 'undefined') {
    window.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'hidden') _flushLatency();
    });
    window.addEventListener('beforeunload', _flushLatency);
  }

  // ===== 反馈 API 封装（Issue #177） =====
  function deleteFeedback(fid) {
    return _del('/feedback/' + fid);
  }

  return {
    get: _get,
    post: _post,
    put: _put,
    patch: _patch,
    del: _del,
    localizeOrgError: localizeOrgError,
    orgRepos: orgRepos,
    orgDispatches: orgDispatches,
    orgContext: orgContext,       // Issue #2236: 组织上下文（brief/policy/decision）
    orgKnowledge: orgKnowledge,   // Issue #2236: 组织知识库
    deleteFeedback: deleteFeedback,  // Issue #177: 删除反馈封装
    scheduleProactiveRefresh: scheduleProactiveRefresh,  // Issue #86: app.js 登录成功后调用
    // R6: Export getRaw for blob downloads / SSE streams that need raw Response
    getRaw: (path, timeout) => {
      return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {headers: h}, timeout, false, false));
    },
    // R6: Export ensureTokenValid for SSE streams that need fresh token before fetch
    ensureTokenValid: ensureTokenValid,
    // 改密/换绑后 token 被外部更新时同步 API 模块：落盘 + 重置并发刷新态 +
    // 重排主动刷新（此前 app.js/account.js 调 API.setToken 报 TypeError——
    // account.js 有 typeof 守卫静默跳过，app.js doChangePassword 无守卫，
    // 改密成功弹窗卡死）。
    setToken: (t) => {
      if (t) localStorage.setItem('token', t); else localStorage.removeItem('token');
      _refreshPromise = null;
      scheduleProactiveRefresh();
    },
    upload: (path, formData, onUploadProgress) => {
      return ensureTokenValid().then(token => {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', BASE + path);
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        const _orgId = localStorage.getItem('current_org_id');
        if (_orgId) xhr.setRequestHeader('X-Org-ID', _orgId);
        xhr.upload.onprogress = (e) => {
          if (onUploadProgress && e.lengthComputable) {
            onUploadProgress(Math.round((e.loaded / e.total) * 100));
          }
        };
        xhr.onload = () => {
          const resp = xhr.responseText;
          let json = null;
          try { json = JSON.parse(resp); } catch { /* ignore */ }
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(json || {message: resp});
          } else {
            if (xhr.status === 401) {
              // R9-1: Attempt refresh before giving up. If refresh succeeds,
              // retry the upload with the new token. If refresh fails, clear token + showLogin.
              // NOTE: FormData is consumed after xhr.send() — cannot reuse directly.
              // The retry creates a new XHR with the same formData (File/Blob objects
              // are not consumed by send(), only FormData entries are read).
              doRefresh().then(newToken => {
                const xhr2 = new XMLHttpRequest();
                xhr2.open('POST', BASE + path);
                xhr2.setRequestHeader('Authorization', 'Bearer ' + newToken);
                if (_orgId) xhr2.setRequestHeader('X-Org-ID', _orgId);
                xhr2.upload.onprogress = (e) => {
                  if (onUploadProgress && e.lengthComputable) {
                    onUploadProgress(Math.round((e.loaded / e.total) * 100));
                  }
                };
                xhr2.onload = () => {
                  let json2 = null;
                  try { json2 = JSON.parse(xhr2.responseText); } catch { /* ignore */ }
                  if (xhr2.status >= 200 && xhr2.status < 300) {
                    resolve(json2 || {message: xhr2.responseText});
                  } else {
                    const err2 = new Error(json2?.detail?.message || I18N.t('common.request_failed', {status: xhr2.status}));
                    err2.errors = json2?.detail?.errors || null;
                    err2.status = xhr2.status;
                    err2.error_code = json2?.detail?.error_code || '';
                    reject(err2);
                  }
                };
                xhr2.onerror = () => reject(new Error(I18N.t('common.network_upload_error')));
                xhr2.send(formData);
              }).catch(() => {
                // Refresh failed — clear token + show login (same as before)
                localStorage.removeItem('token');
                localStorage.removeItem('user');
                if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
                const err = new Error(json?.detail?.message || I18N.t('common.session_expired'));
                err.errors = json?.detail?.errors || null;
                reject(err);
              });
              return;  // Don't reject yet — refresh in progress
            }
            const err = new Error(json?.detail?.message || I18N.t('common.request_failed', {status: xhr.status}));
            err.errors = json?.detail?.errors || null;
            err.status = xhr.status;
            err.error_code = json?.detail?.error_code || '';
            reject(err);
          }
        };
        xhr.onerror = () => reject(new Error(I18N.t('common.network_upload_error')));
        xhr.send(formData);
      });
      });
    },
    // Issue #3569: reauth 跳转/标记（跨平台跳转 + 防整页重载循环）；导出供回归测试与后续视图复用
    _redirectToOAuth: _redirectToOAuth,
    _clearReauthMark: _clearReauthMark,
    _recentlyRedirected: _recentlyRedirected,
  };
})();

window.API = API; // Vue view chunks bridge (batch 2)
window.renderMarkdown = renderMarkdown; // Vue view chunks bridge (batch 5)