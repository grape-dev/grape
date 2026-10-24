// ═══════════════════════════════════════════════════════════════
// auto-recovery.js — 页面空闲后 Failed to fetch 恢复（Issue #206）
//
// 业界主流收敛（v2）：
//   核心 = 请求层重试（在 api.js，指数退避+仅幂等）；此处为兜底：
//   1. /api/ping 60s 探活（低成本断线感知，不做"保活"——浏览器连接池自动管理）
//   2. unhandledrejection 拦截：捕获未处理的 TypeError: Failed to fetch
//   3. 默认行为 = Toast「连接已断开，点击重试」（非阻塞、不中断输入、不整页刷新）
//   4. auto-reload 仅显式开启时（localStorage.grapeAutoReload='1' 或 data-auto-reload）
//      才自动刷新，且带关键操作保护（聚焦输入 / [data-critical] 不刷）+ 抖动 + 防循环
//
// 设计取舍：
//   - 请求层重试（api.js）负责瞬时断连自愈——本文件不重复重试
//   - 自动整页刷新是反模式（丢状态/放大压力），默认关闭，仅 opt-in
//   - 写操作（POST/PUT/DELETE）失败由 api.js 抛给业务，本文件不刷新（防重复提交）
// ═══════════════════════════════════════════════════════════════
(function () {
  if (typeof window === 'undefined') return;

  var PROBE_URL = '/api/ping';
  var HEARTBEAT_INTERVAL = 60 * 1000;      // 60s 探活
  var RELOAD_BASE_DELAY = 3000;             // auto-reload 基准延迟
  var RELOAD_JITTER = 3000;                 // 抖动（打散多人同时恢复齐刷）
  var RELOAD_LOCK_KEY = 'grapeAutoReloadAt'; // 防循环（sessionStorage）
  var AUTO_RELOAD_FLAG = 'grapeAutoReload';  // localStorage 显式开启开关

  // ── 简化 Toast（不依赖 app.showToast，避免循环依赖）──
  var _toastEl = null;
  var _toastTimer = null;
  function _toast(msg) {
    try {
      if (!_toastEl) {
        _toastEl = document.createElement('div');
        _toastEl.style.cssText =
          'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:99999;' +
          'background:rgba(30,30,32,.92);color:#fff;padding:10px 18px;border-radius:10px;' +
          'font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;align-items:center;gap:10px;' +
          'max-width:80vw';
        document.body.appendChild(_toastEl);
      }
      var btnTxt = (window.I18N && I18N.t('common.refresh')) || '刷新';
      _toastEl.innerHTML = '<span style="flex:1">' + msg + '</span>' +
        '<button style="border:1px solid #fff6;background:transparent;color:#fff;border-radius:6px;' +
        'padding:3px 10px;font-size:12px;cursor:pointer;flex-shrink:0">' + btnTxt + '</button>';
      _toastEl.style.display = 'flex';
      var btn = _toastEl.querySelector('button');
      btn.onclick = function () { window.location.reload(); };
      if (_toastTimer) clearTimeout(_toastTimer);
      _toastTimer = setTimeout(function () { if (_toastEl) _toastEl.style.display = 'none'; }, 12000);
    } catch (e) { /* ignore */ }
  }

  // ── 关键操作检测 ──
  function _isTyping() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = el.tagName || '';
    return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable === true;
  }
  function _isCriticalPage() {
    return !!document.querySelector('[data-critical]');
  }

  // ── auto-reload 是否显式开启（页面级 data-auto-reload 优先，其次 localStorage 开关）──
  function _autoReloadEnabled() {
    try {
      if (document.documentElement && document.documentElement.getAttribute('data-auto-reload') === '1') return true;
      return localStorage.getItem(AUTO_RELOAD_FLAG) === '1';
    } catch (e) { return false; }
  }

  // ── 防循环：auto-reload 刷新后 5s 内不再自动刷 ──
  function _reloadLocked() {
    try {
      var last = parseInt(sessionStorage.getItem(RELOAD_LOCK_KEY) || '0', 10);
      if (last && Date.now() - last < 5000) return true;
    } catch (e) { /* ignore */ }
    return false;
  }
  function _lockReload() {
    try { sessionStorage.setItem(RELOAD_LOCK_KEY, String(Date.now())); } catch (e) { /* ignore */ }
  }

  // ── 去抖：同一会话内多次失败只触发一次恢复评估 ──
  var _recoveryScheduled = false;
  function _scheduleRecovery() {
    if (_recoveryScheduled) return;
    _recoveryScheduled = true;

    // 1) 连接预检：ping 可达 → 瞬断已自愈（请求层已重试过），无需动作
    fetch(PROBE_URL, {method: 'GET', cache: 'no-store'})
      .then(function (r) {
        if (r && r.ok) { _recoveryScheduled = false; return; }
        return _afterFailedProbe();
      })
      .catch(function () { return _afterFailedProbe(); });
  }

  function _afterFailedProbe() {
    // 2) 默认行为：非阻塞提示 + 手动重试（不中断输入、不整页刷新）
    var msg = (window.I18N && I18N.t('common.network_error')) || '连接已断开，点击刷新';
    _toast(msg);
    _recoveryScheduled = false;  // 释放，用户后续失败可再次提示

    // 3) auto-reload 显式开启时：关键操作保护 + 抖动 + 防循环后自动刷新
    if (_autoReloadEnabled() && !_isTyping() && !_isCriticalPage() && !_reloadLocked()) {
      _lockReload();
      var delay = RELOAD_BASE_DELAY + Math.round(Math.random() * RELOAD_JITTER);
      setTimeout(function () {
        try { if (window._flushLatency) window._flushLatency(); } catch (e) { /* ignore */ }
        window.location.reload();
      }, delay);
    }
  }

  // ── 探活：页面可见时每 60s ping 一次（感知断线，非防断连）──
  var _hbTimer = null;
  function _startHeartbeat() {
    if (_hbTimer) clearInterval(_hbTimer);
    _hbTimer = setInterval(function () {
      if (document.visibilityState === 'hidden') return;  // 隐藏暂停
      try {
        fetch(PROBE_URL, {method: 'GET', cache: 'no-store'}).catch(function () { /* 失败留给 unhandledrejection 恢复 */ });
      } catch (e) { /* ignore */ }
    }, HEARTBEAT_INTERVAL);
  }

  // ── unhandledrejection：捕获未被业务 catch 的 fetch 失败 ──
  window.addEventListener('unhandledrejection', function (ev) {
    try {
      var reason = ev && ev.reason;
      // 只处理网络层失败（TypeError: Failed to fetch），超时/外部取消（silent）不处理
      if (reason && reason.name === 'TypeError' && /Failed to fetch/i.test(String(reason.message || ''))) {
        ev.preventDefault();  // 避免默认 console 报错噪音
        _scheduleRecovery();
      }
    } catch (e) { /* ignore */ }
  });

  // ── 启动 ──
  function _init() { _startHeartbeat(); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }
})();