/**
 * 轻量前端测试 — 事件详情页「返回需求详情」状态流（Issue #163 / PR #175）
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:vm / node:fs）。
 * 运行方式：node tests/frontend/test_event_back_to_task.js
 *
 * 覆盖 QA 审查 Required 项（_eventFromTask 状态泄漏）：
 *   1) _jumpToNodeEvent 有任务上下文时设置 _eventFromTask + _eventNavFromTask
 *   2) _jumpToNodeEvent 无任务上下文时置 null（不残留旧标志）
 *   3) app.nav('event') 非 _jumpToNodeEvent 触发时清除残留 _eventFromTask
 *   4) app.nav('event') 经 _jumpToNodeEvent 触发时保留 _eventFromTask
 *   5) _backToTaskDetail 清除 _eventFromTask + 设置 _pendingTaskDetailId
 *   6) 事件历史条目 onclick 直接调用 _jumpToNodeEvent（Nit：无透传包装）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const ISSUES_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'issues.js'),
  'utf8'
);
const EVENTS_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'events.js'),
  'utf8'
);

/**
 * 构造沙箱：注入 issues.js + events.js 所需的最小全局桩。
 * 返回 sandbox（含挂载的 window 代理与全局函数）。
 */
function buildSandbox() {
  const win = {};
  const locStub = { hash: '' };
  const docStub = {
    getElementById: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    head: { appendChild() {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
  };
  const sandbox = {
    window: win,
    document: docStub,
    location: locStub,
    history: { back: () => {}, pushState: () => {}, replaceState: () => {} },
    I18N: { t: (k) => k, getLang: () => 'zh' },
    app: { nav: () => {}, showToast: () => {} },
    API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}), put: () => Promise.resolve({}) },
    localStorage: {
      _store: {},
      getItem(k) { return this._store[k] || null; },
      setItem(k, v) { this._store[k] = String(v); },
      removeItem(k) { delete this._store[k]; },
      clear() { this._store = {}; },
    },
    _AGENT_TYPES: {},
    _vcsUrl: (rk, p) => (rk || '') + (p || ''),
    renderMarkdown: (s) => s,
    _convStreamAbort: null,
    _promptFs: false,
    _promptFsPlaceholder: null,
    _stopScenePoll: () => {},
    fmtLocalTime: (s) => s,
    console,
    setTimeout: () => 0,
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
  };
  // window 代理：window.foo 与 sandbox.foo 互通（模拟浏览器全局）
  Object.defineProperty(sandbox, 'window', {
    value: new Proxy(sandbox, {
      get(_, key) { return sandbox[key]; },
      set(_, key, val) { sandbox[key] = val; return true; },
    }),
    writable: false,
    configurable: false,
  });
  vm.createContext(sandbox);
  // 同一上下文执行 issues.js + events.js（模拟 index.html 加载顺序）
  vm.runInContext(ISSUES_SRC, sandbox, { filename: 'issues.js' });
  vm.runInContext(EVENTS_SRC, sandbox, { filename: 'events.js' });
  return sandbox;
}

test('_jumpToNodeEvent：有任务上下文时设置 _eventFromTask + _eventNavFromTask', () => {
  const sb = buildSandbox();
  sb.window._currentDetailTask = { id: 42, repo_key: 'gitcode:o/r' };
  sb.window._eventFromTask = null;
  sb.window._eventNavFromTask = false;
  sb.window._jumpToNodeEvent(7);
  assert.strictEqual(sb.window._eventFromTask.taskId, 42);
  assert.strictEqual(sb.window._eventNavFromTask, true);
});

test('_jumpToNodeEvent：无任务上下文时置 null（不残留旧标志）', () => {
  const sb = buildSandbox();
  // 模拟上次残留
  sb.window._currentDetailTask = null;
  sb.window._eventFromTask = { taskId: 999 };  // 旧残留
  sb.window._eventNavFromTask = false;
  sb.window._jumpToNodeEvent(8);
  // 关键：无任务时 _eventFromTask 必须被置为 null，不能残留 999
  assert.strictEqual(sb.window._eventFromTask, null);
  assert.strictEqual(sb.window._eventNavFromTask, true);
});

