/*
 * 认证页面邮箱配置闪动修复 — 静态断言测试
 *
 * 运行：node tests/js/test_auth_page_email_config.js
 *
 * 对应需求文档：auth-page-email-config-flicker-requirements.md
 * 对应设计文档：auth-page-email-config-flicker-design.md
 *
 * 验证点（59 个）：
 *   T01-T04: main.py 服务端注入
 *   T05-t10: register.html HTML + JS
 *   T11-t12: register.html 错误处理
 *   T13-t22: forgot.html HTML + JS
 *   T23-t24: index.html
 *   T25-t28: grape_auth.py 后端 error_code
 *   T29-t32: locale app.error.* 补全
 *   T33-t34: auth.js 零改动
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..', '..');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('认证页面邮箱配置闪动修复 — 静态断言测试\n');

// ── 加载文件 ──
const MAIN_PY = read('src/api/main.py');
const REGISTER_HTML = read('src/api/static/register.html');
const FORGOT_HTML = read('src/api/static/forgot.html');
const INDEX_HTML = read('src/api/static/index.html');
const GRAPE_AUTH_PY = read('src/api/routes/grape_auth.py');
const AUTH_JS = read('src/api/static/js/auth.js');
const ZH_APP = JSON.parse(read('src/api/static/locales/zh/app.json'));
const EN_APP = JSON.parse(read('src/api/static/locales/en/app.json'));

// ── T01: main.py 含 _serve_auth_page ──
test('T01: main.py 含 _serve_auth_page 函数', () => {
  assert.ok(MAIN_PY.indexOf('_serve_auth_page') >= 0, '_serve_auth_page 函数应存在');
});

// ── T02: main.py 含 is_email_configured 导入 ──
test('T02: main.py 含 is_email_configured 导入', () => {
  assert.ok(MAIN_PY.indexOf('_is_email_configured') >= 0, '应导入 is_email_configured');
});

// ── T03: main.py 4个路由调用 _serve_auth_page ──
test('T03: main.py 4个路由调用 _serve_auth_page', () => {
  const matches = MAIN_PY.match(/_serve_auth_page\(/g);
  assert.ok(matches && matches.length >= 5, '应至少 5 处调用（1定义+4路由）, 实际: ' + (matches ? matches.length : 0));
});

// ── T04: main.py _spa_response 含 __EMAIL_CONFIGURED__ 注入 ──
test('T04: main.py _spa_response 含 __EMAIL_CONFIGURED__ 注入', () => {
  assert.ok(MAIN_PY.indexOf('__EMAIL_CONFIGURED__') >= 0, '_spa_response 应注入 __EMAIL_CONFIGURED__');
});

// ── T05: register.html 不含 API.get('/auth/providers') ──
test('T05: register.html 不含 API.get(\'/auth/providers\')', () => {
  assert.ok(REGISTER_HTML.indexOf("API.get('/auth/providers')") < 0, '不应含异步 API.get');
});

// ── T06: register.html 含 window.__EMAIL_CONFIGURED__ ──
test('T06: register.html 含 window.__EMAIL_CONFIGURED__', () => {
  assert.ok(REGISTER_HTML.indexOf('__EMAIL_CONFIGURED__') >= 0, '应含 __EMAIL_CONFIGURED__');
});

// ── T07: register.html regCodeArea 保持 display:none ──
test('T07: register.html regCodeArea 保持 display:none 初始状态', () => {
  assert.ok(REGISTER_HTML.indexOf('id="regCodeArea" style="display:none"') >= 0, 'regCodeArea 应保持 display:none');
});

// ── T08: register.html emailField 无 display:none ──
test('T08: register.html emailField 无 display:none 初始状态', () => {
  const m = REGISTER_HTML.match(/id="emailField"[^>]*/);
  assert.ok(m, 'emailField 元素应存在');
  assert.ok(m[0].indexOf('display:none') < 0, 'emailField 不应有 display:none');
});

// ── T09: register.html stepIndicator 无 display:none ──
test('T09: register.html stepIndicator 无 display:none 初始状态', () => {
  const m = REGISTER_HTML.match(/id="stepIndicator"[^>]*/);
  assert.ok(m, 'stepIndicator 元素应存在');
  assert.ok(m[0].indexOf('display:none') < 0, 'stepIndicator 不应有 display:none');
});

