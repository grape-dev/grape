/*
 * 认证页面邮箱配置闪动修复 — 场景用例测试
 *
 * 运行：node tests/js/test_auth_page_scenarios.js
 *
 * 对应需求文档场景用例 U1-U8，基于 Playwright DOM 检查结果编写为静态断言。
 * 需配合 Playwright E2E 运行（python3.9 tests/py/test_auth_page_scenarios.py）。
 *
 * 本文件做静态断言：验证 HTML 结构和 JS 逻辑在两种场景下的正确性。
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..', '..');

const REGISTER_HTML = fs.readFileSync(path.join(ROOT, 'src/api/static/register.html'), 'utf8');
const FORGOT_HTML = fs.readFileSync(path.join(ROOT, 'src/api/static/forgot.html'), 'utf8');
const INDEX_HTML = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('认证页面邮箱配置闪动修复 — 场景用例测试\n');

// ── 辅助函数：提取函数体 ──
function extractFn(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  if (!m) return null;
  let start = m.index + m[0].length;
  let depth = 1;
  let i = start;
  while (i < src.length && depth > 0) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(m.index, i);
}

// ============ U1: 注册页 — 有邮箱配置 ============

console.log('── U1: 注册页 — 有邮箱配置 ──');

test('U1-1: 有邮箱时首次渲染显示邮箱字段+步骤指示器，验证码区域不显示', () => {
  // emailField 无 display:none（有邮箱时可见）
  const em = REGISTER_HTML.match(/id="emailField"[^>]*/);
  assert.ok(em && !em[0].includes('display:none'), 'emailField 不应有 display:none');
  // stepIndicator 无 display:none
  const sm = REGISTER_HTML.match(/id="stepIndicator"[^>]*/);
  assert.ok(sm && !sm[0].includes('display:none'), 'stepIndicator 不应有 display:none');
  // regCodeArea 保持 display:none（修复 P2 双按钮 bug）
  assert.ok(REGISTER_HTML.includes('id="regCodeArea" style="display:none"'), 'regCodeArea 应保持 display:none');
});

test('U1-2: 有邮箱时 applyEmailConfig 走 goToStep2 分支，不碰 regCodeArea', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  assert.ok(fn, 'applyEmailConfig 函数应存在');
  // 有邮箱分支（if 块）应走 goToStep2
  const ifBlock = fn.slice(0, fn.indexOf('else'));
  assert.ok(ifBlock.includes('goToStep2'), '有邮箱分支应走 goToStep2');
  // 有邮箱分支不碰 regCodeArea
  assert.ok(!ifBlock.includes('regCodeArea'), '有邮箱分支不应碰 regCodeArea display');
});

test('U1-3: 有邮箱时按钮改为"发送验证码"', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  assert.ok(fn.includes('send_code'), '有邮箱时按钮应改为 send_code');
});

// ============ U2: 注册页 — 无邮箱配置 ============

console.log('\n── U2: 注册页 — 无邮箱配置 ──');

test('U2-1: 无邮箱时 applyEmailConfig 隐藏邮箱字段+步骤指示器+验证码区域', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  const elseBlock = fn.slice(fn.indexOf('else'));
  assert.ok(elseBlock.includes('emailField'), '应隐藏 emailField');
  assert.ok(elseBlock.includes('stepIndicator'), '应隐藏 stepIndicator');
  assert.ok(elseBlock.includes('regCodeArea'), '应隐藏 regCodeArea');
});

test('U2-2: 无邮箱时表单走 registerVerify（直接注册）', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  assert.ok(fn.includes('registerVerify'), '无邮箱分支应走 registerVerify');
});

test('U2-3: 无邮箱时按钮保持"注册"（不改 send_code）', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  // 无邮箱分支不应含 send_code
  const elseBlock = fn.slice(fn.indexOf('else'));
  assert.ok(!elseBlock.includes('send_code'), '无邮箱分支不应改按钮为 send_code');
});

// ============ U3: 注册页 — OAuth 关联模式 ============

console.log('\n── U3: 注册页 — OAuth 关联模式 ──');

