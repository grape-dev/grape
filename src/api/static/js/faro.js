/* Grape 前端可观测性接入 — Grafana Faro (vendored IIFE @2.13.0)
 * 上报链路: 浏览器 SDK → 本站 /faro/collect (spa.py 反代) → Alloy :12347 → Loki/Tempo
 * 采集: console 日志、未捕获异常、Web Vitals、RUM trace、页面视图
 */
(function () {
  'use strict';
  var SDK = window.GrafanaFaroWebSdk;
  if (!SDK) { console.warn('[faro] SDK not loaded, telemetry disabled'); return; }

  var ver = (window.GRAPE_VERSION || (document.documentElement.getAttribute('data-version')) || 'dev');

  SDK.initializeFaro({
    url: '/faro/collect',
    app: {
      name: 'grape-web',
      version: ver,
      environment: window.location.port === '8081' ? 'dev' : 'prod'
    },
    sessionTracking: { persistent: false }
  });

  // 环境信息补齐（按登录后 localStorage 可用字段，未登录时跳过）
  var _applyUser = function () {
    try {
      var u = JSON.parse(localStorage.getItem('userInfo') || 'null');
      if (u && (u.username || u.id)) {
        SDK.faro.api.setUser({ id: String(u.id || ''), username: u.username || u.nickname || '' });
      }
    } catch (e) { /* ignore */ }
  };
  _applyUser();
  window.addEventListener('storage', _applyUser);

  // 页面视图: SPA 内部切换 (hash/router 页面切换) 手动上报
  var _lastPath = null;
  var reportView = function () {
    var p = location.pathname + (location.search || '');
    if (p === _lastPath) return;
    _lastPath = p;
    try { SDK.faro.api.pushEvent('view', { path: p }); } catch (e) { /* ignore */ }
  };
  reportView();
  // 历史切换钩子（SPA 页面切换多为 hash/replaceState）
  ['pushState', 'replaceState'].forEach(function (m) {
    var orig = history[m];
    if (typeof orig === 'function') {
      history[m] = function () { orig.apply(this, arguments); setTimeout(reportView, 0); };
    }
  });
  window.addEventListener('hashchange', reportView);

  window.__faro = SDK.faro; // 调试入口: __faro.api.pushLog / pushEvent / pushError
})();