// ── T10: register.html api.js?v=31, app.css?v=85 ──
test('T10: register.html api.js?v=31, app.css?v=85', () => {
  assert.ok(REGISTER_HTML.indexOf('api.js?v=31') >= 0, 'api.js 应为 v31');
  assert.ok(REGISTER_HTML.indexOf('app.css?v=85') >= 0, 'app.css 应为 v85');
});

// ── T11: register.html 不含 msg.indexOf ──
test('T11: register.html 不含 msg.indexOf 字符串匹配', () => {
  assert.ok(REGISTER_HTML.indexOf('msg.indexOf') < 0, '不应含 msg.indexOf 字符串匹配');
});

// ── T12: register.html 含 Auth.t(\'app.error.\' ──
test('T12: register.html 含 Auth.t(\'app.error.\') i18n 调用', () => {
  assert.ok(REGISTER_HTML.indexOf("Auth.t('app.error.'") >= 0, '应含 Auth.t(\'app.error.\') i18n 调用');
});

// ── T13: forgot.html 不含 API.get('/auth/providers') ──
test('T13: forgot.html 不含 API.get(\'/auth/providers\')', () => {
  assert.ok(FORGOT_HTML.indexOf("API.get('/auth/providers')") < 0, '不应含异步 API.get');
});

// ── T14: forgot.html 含 window.__EMAIL_CONFIGURED__ ──
test('T14: forgot.html 含 window.__EMAIL_CONFIGURED__', () => {
  assert.ok(FORGOT_HTML.indexOf('__EMAIL_CONFIGURED__') >= 0, '应含 __EMAIL_CONFIGURED__');
});

// ── T15: forgot.html 含 forgotNotConfigured 预置 div ──
test('T15: forgot.html 含 forgotNotConfigured 预置 div', () => {
  assert.ok(FORGOT_HTML.indexOf('id="forgotNotConfigured"') >= 0, '应含 forgotNotConfigured 预置 div');
});

// ── T16: forgot.html forgotNotConfigured 的 p 有 data-i18n ──
test('T16: forgot.html forgotNotConfigured 的 p 有 data-i18n', () => {
  const idx = FORGOT_HTML.indexOf('id="forgotNotConfigured"');
  const block = FORGOT_HTML.slice(idx, idx + 500);
  assert.ok(block.indexOf('data-i18n="app.login.forgot_email_not_configured"') >= 0, '提示 p 应有 data-i18n');
});

// ── T17: forgot.html forgotStep1 无 display:none ──
test('T17: forgot.html forgotStep1 无 display:none 初始状态', () => {
  const m = FORGOT_HTML.match(/id="forgotStep1"[^>]*/);
  assert.ok(m, 'forgotStep1 元素应存在');
  assert.ok(m[0].indexOf('display:none') < 0, 'forgotStep1 不应有 display:none');
});

// ── T18: forgot.html forgotEmailDisplay label 有 for 属性 ──
test('T18: forgot.html forgotEmailDisplay label 有 for 属性', () => {
  assert.ok(FORGOT_HTML.indexOf('for="forgotEmailDisplay"') >= 0, 'label 应有 for="forgotEmailDisplay"');
});

// ── T19: forgot.html api.js?v=31, app.css?v=85 ──
test('T19: forgot.html api.js?v=31, app.css?v=85', () => {
  assert.ok(FORGOT_HTML.indexOf('api.js?v=31') >= 0, 'api.js 应为 v31');
  assert.ok(FORGOT_HTML.indexOf('app.css?v=85') >= 0, 'app.css 应为 v85');
});

// ── T20: forgot.html 不含 msg.indexOf ──
test('T20: forgot.html 不含 msg.indexOf 字符串匹配', () => {
  assert.ok(FORGOT_HTML.indexOf('msg.indexOf') < 0, '不应含 msg.indexOf 字符串匹配');
});

// ── T21: forgot.html 不含 createElement（排除注释）──
test('T21: forgot.html 不含 createElement（排除注释）', () => {
  const lines = FORGOT_HTML.split('\n');
  // 排除：以 // 开头的行、以 <!-- 开头的注释行
  const offending = lines.filter(l => {
    var t = l.trim();
    return t.indexOf('createElement') >= 0 && !t.startsWith('//') && !t.startsWith('<!--');
  });
  assert.ok(offending.length === 0, '不应含 createElement 调用, 发现: ' + offending.join('; '));
});