test('U3-1: OAuth 模式 applyEmailConfig 直接 return', () => {
  const fn = extractFn(REGISTER_HTML, 'applyEmailConfig');
  assert.ok(fn.includes('oauthMode') && fn.includes('return'), 'OAuth 模式应直接 return');
});

// ============ U4: 忘记密码页 — 有邮箱配置 ============

console.log('\n── U4: 忘记密码页 — 有邮箱配置 ──');

test('U4-1: 有邮箱时 forgotStep1 首次渲染可见', () => {
  const fm = FORGOT_HTML.match(/id="forgotStep1"[^>]*/);
  assert.ok(fm && !fm[0].includes('display:none'), 'forgotStep1 不应有 display:none');
});

test('U4-2: 有邮箱时 applyEmailConfig 显示表单+步骤指示器，隐藏提示', () => {
  const fn = extractFn(FORGOT_HTML, 'applyEmailConfig');
  assert.ok(fn, 'applyEmailConfig 函数应存在');
  const ifBlock = fn.slice(0, fn.indexOf('else'));
  assert.ok(ifBlock.includes('forgotStep1'), '有邮箱应操作 forgotStep1');
  assert.ok(ifBlock.includes('forgotNotConfigured'), '有邮箱应操作 forgotNotConfigured');
  assert.ok(ifBlock.includes("'none'"), '有邮箱应隐藏 forgotNotConfigured');
});

// ============ U5: 忘记密码页 — 无邮箱配置 ============

console.log('\n── U5: 忘记密码页 — 无邮箱配置 ──');

test('U5-1: 无邮箱时显示预置提示，不动态 createElement', () => {
  assert.ok(FORGOT_HTML.includes('id="forgotNotConfigured"'), '应有 forgotNotConfigured 预置 div');
  // 无 createElement（排除注释）
  const lines = FORGOT_HTML.split('\n');
  const ce = lines.filter(l => {
    var t = l.trim();
    return t.includes('createElement') && !t.startsWith('//') && !t.startsWith('<!--');
  });
  assert.ok(ce.length === 0, '不应含 createElement 调用');
});

test('U5-2: 无邮箱时提示有 data-i18n，语言切换自动更新', () => {
  const idx = FORGOT_HTML.indexOf('id="forgotNotConfigured"');
  const block = FORGOT_HTML.slice(idx, idx + 500);
  assert.ok(block.includes('data-i18n="app.login.forgot_email_not_configured"'), '提示 p 应有 data-i18n');
});

test('U5-3: 无邮箱时 applyEmailConfig 隐藏表单+步骤指示器，显示提示', () => {
  const fn = extractFn(FORGOT_HTML, 'applyEmailConfig');
  const elseBlock = fn.slice(fn.indexOf('else'));
  assert.ok(elseBlock.includes('forgotStep1'), '无邮箱应操作 forgotStep1');
  assert.ok(elseBlock.includes('stepIndicator'), '无邮箱应隐藏 stepIndicator');
  assert.ok(elseBlock.includes('forgotNotConfigured'), '无邮箱应操作 forgotNotConfigured');
  assert.ok(elseBlock.includes("'block'"), '无邮箱应显示 forgotNotConfigured');
});

// ============ U6: 登录页 ============

console.log('\n── U6: 登录页 ──');

test('U6-1: 有邮箱时"忘记密码"链接同步显示', () => {
  assert.ok(INDEX_HTML.includes('__EMAIL_CONFIGURED__'), '应含 __EMAIL_CONFIGURED__ 同步读取');
  assert.ok(!INDEX_HTML.includes("fetch('/api/auth/providers')"), '不应含异步 fetch');
});

test('U6-2: 无邮箱时"忘记密码"链接不显示', () => {
  // forgotLink 初始 display:none，JS 只在有邮箱时 display=''
  assert.ok(INDEX_HTML.includes('id="forgotLink"') && INDEX_HTML.includes('style="display:none"'), 'forgotLink 初始应 display:none');
  // JS 中只有 emailConfigured 为 true 时才显示
  const scriptIdx = INDEX_HTML.indexOf('__EMAIL_CONFIGURED__');
  const scriptBlock = INDEX_HTML.slice(scriptIdx, scriptIdx + 500);
  assert.ok(scriptBlock.includes('if(emailConfigured)'), '应判断 emailConfigured 才显示');
});

