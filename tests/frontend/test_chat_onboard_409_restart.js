/**
 * 飞书扫码 409 确认重开（docs/weekly-08/feishu-onboard-409-confirm-restart-design.md Step 1.2）。
 *
 * 背景：用户点「扫码配置」但已有进行中任务 → 后端 409（detail 带旧 task_id）。
 * 现状：chat.js startFeishuOnboard 直接 app.showToast，无重开入口。
 * 目标：409 时改弹 app.showConfirm 确认框；用户确认后先 cancel 旧任务再重调 start。
 *
 * UT-02a: 409 → showConfirm（不再 toast）
 * UT-02b: 确认回调 → POST /feishu/onboard/cancel(旧task_id) → 再 POST /feishu/onboard/start
 * UT-05 : 非 409 错误 → 仍 toast（回归）
 *
 * 运行：node tests/frontend/test_chat_onboard_409_restart.js（零依赖 node:test/vm）
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
  var state = {
    posts: [],                 // API.post 调用记录 {url, body}
    toastCount: 0,
    confirmCalls: [],          // app.showConfirm 调用记录 {message, onConfirm, danger}
  };
  var onboardBody = {innerHTML: ''};
  var docStub = {
    getElementById: function(id) {
      if (id === 'chatBody') return {innerHTML: '', scrollTop: 0, scrollHeight: 0};
      if (id === 'onboardBody') return onboardBody;
      if (id === 'onboardModal') return null;
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
    I18N: {
      t: function(k) {
        // 让 i18n 对测试 key 返回真实中文文案（模拟查表；其余返回 key 名）
        if (k === 'chat.onboard.confirm_restart') return '当前已有飞书机器人正在配置中，是否放弃并重新开始配置？（旧配置不会保存）';
        if (k === 'chat.onboard.confirm_rebind') return '当前已绑定飞书机器人（{{appId}}）。更换后将使旧配置失效且不可恢复，确认继续扫码换绑？';
        if (k === 'chat.onboard.close_warning') return '配置中，请勿关闭页面；关闭将取消本次配置';
        if (k === 'chat.onboard.confirm_close') return '本次扫码配置尚未完成，关闭将取消配置，确认关闭？';
        return k;
      },
      getLang: function() { return 'zh'; },
    },
    app: {
      nav: function(){},
      showToast: function(){ state.toastCount++; },
      currentRepo: function() { return null; },
      setTheme: function(){},
      setThemeMode: function(){},
      showConfirm: function(message, onConfirm, o) { state.confirmCalls.push({message: message, onConfirm: onConfirm, danger: !!(o && o.danger)}); },
    },
    API: {
      get: function(url){
        // 换绑确认（Step 1.4）依赖 /chat/config 返回 im.feishu.app_id
        if (String(url).indexOf('/chat/config') !== -1) {
          return Promise.resolve(opts.chatConfig || {im: {feishu: {app_id: ''}}});
        }
        return Promise.resolve({});
      },
      post: function(url, body){
        state.posts.push({url: url, body: body});
        // 可注入 post 行为（正常 resolve / 409 reject / 其它失败）
        if (typeof opts.postImpl === 'function') return opts.postImpl(url, body);
        return Promise.resolve({task_id: 'new_task'});
      },
      put: function(){ return Promise.resolve({}); },
      del: function(){ return Promise.resolve({}); },
    },
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
    setTimeout: function(fn) { return 0; },  // 不真跑轮询（测试同步验证调用链）
    setInterval: function() { return 0; },
    clearInterval: function() {},
    clearTimeout: function() {},
    requestAnimationFrame: function() { return 0; },
    QRCode: function() {},
    // 测试读取
    _getState: function() { return state; },
    _getOnboardHtml: function() { return onboardBody.innerHTML; },
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

function wait(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

// 构造一个 409 错误对象（模拟 api.js 的 e.status/e.detail）
function make409Error(taskId, msg) {
  const e = new Error(msg || '已有扫码配置正在进行');
  e.status = 409;
  e.detail = {code: 409, message: msg || '已有扫码配置正在进行', task_id: taskId};
  return e;
}

test('UT-02a: 409 时弹 showConfirm（确认重开），不直接 toast', async () => {
  const sb = buildSandbox({
    postImpl: function() { return Promise.reject(make409Error('old_task_123')); },
  });
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 1, '409 应弹确认框');
  assert.ok(String(st.confirmCalls[0].message).indexOf('重新开始') !== -1 || String(st.confirmCalls[0].message).indexOf('restart') !== -1, '文案应含「重新开始」');
  assert.strictEqual(st.confirmCalls[0].danger, true, '确认框应为危险样式');
  assert.strictEqual(st.toastCount, 0, '409 不应再直接 toast');
});

test('UT-02b: 确认重开 → 先 cancel 旧任务，再重调 start', async () => {
  const sb = buildSandbox({
    postImpl: function(url, body) {
      // 第一次 start → 409；cancel → ok；第二次 start → ok
      if (url === '/feishu/onboard/start' && body && body.__seq !== 2) {
        return Promise.reject(make409Error('old_task_123'));
      }
      return Promise.resolve({task_id: 'new_task'});
    },
  });
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 1, '应弹确认框');
  // 触发确认回调（用户点「重新开始配置」）
  st.confirmCalls[0].onConfirm();
  await wait(30);
  // 调用序列应为：start(→409) → cancel(old) → start(new)
  const urls = st.posts.map(p => p.url);
  assert.strictEqual(urls[0], '/feishu/onboard/start', '第一次是 start');
  assert.strictEqual(urls[1], '/feishu/onboard/cancel', '确认后先 cancel 旧任务');
  assert.strictEqual(st.posts[1].body && st.posts[1].body.task_id, 'old_task_123', 'cancel 应带旧 task_id');
  assert.ok(urls.length >= 3 && urls[2] === '/feishu/onboard/start', '最后应重调 start');
});

test('UT-02b: 非 409 错误仍 toast（回归）', async () => {
  const sb = buildSandbox({
    postImpl: function() {
      const e = new Error('network down');
      e.status = 500;
      return Promise.reject(e);
    },
  });
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 0, '非 409 不弹确认框');
  assert.strictEqual(st.toastCount, 1, '非 409 仍 toast');
});
// ===================== 场景②：已配置(connected) 换绑确认（Step 1.4，纯前端）=====================

test('UT-06a: 已配置 app_id → 入口弹换绑确认（不直接 start）', async () => {
  const sb = buildSandbox({chatConfig: {im: {feishu: {app_id: 'cli_existing_123'}}}});
  let starts = 0;
  const origPost = sb.API.post;
  sb.API.post = function(url, body){
    if (url === '/feishu/onboard/start') starts++;
    return origPost(url, body);
  };
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 1, '已配置时应弹换绑确认框');
  const msg = String(st.confirmCalls[0].message);
  assert.ok(msg.indexOf('换绑') !== -1 || msg.indexOf('更换') !== -1, '文案应含「换绑/更换」');
  assert.ok(msg.indexOf('不可恢复') !== -1 || msg.indexOf('失效') !== -1, '文案应提示不可恢复/失效');
  assert.strictEqual(st.confirmCalls[0].danger, true, '换绑确认应为危险样式');
  assert.strictEqual(starts, 0, '确认前不应发起 start');
});

test('UT-06b: 换绑确认 onConfirm → 继续 start（新二维码）', async () => {
  const sb = buildSandbox({chatConfig: {im: {feishu: {app_id: 'cli_existing_123'}}}});
  let starts = 0;
  const origPost = sb.API.post;
  sb.API.post = function(url, body){
    if (url === '/feishu/onboard/start') starts++;
    return origPost(url, body);
  };
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st0 = sb._getState();
  assert.strictEqual(st0.confirmCalls.length, 1, '应弹确认');
  st0.confirmCalls[0].onConfirm();  // 用户点「确认换绑」
  await wait(20);
  assert.strictEqual(starts, 1, '确认后应发起 start');
});

test('UT-06c: 换绑确认取消 → 不动作（无 start）', async () => {
  const sb = buildSandbox({chatConfig: {im: {feishu: {app_id: 'cli_existing_123'}}}});
  let starts = 0;
  const origPost = sb.API.post;
  sb.API.post = function(url, body){
    if (url === '/feishu/onboard/start') starts++;
    return origPost(url, body);
  };
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 1, '应弹确认框');
  // 用户「取消」= 不调用 onConfirm；等待确认无 start
  await wait(20);
  assert.strictEqual(starts, 0, '取消（未确认）后不应发起 start');
});

test('UT-06d: skipRebindCheck=true（确认后递归）→ 不重复确认、直接 start', async () => {
  const sb = buildSandbox({chatConfig: {im: {feishu: {app_id: 'cli_existing_123'}}}});
  let starts = 0;
  const origPost = sb.API.post;
  sb.API.post = function(url, body){
    if (url === '/feishu/onboard/start') starts++;
    return origPost(url, body);
  };
  sb.startFeishuOnboard('chat_bot', '', true);  // 换绑确认/409重开后的递归调用
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 0, 'skipRebindCheck 时不应再弹换绑确认');
  assert.strictEqual(starts, 1, '应直接发起 start');
});

test('UT-06e: 未配置（app_id 空）→ 直接 start 无确认', async () => {
  const sb = buildSandbox({chatConfig: {im: {feishu: {app_id: ''}}}});
  let starts = 0;
  const origPost = sb.API.post;
  sb.API.post = function(url, body){
    if (url === '/feishu/onboard/start') starts++;
    return origPost(url, body);
  };
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 0, '未配置时不弹确认');
  assert.strictEqual(starts, 1, '未配置应直接 start');
});

// ===================== 场景③：配置中勿关页提示 + 关闭二次确认 =====================

test('UT-07a: waiting_confirm 渲染含「配置中，请勿关闭页面」提示', async () => {
  const sb = buildSandbox({});
  sb.renderOnboardStatus({status: 'waiting_confirm', qr_url: 'qr://abc', expire_in: 300});
  const html = sb._getOnboardHtml();
  assert.ok(html.indexOf('请勿关闭页面') !== -1, '应含「请勿关闭页面」提示, got: ' + html.substring(0,120));
  assert.ok(html.indexOf('配置中') !== -1, '提示应含「配置中」');
});

test('UT-07b: cancelOnboard 进行中先弹二次确认，确认才 cancel', async () => {
  const sb = buildSandbox({
    chatConfig: {im: {feishu: {app_id: ''}}},  // 未配置→直接进 start
    postImpl: function(url, body){ return Promise.resolve({task_id: 'task_in_progress'}); },
  });
  // 先正常 start：_onboardTaskId 被 set 为 task_in_progress（词法，vm 可见）
  sb.startFeishuOnboard('chat_bot', '');
  await wait(20);
  sb.cancelOnboard();
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 1, '进行中关闭应先弹二次确认');
  const msg = String(st.confirmCalls[0].message);
  assert.ok(msg.indexOf('关闭') !== -1 && (msg.indexOf('取消') !== -1 || msg.indexOf('丢失') !== -1), '文案应说明关闭将取消/丢失配置');
  assert.strictEqual(st.confirmCalls[0].danger, true, '二次确认应为 danger');
  // 确认后 → cancel + 关弹窗（不重复弹）
  st.confirmCalls[0].onConfirm();
  await wait(20);
  const st2 = sb._getState();
  const cancels = st2.posts.filter(p => p.url === '/feishu/onboard/cancel');
  assert.strictEqual(cancels.length, 1, '确认后应 POST cancel');
  assert.strictEqual(st2.confirmCalls.length, 1, '确认后不再重复弹确认');
});

test('UT-07c: cancelOnboard 无进行中任务 → 不弹二次确认、直接关', async () => {
  const sb = buildSandbox({});
  // 未 start：_onboardTaskId 保持初始 null（无进行中任务）
  sb.cancelOnboard();
  await wait(20);
  const st = sb._getState();
  assert.strictEqual(st.confirmCalls.length, 0, '_onboardTaskId 为空时不应弹二次确认');
});