// ── T22: forgot.html 含 Auth.t(\'app.error.\' ──
test('T22: forgot.html 含 Auth.t(\'app.error.\') i18n 调用', () => {
  assert.ok(FORGOT_HTML.indexOf("Auth.t('app.error.'") >= 0, '应含 Auth.t(\'app.error.\') i18n 调用');
});

// ── T23: index.html 不含 fetch('/api/auth/providers') ──
test('T23: index.html 不含 fetch(\'/api/auth/providers\')', () => {
  assert.ok(INDEX_HTML.indexOf("fetch('/api/auth/providers')") < 0, '不应含异步 fetch');
});

// ── T24: index.html 含 window.__EMAIL_CONFIGURED__ ──
test('T24: index.html 含 window.__EMAIL_CONFIGURED__', () => {
  assert.ok(INDEX_HTML.indexOf('__EMAIL_CONFIGURED__') >= 0, '应含 __EMAIL_CONFIGURED__');
});

// ── T25: grape_auth.py register/send-code 含 error_code ──
test('T25: grape_auth.py register/send-code 含 error_code', () => {
  const idx = GRAPE_AUTH_PY.indexOf('def register_send_code');
  const block = GRAPE_AUTH_PY.slice(idx, idx + 2000);
  assert.ok(block.indexOf('error_code') >= 0, 'register/send-code 应含 error_code');
  assert.ok(block.indexOf('USERNAME_INVALID') >= 0, '应含 USERNAME_INVALID');
  assert.ok(block.indexOf('EMAIL_INVALID') >= 0, '应含 EMAIL_INVALID');
});

// ── T26: grape_auth.py register/verify 含 error_code ──
test('T26: grape_auth.py register/verify 含 error_code', () => {
  const idx = GRAPE_AUTH_PY.indexOf('def register_verify');
  const block = GRAPE_AUTH_PY.slice(idx, idx + 2000);
  assert.ok(block.indexOf('error_code') >= 0, 'register/verify 应含 error_code');
  assert.ok(block.indexOf('CODE_INVALID') >= 0, '应含 CODE_INVALID');
  assert.ok(block.indexOf('PASSWORD_WEAK') >= 0, '应含 PASSWORD_WEAK');
});

// ── T27: grape_auth.py forgot-password/reset 含 error_code ──
test('T27: grape_auth.py forgot-password/reset 含 error_code', () => {
  const idx = GRAPE_AUTH_PY.indexOf('def forgot_password_reset');
  const block = GRAPE_AUTH_PY.slice(idx, idx + 1500);
  assert.ok(block.indexOf('error_code') >= 0, 'forgot-password/reset 应含 error_code');
  assert.ok(block.indexOf('CODE_INVALID') >= 0, '应含 CODE_INVALID');
  assert.ok(block.indexOf('ACCOUNT_NOT_FOUND') >= 0, '应含 ACCOUNT_NOT_FOUND');
});

// ── T28: grape_auth.py oauth/register 含 error_code ──
test('T28: grape_auth.py oauth/register 含 error_code', () => {
  const idx = GRAPE_AUTH_PY.indexOf('def oauth_register');
  const block = GRAPE_AUTH_PY.slice(idx, idx + 3000);
  assert.ok(block.indexOf('error_code') >= 0, 'oauth/register 应含 error_code');
  assert.ok(block.indexOf('OAUTH_STATE_EXPIRED') >= 0, '应含 OAUTH_STATE_EXPIRED');
  assert.ok(block.indexOf('OAUTH_ALREADY_BOUND') >= 0, '应含 OAUTH_ALREADY_BOUND');
});

// ── T29: zh error.USERNAME_INVALID 存在（按 JSON 路径解析）──
test('T29: zh error.USERNAME_INVALID exists (JSON path lookup)', () => {
  const err = ZH_APP.error;
  assert.ok(err, 'zh error namespace should exist at top level');
  assert.ok(err.USERNAME_INVALID, 'zh error.USERNAME_INVALID should exist');
  // 确保不在 app.error 下（防嵌套层级 bug 回归）
  assert.ok(!ZH_APP.app || !ZH_APP.app.error, 'zh should NOT have app.error (nested wrong level)');
});

