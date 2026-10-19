/**
 * 飞书扫码成功后 closeOnboardModal 按当前视图刷新（im-channel 页即时注入 App ID/Secret）。
 *
 * 背景：closeOnboardModal(refresh) 原先只有 vod_bot / chat_bot 两个分支；
 * IM 渠道页（im_channel，window._currentView==='im-channel'）无对应分支，
 * 扫码成功后关闭弹窗页面不刷新，需切走再切回才能看到新绑定的 App ID/Secret。
 *
 * 修复：refresh 分支增加 im-channel 视图判断 → 调 loadImChannelConfig() 重拉 /chat/config。
 *
 * 运行：node tests/frontend/test_chat_onboard_refresh.js（零依赖 node:test/vm）
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

function buildSandbox(opts) {
  opts = opts || {};
  var calls = {loadImChannelConfig: 0, loadChatConfigPanel: 0, refreshVodOnboard: 0};
  var docStub = {
    getElementById: function(id) {
      if (id === 'chatBody') return {innerHTML: '', scrollTop: 0, scrollHeight: 0};
      if (id === 'onboardModal') return null; // 模拟弹窗不存在（close 时 remove 跳过）
      if (id === 'chatInput') return {disabled: false, addEventListener: function(){}};
      if (id === 'convList') return {innerHTML: '', _html: ''};
      if (id === 'pageContent') return {innerHTML: ''};
      if (id === 'chatImgPreview') return {style: {display: 'none'}};
      return null;
    },
    querySelector: function() { return null; },
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
    API: {get: function(){ return Promise.resolve({}); }, post: function(){ return Promise.resolve({}); }, put: function(){ return Promise.resolve({}); }, del: function(){ return Promise.resolve({}); }},
    localStorage: {_store: {}, getItem: function(k){ return this._store[k]||null; }, setItem: function(k,v){ this._store[k]=String(v); }, removeItem: function(k){ delete this._store[k]; }},
    sessionStorage: {_store: {}, getItem: function(k){ return this._store[k]||null; }, setItem: function(k,v){ this._store[k]=String(v); }, removeItem: function(k){ delete this._store[k]; }},
    _vcsUrl: function(rk, p) { return (rk || '') + (p || ''); },
    renderMarkdown: function(s) { return s; },
    fmtLocalTime: function(s) { return s; },
    linkifyIssueNumbers: function(s) { return s; },
    _chatTasks: {},
    _chatAbort: null,
    _openConvId: null,
    _currentConvId: null,
    _chatStreamAbort: null,
    _chatTaskStreamAbort: null,
    _chatSkillScope: 'official',
    _chatConfigData: null,
    _chatPickerOpen: null,
    _onboardTaskId: null,
    _onboardTimer: null,
    _onboardExpireAt: 0,
    _onboardQrRendered: false,
    _onboardBotType: 'chat_bot',
    _onboardRepoKey: '',
    console: console,
    setTimeout: function(fn) { try { fn(); } catch(e) {} return 0; },
    setInterval: function() { return 0; },
    clearInterval: function() {},
    clearTimeout: function() {},
    requestAnimationFrame: function() { return 0; },
    QRCode: function() {},
    // 按视图可注入的刷新函数（由 closeOnboardModal refresh 分支调用）
    loadImChannelConfig: function() { calls.loadImChannelConfig++; },
    loadChatConfigPanel: function() { calls.loadChatConfigPanel++; },
    _getCalls: function() { return calls; },
  };
  // refreshVodOnboard 以 window.refreshVodOnboard 暴露（chat.js 检测 typeof window.refreshVodOnboard）
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

test('im-channel 视图：refresh=true 时调 loadImChannelConfig（即时注入 App ID/Secret）', () => {
  var sb = buildSandbox();
  sb.window._currentView = 'im-channel';
  sb._onboardBotType = 'chat_bot';
  sb.closeOnboardModal(true);
  var c = sb._getCalls();
  assert.strictEqual(c.loadImChannelConfig, 1, 'im-channel 视图关闭后应重载 IM 渠道配置');
  assert.strictEqual(c.loadChatConfigPanel, 0, '不应落到 Chat 页刷新分支');
});

test('vod/channel 以外视图：不触发 im-channel 刷新（loadImChannelConfig=0）', () => {
  var sb = buildSandbox();
  sb.window._currentView = 'agents';
  sb.closeOnboardModal(true);
  var c = sb._getCalls();
  assert.strictEqual(c.loadImChannelConfig, 0, '非 im-channel 视图不应用 im-channel 分支');
});

test('chat_bot（chat 视图）：refresh=true 时仍走 loadChatConfigPanel（现有行为不回归）', () => {
  var sb = buildSandbox();
  sb.window._currentView = 'chat';
  sb._onboardBotType = 'chat_bot';
  sb.closeOnboardModal(true);
  var c = sb._getCalls();
  assert.strictEqual(c.loadChatConfigPanel, 1, 'chat 视图用 loadChatConfigPanel');
  assert.strictEqual(c.loadImChannelConfig, 0, 'chat 视图不用 im-channel 分支');
});

test('refresh=false 时不做任何刷新（取消/失败关闭）', () => {
  var sb = buildSandbox();
  sb.window._currentView = 'im-channel';
  sb._onboardBotType = 'chat_bot';
  sb.closeOnboardModal(false);
  var c = sb._getCalls();
  assert.strictEqual(c.loadImChannelConfig, 0, 'refresh=false 不重载');
  assert.strictEqual(c.loadChatConfigPanel, 0, 'refresh=false 不重载');
});

test('im-channel 视图 + vod_bot 组合：优先走 im-channel（当前页优先）', () => {
  var sb = buildSandbox();
  sb.window._currentView = 'im-channel';
  sb._onboardBotType = 'vod_bot';
  var vodCalls = 0;
  sb.window.refreshVodOnboard = function() { vodCalls++; };
  sb.closeOnboardModal(true);
  var c = sb._getCalls();
  assert.strictEqual(c.loadImChannelConfig, 1, 'im-channel 视图优先于 vod_bot 分支');
  assert.strictEqual(vodCalls, 0, '当前在 IM 页不应触发 VOD 页刷新');
});