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
      return r.json().then(err => { throw new Error(err?.detail?.message || `请求失败 (${r.status})`); });
    }
    return r.json();
  }

  return {
    get: (path) => fetch(BASE + path, {headers: headers()}).then(handleResp),
    post: (path, body) => fetch(BASE + path, {method: 'POST', headers: headers(), body: JSON.stringify(body)}).then(handleResp),
    put: (path, body) => fetch(BASE + path, {method: 'PUT', headers: headers(), body: JSON.stringify(body)}).then(handleResp),
    del: (path) => fetch(BASE + path, {method: 'DELETE', headers: headers()}).then(handleResp),
  };
})();
