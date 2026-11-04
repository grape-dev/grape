/**
 * 飞书消息在 Web Chat 对话框不产生重复气泡（feishu-chat-stream-unified-design 双气泡 bug, 2026-10-18）。
 *
 * 背景：Step 1 让飞书 user 行带上 task_id 后，switchConversation 的 running 探测
 * 对「DB 已有 agent 终态行的已完成会话」也追加占位气泡 + streamTask，与 msgs.map
 * 渲染的历史 agent 行并存 → 双气泡（一个显示 step 动态、一个是空气泡）。
 *
 * 修复：仅当「最后 user 带 task_id 且 DB/列表中无该 task 的 agent 行」时才追加占位；
 * 已有 agent 行 → 直接走历史渲染，不追加。
 *
 * 运行：node tests/frontend/test_chat_task_bubble.js
 * 零依赖：node:test / node:assert / node:vm / node:fs。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const CHAT_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'chat.js'),
  'utf8'
);

/**
 * 构造沙箱：注入 chat.js 所需桩。
 * opts.msgs  / opts.status ：switchConversation 渲染的消息列表与 /status 返回值
 */
function buildSandbox(opts) {
  opts = opts || {};
  var bodyInnerHTML = '';
  var bodyEl = {
    get innerHTML() { return bodyInnerHTML; },
    set innerHTML(v) { bodyInnerHTML = String(v); },
    scrollTop: 0,
    scrollHeight: 0,
    contains: function() { return false; },
  };

  var docStub = {
    getElementById: function(id) {
      if (id === 'chatBody') return bodyEl;
      if (id === 'chatInput') return {disabled: false, addEventListener: function(){}};
      if (id === 'convList') return {innerHTML: '', _html: ''};
      if (id === 'pageContent') return {innerHTML: ''};
      if (id === 'chatImgPreview') return {style: {display: 'none'}};
      if (id && id.indexOf('t') === 0) return {innerHTML: '', style: {}};
      return null;
    },
    querySelector: function(sel) {
      if (sel === '.chat-hdr h2') return {innerHTML: '', textContent: ''};
      if (sel === '.chat-in button') return {disabled: false};
      return null;
    },
    querySelectorAll: function() { return []; },
    createElement: function() {
      return {style: {}, classList: {add: function(){}, remove: function(){}}, setAttribute: function(){}, appendChild: function(){}, addEventListener: function(){}};
    },
    body: {appendChild: function() {}},
    addEventListener: function() {},
  };

  var sandbox = {
    document: docStub,
    location: {hash: ''},
    history: {back: function(){}, pushState: function(){}, replaceState: function(){}},
    I18N: {t: function(k) { return k; }, getLang: function() { return 'zh'; }},
    app: {nav: function(){}, showToast: function(){}, currentRepo: function() { return null; }, setTheme: function(){}, setThemeMode: function(){}},
    API: {
      get: function(url) {
        if (url.indexOf('/chat/messages/') !== -1) {
          return Promise.resolve({messages: opts.msgs || [], conversation: {}});
        }
        if (url.indexOf('/chat/conversations') !== -1) {
          return Promise.resolve({conversations: opts.conversations || []});
        }
        return Promise.resolve({});
      },
      post: function() { return Promise.resolve({}); },
      put: function() { return Promise.resolve({}); },
      del: function() { return Promise.resolve({}); },
    },
    localStorage: {_store: {}, getItem: function(k){return this._store[k]||null;}, setItem: function(k,v){this._store[k]=String(v);}, removeItem: function(k){delete this._store[k];}},
    sessionStorage: {_store: {}, getItem: function(k){return this._store[k]||null;}, setItem: function(k,v){this._store[k]=String(v);}, removeItem: function(k){delete this._store[k];}},
    _vcsUrl: function(rk, p) { return (rk || '') + (p || ''); },
    renderMarkdown: function(s) { return s; },
    fmtLocalTime: function(s) { return s; },
    linkifyIssueNumbers: function(s) { return s; },
    _chatTasks: {},
    _chatAbort: null,
    _openConvId: null,
    _currentConvId: 'conv-test',
    _chatStreamAbort: null,
    _chatTaskStreamAbort: null,
    _chatSkillScope: 'official',
    _chatConfigData: null,
    _chatPickerOpen: null,
    _onboardTaskId: null,
    console: console,
    setTimeout: function(fn) { try { fn(); } catch(e) {} return 0; },
    setInterval: function() { return 0; },
    clearInterval: function() {},
    clearTimeout: function() {},
    requestAnimationFrame: function() { return 0; },
    AbortController: function() {
      return {signal: {}, abort: function() {}};
    },
    TextDecoder: function() { return {decode: function() { return ''; }}; },
    // 沙箱内部状态：SSE/status fetch 计数（streamTask 是 chat.js 源码，会真实 fetch /stream）
    _streamFetchCount: 0,
    _statusFetchCount: 0,
    fetch: function(url) {
      var u = String(url);
      if (u.indexOf('/stream') !== -1) {
        sandbox._streamFetchCount++;
        return Promise.resolve({ok: true, status: 200, body: null});
      }
      if (u.indexOf('/status') !== -1) {
        sandbox._statusFetchCount++;
        return Promise.resolve({ok: true, status: 200, json: function() { return Promise.resolve({status: opts.status || 'done'}); }});
      }
      return Promise.resolve({ok: true, status: 200, json: function() { return Promise.resolve({}); }});
    },
  };

  Object.defineProperty(sandbox, 'window', {
    value: new Proxy(sandbox, {
      get: function(_, key) { return sandbox[key]; },
      set: function(_, key, val) { sandbox[key] = val; return true; },
    }),
    writable: false,
    configurable: false,
  });
  vm.createContext(sandbox);
  vm.runInContext(CHAT_SRC, sandbox, {filename: 'chat.js'});
  // 暴露只读访问器（vm 中闭包变量不可直读）
  sandbox._getBodyHtml = function() { return bodyInnerHTML; };
  sandbox._streamFetchCount = 0;
  sandbox._statusFetchCount = 0;
  return sandbox;
}

