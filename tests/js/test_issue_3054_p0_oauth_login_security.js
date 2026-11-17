/*
 * Issue #3054 P0 —— 登录页安全与健壮性回归测试
 *
 * 运行：node tests/js/test_issue_3054_p0_oauth_login_security.js
 *
 * 验证点（每条都能红能绿）：
 *   P0-1  ?oauth_error= 反射型 DOM XSS
 *         T1 未知 provider / HTML payload → 不建 DOM、不进 innerHTML
 *         T2 已知 provider + 恶意 username → innerHTML 全部转义、onclick 里的值走 URI 编码
 *         T3 服务端错误跳转只放错误码（不夹带第三方 external_login）
 *   P0-2  postMessage 只校验 origin 不校验 source
 *         T4 source 不符 → 丢弃（不兑票）
 *         T5 source 相符 → 正常兑票；未记录 popup 引用 → 一律丢弃
 *         T6 Vue 侧 AccountView / BindDialog 同样有 source 校验
 *   P0-3  错误参数双重编码 / decode 无 try
 *         T7 ?oauth_error=100%25（URLSearchParams 解一次后是 '100%'）不抛，按“参数无效”处理
 *         T8 弹窗中间页 safeDecode 兜底，不再裸调 decodeURIComponent
 *         T9 服务端只做一次编码（不再 quote 之后又 urlencode）
 *
 * 做法：用大括号配对从真实 app.js 抽函数体，在桩环境里跑真实分支——不做源码字符串
 * 层面的“看起来像”断言（除少量结构断言）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const SHELL = path.join(ROOT, 'src/api/frontend/shell');
const APP = fs.readFileSync(path.join(SHELL, 'js/app.js'), 'utf8');
const POPUP_HTML = fs.readFileSync(path.join(SHELL, 'oauth-popup-callback.html'), 'utf8');
const AUTH_PY = fs.readFileSync(path.join(ROOT, 'src/domains/mgmt/routes/grape_auth.py'), 'utf8');
const ACCOUNT_VUE = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/AccountView.vue'), 'utf8');
const BIND_VUE = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/components/BindDialog.vue'), 'utf8');

let pass = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  pass++;
}

// ---------------------------------------------------------------- 抽取工具
function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, '未找到函数 ' + name);
  const braceStart = src.indexOf('{', src.indexOf(')', start));
  let depth = 0;
  for (let i = braceStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('无法配对 ' + name);
}

// P0-1/P0-3 相关函数及其模块级常量：从「P0-1 注释头」切到 _showWhitelistGuidance 结束
function whitelistBlock() {
  const head = APP.indexOf('// P0-1（Issue #3054）：?oauth_error= 反射型 DOM XSS 防护。');
  assert.ok(head >= 0, '未找到 P0-1 注释头（模块级白名单常量被删？）');
  const fnSrc = extractFn(APP, '_showWhitelistGuidance');
  const end = APP.indexOf(fnSrc) + fnSrc.length;
  return APP.slice(head, end);
}

const PRELUDE = 'var window = __env.window, document = __env.document, I18N = __env.I18N,'
    + ' showToast = __env.showToast, fetch = __env.fetch, localStorage = __env.localStorage,'
    + ' sessionStorage = __env.sessionStorage, location = __env.location;\n'
    + extractFn(APP, '_esc') + '\n';

function sandbox(opts) {
  opts = opts || {};
  const toasts = [];
  const fetches = [];
  const dom = { created: [], appended: [], innerHTML: '' };
  const win = {
    location: { origin: 'https://grape.test', pathname: '/', search: opts.search || '', href: 'https://grape.test/' },
    __OAUTH_PROVIDERS__: opts.providers
  };
  if (opts.popup) win.__oauthPopupWin = opts.popup;
  const doc = {
    createElement: function (tag) {
      dom.created.push(tag);
      const el = {
        tagName: tag, id: '', style: { cssText: '' }, innerHTML: '',
        appendChild: function () {}, remove: function () {},
        addEventListener: function () {}
      };
      Object.defineProperty(el, 'innerHTML', {
        get: function () { return dom.innerHTML; },
        set: function (v) { dom.innerHTML = v; }
      });
      return el;
    },
    body: { appendChild: function (el) { dom.appended.push(el); } },
    getElementById: function () { return null; },
    querySelector: function () { return null; }
  };
  const I18N = {
    t: function (k, vars) {
      const base = '[' + k + ']';
      if (!vars) return base;
      return base + JSON.stringify(vars);
    },
    getLang: function () { return 'zh'; }
  };
  const runner = new Function(
    '__env',
    PRELUDE
    + whitelistBlock() + '\n'
    + extractFn(APP, '_handleOAuthErrorParam') + '\n'
    + extractFn(APP, '_onOAuthPopupMessage') + '\n'
    + 'return {handleErr: _handleOAuthErrorParam, onMsg: _onOAuthPopupMessage,'
    + ' isKnown: _isKnownOAuthProvider, safeDecode: _safeDecodeParam};'
  );
  const api = runner({
    window: win,
    document: doc,
    I18N: I18N,
    showToast: function (msg) { toasts.push(msg); },
    fetch: function (url, init) {
      fetches.push({ url: url, init: init });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ token: 't' }) });
    },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    location: win.location
  });
  return { api, win, dom, toasts, fetches, doc };
}

function params(obj) {
  // 模拟 URLSearchParams：get() 返回「已解一层」的值
  return { get: function (k) { return Object.prototype.hasOwnProperty.call(obj, k) ? obj[k] : null; } };
}

// ---------------------------------------------------------------- T1（P0-1，能红）
// 注入 payload：platform 位置是未知值 → 不应有任何 DOM 写入（原型上直接拼 innerHTML 会执行脚本）
{
  const env = sandbox({});
  env.api.handleErr(params({ oauth_error: 'WHITELIST:%3Cimg%20src=x%20onerror=alert(1)%3E' }));
  ok(env.dom.created.length === 0, 'T1 未知 provider 不得建弹窗 DOM（实际创建: ' + env.dom.created.join(',') + '）');
  ok(!/<img|onerror=/i.test(env.dom.innerHTML), 'T1 payload 不得进 innerHTML');
  ok(env.toasts.length === 0, 'T1 无弹窗、无多余 toast');
}

// 未知 provider 直接调引导函数（防御纵深）：同样不建 DOM
{
  const env = sandbox({});
  // 通过 handleErr 传入 WHITELIST:<script>... 形式
  env.api.handleErr(params({ oauth_error: 'WHITELIST:evil<script>alert(1)</script>' }));
  ok(env.dom.created.length === 0, 'T1b 非法字符 provider 被丢弃');
  ok(env.api.isKnown('<img src=x>') === false, 'T1b isKnown 拒绝 HTML');
  ok(env.api.isKnown('Gitcode') === false, 'T1b isKnown 大小写敏感（渠道 key 全小写）');
  ok(env.api.isKnown('gitcode') === true, 'T1b isKnown 接受 gitcode');
  ok(env.api.isKnown('gitee') === true, 'T1b isKnown 接受 gitee');
  // 运行时渠道目录（服务端注入）也认
  const env2 = sandbox({ providers: [{ provider: 'gitcode' }, { provider: 'huawei' }, { provider: 'custom-vcs' }] });
  ok(env2.api.isKnown('custom-vcs') === true, 'T1b isKnown 认服务端注入的渠道 key');
  ok(env2.api.isKnown('not-a-channel') === false, 'T1b isKnown 拒绝目录外 key');
}

// 合法白名单拒绝仍要显示引导弹窗（P0 验收：不得因加固丢掉合法 UX）
{
  const env = sandbox({});
  env.api.handleErr(params({ oauth_error: 'WHITELIST:gitcode' }));
  ok(env.dom.created.length === 1, 'T1c 合法白名单拒绝仍弹引导弹窗');
  ok(/beta-apply\.html\?platform=gitcode/.test(env.dom.innerHTML), 'T1c 申请按钮带正确 platform');
  ok(/whitelist_title/.test(env.dom.innerHTML), 'T1c 弹窗文案走 i18n');
}

// ---------------------------------------------------------------- T2（P0-1，能红）
// 已知 provider + 第三方 username（历史服务端格式 WHITELIST:provider:login）：全部转义
{
  const env = sandbox({});
  env.api.handleErr(params({ oauth_error: 'WHITELIST:gitcode:%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E%27%3E' }));
  ok(env.dom.created.length === 1, 'T2 已知 provider 弹窗');
  const html = env.dom.innerHTML;
  ok(!/<img/i.test(html), 'T2 username 不得原样进 innerHTML: ' + html.slice(0, 200));
  ok(!/<[^>]*onerror\s*=/i.test(html), 'T2 不得形成可执行标签（onerror 只能作为转义后的文本）');
  ok(html.indexOf('&lt;img') >= 0, 'T2 username 以实体形式展示');
  // onclick 里的 URL：单引号必须编码，否则跳出 JS 字符串
  const onclick = /onclick="window\.location\.href='([^"]*)'"/.exec(html);
  ok(onclick, 'T2 申请按钮 onclick 存在');
  ok(onclick[1].indexOf("'") < 0, 'T2 onclick 内不得有裸单引号');
  ok(onclick[1].indexOf('%27') >= 0, 'T2 单引号已编码为 %27');
  ok(onclick[1].indexOf('%3Cimg') >= 0, 'T2 username 在 URL 里走 URI 编码');
}

// ---------------------------------------------------------------- T3（P0-1 服务端，能红）
// 白名单失败跳转不得夹带第三方 external_login（反射源）
{
  ok(AUTH_PY.indexOf('WHITELIST:{provider}:{external_login}') < 0, 'T3 服务端不得再把 external_login 写进跳转 URL');
  ok(/_WL_PROVIDER_KEY_RE/.test(AUTH_PY), 'T3 服务端有 provider key 形态校验');
  ok(/WHITELIST:" \+ provider/.test(AUTH_PY), 'T3 白名单错误码只带 provider');
}

// ---------------------------------------------------------------- T4/T5（P0-2，能红）
// source 不符 → 丢弃（不兑票）
{
  const popup = { name: 'real-popup' };
  const env = sandbox({ popup: popup });
  const forged = { name: 'same-origin-other-window' };
  env.api.onMsg({
    origin: 'https://grape.test', source: forged,
    data: { type: 'oauth_result', action: 'login', success: true, exchange_code: 'forged' }
  });
  ok(env.fetches.length === 0, 'T4 同源其它窗口伪造的 oauth_result 被丢弃');

  // origin 不符同样丢弃
  env.api.onMsg({
    origin: 'https://evil.test', source: popup,
    data: { type: 'oauth_result', action: 'login', success: true, exchange_code: 'x' }
  });
  ok(env.fetches.length === 0, 'T4 origin 不符被丢弃');
}

// source 相符 → 正常兑票
{
  const popup = { name: 'real-popup' };
  const env = sandbox({ popup: popup });
  env.api.onMsg({
    origin: 'https://grape.test', source: popup,
    data: { type: 'oauth_result', action: 'login', success: true, exchange_code: 'code-1' }
  });
  ok(env.fetches.length === 1, 'T5 本流程 popup 的消息正常兑票');
  ok(env.fetches[0].url === '/api/auth/exchange', 'T5 兑票端点正确');
  ok(JSON.parse(env.fetches[0].init.body).code === 'code-1', 'T5 exchange_code 透传');
}

// 未记录 popup 引用 → 一律丢弃（休眠路径默认安全）
{
  const env = sandbox({});
  env.api.onMsg({
    origin: 'https://grape.test', source: { name: 'anything' },
    data: { type: 'oauth_result', action: 'login', success: true, exchange_code: 'x' }
  });
  ok(env.fetches.length === 0, 'T5 无 popup 引用时不接受任何消息');
}

// ---------------------------------------------------------------- T6（P0-2 Vue 侧，能红）
{
  ok(/if \(ev\.source !== popup\) return/.test(ACCOUNT_VUE), 'T6 AccountView message handler 校验 source');
  ok(/if \(ev\.source !== popup\) return/.test(BIND_VUE), 'T6 BindDialog onMsg 校验 source');
  // source 校验必须在消费数据之前
  ['AccountView.vue'].forEach(function () {});
  const avIdx = ACCOUNT_VUE.indexOf('if (ev.source !== popup) return');
  const avData = ACCOUNT_VUE.indexOf('const data = ev.data', avIdx);
  ok(avIdx > 0 && avData > avIdx, 'T6 AccountView 先校验 source 再读 data');
  const bdIdx = BIND_VUE.indexOf('if (ev.source !== popup) return');
  const bdData = BIND_VUE.indexOf('const d2 = ev.data', bdIdx);
  ok(bdIdx > 0 && bdData > bdIdx, 'T6 BindDialog 先校验 source 再读 data');
}

// ---------------------------------------------------------------- T7（P0-3，能红）
// ?oauth_error=100%25 → URLSearchParams 解一层后是 '100%'，再 decodeURIComponent 会抛
{
  const env = sandbox({});
  ok(env.api.safeDecode('100%') === null, 'T7 裸 % 解码返回 null（不抛）');
  ok(env.api.safeDecode('state_invalid') === 'state_invalid', 'T7 正常值原样解码');
  ok(env.api.safeDecode('%E4%B8%8D') === '不', 'T7 合法编码正常解码');

  let threw = false;
  try {
    env.api.handleErr(params({ oauth_error: '100%' }));
  } catch (e) { threw = true; }
  ok(!threw, 'T7 解码失败不得抛出（否则 init 被中断）');
  ok(env.toasts.length === 1 && env.toasts[0].indexOf('oauth_error_invalid_param') >= 0,
     'T7 按“参数无效”提示: ' + JSON.stringify(env.toasts));
  ok(env.dom.created.length === 0, 'T7 参数无效不建弹窗');

  // 正常错误码仍走 i18n 翻译
  const env2 = sandbox({});
  env2.api.handleErr(params({ oauth_error: 'state_invalid' }));
  ok(env2.toasts.length === 1 && env2.toasts[0].indexOf('app.login.oauth_error_state_invalid') >= 0,
     'T7 正常错误码走 i18n');
}

// 白名单错误仍能正常解码（单次编码后的真实形态）
{
  const env = sandbox({});
  env.api.handleErr(params({ oauth_error: 'WHITELIST%3Agitcode' }));
  ok(env.dom.created.length === 1, 'T7 单次编码的 WHITELIST 码解出后仍弹引导');
}

// ---------------------------------------------------------------- T8（P0-3 弹窗页，能红）
{
  const m = /function safeDecode\(v\) \{[\s\S]*?\n  \}/.exec(POPUP_HTML);
  ok(m, 'T8 中间页有 safeDecode 兜底');
  const fn = new Function('v', m[0].replace('function safeDecode(v) {', '').replace(/\n  \}$/, ''));
  ok(fn('100%') === 'invalid_param', 'T8 中间页裸 % 返回 invalid_param（不停在 Processing）');
  ok(fn('WHITELIST%3Agitcode') === 'WHITELIST:gitcode', 'T8 中间页正常解码');
  ok(fn('a+b') === 'a b', 'T8 + 仍然转空格（保持原行为）');
  ok(POPUP_HTML.indexOf("result.error = safeDecode(") >= 0, 'T8 错误分支走 safeDecode');
  ok(!/decodeURIComponent\((bindError|oauthError)/.test(POPUP_HTML), 'T8 不得再裸调 decodeURIComponent');
  ok(/invalid_param/.test(POPUP_HTML), 'T8 有 invalid_param 友好文案');
}

// ---------------------------------------------------------------- T9（P0-3 服务端，能红）
{
  ok(AUTH_PY.indexOf('_up.quote(message)') < 0, 'T9 服务端不得 quote 后再 urlencode（双重编码）');
  ok(AUTH_PY.indexOf('{"oauth_error": message}') >= 0, 'T9 oauth_error 直接交给 urlencode');
  ok(AUTH_PY.indexOf('{"bind_error": message}') >= 0, 'T9 bind_error 同上');
}

// ---------------------------------------------------------------- T10 i18n 键 zh/en 齐备
{
  ['zh', 'en'].forEach(function (lang) {
    const d = JSON.parse(fs.readFileSync(path.join(SHELL, 'locales', lang, 'app.json'), 'utf8'));
    ok(d.login && typeof d.login.oauth_error_invalid_param === 'string' && d.login.oauth_error_invalid_param,
       'T10 ' + lang + ' 有 app.login.oauth_error_invalid_param');
  });
}

console.log('结果: ' + pass + ' 通过, 0 失败');
console.log('✓ Issue #3054 P0（登录页安全与健壮性）全部断言通过');