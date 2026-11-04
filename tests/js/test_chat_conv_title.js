/**
 * Issue #293: chat agent 会话列表标题显示优化测试。
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:vm / node:fs）。
 * 运行方式：node tests/frontend/test_chat_conv_title.js
 *
 * 覆盖：
 *   1) _stripIssuePrefix: 剥离 "Issue #N · " 前缀并返回编号
 *   2) _stripIssuePrefix: 无前缀标题不受影响
 *   3) _stripIssuePrefix: 空标题安全处理
 *   4) loadConversations: meta 行含 #N 纯文本（issue_number > 0）
 *   5) loadConversations: 普通会话 meta 行无 #N
 *   6) loadConversations: 标题不再含 Issue 链接
 *   7) switchConversation: 头部标题剥离前缀
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
 * 构造沙箱：注入 chat.js 所需的最小全局桩。
 */
function buildSandbox(opts) {
  opts = opts || {};
  var convListHtml = '';
  var hdrInnerHTML = '';
  var apiGetHandler = opts.apiGetHandler || function(url) {
    if (url.indexOf('/chat/conversations') !== -1) {
      return {conversations: opts.conversations || []};
    }
    if (url.indexOf('/chat/messages/') !== -1) {
      return {messages: [], conversation: opts.conv || {}};
    }
    if (url.indexOf('/chat/config') !== -1) {
      return {backend: {}, skill_scope: 'official'};
    }
    return {};
  };

  var docStub = {
    _convListHtml: '',
    _hdrHtml: '',
    getElementById: function(id) {
      if (id === 'convList') return {innerHTML: '', _html: ''};
      if (id === 'pageContent') return {innerHTML: ''};
      if (id === 'chatBody') return {innerHTML: '', scrollTop: 0, scrollHeight: 0};
      if (id === 'chatInput') return {disabled: false};
      if (id === 'chatImgPreview') return {style: {display: 'none'}};
      return null;
    },
    querySelector: function(sel) {
      if (sel === '.chat-hdr h2') return {innerHTML: '', _html: '', textContent: ''};
      if (sel === '.chat-in button') return {disabled: false};
      return null;
    },
    querySelectorAll: function() { return []; },
    addEventListener: function() {},
    createElement: function() {
      return {style: {}, classList: {add(){}, remove(){}}, setAttribute(){}, appendChild(){}, addEventListener(){}};
    },
    body: {appendChild() {}},
  };

  // Override getElementById to capture HTML output
  var _convListEl = null;
  var _hdrEl = null;
  var _chatBodyEl = null;
  docStub.getElementById = function(id) {
    if (id === 'convList') {
      if (!_convListEl) _convListEl = {innerHTML: '', _html: ''};
      return _convListEl;
    }
    if (id === 'pageContent') return {innerHTML: ''};
    if (id === 'chatBody') {
      if (!_chatBodyEl) _chatBodyEl = {innerHTML: '', scrollTop: 0, scrollHeight: 0};
      return _chatBodyEl;
    }
    if (id === 'chatInput') return {disabled: false, addEventListener(){}};
    if (id === 'chatImgPreview') return {style: {display: 'none'}};
    if (id === 'chatModelBtn') return {classList: {add(){}, remove(){}}, getBoundingClientRect: function(){return {top:0,left:0};}};
    if (id === 'chatSkillBtn') return {classList: {add(){}, remove(){}}, getBoundingClientRect: function(){return {top:0,left:0};}};
    return null;
  };
  docStub.querySelector = function(sel) {
    if (sel === '.chat-hdr h2') {
      if (!_hdrEl) _hdrEl = {innerHTML: '', _html: '', textContent: ''};
      return _hdrEl;
    }
    if (sel === '.chat-in button') return {disabled: false};
    return null;
  };

  var sandbox = {
    document: docStub,
    location: {hash: ''},
    history: {back(){}, pushState(){}, replaceState(){}},
    I18N: {t: function(k) { return k; }, getLang: function() { return 'zh'; }},
    app: {
      nav() {},
      showToast() {},
      currentRepo: function() { return opts.repo || null; },
      setTheme() {},
      setThemeMode() {},
    },
    API: {
      get: function(url) { return Promise.resolve(apiGetHandler(url)); },
      post: function() { return Promise.resolve({}); },
      put: function() { return Promise.resolve({}); },
      del: function() { return Promise.resolve({}); },
    },
    localStorage: {
      _store: {},
      getItem: function(k) { return this._store[k] || null; },
      setItem: function(k, v) { this._store[k] = String(v); },
      removeItem: function(k) { delete this._store[k]; },
    },
    sessionStorage: {
      _store: {},
      getItem: function(k) { return this._store[k] || null; },
      setItem: function(k, v) { this._store[k] = String(v); },
      removeItem: function(k) { delete this._store[k]; },
    },
    _vcsUrl: function(rk, p) { return (rk || '') + (p || ''); },
    renderMarkdown: function(s) { return s; },
    fmtLocalTime: function(s) { return s; },
    _chatTasks: {},
    _chatAbort: null,
    _openConvId: null,
    _currentConvId: null,
    _chatStreamAbort: null,
    _chatSkillScope: 'official',
    _chatConfigData: null,
    _chatPickerOpen: null,
    _onboardTaskId: null,
    console: console,
    setTimeout: setTimeout,
    setInterval: function() { return 0; },
    clearInterval: function() {},
    requestAnimationFrame: function() { return 0; },
    fetch: function() { return Promise.resolve({json: function() { return Promise.resolve({status: 'done'}); }}); },
    _getConvListHtml: function() { return _convListEl ? _convListEl.innerHTML : ''; },
    _getHdrHtml: function() { return _hdrEl ? _hdrEl.innerHTML : ''; },
    _resetCaptured: function() { if (_convListEl) _convListEl.innerHTML = ''; if (_hdrEl) _hdrEl.innerHTML = ''; if (_chatBodyEl) _chatBodyEl.innerHTML = ''; },
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
  return sandbox;
}

test('_stripIssuePrefix: 剥离 "Issue #N · " 前缀并返回编号', () => {
  var sb = buildSandbox();
  var r = sb._stripIssuePrefix('Issue #293 · Fix bug');
  assert.strictEqual(r.title, 'Fix bug');
  assert.strictEqual(r.issueNumber, 293);
});

test('_stripIssuePrefix: 无前缀标题不受影响', () => {
  var sb = buildSandbox();
  var r = sb._stripIssuePrefix('普通会话标题');
  assert.strictEqual(r.title, '普通会话标题');
  assert.strictEqual(r.issueNumber, 0);
});

test('_stripIssuePrefix: 空标题安全处理', () => {
  var sb = buildSandbox();
  var r1 = sb._stripIssuePrefix('');
  assert.strictEqual(r1.title, '');
  assert.strictEqual(r1.issueNumber, 0);
  var r2 = sb._stripIssuePrefix(null);
  assert.strictEqual(r2.title, '');
  assert.strictEqual(r2.issueNumber, 0);
});

test('_stripIssuePrefix: 无 · 分隔符不剥离', () => {
  var sb = buildSandbox();
  var r = sb._stripIssuePrefix('Issue #293');
  assert.strictEqual(r.title, 'Issue #293');
  assert.strictEqual(r.issueNumber, 0);
});

test('loadConversations: meta 行含 #N 纯文本（issue_number > 0）', async () => {
  var sb = buildSandbox({
    conversations: [{
      id: 'conv1',
      title: 'Issue #293 · Fix bug',
      issue_number: 293,
      created_at: '2026-10-11T15:34:00',
      repo_key: 'gitcode:test/repo',
    }],
  });
  sb._currentConvId = 'conv1';
  sb.loadConversations(false);
  await new Promise(function(r) { setTimeout(r, 10); });
  var html = sb._getConvListHtml();
  assert.ok(html.indexOf('#293') !== -1, 'meta 行应含 #293');
  assert.ok(html.indexOf('conv-meta') !== -1, '应使用 conv-meta 样式');
  assert.ok(html.indexOf('<a ') === -1 || html.indexOf('<a ') > html.indexOf('conv-title'), '标题不应含链接');
});

test('loadConversations: 普通会话 meta 行无 #N', async () => {
  var sb = buildSandbox({
    conversations: [{
      id: 'conv2',
      title: '普通聊天',
      issue_number: 0,
      created_at: '2026-10-11T15:34:00',
      repo_key: '',
    }],
  });
  sb._currentConvId = 'conv2';
  sb.loadConversations(false);
  await new Promise(function(r) { setTimeout(r, 10); });
  var html = sb._getConvListHtml();
  assert.ok(html.indexOf('普通聊天') !== -1, '标题应显示普通聊天');
  assert.ok(html.indexOf('#0') === -1, 'meta 行不应含 #0');
});

test('loadConversations: 标题不再含 Issue 链接', async () => {
  var sb = buildSandbox({
    conversations: [{
      id: 'conv3',
      title: 'Issue #293 · Fix bug',
      issue_number: 293,
      created_at: '2026-10-11T15:34:00',
      repo_key: 'gitcode:test/repo',
    }],
  });
  sb._currentConvId = 'conv3';
  sb.loadConversations(false);
  await new Promise(function(r) { setTimeout(r, 10); });
  var html = sb._getConvListHtml();
  var titleStart = html.indexOf('conv-title');
  var titleEnd = html.indexOf('</div>', titleStart);
  var titleSection = html.substring(titleStart, titleEnd);
  assert.ok(titleSection.indexOf('Fix bug') !== -1, '标题应含 Fix bug');
  assert.ok(titleSection.indexOf('Issue #293') === -1, '标题不应含 Issue #293');
  assert.ok(titleSection.indexOf('<a ') === -1, '标题不应含链接标签');
});

test('loadConversations: issue_number=0 回退标题前缀解析', async () => {
  var sb = buildSandbox({
    conversations: [{
      id: 'conv4',
      title: 'Issue #42 · Answer question',
      issue_number: 0,
      created_at: '2026-10-11T15:34:00',
      repo_key: 'gitcode:test/repo',
    }],
  });
  sb._currentConvId = 'conv4';
  sb.loadConversations(false);
  await new Promise(function(r) { setTimeout(r, 10); });
  var html = sb._getConvListHtml();
  assert.ok(html.indexOf('#42') !== -1, 'meta 行应含从标题解析的 #42');
  assert.ok(html.indexOf('Answer question') !== -1, '标题应含剥离后的 Answer question');
});

test('switchConversation: 头部标题剥离前缀', async () => {
  var sb = buildSandbox({
    conv: {
      title: 'Issue #293 · Fix bug',
      repo_key: 'gitcode:test/repo',
    },
  });
  sb._currentConvId = 'conv5';
  sb.switchConversation('conv5');
  await new Promise(function(r) { setTimeout(r, 20); });
  var hdrHtml = sb._getHdrHtml();
  assert.ok(hdrHtml.indexOf('Fix bug') !== -1, '头部标题应含 Fix bug');
  assert.ok(hdrHtml.indexOf('Issue #293 ·') === -1, '头部标题不应含 Issue #293 · 前缀');
});

test('switchConversation: 无前缀标题头部保持不变', async () => {
  var sb = buildSandbox({
    conv: {
      title: 'Personal Chat',
      repo_key: '',
    },
  });
  sb._currentConvId = 'conv6';
  sb.switchConversation('conv6');
  await new Promise(function(r) { setTimeout(r, 20); });
  var hdrHtml = sb._getHdrHtml();
  assert.ok(hdrHtml.indexOf('Personal Chat') !== -1, '头部标题应含 Personal Chat');
});