// ── T30: en error.USERNAME_INVALID 存在（按 JSON 路径解析）──
test('T30: en error.USERNAME_INVALID exists (JSON path lookup)', () => {
  const err = EN_APP.error;
  assert.ok(err, 'en error namespace should exist at top level');
  assert.ok(err.USERNAME_INVALID, 'en error.USERNAME_INVALID should exist');
  assert.ok(!EN_APP.app || !EN_APP.app.error, 'en should NOT have app.error (nested wrong level)');
});

// ── T31: zh error 含 16 个 key（按 JSON 路径解析）──
test('T31: zh error contains 16 error_code keys (JSON path lookup)', () => {
  const expected = ['USERNAME_INVALID','USERNAME_TAKEN','EMAIL_INVALID','EMAIL_NOT_ALLOWED',
    'EMAIL_ALREADY_REGISTERED','PASSWORD_WEAK','CODE_INVALID','ACCOUNT_NOT_FOUND',
    'ACCOUNT_CREATE_FAILED','OAUTH_STATE_EXPIRED','OAUTH_NOT_WHITELISTED','OAUTH_ALREADY_BOUND',
    'OAUTH_TOKEN_DECRYPT_FAILED','CREDENTIAL_INCORRECT','ACCOUNT_DEACTIVATED','ACCOUNT_DEACTIVATING'];
  const err = ZH_APP.error;
  assert.ok(err, 'zh error namespace should exist');
  for (const code of expected) {
    assert.ok(err[code], 'zh error should contain ' + code + ' (got: ' + (err[code]||'MISSING') + ')');
  }
});

// ── T32: en error 含 16 个 key（按 JSON 路径解析）──
test('T32: en error contains 16 error_code keys (JSON path lookup)', () => {
  const expected = ['USERNAME_INVALID','USERNAME_TAKEN','EMAIL_INVALID','EMAIL_NOT_ALLOWED',
    'EMAIL_ALREADY_REGISTERED','PASSWORD_WEAK','CODE_INVALID','ACCOUNT_NOT_FOUND',
    'ACCOUNT_CREATE_FAILED','OAUTH_STATE_EXPIRED','OAUTH_NOT_WHITELISTED','OAUTH_ALREADY_BOUND',
    'OAUTH_TOKEN_DECRYPT_FAILED','CREDENTIAL_INCORRECT','ACCOUNT_DEACTIVATED','ACCOUNT_DEACTIVATING'];
  const err = EN_APP.error;
  assert.ok(err, 'en error namespace should exist');
  for (const code of expected) {
    assert.ok(err[code], 'en error should contain ' + code + ' (got: ' + (err[code]||'MISSING') + ')');
  }
});

// ── T33: auth.js t() 支持第三参数 fallback ──
test('T33: auth.js t() 支持第三参数 fallback', () => {
  assert.ok(/function t\(key,\s*vars,\s*fallback\)/.test(AUTH_JS), 't() 应支持 (key, vars, fallback) 签名');
  assert.ok(AUTH_JS.indexOf('fallback !== undefined ? fallback : key') >= 0, '缺 key 时应返回 fallback');
});

// ── T34: auth.js 不含 handleApiError/errMsg 新增函数 ──
test('T34: auth.js 不含 handleApiError/errMsg 新增函数', () => {
  assert.ok(AUTH_JS.indexOf('handleApiError') < 0, '不应含 handleApiError');
  assert.ok(AUTH_JS.indexOf('function errMsg') < 0, '不应含 errMsg 新增函数');
});

// ── T35: i18n.js inline pack 含 common.server_error ──
test('T35: i18n.js inline pack 含 common.server_error', () => {
  const I18N_JS = read('src/api/static/js/i18n.js');
  assert.ok(I18N_JS.indexOf('server_error') >= 0, 'inline pack 应含 server_error');
});

// ── T36: register.html 验证码区域含帮助入口 ──
test('T36: register.html has code-not-received help hint', () => {
  assert.ok(REGISTER_HTML.indexOf('code_not_received_hint') >= 0, '应有 code_not_received_hint 帮助入口');
  assert.ok(REGISTER_HTML.indexOf('data-i18n=\"app.login.forgot_password\"') >= 0, '帮助入口应有找回密码链接');
});