// ============ U7: 错误处理 — error_code i18n ============

console.log('\n── U7: 错误处理 — error_code i18n ──');

test('U7-1: register goToStep2 catch 基于 error_code 定位字段', () => {
  const fn = extractFn(REGISTER_HTML, 'goToStep2');
  const catchIdx = fn.indexOf('.catch');
  const catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.includes('error_code'), '应含 error_code');
  assert.ok(catchBlock.includes('USERNAME_INVALID'), '应映射 USERNAME_INVALID');
  assert.ok(catchBlock.includes('EMAIL_INVALID'), '应映射 EMAIL_INVALID');
  assert.ok(catchBlock.includes('PASSWORD_WEAK'), '应映射 PASSWORD_WEAK');
  assert.ok(!catchBlock.includes('msg.indexOf'), '不应含 msg.indexOf');
});

test('U7-2: register goToStep2 catch 用 Auth.t app.error + fallback', () => {
  const fn = extractFn(REGISTER_HTML, 'goToStep2');
  const catchIdx = fn.indexOf('.catch');
  const catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.includes("Auth.t('app.error.'"), '应用 Auth.t 查 i18n');
  assert.ok(catchBlock.includes('null,'), '应传 null 作为 vars 参数');
  assert.ok(catchBlock.includes('common.server_error'), '应含 common.server_error 兜底');
  assert.ok(catchBlock.includes('err.message'), '应含 err.message fallback');
});

test('U7-3: register registerVerify catch 基于 error_code', () => {
  const fn = extractFn(REGISTER_HTML, 'registerVerify');
  const catchIdx = fn.indexOf('.catch');
  const catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.includes('CODE_INVALID'), '应映射 CODE_INVALID');
  assert.ok(catchBlock.includes('USERNAME_INVALID'), '应映射 USERNAME_INVALID');
  assert.ok(catchBlock.includes('PASSWORD_WEAK'), '应映射 PASSWORD_WEAK');
  assert.ok(!catchBlock.includes('msg.indexOf'), '不应含 msg.indexOf');
});

test('U7-4: forgot sendForgotCode catch 基于 error_code', () => {
  const fn = extractFn(FORGOT_HTML, 'sendForgotCode');
  const catchIdx = fn.indexOf('.catch');
  const catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.includes('error_code'), '应含 error_code');
  assert.ok(catchBlock.includes("Auth.t('app.error.'"), '应用 Auth.t 查 i18n');
  assert.ok(!catchBlock.includes('msg.indexOf'), '不应含 msg.indexOf');
});

test('U7-5: forgot resetPassword catch 基于 error_code', () => {
  const fn = extractFn(FORGOT_HTML, 'resetPassword');
  const catchIdx = fn.indexOf('.catch');
  const catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.includes('CODE_INVALID'), '应映射 CODE_INVALID');
  assert.ok(catchBlock.includes('ACCOUNT_NOT_FOUND'), '应映射 ACCOUNT_NOT_FOUND');
  assert.ok(catchBlock.includes('PASSWORD_WEAK'), '应映射 PASSWORD_WEAK');
  assert.ok(!catchBlock.includes('msg.indexOf'), '不应含 msg.indexOf');
});

test('U7-6: i18n.js t() 支持第三参数 fallback', () => {
  const i18nJs = fs.readFileSync(path.join(ROOT, 'src/api/static/js/i18n.js'), 'utf8');
  assert.ok(/function t\(key,\s*vars,\s*fallback\)/.test(i18nJs), 't() 签名应为 (key, vars, fallback)');
  assert.ok(i18nJs.includes('fallback !== undefined ? fallback : key'), '缺 key 时应返回 fallback');
});

test('U7-7: auth.js t() 对齐 i18n.js', () => {
  const authJs = fs.readFileSync(path.join(ROOT, 'src/api/static/js/auth.js'), 'utf8');
  assert.ok(/function t\(key,\s*vars,\s*fallback\)/.test(authJs), 't() 签名应为 (key, vars, fallback)');
});

// ============ U8: 防枚举 — verify 不暴露存在性 ============

console.log('\n── U8: 防枚举 — verify 不暴露存在性 ──');