function wait(ms) { return new Promise(function(res) { setTimeout(res, ms); }); }

// 带 task_id 的 user + 对应 agent 行的历史（已完成）
function msgsWithAgentReply() {
  return [
    {role: 'user', content: '在飞书发的消息', task_id: 'chat_abc123', generate_status: 1},
    {role: 'agent', content: '这是 agent 回复', task_id: 'chat_abc123', generate_status: 2},
  ];
}

// 带 task_id 的 user、无 agent 行（进行中）
function msgsWithoutAgentReply() {
  return [
    {role: 'user', content: '在飞书发的消息', task_id: 'chat_abc123', generate_status: 1},
  ];
}

test('user 带 task_id 且已有 agent 行（status=running）→ 不追加占位，不发起 streamTask（飞书双泡主 case）', async () => {
  var sb = buildSandbox({msgs: msgsWithAgentReply(), status: 'running'});
  sb.switchConversation('conv-test');
  await wait(30);
  var html = sb._getBodyHtml();
  assert.ok(html.indexOf('这是 agent 回复') !== -1, '历史 agent 行应被渲染');
  var dotsCount = (html.match(/chat-dots/g) || []).length;
  assert.strictEqual(dotsCount, 0, '已有 agent 行时不应追加三点占位（无空气泡）');
  assert.strictEqual(sb._streamFetchCount, 0, '已有 agent 行时不应对已完成历史重新发起 SSE');
});

test('user 带 task_id 且无 agent 行 + status=running → 追加占位 + streamTask（进行中动态保留）', async () => {
  var sb = buildSandbox({msgs: msgsWithoutAgentReply(), status: 'running'});
  sb.switchConversation('conv-test');
  await wait(30);
  var html = sb._getBodyHtml();
  assert.ok(html.indexOf('chat-dots') !== -1, '进行中无 agent 行时应追加三点占位');
  assert.strictEqual(sb._streamFetchCount, 1, '应发起 SSE 订阅 (streamTask → fetch /stream)');
});

test('user 带 task_id 且无 agent 行 + status=done → 不追加占位，触发补齐拉取', async () => {
  var sb = buildSandbox({msgs: msgsWithoutAgentReply(), status: 'done'});
  sb.switchConversation('conv-test');
  await wait(30);
  var html = sb._getBodyHtml();
  assert.strictEqual((html.match(/chat-dots/g) || []).length, 0, 'done 且无 agent 行时不应追加占位');
  assert.strictEqual(sb._streamFetchCount, 0, 'done 不应再发起 SSE');
});

test('user 无 task_id → 不触发探测，无占位追加', async () => {
  var sb = buildSandbox({msgs: [{role: 'user', content: '普通消息', task_id: '', generate_status: null}]});
  sb.switchConversation('conv-test');
  await wait(30);
  var html = sb._getBodyHtml();
  assert.strictEqual((html.match(/chat-dots/g) || []).length, 0, '无 task_id 不应追加占位');
  assert.strictEqual(sb._streamFetchCount, 0, '无 task_id 不应发起 streamTask');
});