// ── T37: register.html catch 含 common.server_error 兜底 ──
test('T37: register.html catch 含 common.server_error 兜底', () => {
  assert.ok(REGISTER_HTML.indexOf('common.server_error') >= 0, 'catch 应含 common.server_error 兜底');
});

// ── T38: forgot.html catch 含 common.server_error 兜底 ──
test('T38: forgot.html catch 含 common.server_error 兜底', () => {
  assert.ok(FORGOT_HTML.indexOf('common.server_error') >= 0, 'catch 应含 common.server_error 兜底');
});

// ── T39: send-code 不返回 EMAIL_ALREADY_REGISTERED（防枚举）──
test('T39: send-code 不返回 EMAIL_ALREADY_REGISTERED', () => {
  const fnIdx = GRAPE_AUTH_PY.indexOf('def register_send_code');
  const fnEnd = GRAPE_AUTH_PY.indexOf('\n@router', fnIdx + 10);
  const fnBody = GRAPE_AUTH_PY.slice(fnIdx, fnEnd > 0 ? fnEnd : fnIdx + 3000);
  assert.ok(!fnBody.includes('EMAIL_ALREADY_REGISTERED'), 'send-code 不应返回 EMAIL_ALREADY_REGISTERED');
});

// ── T40: main.py 注入 email-configured/email-not-configured html class ──
test('T40: main.py injects email-configured/email-not-configured html class', () => {
  assert.ok(MAIN_PY.indexOf('email-configured') >= 0, '应含 email-configured class');
  assert.ok(MAIN_PY.indexOf('email-not-configured') >= 0, '应含 email-not-configured class');
  assert.ok(MAIN_PY.indexOf('document.documentElement.className') >= 0, '应注入 html class');
});

// ── T41: register.html CSS 含 .email-not-configured #emailField 隐藏规则 ──
test('T41: register.html CSS has .email-not-configured #emailField hide rule', () => {
  assert.ok(REGISTER_HTML.indexOf('.email-not-configured #emailField') >= 0, 'CSS 应含 .email-not-configured #emailField');
  // 检查该规则附近有 display: none !important
  const idx = REGISTER_HTML.indexOf('.email-not-configured #emailField');
  const block = REGISTER_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
});

// ── T42: register.html CSS 含 .email-not-configured #stepIndicator 隐藏规则 ──
test('T42: register.html CSS has .email-not-configured #stepIndicator hide rule', () => {
  assert.ok(REGISTER_HTML.indexOf('.email-not-configured #stepIndicator') >= 0, 'CSS 应含 .email-not-configured #stepIndicator');
  const idx = REGISTER_HTML.indexOf('.email-not-configured #stepIndicator');
  const block = REGISTER_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
});

// ── T43: forgot.html CSS 含 .email-not-configured #forgotStep1 隐藏规则 ──
test('T43: forgot.html CSS has .email-not-configured #forgotStep1 hide rule', () => {
  assert.ok(FORGOT_HTML.indexOf('.email-not-configured #forgotStep1') >= 0, 'CSS 应含 .email-not-configured #forgotStep1');
  const idx = FORGOT_HTML.indexOf('.email-not-configured #forgotStep1');
  const block = FORGOT_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
});

// ── T44: forgot.html CSS 含 .email-not-configured #forgotNotConfigured 显示规则 ──
test('T44: forgot.html CSS has .email-not-configured #forgotNotConfigured show rule', () => {
  assert.ok(FORGOT_HTML.indexOf('.email-not-configured #forgotNotConfigured') >= 0, 'CSS 应含 .email-not-configured #forgotNotConfigured');
  const idx = FORGOT_HTML.indexOf('.email-not-configured #forgotNotConfigured');
  const block = FORGOT_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: block !important') >= 0 || block.indexOf('display:block !important') >= 0, '应有 display: block !important');
});

// ── T45: forgot.html CSS 含 .email-configured #forgotNotConfigured 隐藏规则 ──
test('T45: forgot.html CSS has .email-configured #forgotNotConfigured hide rule', () => {
  assert.ok(FORGOT_HTML.indexOf('.email-configured #forgotNotConfigured') >= 0, 'CSS 应含 .email-configured #forgotNotConfigured');
  const idx = FORGOT_HTML.indexOf('.email-configured #forgotNotConfigured');
  const block = FORGOT_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
});

