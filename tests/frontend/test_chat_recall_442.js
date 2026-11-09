/**
 * Issue #442: ChatAgent 消息撤回前端逻辑（历史渲染撤回提示 / 撤回入口 / 重新编辑追加输入框）。
 *
 * 覆盖：
 * 1. 历史渲染：已撤回 user 消息 → chat-msg-recalled 提示 + 「重新编辑」链接（原文剥离 [chat_img:]）
 * 2. 历史渲染：未撤回 user 消息 → data-msg-id + hover 撤回按钮
 * 3. _recallReEdit：原文追加到输入框现有内容之后（不覆盖）
 * 4. recallChatMessage：二次确认 → API.post recall → 原位置替换撤回提示 + 终止 SSE 占位
 *
 * 运行：node tests/frontend/test_chat_recall_442.js
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

function buildSandbox(opts) {
  opts = opts || {};
  const inputState = {value: opts.inputValue || ''};
  const recalled = [];   // 已被替换为撤回提示的 msgId
  const posts = [];      // API.post 调用记录
  let confirmCb = null;  // app.showConfirm 捕获的 onConfirm

  const bodyEl = {
    _html: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    scrollTop: 0, scrollHeight: 0,
    contains: () => false,
    querySelector: function(sel) {
      const m = /^\.msg\[data-msg-id="(\d+)"\]$/.exec(sel);
      if (m) return {replaceWith: function() { recalled.push(Number(m[1])); }};
      return null;
    },
    getElementById: () => null,
  };
  const inputEl = {
    get value() { return inputState.value; },
    set value(v) { inputState.value = String(v); },
    focus: function() { this.focused = true; },
    style: {},
    addEventListener: function() {},
  };

  const docStub = {
    _els: {},
    getElementById: function(id) {
      if (id === 'chatBody') return bodyEl;
      if (id === 'chatInput') return inputEl;
      if (id && id.indexOf('t') === 0) {
        if (!this._els[id]) this._els[id] = {removed: false, remove() { this.removed = true; }, innerHTML: ''};
        return this._els[id];
      }
      return null;
    },
    querySelector: function(sel) {
      if (sel === '.chat-hdr h2') return {innerHTML: ''};
      if (sel === '.chat-in button') return {disabled: false};
      return null;
    },
    querySelectorAll: () => [],
    createElement: () => ({style: {}, classList: {add(){}, remove(){}}, setAttribute(){}, appendChild(){}}),
    body: {appendChild: function() {}},
    addEventListener: function() {},
  };

  const sandbox = {
    document: docStub,
    location: {hash: ''},
    history: {back(){}, pushState(){}, replaceState(){}},
    I18N: {t: function(k) { return k; }, getLang: () => 'zh'},
    app: {
      nav(){}, currentRepo: () => null, setTheme(){}, setThemeMode(){},
      showToast: function(msg) { sandbox._toasts.push(String(msg)); },
      showConfirm: function(msg, onConfirm, o) { sandbox._confirms.push({msg, opts: o}); confirmCb = onConfirm; },
    },
    API: {
      get: function(url) {
        if (url.indexOf('/chat/messages/') !== -1) {
          return Promise.resolve({messages: opts.msgs || [], conversation: {}});
        }
        if (url.indexOf('/chat/conversations') !== -1) return Promise.resolve({conversations: []});
        if (url.indexOf('/status') !== -1) return Promise.resolve({status: opts.status || 'done'});
        return Promise.resolve({});
      },
      post: function(url, body) { posts.push({url, body}); return Promise.resolve(opts.postResult || {recalled: true}); },
      put: () => Promise.resolve({}),
      del: () => Promise.resolve({}),
    },
    localStorage: {_s:{}, getItem(k){return this._s[k]||null;}, setItem(k,v){this._s[k]=String(v);}, removeItem(k){delete this._s[k];}},
    sessionStorage: {_s:{}, getItem(k){return this._s[k]||null;}, setItem(k,v){this._s[k]=String(v);}, removeItem(k){delete this._s[k];}},
    _vcsUrl: (rk, p) => (rk || '') + (p || ''),
    renderMarkdown: s => s,
    fmtLocalTime: s => s,
    linkifyIssueNumbers: s => s,
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
    console,
    setTimeout: function(fn) { try { fn(); } catch(e) {} return 0; },
    setInterval: () => 0, clearInterval(){}, clearTimeout(){},
    requestAnimationFrame: () => 0,
    AbortController: function() { return {signal: {}, abort: function() { sandbox._aborted = true; }}; },
    TextDecoder: function() { return {decode: () => ''}; },
    fetch: function(url) {
      const u = String(url);
      if (u.indexOf('/stream') !== -1) return Promise.resolve({ok: true, status: 200, body: null});
      return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})});
    },
    // 观测点
    _toasts: [], _confirms: [], _aborted: false,
    _getPosts: () => posts,
    _getRecalled: () => recalled,
    _fireConfirm: function() { if (confirmCb) confirmCb(); },
    _getInput: () => inputState,
    _getInputEl: () => inputEl,
    _isElRemoved: function(id) { return !!(docStub._els[id] && docStub._els[id].removed); },
  };

  Object.defineProperty(sandbox, 'window', {
    value: new Proxy(sandbox, {get: (_, k) => sandbox[k], set: (_, k, v) => { sandbox[k] = v; return true; }}),
    writable: false, configurable: false,
  });
  vm.createContext(sandbox);
  vm.runInContext(CHAT_SRC, sandbox, {filename: 'chat.js'});
  sandbox._getBodyHtml = function() { return bodyEl._html; };
  return sandbox;
}

function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

test('历史渲染：已撤回 user 消息 → 撤回提示 + 重新编辑（原文剥离 img 标记）', async () => {
  const sb = buildSandbox({msgs: [
    {id: 11, role: 'user', content: '发错的 [chat_img:/a.png] 消息', deleted: 1, task_id: 'chat_x'},
    {id: 12, role: 'agent', content: '回复', task_id: 'chat_x', deleted: 1},
  ]});
  sb.switchConversation('conv-test');
  await wait(10);
  const html = sb._getBodyHtml();
  assert.ok(html.indexOf('chat-msg-recalled') !== -1, 'recalled hint class missing');
  assert.ok(html.indexOf('chat.recall.hint') !== -1, 'recalled hint text missing');
  assert.ok(html.indexOf('_recallReEdit(11)') !== -1, 're-edit link missing');
  assert.ok(html.indexOf('发错的') === -1, 'recalled original content should not render');
});

test('历史渲染：未撤回 user 消息 → data-msg-id + 撤回入口；agent 消息无撤回', async () => {
  const sb = buildSandbox({msgs: [
    {id: 21, role: 'user', content: '普通消息', deleted: 0, task_id: 'chat_y'},
    {id: 22, role: 'agent', content: '回复', deleted: 0, task_id: 'chat_y'},
  ]});
  sb.switchConversation('conv-test');
  await wait(10);
  const html = sb._getBodyHtml();
  assert.ok(html.indexOf('data-msg-id="21"') !== -1, 'user bubble lacks data-msg-id');
  assert.ok(html.indexOf('recallChatMessage(21') !== -1, 'recall button missing');
  assert.ok(html.indexOf('data-msg-id="22"') === -1, 'agent bubble must not be recallable');
});

test('_recallReEdit：原文追加到输入框现有内容之后（不覆盖）', () => {
  const sb = buildSandbox({inputValue: '草稿内容'});
  // 经 _userMsgHtml 注册原文（chat.js 闭包内 _chatRecallTexts）
  sb._userMsgHtml(31, 'bubble', '被撤回的原文');
  sb._recallReEdit(31);
  assert.strictEqual(sb._getInput().value, '草稿内容\n被撤回的原文');
  assert.ok(sb._getInputEl().focused, 'input should be focused');
  // 空输入框 → 直接填原文（无前导换行）
  const sb2 = buildSandbox({inputValue: ''});
  sb2._userMsgHtml(32, 'bubble', '原文2');
  sb2._recallReEdit(32);
  assert.strictEqual(sb2._getInput().value, '原文2');
});

test('recallChatMessage：二次确认 → 调撤回端点 → 原位置替换撤回提示 + 终止任务占位', async () => {
  const sb = buildSandbox({msgs: [], postResult: {recalled: true, task_id: 'chat_z', terminated: true}});
  sb._userMsgHtml(41, 'bubble', '被撤回的 live 消息');
  sb.switchConversation('conv-test');  // 置 chat.js 词法 _currentConvId
  sb._setTask('conv-test', {task_id: 'chat_z', conv_id: 'conv-test', el_id: 't123', status: 'running'});
  sb.recallChatMessage(41);
  // 二次确认文案 + danger
  assert.strictEqual(sb._confirms.length, 1);
  assert.strictEqual(sb._confirms[0].msg, 'chat.recall.confirm');
  assert.strictEqual(sb._confirms[0].opts.danger, true);
  assert.strictEqual(sb._getPosts().length, 0, 'API must not be called before confirm');
  // 确认 → 调端点
  sb._fireConfirm();
  await wait(10);
  const posts = sb._getPosts();
  assert.strictEqual(posts.length, 1);
  assert.ok(posts[0].url.indexOf('/chat/messages/41/recall') !== -1);
  // 原位置替换 + 三点占位移除 + 任务态清理
  assert.deepStrictEqual(sb._getRecalled(), [41]);
  assert.ok(sb._isElRemoved('t123'), 'dots placeholder should be removed');
  assert.strictEqual(sb._getTask('conv-test'), null);
});