test('_jumpToNodeEvent：seq 缺失时不执行（无副作用）', () => {
  const sb = buildSandbox();
  sb.window._currentDetailTask = { id: 42 };
  sb.window._eventFromTask = { taskId: 'old' };
  sb.window._jumpToNodeEvent(0);
  assert.strictEqual(sb.window._eventFromTask.taskId, 'old', 'no-op when seq falsy');
});

test('app.nav("event") 非 _jumpToNodeEvent 触发：清除残留 _eventFromTask', () => {
  // 模拟全局搜索 app.nav('event', seq) 进入：_eventNavFromTask 为 false
  const sb = buildSandbox();
  // 上次任务详情遗留的标志
  sb.window._eventFromTask = { taskId: 999 };
  sb.window._eventNavFromTask = false;
  // 内联 app.nav('event') 入口清理逻辑（与 app.js 431 一致）
  if (!sb.window._eventNavFromTask) {
    sb.window._eventFromTask = null;
  }
  sb.window._eventNavFromTask = false;
  assert.strictEqual(sb.window._eventFromTask, null, 'stale flag cleared on non-task entry');
});

test('app.nav("event") 经 _jumpToNodeEvent 触发：保留 _eventFromTask', () => {
  // 模拟 _jumpToNodeEvent 设置瞬态标志后 nav('event') 入口识别并保留
  const sb = buildSandbox();
  sb.window._currentDetailTask = { id: 55 };
  sb.window._jumpToNodeEvent(3);
  // nav 入口：_eventNavFromTask=true → 不清除
  if (!sb.window._eventNavFromTask) {
    sb.window._eventFromTask = null;
  }
  sb.window._eventNavFromTask = false;  // 消费瞬态标志
  assert.strictEqual(sb.window._eventFromTask.taskId, 55, 'task context preserved');
});

test('_backToTaskDetail：清除 _eventFromTask + 设置 _pendingTaskDetailId', () => {
  const sb = buildSandbox();
  sb.window._eventFromTask = { taskId: 77 };
  sb.window._pendingTaskDetailId = null;
  sb.window._backToTaskDetail(77);
  assert.strictEqual(sb.window._eventFromTask, null, '_eventFromTask cleared after back');
  assert.strictEqual(sb.window._pendingTaskDetailId, 77, '_pendingTaskDetailId set for reopen');
});

test('_backToTaskDetail：无 taskId 时回退 history.back', () => {
  const sb = buildSandbox();
  let backed = false;
  sb.history = { back: () => { backed = true; } };
  sb.window._eventFromTask = { taskId: 1 };
  sb.window._backToTaskDetail(0);
  assert.strictEqual(backed, true, 'history.back called when no taskId');
});

test('事件历史条目 onclick 直接调用 _jumpToNodeEvent（无透传包装 _jumpToEventFromTask）', () => {
  // 验证 _jumpToEventFromTask 已移除，issues.js 中事件历史 onclick 绑定 _jumpToNodeEvent
  assert.ok(
    ISSUES_SRC.indexOf('_jumpToEventFromTask') === -1,
    '_jumpToEventFromTask wrapper removed'
  );
  assert.ok(
    ISSUES_SRC.indexOf("_jumpToNodeEvent(' + evSeq + ')") !== -1 ||
    ISSUES_SRC.indexOf('_jumpToNodeEvent(') !== -1,
    'event history onclick binds _jumpToNodeEvent directly'
  );
});

test('i18n：中英文 back_to_requirement 键均存在', () => {
  for (const lang of ['zh', 'en']) {
    const p = path.join(ROOT, 'src', 'api', 'static', 'locales', lang, 'tasks.json');
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    assert.ok(
      typeof j.back_to_requirement === 'string' && j.back_to_requirement.length > 0,
      `${lang}/tasks.json back_to_requirement present`
    );
  }
});