// ── T46: main.py 含 __I18N_BOOST__ 注入 + 早期翻译 script ──
test('T46: main.py injects __I18N_BOOST__ + early-translate script', () => {
  assert.ok(MAIN_PY.indexOf('__I18N_BOOST__') >= 0, 'main.py 应含 __I18N_BOOST__ 注入');
  assert.ok(MAIN_PY.indexOf('_get_i18n_boost') >= 0, 'main.py 应含 _get_i18n_boost 函数');
  assert.ok(MAIN_PY.indexOf('_EARLY_TRANSLATE_SCRIPT') >= 0, 'main.py 应含早期翻译 script');
  assert.ok(MAIN_PY.indexOf('querySelectorAll') >= 0, '早期翻译 script 应含 querySelectorAll');
  assert.ok(MAIN_PY.indexOf("classList.remove('i18n-loading')") >= 0, '早期翻译 script 应移除 i18n-loading');
});

// ── T47: i18n.js 含 _initialized 防重复标志 ──
test('T47: i18n.js has _initialized guard', () => {
  const I18N_JS = read('src/api/static/js/i18n.js');
  assert.ok(I18N_JS.indexOf('_initialized') >= 0, 'i18n.js 应含 _initialized 标志');
  assert.ok(I18N_JS.indexOf('if (_initialized) return') >= 0, 'init 应检查 _initialized');
});

// ── T48: i18n.js 含自动 init（readyState 检查） ──
test('T48: i18n.js has auto-init with readyState check', () => {
  const I18N_JS = read('src/api/static/js/i18n.js');
  assert.ok(I18N_JS.indexOf('readyState') >= 0, '应含 readyState 检查');
  assert.ok(I18N_JS.indexOf('isInitialized') >= 0, '应暴露 isInitialized getter');
});

// ── T49: i18n.js 含 __I18N_BOOST__ 读取逻辑 ──
test('T49: i18n.js reads __I18N_BOOST__', () => {
  const I18N_JS = read('src/api/static/js/i18n.js');
  assert.ok(I18N_JS.indexOf('__I18N_BOOST__') >= 0, 'i18n.js 应读取 __I18N_BOOST__');
  assert.ok(I18N_JS.indexOf('_deepMerge') >= 0, '应有 _deepMerge 函数合并 boost');
});

// ── T50: register/forgot/index i18n.js 标签含 defer ──
test('T50: register/forgot/index i18n.js has defer attribute', () => {
  assert.ok(REGISTER_HTML.indexOf('i18n.js?v=16" defer') >= 0 || REGISTER_HTML.indexOf('i18n.js?v=16"defer') >= 0, 'register.html i18n.js 应有 defer');
  assert.ok(FORGOT_HTML.indexOf('i18n.js?v=16" defer') >= 0 || FORGOT_HTML.indexOf('i18n.js?v=16"defer') >= 0, 'forgot.html i18n.js 应有 defer');
  assert.ok(INDEX_HTML.indexOf('defer') >= 0 && INDEX_HTML.indexOf('i18n.js') >= 0, 'index.html i18n.js 应有 defer');
});

// ── T51: 三页面 CSS 含 html.i18n-loading 规则 ──
test('T51: all pages have html.i18n-loading CSS rule', () => {
  assert.ok(REGISTER_HTML.indexOf('html.i18n-loading [data-i18n]') >= 0, 'register.html 应有 html.i18n-loading CSS');
  assert.ok(FORGOT_HTML.indexOf('html.i18n-loading [data-i18n]') >= 0, 'forgot.html 应有 html.i18n-loading CSS');
  assert.ok(INDEX_HTML.indexOf('html.i18n-loading [data-i18n]') >= 0, 'index.html 应有 html.i18n-loading CSS');
});

// ── T52: 三页面含超时兜底（5 秒移除 i18n-loading） ──
test('T52: all pages have 5s timeout fallback', () => {
  assert.ok(REGISTER_HTML.indexOf('5000') >= 0 && REGISTER_HTML.indexOf('classList.remove') >= 0, 'register.html 应有 5 秒超时兜底');
  assert.ok(FORGOT_HTML.indexOf('5000') >= 0 && FORGOT_HTML.indexOf('classList.remove') >= 0, 'forgot.html 应有 5 秒超时兜底');
  assert.ok(INDEX_HTML.indexOf('5000') >= 0 && INDEX_HTML.indexOf('classList.remove') >= 0, 'index.html 应有 5 秒超时兜底');
});

