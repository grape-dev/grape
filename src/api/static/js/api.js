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
  let _refreshing = false;  // 防止并发 refresh

  function headers() {
    const h = {'Content-Type': 'application/json'};
    const token = localStorage.getItem('token');
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  // 用 refresh_token 换新的 access_token
  function doRefresh() {
    if (_refreshing) return Promise.reject(new Error('refresh in progress'));
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) return Promise.reject(new Error('no refresh token'));
    _refreshing = true;
    return fetch(BASE + '/auth/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({refresh_token: refreshToken})
    }).then(r => {
      _refreshing = false;
      if (!r.ok) throw new Error('refresh failed');
      return r.json();
    }).then(d => {
      if (d.token) {
        localStorage.setItem('token', d.token);
        return d.token;
      }
      throw new Error('no token in refresh response');
    }).catch(e => {
      _refreshing = false;
      throw e;
    });
  }

  // 401 时尝试 refresh，成功后重发原请求
  function handle401(retryFn) {
    return doRefresh().then(() => retryFn()).catch(() => {
      // refresh 也失败——清 token 跳登录
      localStorage.removeItem('token');
      localStorage.removeItem('refresh_token');
      localStorage.removeItem('user');
      if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      throw new Error('会话过期，请重新登录');
    });
  }

  function handleResp(r, retryFn) {
    if (!r.ok) {
      if (r.status === 401 && retryFn && localStorage.getItem('refresh_token')) {
        // 有 refresh_token——尝试刷新后重发
        return handle401(retryFn);
      }
      if (r.status === 401) {
        // 没有 refresh_token——直接跳登录
        localStorage.removeItem('token');
        localStorage.removeItem('refresh_token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      }
      return r.json().then(err => {
        const e = new Error(err?.detail?.message || `请求失败 (${r.status})`);
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
              localStorage.removeItem('refresh_token');
              localStorage.removeItem('user');
              if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
            }
            const err = new Error(json?.detail?.message || `请求失败 (${xhr.status})`);
            err.errors = json?.detail?.errors || null;
            reject(err);
          }
        };
        xhr.onerror = () => reject(new Error('网络错误，上传失败'));
        xhr.send(formData);
      });
    },
  };
})();
