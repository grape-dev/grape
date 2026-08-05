var _AGENT_TYPES = {};  // 全局 Agent 类型缓存（由 app.js 填充）

// 全局工具：UTC 时间字符串（SQLite "YYYY-MM-DD HH:MM:SS" 或 ISO）转本地（+8）展示
function fmtLocalTime(utcStr) {
  if (!utcStr) return '';
  let s = String(utcStr).replace('T', ' ').replace('Z', '');
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ ](\d{2}):(\d{2}):(\d{2})/);
  if (!m) return utcStr;
  const d = new Date(Date.UTC(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]));
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const API = (() => {
  const BASE = '/api';

  function headers() {
    const h = {'Content-Type': 'application/json'};
    const token = localStorage.getItem('token');
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  function handleResp(r) {
    if (!r.ok) {
      if (r.status === 401) {
        localStorage.removeItem('token');
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

  return {
    get: (path) => fetch(BASE + path, {headers: headers()}).then(handleResp),
    post: (path, body) => fetch(BASE + path, {method: 'POST', headers: headers(), body: JSON.stringify(body)}).then(handleResp),
    put: (path, body) => fetch(BASE + path, {method: 'PUT', headers: headers(), body: JSON.stringify(body)}).then(handleResp),
    del: (path) => fetch(BASE + path, {method: 'DELETE', headers: headers()}).then(handleResp),
    upload: (path, formData, onUploadProgress) => {
      const token = localStorage.getItem('token');
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', BASE + path);
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
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
              localStorage.removeItem('token');
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