// ── T53: 三页面含 window.__I18N_CACHE_VER__ 设置 ──
test('T53: all pages set __I18N_CACHE_VER__', () => {
  assert.ok(REGISTER_HTML.indexOf('__I18N_CACHE_VER__') >= 0, 'register.html 应设置 __I18N_CACHE_VER__');
  assert.ok(FORGOT_HTML.indexOf('__I18N_CACHE_VER__') >= 0, 'forgot.html 应设置 __I18N_CACHE_VER__');
  assert.ok(INDEX_HTML.indexOf('__I18N_CACHE_VER__') >= 0, 'index.html 应设置 __I18N_CACHE_VER__');
});

// ── T54: i18n.js applyI18nToDOM 移除 i18n-loading class ──
test('T54: i18n.js applyI18nToDOM removes i18n-loading class', () => {
  const I18N_JS = read('src/api/static/js/i18n.js');
  assert.ok(I18N_JS.indexOf("classList.remove('i18n-loading')") >= 0, 'applyI18nToDOM 应移除 i18n-loading class');
});

// ── T55: main.py 含 _EARLY_TRANSLATE_SCRIPT + querySelectorAll + classList.remove ──
test('T55: main.py has _EARLY_TRANSLATE_SCRIPT with querySelectorAll + classList.remove', () => {
  assert.ok(MAIN_PY.indexOf('_EARLY_TRANSLATE_SCRIPT') >= 0, '应含 _EARLY_TRANSLATE_SCRIPT');
  assert.ok(MAIN_PY.indexOf("querySelectorAll('[data-i18n]')") >= 0, 'early-translate 应含 querySelectorAll');
  assert.ok(MAIN_PY.indexOf("classList.remove('i18n-loading')") >= 0, 'early-translate 应移除 i18n-loading');
  // 注入到 </body> 前
  assert.ok(MAIN_PY.indexOf("_EARLY_TRANSLATE_SCRIPT + '</body>'") >= 0, '应注入到 </body> 前');
});

// ── T56: main.py boost 构建含 try-except 兜底返回 '{}' ──
test('T56: main.py boost build has try-except fallback returning {}', () => {
  assert.ok(MAIN_PY.indexOf('except Exception') >= 0, '应有 try-except');
  assert.ok(MAIN_PY.indexOf("_I18N_BOOST_CACHE = '{}'") >= 0, 'except 应返回空 JSON {}');
});

// ── T57: main.py boost 提取覆盖三种 key 结构 ──
test('T57: main.py boost extraction handles nested + top-level + cross-file keys', () => {
  // 嵌套 key: app.login.xxx
  assert.ok(MAIN_PY.indexOf('isinstance(app_data[section], dict)') >= 0, '应处理嵌套 dict key');
  // 顶层字符串 key: app.back_home
  assert.ok(MAIN_PY.indexOf('parts[1] == leaf') >= 0, '应处理顶层字符串 key');
  // 跨文件 key: organizations.xxx
  assert.ok(MAIN_PY.indexOf('organizations.json') >= 0, '应处理跨文件 organizations key');
});

// ── T58: early-translate script 处理 data-i18n-placeholder ──
test('T58: early-translate script handles data-i18n-placeholder', () => {
  assert.ok(MAIN_PY.indexOf("querySelectorAll('[data-i18n-placeholder]')") >= 0, 'early-translate 应处理 data-i18n-placeholder');
  assert.ok(MAIN_PY.indexOf('setAttribute') >= 0, '应设置 placeholder 属性');
});

// ── T59: index.html body 中所有 <script src> 含 defer（无同步阻塞脚本） ──
test('T59: index.html body scripts all have defer (no sync blocking)', () => {
  // 提取所有 <script src="..."> 标签
  var scripts = INDEX_HTML.match(/<script\s+src="[^"]*"><\/script>/g) || [];
  assert.ok(scripts.length === 0, 'index.html 不应有不含 defer 的 <script src> 标签，实际: ' + scripts.length);
  // 确认有 defer 标签
  var deferScripts = INDEX_HTML.match(/<script\s+src="[^"]*"\s+defer><\/script>/g) || [];
  assert.ok(deferScripts.length >= 30, 'index.html 应有至少 30 个 defer 脚本，实际: ' + deferScripts.length);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');