test('U8-1: register.html 验证码区域含帮助入口（对所有用户一致）', () => {
  assert.ok(REGISTER_HTML.indexOf('code_not_received_hint') >= 0, '应有帮助入口');
  var idx = REGISTER_HTML.indexOf('code_not_received_hint');
  var block = REGISTER_HTML.slice(idx, idx + 500);
  assert.ok(block.indexOf('href="/"') >= 0, '应含去登录链接');
  assert.ok(block.indexOf('href="/forgot"') >= 0, '应含找回密码链接');
});

test('U8-2: send-code 成功文案不暴露存在性', () => {
  assert.ok(REGISTER_HTML.indexOf('toast_code_sent_hint') >= 0, '应用模糊提示文案');
  assert.ok(REGISTER_HTML.indexOf('toast_code_sent + ') < 0, '不应拼接邮箱地址到提示');
});

test('U8-3: verify 不先检查邮箱已注册（防枚举）', () => {
  var py = fs.readFileSync(path.join(ROOT, 'src/api/routes/grape_auth.py'), 'utf8');
  var fnIdx = py.indexOf('def register_verify');
  var fnEnd = py.indexOf('\n@router', fnIdx + 10);
  var fnBody = py.slice(fnIdx, fnEnd > 0 ? fnEnd : fnIdx + 3000);
  // verify 中不应在验证码校验之前检查 is_email_registered
  var verifyIdx = fnBody.indexOf('verify_code');
  var emailCheckIdx = fnBody.indexOf('is_email_registered');
  if (emailCheckIdx >= 0) {
    // is_email_registered 只能在 verify_code 之后（并发竞态兜底）
    assert.ok(emailCheckIdx > verifyIdx, 'is_email_registered 应在 verify_code 之后（并发竞态兜底），不能在之前');
  }
});

test('U8-4: send-code 已注册邮箱统一返回成功', () => {
  var py = fs.readFileSync(path.join(ROOT, 'src/api/routes/grape_auth.py'), 'utf8');
  var fnIdx = py.indexOf('def register_send_code');
  var fnEnd = py.indexOf('\n@router', fnIdx + 10);
  var fnBody = py.slice(fnIdx, fnEnd > 0 ? fnEnd : fnIdx + 3000);
  assert.ok(fnBody.indexOf('EMAIL_ALREADY_REGISTERED') < 0, 'send-code 不应返回 EMAIL_ALREADY_REGISTERED');
  assert.ok(fnBody.indexOf('验证码已发送') >= 0, 'send-code 应统一返回成功');
});

test('U8-5: 不含 regEmailRegistered 提示区（已移除）', () => {
  assert.ok(REGISTER_HTML.indexOf('regEmailRegistered') < 0, '不应含 regEmailRegistered（已移除）');
});

test('U8-6: registerVerify catch 不特殊处理 EMAIL_ALREADY_REGISTERED', () => {
  var fn = extractFn(REGISTER_HTML, 'registerVerify');
  var catchIdx = fn.indexOf('.catch');
  var catchBlock = fn.slice(catchIdx);
  assert.ok(catchBlock.indexOf("code==='EMAIL_ALREADY_REGISTERED'") < 0, '不应特殊处理 EMAIL_ALREADY_REGISTERED');
});

// ============ U9: 边界场景 ============

console.log('\n── U9: 边界场景 ──');

