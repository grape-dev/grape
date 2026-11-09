/* Grape 前端可观测性接入 — Grafana Faro (vendored IIFE @2.13.0)
 * 上报链路: 浏览器 SDK → 本站 /faro/collect (spa.py 反代) → Alloy :12347 → Loki/Tempo
 * 采集: console 日志、未捕获异常、Web Vitals、RUM trace、页面视图
 * Tracing: fetch 注入 W3C traceparent（propagateTraceHeaderCorsUrls 同源覆盖），
 *          后端 OTel span 同 trace 关联 → Grafana 里前端点击可跳完整调用链
 */
(function () {
  'use strict';
  var SDK = window.GrafanaFaroWebSdk;
  if (!SDK) { console.warn('[faro] SDK not loaded, telemetry disabled'); return; }
  var TRACING = window.GrafanaFaroWebTracing;

  var ver = (window.GRAPE_VERSION || (document.documentElement.getAttribute('data-version')) || 'dev');
  // 环境标识：优先读服务端注入的 <meta name="grape-env">，否则仅按回环地址判 local，
  // 其余一律 dev（不在前端硬编码任何内部域名，QA §1.1-1-5）。
  var envMeta = document.querySelector('meta[name="grape-env"]');
  var env = (envMeta && envMeta.content) || '';
  if (!env) {
    env = (location.hostname === '127.0.0.1' || location.hostname === 'localhost') ? 'local' : 'dev';
  }

  var instrumentations = undefined;
  if (TRACING && TRACING.TracingInstrumentation) {
    // SDK 自带默认三件套（Console/WebVitals/Session）+ Tracing（fetch 注入 traceparent）
    // propagateTraceHeaderCorsUrls 同源也要覆盖（默认只对跨域 API 注入）
    instrumentations = SDK.getWebInstrumentations().concat([new TRACING.TracingInstrumentation({
      instrumentationOptions: {
        propagateTraceHeaderCorsUrls: [/./],
      },
    })]);
  }

  SDK.initializeFaro({
    url: '/faro/collect',
    app: {
      name: 'grape-web',
      version: ver,
      environment: env
    },
    instrumentations: instrumentations,
    sessionTracking: { persistent: false, samplingRate: 1 }
  });

  // 环境信息补齐（按登录后 localStorage 可用字段，未登录时跳过）
  var _applyUser = function () {
    try {
      var u = JSON.parse(localStorage.getItem('user') || 'null');
      if (u && (u.username || u.nickname || u.id)) {
        SDK.faro.api.setUser({ id: String(u.id || u.username || ''), username: (u.username || u.nickname || '') });
      }
    } catch (e) { /* ignore */ }
  };
  _applyUser();
  // 同 tab 登录不会触发 storage 事件 → 轮询兜底（登录后 app.js 写 localStorage 'user'）
  var _lastU = null;
  setInterval(function () {
    var raw = null;
    try { raw = localStorage.getItem('user'); } catch (e) { return; }
    if (raw && raw !== _lastU) { _lastU = raw; _applyUser(); }
  }, 5000);
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
