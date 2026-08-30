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
        }
      }
    });
  }

  function headers() {
    const h = {'Content-Type': 'application/json'};
    const token = localStorage.getItem('token');
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  // 用 refresh_token 换新的 access_token（滚动续期）
  // refresh_token 从 HttpOnly cookie 自动发送——JS 不需要读
  // 并发 401 时所有请求等待同一个 Promise——避免重复 refresh
  function doRefresh() {
    if (_refreshPromise) return _refreshPromise;
    _refreshPromise = fetch(BASE + '/auth/refresh', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',  // CSRF 保护——跨站伪造无法加自定义 header
      },
      credentials: 'same-origin',  // 自动带 HttpOnly cookie 里的 refresh_token
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
      throw e;
    }).finally(() => {
      _refreshPromise = null;
    });
    return _refreshPromise;
  }

  // 401 时尝试 refresh，成功后重发原请求
  function handle401(retryFn) {
    return doRefresh().then(() => retryFn()).catch(() => {
      // refresh 也失败——清 token 跳登录（refresh_token cookie 由服务端清除）
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      throw new Error(I18N.t('common.session_expired'));
    });
  }

  function handleResp(r, retryFn) {
    if (!r.ok) {
      if (r.status === 401 && retryFn) {
        // 尝试 refresh（refresh_token 在 HttpOnly cookie 里自动带）
        return handle401(retryFn);
      }
      if (r.status === 401) {
        // 没有 retryFn——直接跳登录
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      }
      return r.json().then(err => {
        const e = new Error(err?.detail?.message || I18N.t('common.request_failed', {status: r.status}));
        e.status = r.status;
        e.errors = err?.detail?.errors || null;
        throw e;
      });
    }
    return r.json();
  }

  function _get(path) {
    return fetch(BASE + path, {headers: headers()}).then(r => handleResp(r, () => _get(path)));
  }
  function _post(path, body) {
    return fetch(BASE + path, {method: 'POST', headers: headers(), body: JSON.stringify(body)}).then(r => handleResp(r, () => _post(path, body)));
  }
  function _put(path, body) {
    return fetch(BASE + path, {method: 'PUT', headers: headers(), body: JSON.stringify(body)}).then(r => handleResp(r, () => _put(path, body)));
  }
  function _del(path) {
    return fetch(BASE + path, {method: 'DELETE', headers: headers()}).then(r => handleResp(r, () => _del(path)));
  }

  return {
    get: _get,
    post: _post,
    put: _put,
    del: _del,
    upload: (path, formData, onUploadProgress) => {
      const token = localStorage.getItem('token');
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', BASE + path);
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        xhr.upload.onprogress = (e) => {
          if (onUploadProgress && e.lengthcomputable) {
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
    },
  };
})();
