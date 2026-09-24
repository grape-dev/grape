// ═══════════════════════════════════════════════════════════════
// auto-recovery.js — 页面空闲后 Failed to fetch 自动恢复（Issue #206）
//
// 三层：
//   1. 会话保活 heartbeat：每 60s ping /api/ping，保持连接在 keepalive 窗口内活跃
//   2. unhandledrejection 拦截：捕获未处理的 TypeError: Failed to fetch，统一走恢复流程
//   3. 关键操作感知恢复：聚焦输入/[data-critical] 页 → Toast 不刷；非关键页 → 3s+抖动自动 reload
//
// 压力控制：
//   - heartbeat 仅页面可见时发送
//   - 自动刷新带随机抖动（0~3s）+ sessionStorage 防循环（5s 窗口）
//   - 刷新只发生在读/静默失败；写操作失败仅提示
// ═══════════════════════════════════════════════════════════════
(function () {
  if (typeof window === 'undefined') return;

  var HEARTBEAT_INTERVAL = 60 * 1000;      // 60s 保活
  var RELOAD_BASE_DELAY = 3000;             // 非关键页刷新基准延迟
  var RELOAD_JITTER = 3000;                 // 抖动幅度（打散多人同时恢复齐刷）
  var RELOAD_LOCK_KEY = 'grapeAutoReloadAt'; // 防循环标记

  // ── Toast 极简实现（不依赖 app.showToast，避免循环依赖）──
  var _toastEl = null;
  function _toast(msg) {
    try {
      if (!_toastEl) {
        _toastEl = document.createElement('div');
        _toastEl.style.cssText =
          'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:99999;' +
          'background:rgba(30,30,32,.92);color:#fff;padding:10px 18px;border-radius:10px;' +
          'font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;align-items:center;gap:10px';
        document.body.appendChild(_toastEl);
      }
      _toastEl.innerHTML = '<span>' + msg + '</span>' +
        '<button style="border:1px solid #fff6;background:transparent;color:#fff;border-radius:6px;' +
        'padding:3px 10px;font-size:12px;cursor:pointer">' + (window.I18N && I18N.t('common.refresh')) + '</button>';
      _toastEl.style.display = 'flex';
      var btn = _toastEl.querySelector('button');
      btn.onclick = function () { window.location.reload(); };
      // 12s 自动隐藏
      if (_toastTimer) clearTimeout(_toastTimer);
      var _toastTimer = setTimeout(function () { if (_toastEl) _toastEl.style.display = 'none'; }, 12000);
    } catch (e) { /* ignore */ }
  }

  // ── 关键操作检测 ──
  function _isTyping() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = el.tagName || '';
    return tag === 'INPUT' || tag === 'TEXTAREA' || (el.isContentEditable === true);
  }
  function _isCriticalPage() {
    return !!document.querySelector('[data-critical]');
  }

  // ── 防循环：刷新后 5s 内不自动再刷 ──
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

  // ── 去抖：同一会话内多次失败只触发一次恢复调度 ──
  var _recoveryScheduled = false;
  function _scheduleRecovery() {
    if (_recoveryScheduled) return;
    _recoveryScheduled = true;

    // 1) 连接预检：health 可达 → 瞬断恢复，无需刷新
    fetch('/api/ping', {method: 'GET', cache: 'no-store'})
      .then(function (r) {
        if (r && r.ok) { _recoveryScheduled = false; return; }  // 已恢复
        return _afterFailedProbe();
      })
      .catch(function () { return _afterFailedProbe(); });
  }

  function _afterFailedProbe() {
    // 2) 关键操作感知：
    //    - 正在输入 / 关键页 → Toast 提示，不自动刷新
    //    - 非关键页 → 3s+抖动 自动刷新
    if (_isTyping() || _isCriticalPage()) {
      var msg = (window.I18N && I18N.t('common.network_error')) || '连接已断开，点击刷新';
      _toast(msg);
      _recoveryScheduled = false;  // 允许用户回来后再次触发
      return;
    }
    if (_reloadLocked()) { _recoveryScheduled = false; return; }
    _lockReload();
    var delay = RELOAD_BASE_DELAY + Math.round(Math.random() * RELOAD_JITTER);
    setTimeout(function () {
      // 刷新前冲刷时延缓冲
      try { if (window._flushLatency) window._flushLatency(); } catch (e) { /* ignore */ }
      window.location.reload();
    }, delay);
  }

  // ── heartbeat：页面可见时每 60s ping 一次 ──
  var _hbTimer = null;
  function _startHeartbeat() {
    if (_hbTimer) clearInterval(_hbTimer);
    _hbTimer = setInterval(function () {
      if (document.visibilityState === 'hidden') return;  // 隐藏暂停
      try {
        fetch('/api/ping', {method: 'GET', cache: 'no-store'}).catch(function () { /* 失败留给 unhandledrejection 恢复 */ });
      } catch (e) { /* ignore */ }
    }, HEARTBEAT_INTERVAL);
  }

  // ── unhandledrejection：捕获未被业务 catch 的 fetch 失败 ──
  window.addEventListener('unhandledrejection', function (ev) {
    try {
      var reason = ev && ev.reason;
      if (reason && reason.name === 'TypeError' && /Failed to fetch/i.test(String(reason.message || ''))) {
        ev.preventDefault();  // 避免默认 console 报错噪音
        _scheduleRecovery();
      }
      // 超时/外部取消（silent）不处理
    } catch (e) { /* ignore */ }
  });

  // ── 启动 ──
  document.addEventListener('DOMContentLoaded', function () { _startHeartbeat(); });
  _startHeartbeat();
})();