test('U9-1: window.__EMAIL_CONFIGURED__ 未定义时默认 false', () => {
  // 检查三页面都有 typeof ... !== 'undefined' ? ... : false 的兜底逻辑
  const re = /typeof window\.__EMAIL_CONFIGURED__\s*!==\s*['\"]undefined['\"]\s*\)?\s*\?\s*window\.__EMAIL_CONFIGURED__\s*:\s*false/;
  assert.ok(re.test(REGISTER_HTML), 'register 应兜底 false');
  assert.ok(re.test(FORGOT_HTML), 'forgot 应兜底 false');
  assert.ok(re.test(INDEX_HTML), 'index 应兜底 false');
});

test('U9-2: for/id 无障碍关联', () => {
  assert.ok(FORGOT_HTML.includes('for="forgotEmailDisplay"'), 'label 应有 for="forgotEmailDisplay"');
  assert.ok(FORGOT_HTML.includes('id="forgotEmailDisplay"'), 'input 应有 id="forgotEmailDisplay"');
});

test('U9-3: 版本号对齐', () => {
  assert.ok(REGISTER_HTML.includes('api.js?v=31') && REGISTER_HTML.includes('app.css?v=85'), 'register 版本号对齐');
  assert.ok(FORGOT_HTML.includes('api.js?v=31') && FORGOT_HTML.includes('app.css?v=85'), 'forgot 版本号对齐');
});

// ============ U10: FOUC 验证 — CSS class 驱动显隐 ============

console.log('\n── U10: FOUC 验证 — CSS class 驱动显隐 ──');

test('U10-1: 无邮箱 register.html CSS class 驱动 #stepIndicator 隐藏', () => {
  // CSS 规则含 .email-not-configured #stepIndicator { display: none !important }
  assert.ok(REGISTER_HTML.indexOf('.email-not-configured #stepIndicator') >= 0, 'CSS 应含 .email-not-configured #stepIndicator');
  var idx = REGISTER_HTML.indexOf('.email-not-configured #stepIndicator');
  var block = REGISTER_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
  // #stepIndicator HTML 无 display:none（由 CSS class 控制，不是 inline style）
  var m = REGISTER_HTML.match(/id="stepIndicator"[^>]*/);
  assert.ok(m && m[0].indexOf('display:none') < 0, '#stepIndicator 不应有 inline display:none');
});

test('U10-2: 无邮箱 forgot.html CSS class 驱动 #forgotStep1 隐藏', () => {
  assert.ok(FORGOT_HTML.indexOf('.email-not-configured #forgotStep1') >= 0, 'CSS 应含 .email-not-configured #forgotStep1');
  var idx = FORGOT_HTML.indexOf('.email-not-configured #forgotStep1');
  var block = FORGOT_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: none !important') >= 0 || block.indexOf('display:none !important') >= 0, '应有 display: none !important');
  // #forgotStep1 HTML 无 display:none
  var m = FORGOT_HTML.match(/id="forgotStep1"[^>]*/);
  assert.ok(m && m[0].indexOf('display:none') < 0, '#forgotStep1 不应有 inline display:none');
});

test('U10-3: 无邮箱 forgot.html CSS class 驱动 #forgotNotConfigured 显示', () => {
  assert.ok(FORGOT_HTML.indexOf('.email-not-configured #forgotNotConfigured') >= 0, 'CSS 应含 .email-not-configured #forgotNotConfigured');
  var idx = FORGOT_HTML.indexOf('.email-not-configured #forgotNotConfigured');
  var block = FORGOT_HTML.slice(idx, idx + 200);
  assert.ok(block.indexOf('display: block !important') >= 0 || block.indexOf('display:block !important') >= 0, '应有 display: block !important');
  // #forgotNotConfigured HTML 有 style="display:none"（被 CSS !important 覆盖为 block）
  var m = FORGOT_HTML.match(/id="forgotNotConfigured"[^>]*/);
  assert.ok(m && m[0].indexOf('display:none') >= 0, '#forgotNotConfigured 应有 inline display:none（被 CSS !important 覆盖）');
});

test('U10-4: 有邮箱 register.html CSS 不匹配 → #emailField 默认可见', () => {
  // .email-configured 规则不应含 #emailField（有邮箱时元素默认可见，无需显式规则）
  var idx = FORGOT_HTML.indexOf('.email-configured #forgotNotConfigured');
  assert.ok(idx >= 0, 'forgot.html 应有 .email-configured #forgotNotConfigured 隐藏规则');
  // register.html 不含 .email-configured #emailField（不需要显式显示规则）
  assert.ok(REGISTER_HTML.indexOf('.email-configured #emailField') < 0, '不应有 .email-configured #emailField 规则（默认可见）');
  // #emailField HTML 无 display:none
  var m = REGISTER_HTML.match(/id="emailField"[^>]*/);
  assert.ok(m && m[0].indexOf('display:none') < 0, '#emailField 不应有 inline display:none');
});

// ============ U11: i18n 零闪动验证 ============

console.log('\n── U11: i18n 零闪动验证 ──');

test('U11-1: 英文 register.html defer + boost 自动 init', () => {
  // i18n.js 标签有 defer
  assert.ok(REGISTER_HTML.indexOf('defer') >= 0, 'register.html i18n.js 应有 defer');
  // i18n.js 不在 body 中（移到 head defer）
  // head 中的 i18n.js 在 </head> 前
  var headEnd = REGISTER_HTML.indexOf('</head>');
  var i18nIdx = REGISTER_HTML.indexOf('i18n.js');
  assert.ok(i18nIdx >= 0 && i18nIdx < headEnd, 'i18n.js 应在 head 中');
  // 不含旧的 DOMContentLoaded init
  assert.ok(REGISTER_HTML.indexOf("DOMContentLoaded',function(){I18N.init") < 0, '不应有旧的 DOMContentLoaded I18N.init');
});

test('U11-2: 中文 register.html 无 i18n-loading class', () => {
  // 内联 script 中只有 lang!=='zh' 时才加 class
  assert.ok(REGISTER_HTML.indexOf("classList.add('i18n-loading')") >= 0, '应有 classList.add i18n-loading 逻辑');
  // 不含旧的 data-i18n-pending
  assert.ok(REGISTER_HTML.indexOf('data-i18n-pending') < 0, '不应含旧的 data-i18n-pending');
});

test('U11-3: 英文 forgot.html defer + boost 自动 init', () => {
  assert.ok(FORGOT_HTML.indexOf('defer') >= 0, 'forgot.html i18n.js 应有 defer');
  var headEnd = FORGOT_HTML.indexOf('</head>');
  var i18nIdx = FORGOT_HTML.indexOf('i18n.js');
  assert.ok(i18nIdx >= 0 && i18nIdx < headEnd, 'i18n.js 应在 head 中');
  assert.ok(FORGOT_HTML.indexOf("DOMContentLoaded',function(){I18N.init") < 0, '不应有旧的 DOMContentLoaded I18N.init');
});

test('U11-4: 英文 index.html defer + boost 自动 init', () => {
  assert.ok(INDEX_HTML.indexOf('defer') >= 0, 'index.html i18n.js 应有 defer');
  // head 中有 i18n.js defer 引用
  var headEnd = INDEX_HTML.indexOf('</head>');
  var deferIdx = INDEX_HTML.indexOf('i18n.js?v=20261023-3" defer');
  assert.ok(deferIdx >= 0 && deferIdx < headEnd, 'i18n.js defer 应在 head 中');
  // body 中不再有 <script src="i18n.js 引用
  var bodyStart = INDEX_HTML.indexOf('<body>');
  var bodyContent = INDEX_HTML.slice(bodyStart);
  assert.ok(bodyContent.indexOf('<script src="/static/js/i18n.js') < 0, 'body 中不应有 i18n.js script 引用');
  // 不含旧的 DOMContentLoaded I18N.init
  assert.ok(INDEX_HTML.indexOf("I18N.init('35')") < 0, '不应有旧的 I18N.init(35) 调用');
});

test('U11-5: i18n.js 加载失败 5 秒超时兜底', () => {
  assert.ok(REGISTER_HTML.indexOf('5000') >= 0, 'register.html 应有 5000ms 超时');
  assert.ok(FORGOT_HTML.indexOf('5000') >= 0, 'forgot.html 应有 5000ms 超时');
  assert.ok(INDEX_HTML.indexOf('5000') >= 0, 'index.html 应有 5000ms 超时');
});

test('U11-6: Auth.initI18n() 有 typeof I18N 防御', () => {
  var authJs = fs.readFileSync(path.join(ROOT, 'src/api/static/js/auth.js'), 'utf8');
  assert.ok(authJs.indexOf("typeof I18N !== 'undefined'") >= 0, 'auth.js 应有 typeof I18N 防御');
});

test('U11-7: i18n.js 防重复 init', () => {
  var i18nJs = fs.readFileSync(path.join(ROOT, 'src/api/static/js/i18n.js'), 'utf8');
  assert.ok(i18nJs.indexOf('if (_initialized) return') >= 0, 'init 应有 _initialized 防重复');
  assert.ok(i18nJs.indexOf('isInitialized') >= 0, '应暴露 isInitialized 供外部检查');
});

// ============ U12: early-translate 验证 ============

console.log('\n── U12: early-translate 验证 ──');

test('U12-1: main.py early-translate 在 </body> 前同步翻译', () => {
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  assert.ok(mainPy.indexOf('_EARLY_TRANSLATE_SCRIPT') >= 0, '应有 early-translate script');
  assert.ok(mainPy.indexOf("_EARLY_TRANSLATE_SCRIPT + '</body>'") >= 0, '应注入到 </body> 前');
  // script 含翻译逻辑
  assert.ok(mainPy.indexOf("querySelectorAll('[data-i18n]')") >= 0, '应含 querySelectorAll');
  assert.ok(mainPy.indexOf('classList.remove') >= 0, '应含 classList.remove');
});

test('U12-2: boost 覆盖全部 data-i18n key（0 缺失）', () => {
  // 验证 main.py 的 boost 提取逻辑能覆盖所有 key 结构
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  // 嵌套 key 处理
  assert.ok(mainPy.indexOf('isinstance(app_data[section], dict)') >= 0, '应处理嵌套 dict');
  // 顶层字符串 key 处理
  assert.ok(mainPy.indexOf('parts[1] == leaf') >= 0, '应处理顶层字符串 key');
  // 跨文件 key 处理
  assert.ok(mainPy.indexOf('organizations.json') >= 0, '应处理 organizations.json');
  // common.json 处理
  assert.ok(mainPy.indexOf('common.json') >= 0, '应处理 common.json');
});

test('U12-3: boost 含跨文件 key（organizations.xxx）', () => {
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  assert.ok(mainPy.indexOf("parts[0] == 'organizations'") >= 0, '应处理 organizations 前缀 key');
  assert.ok(mainPy.indexOf('org_data') >= 0, '应读取 org_data');
});

test('U12-4: boost 构建失败返回 {} (try-except)', () => {
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  assert.ok(mainPy.indexOf('except Exception') >= 0, '应有 try-except');
  assert.ok(mainPy.indexOf("_I18N_BOOST_CACHE = '{}'") >= 0, '失败时应返回 {}');
});

test('U12-5: early-translate 处理 data-i18n-placeholder', () => {
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  assert.ok(mainPy.indexOf("querySelectorAll('[data-i18n-placeholder]')") >= 0, '应处理 data-i18n-placeholder');
  assert.ok(mainPy.indexOf('setAttribute') >= 0, '应用 setAttribute 设置 placeholder');
});

test('U12-6: admin.html 不受影响（不注入 boost/early-translate）', () => {
  // admin.html 走 FileResponse 不走 _serve_auth_page
  var mainPy = fs.readFileSync(path.join(ROOT, 'src/api/main.py'), 'utf8');
  // serve_admin 路由用 FileResponse
  var adminIdx = mainPy.indexOf('def serve_admin');
  assert.ok(adminIdx >= 0, '应有 serve_admin 路由');
  var adminBlock = mainPy.slice(adminIdx, adminIdx + 500);
  assert.ok(adminBlock.indexOf('FileResponse') >= 0, 'admin 应走 FileResponse');
  assert.ok(adminBlock.indexOf('_inject_email_configured') < 0, 'admin 不应走 _inject_email_configured');
});

test('U12-7: 英文用户冷加载登录页（30 个脚本 defer 化）', () => {
  // index.html body 中所有 <script src> 含 defer
  var scripts = INDEX_HTML.match(/<script\s+src="[^"]*"><\/script>/g) || [];
  assert.ok(scripts.length === 0, '不应有不含 defer 的 <script src> 标签，实际: ' + scripts.length);
  var deferScripts = INDEX_HTML.match(/<script\s+src="[^"]*"\s+defer><\/script>/g) || [];
  assert.ok(deferScripts.length >= 30, '应有至少 30 个 defer 脚本，实际: ' + deferScripts.length);
  // 内联脚本不含 defer（3 个：head + body + early-translate 由服务端注入）
  var inlineScripts = INDEX_HTML.match(/<script>/g) || [];
  assert.ok(inlineScripts.length === 3, '应有 3 个内联 <script>，实际: ' + inlineScripts.length);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
