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

// 全局工具：Markdown 渲染（先转义 HTML 防 XSS，再由 marked 解析）
let _markedReady = false;
try {
  if (window.marked && typeof marked.parse === 'function') {
    marked.setOptions({breaks: true, gfm: true});
    _markedReady = true;
  }
} catch (e) { _markedReady = false; }

function renderMarkdown(text) {
  if (!text) return '';
  const safe = String(text)
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
      if (location.pathname !== '/login') location.href = '/login';
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
      // 重试 1 次（500ms 后）——_refreshPromise 在 finally 里已清空，重试会发起新请求
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
      // refresh 也失败——清 token 跳登录（refresh_token cookie 由服务端清除）
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
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
        // 但先检查是否是 GitCode OAuth 过期（不需要 JWT refresh，需要重新授权）
        return _safeErrJson(r).then(err => {
          if (err?.detail?.action === 'reauth_required') {
            // GitCode OAuth token 过期且 refresh_token 也失效——跳授权页
            _redirectToGitcodeAuth();
            throw new Error(err.detail.message);
          }
          // 普通 JWT 过期——走 JWT refresh 流程
          return handle401(retryFn);
        });
      }
      if (r.status === 401) {
        // 没有 retryFn——检查是否 GitCode reauth，否则直接跳登录
        return _safeErrJson(r).then(err => {
          if (err?.detail?.action === 'reauth_required') {
            _redirectToGitcodeAuth();
            throw new Error(err.detail.message);
          }
          localStorage.removeItem('token');
          localStorage.removeItem('user');
          if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
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
    // Issue #63: 响应是 HTML（如 nginx 502 页面或登录页重定向）时，不报 "Unexpected token"
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('text/html') !== -1) {
      // 200 + HTML = nginx/proxy 返回了页面而非 API JSON，说明服务异常或 session 失效
      if (r.status === 200) {
        // 可能是 token 过期后反代返回登录页 HTML
        throw new Error(I18N.t('common.session_expired') || '登录已过期，请重新登录');
      }
      // 502/503 等 = 服务器错误，不跳登录
      throw new Error(I18N.t('common.server_error') || '服务器异常，请稍后重试');
    }
    return r.json();
  }

  // Issue #52: GitCode OAuth token 过期且无法刷新时跳转授权页
  function _redirectToGitcodeAuth() {
    if (location.pathname.includes('/login')) return;  // 已在登录页不重复跳
    API.get('/auth/login/gitcode').then(d => {
      if (d.redirect_url) {
        location.href = d.redirect_url;
      } else {
        location.href = '/login';
      }
    }).catch(() => {
      location.href = '/login';
    });
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
      if (navSignal) {
        if (navSignal.aborted) controller.abort();
        else navSignal.addEventListener('abort', function() { controller.abort(); });
      }
      if (tabSignal) {
        if (tabSignal.aborted) controller.abort();
        else tabSignal.addEventListener('abort', function() { controller.abort(); });
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
  function _del(path, timeout) {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {method: 'DELETE', headers: h}, timeout, false, true).then(r => handleResp(r, () => _del(path, timeout))));
  }

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
    del: _del,
    deleteFeedback: deleteFeedback,  // Issue #177: 删除反馈封装
    scheduleProactiveRefresh: scheduleProactiveRefresh,  // Issue #86: app.js 登录成功后调用
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
              // upload 401 不自动 refresh——直接跳登录
              localStorage.removeItem('token');
              localStorage.removeItem('user');
              if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
            }
            const err = new Error(json?.detail?.message || I18N.t('common.request_failed', {status: xhr.status}));
            err.errors = json?.detail?.errors || null;
            reject(err);
          }
        };
        xhr.onerror = () => reject(new Error(I18N.t('common.network_upload_error')));
        xhr.send(formData);
      });
      });
    },
  };
})();
