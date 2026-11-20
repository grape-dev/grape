/*
 * Issue #3569 —— 跨平台 OAuth reauth 跳转 + 防整页重载闭环
 *
 * 运行：node tests/js/test_issue_3569_reauth_loop_guard.js
 *
 * 做法：在桩环境（window/document/fetch/localStorage/sessionStorage/location/app/I18N）里
 * 用 new Function 加载真实的 src/api/frontend/shell/js/api.js，跑真实 401 分支——
 * 不做「源码看起来像」的字符串断言（除结构断言外）。
 *
 * 验证点（对应 Issue 验收标准）：
 *   T1 detail.provider=github → 请求 /api/auth/login/github（不再无条件 GitCode）
 *   T2 跳转前写 sessionStorage 标记（provider + 时间戳）
 *   T3 标记跨「整页重载」生效：60 秒内同 provider 再 401 → 不再跳转，渲染提示 + #account/profile 入口
 *   T4 标记超过 60 秒 → 允许重新跳转
 *   T5 bind_success=github 重新绑定成功 → 清除标记（正向回归）
 *   T6 provider=gitcode / 无 provider（旧后端）→ 均跳 /api/auth/login/gitcode
 *   T6b 已知但无授权入口的平台（gitee）→ 不猜平台、只提示（猜错平台会复制本缺陷）
 *   T7 目标平台 OAuth 未配置（400）→ 只提示，不整页导航
 *   T8 429 → 只提示 + 退避，不整页导航
 *   T9 同页并发 401 → 只发一次登录请求（页面级去重）
 *   T10 删除无条件跳 GitCode 的旧实现（结构断言）
 *   T11 locales zh/en error.reauth_* 齐备且集合一致
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const SHELL = path.join(ROOT, 'src/api/frontend/shell');
const API_JS = fs.readFileSync(path.join(SHELL, 'js/api.js'), 'utf8');
const OAUTH_PY = fs.readFileSync(path.join(ROOT, 'src/api/oauth_token.py'), 'utf8');

let pass = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  pass++;
}

// ---------------------------------------------------------------- 桩环境
function makeElement(tag, byId) {
  const el = {
    tagName: tag, id: '', style: {}, attrs: {}, children: [], parentNode: null,
    setAttribute: function (k, v) { this.attrs[k] = v; },
    appendChild: function (c) {
      c.parentNode = this;
      this.children.push(c);
      if (c.id) byId[c.id] = c;
      return c;
    },
    removeChild: function (c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null;
      if (c.id) delete byId[c.id];
    },
  };
  return el;
}

function sandbox(opts) {
  opts = opts || {};
  const storage = opts.sessionStorage || {};
  const byId = {};
  const doc = {
    createElement: function (tag) { return makeElement(tag, byId); },
    getElementById: function (id) { return byId[id] || null; },
    body: makeElement('body', byId),
    documentElement: makeElement('html', byId),
  };
  const location = { pathname: opts.pathname || '/app/', search: opts.search || '', href: 'https://grape.test' + (opts.pathname || '/app/') };
  const listeners = {};
  const windowObj = {
    location: location,
    addEventListener: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
  };
  const fetches = [];
  const navCalls = [];
  const toasts = [];
  function fetchStub(url, init) {
    fetches.push({ url: url, init: init || {} });
    const full = String(url);
    let status = 200;
    let body = {};
    for (const rule of (opts.routes || [])) {
      if (full.indexOf(rule.match) >= 0) { status = rule.status; body = rule.body || {}; break; }
    }
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status: status,
      url: full,
      headers: { get: function () { return 'application/json'; } },
      json: function () { return Promise.resolve(body); },
    });
  }
  const I18N = {
    getLang: function () { return 'zh'; },
    t: function (key, vars, fallback) {
      if (fallback === undefined) return '[' + key + ']';
      return String(fallback).replace(/\{\{?(\w+)\}?\}/g, function (m, name) {
        return vars && Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : m;
      });
    },
  };
  const app = { nav: function (v, p) { navCalls.push([v, p]); } };
  const runner = new Function('__env',
    'var window = __env.window, document = __env.document, I18N = __env.I18N, app = __env.app;'
    + 'var localStorage = __env.localStorage, sessionStorage = __env.sessionStorage;'
    + 'var location = __env.location, fetch = __env.fetch, performance = __env.performance;'
    + 'var navigator = __env.navigator;\n'
    + API_JS + '\n;return API;');
  const api = runner({
    window: windowObj,
    document: doc,
    I18N: I18N,
    app: app,
    localStorage: {
      getItem: function (k) { return k === 'token' ? 'header.payload.sig' : null; },
      setItem: function () {}, removeItem: function () {},
    },
    sessionStorage: {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
      setItem: function (k, v) { storage[k] = v; },
      removeItem: function (k) { delete storage[k]; },
    },
    location: location,
    fetch: fetchStub,
    performance: { now: function () { return 0; } },
    navigator: { sendBeacon: function () {} },
  });
  function fireStorage(ev) { (listeners['storage'] || []).forEach(fn => fn(ev)); }
  return { api: api, fetches: fetches, location: location, doc: doc, navCalls: navCalls,
    storage: storage, byId: byId, fireStorage: fireStorage };
}

function tick(ms) { return new Promise(r => setTimeout(r, ms || 10)); }
function loginFetches(env) { return env.fetches.filter(f => f.url.indexOf('/auth/login/') >= 0); }
function noticeEl(env) { return env.byId['oauthReauthNotice'] || null; }
function noticeText(env) {
  const el = noticeEl(env);
  return el ? el.children.map(c => c.textContent || '').join(' ') : '';
}
const MARK_KEY = 'grape_reauth_redirect';

const REAUTH_401 = {
  match: '/api/repos/member', status: 401,
  body: { detail: { code: 401, message: 'GitHub 授权已过期，请重新登录授权', action: 'reauth_required', provider: 'github' } },
};

// ---------------------------------------------------------------- T1/T2
(async () => {
  const env = sandbox({
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 200, body: { redirect_url: 'https://github.com/login/oauth/authorize?x=1' } },
    ],
  });
  await env.api.get('/repos/member').catch(() => {});
  await tick();
  const logins = loginFetches(env);
  ok(logins.length === 1 && logins[0].url === '/api/auth/login/github',
    'T1 GitHub 令牌失效应请求 /api/auth/login/github（实际: ' + JSON.stringify(logins.map(f => f.url)) + '）');
  ok(env.fetches.every(f => f.url.indexOf('/auth/login/gitcode') < 0), 'T1 不得再请求 GitCode 登录入口');
  ok(env.location.href === 'https://github.com/login/oauth/authorize?x=1', 'T1 跳转到后端给出的 GitHub 授权地址');
  const mark = JSON.parse(env.storage[MARK_KEY] || '{}');
  ok(typeof mark.github === 'number' && Date.now() - mark.github < 5000, 'T2 跳转前写入 sessionStorage 标记（provider+时间戳）');

  // ---------------------------------------------------------------- T3（整页重载后仍生效）
  const reload = sandbox({
    sessionStorage: env.storage,
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 200, body: { redirect_url: 'https://github.com/login/oauth/authorize?x=2' } },
    ],
  });
  ok(reload.api._recentlyRedirected('github') === true, 'T3 重载后标记仍可读（sessionStorage 跨整页重载）');
  await reload.api.get('/repos/member').catch(() => {});
  await tick();
  ok(loginFetches(reload).length === 0, 'T3 60 秒内同 provider 再次 401 不得再发登录请求（否则整页重载闭环）');
  ok(reload.location.href === 'https://grape.test/app/', 'T3 命中标记不得整页导航');
  const txt = noticeText(reload);
  ok(/GitHub/.test(txt) && /授权已过期/.test(txt) && /账号设置/.test(txt) && /第三方绑定/.test(txt),
    'T3 渲染「GitHub 授权已过期，请到 账号设置 → 第三方绑定 重新授权」: ' + txt);
  const el = noticeEl(reload);
  ok(el && el.attrs.role === 'alert', 'T3 提示条 role=alert');
  const go = el && el.children.filter(c => c.tagName === 'a')[0];
  ok(go && go.href === '#account/profile', 'T3 提供 #account/profile 入口');
  go.onclick({ preventDefault: function () {} });
  ok(reload.navCalls.length === 1 && reload.navCalls[0][0] === 'account' && reload.navCalls[0][1] === 'profile',
    'T3 「去账号设置」路由到 #account/profile');

  // ---------------------------------------------------------------- T4（超时窗口）
  const stale = {};
  stale[MARK_KEY] = JSON.stringify({ github: Date.now() - 61 * 1000 });
  const env4 = sandbox({
    sessionStorage: stale,
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 200, body: { redirect_url: 'https://github.com/login/oauth/authorize?x=3' } },
    ],
  });
  ok(env4.api._recentlyRedirected('github') === false, 'T4 超过 60 秒标记失效');
  await env4.api.get('/repos/member').catch(() => {});
  await tick();
  ok(loginFetches(env4).length === 1, 'T4 超时后允许重新跳转（避免永久拦住正常重新授权）');

  // ---------------------------------------------------------------- T5（重新绑定成功清标记）
  const env5 = sandbox({
    sessionStorage: JSON.parse(JSON.stringify(env.storage)),
    search: '?bind_success=github',
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 200, body: { redirect_url: 'https://github.com/login/oauth/authorize?x=4' } },
    ],
  });
  ok(env5.storage[MARK_KEY] === undefined || !JSON.parse(env5.storage[MARK_KEY]).github,
    'T5 bind_success=github 页面加载时清除 GitHub 标记');
  await env5.api.get('/repos/member').catch(() => {});
  await tick();
  ok(loginFetches(env5).length === 1, 'T5 重新绑定成功后下一次 401 仍能正常跳转（正向回归）');

  // ---------------------------------------------------------------- T5b（popup 重新绑定：storage 事件清标记）
  const env5b = sandbox({
    sessionStorage: { [MARK_KEY]: JSON.stringify({ github: Date.now() }) },
  });
  ok(env5b.api._recentlyRedirected('github') === true, 'T5b 前置：标记生效');
  env5b.fireStorage({ key: 'user', newValue: JSON.stringify({ bindings: ['github'] }) });
  ok(env5b.api._recentlyRedirected('github') === false,
    'T5b bindings 变化（popup 重新绑定）清除对应平台标记');

  // ---------------------------------------------------------------- T6（gitcode / 旧后端无 provider）
  for (const [name, detail] of [
    ['gitcode', { code: 401, message: 'GitCode 授权已过期', action: 'reauth_required', provider: 'gitcode' }],
    ['旧后端无 provider', { code: 401, message: '授权已过期', action: 'reauth_required' }],
  ]) {
    const e = sandbox({
      routes: [
        { match: '/api/repos/member', status: 401, body: { detail: detail } },
        { match: '/auth/login/gitcode', status: 200, body: { redirect_url: 'https://gitcode.com/oauth/authorize?x=1' } },
      ],
    });
    await e.api.get('/repos/member').catch(() => {});
    await tick();
    const l = loginFetches(e);
    ok(l.length === 1 && l[0].url === '/api/auth/login/gitcode', 'T6 ' + name + ' 应跳 GitCode 授权入口');
    ok(e.location.href.indexOf('gitcode.com') >= 0, 'T6 ' + name + ' 跳转 GitCode 授权地址');
  }

  // ---------------------------------------------------------------- T6b（未知平台不猜入口）
  const env6b = sandbox({
    routes: [
      { match: '/api/repos/member', status: 401, body: { detail: { code: 401, message: 'gitee 授权已过期', action: 'reauth_required', provider: 'gitee' } } },
      { match: '/auth/login/gitcode', status: 200, body: { redirect_url: 'https://gitcode.com/oauth/authorize?x=1' } },
    ],
  });
  await env6b.api.get('/repos/member').catch(() => {});
  await tick();
  ok(loginFetches(env6b).length === 0, 'T6b 无授权入口的已知平台（gitee）不得猜跳 GitCode/GitHub');
  ok(env6b.location.href === 'https://grape.test/app/', 'T6b 未知平台不整页导航');
  ok(/gitee/.test(noticeText(env6b)) && /授权已过期/.test(noticeText(env6b)),
    'T6b 未知平台仍渲染明确的重新授权提示: ' + noticeText(env6b));

  // ---------------------------------------------------------------- T7（400 未配置）
  const env7 = sandbox({
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 400, body: { detail: { code: 400, message: '不支持的平台 / OAuth 未配置' } } },
    ],
  });
  await env7.api.get('/repos/member').catch(() => {});
  await tick();
  ok(env7.location.href === 'https://grape.test/app/', 'T7 目标平台未配置（400）不得整页导航');
  ok(/OAuth 未配置/.test(noticeText(env7)), 'T7 400 渲染「OAuth 未配置」提示: ' + noticeText(env7));

  // ---------------------------------------------------------------- T8（429 限流）
  const env8 = sandbox({
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 429, body: { detail: { code: 429, message: '请求过于频繁' } } },
    ],
  });
  await env8.api.get('/repos/member').catch(() => {});
  await tick();
  ok(env8.location.href === 'https://grape.test/app/', 'T8 429 不得新增整页导航（location.href 不变）');
  ok(/请求过于频繁/.test(noticeText(env8)), 'T8 429 渲染退避提示: ' + noticeText(env8));
  ok(env8.fetches.filter(f => f.url === '/login' || f.url === '/en/login').length === 0, 'T8 429 不跳 /login');

  // ---------------------------------------------------------------- T9（同页并发去重）
  const env9 = sandbox({
    routes: [
      REAUTH_401,
      { match: '/auth/login/github', status: 200, body: { redirect_url: 'https://github.com/login/oauth/authorize?x=5' } },
    ],
  });
  env9.api._redirectToOAuth('github');
  env9.api._redirectToOAuth('github');
  await tick();
  ok(loginFetches(env9).length === 1, 'T9 同页并发只发一次登录请求');

  // ---------------------------------------------------------------- T10（结构断言）
  ok(API_JS.indexOf('_redirectToGitcodeAuth') < 0, 'T10 无条件跳 GitCode 的旧函数已删除');
  ok(/location\.href\s*=\s*d\.redirect_url/.test(API_JS), 'T10 仍在拿到 redirect_url 后跳转');
  ok(/_REAUTH_MARK_TTL_MS\s*=\s*60\s*\*\s*1000/.test(API_JS), 'T10 防循环窗口为 60 秒');
  ok(API_JS.indexOf("location.href = _langLoginUrl()") < 0, 'T10 失败分支不再整页跳登录页');

  // ---------------------------------------------------------------- T11（后端 + i18n）
  const providerFields = (OAUTH_PY.match(/"provider": provider,/g) || []).length;
  ok(providerFields === 3, 'T11 oauth_token.py 三处 401 detail 均补 provider（实际 ' + providerFields + '）');
  const zh = JSON.parse(fs.readFileSync(path.join(SHELL, 'locales/zh/app.json'), 'utf8')).error;
  const en = JSON.parse(fs.readFileSync(path.join(SHELL, 'locales/en/app.json'), 'utf8')).error;
  const keys = ['reauth_expired', 'reauth_retry_later', 'reauth_not_configured', 'reauth_redirect_failed', 'reauth_go_settings'];
  keys.forEach(function (k) {
    ok(typeof zh[k] === 'string' && zh[k], 'T11 zh 有 app.error.' + k);
    ok(typeof en[k] === 'string' && en[k], 'T11 en 有 app.error.' + k);
  });
  const zhKeys = Object.keys(zh).sort().join(',');
  const enKeys = Object.keys(en).sort().join(',');
  ok(zhKeys === enKeys, 'T11 app.error zh/en key 集合一致');

  console.log('结果: ' + pass + ' 通过, 0 失败');
  console.log('✓ Issue #3569（跨平台 OAuth reauth + 防整页重载闭环）全部断言通过');
})().catch(function (e) {
  console.error('FAILED: ' + e.message);
  process.exit(1);
});