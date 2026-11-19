/*
 * Issue #3469 前端（G / P / 验收 11）— 静态断言测试
 *
 * 运行：node tests/js/test_issue_3469_change_email.js
 *
 * 覆盖：
 *   G   改绑二次认证：send-code 返回的 require_factor 决定渲染
 *       当前密码框（password）/ 原邮箱验证码框（old_email_code）/ 不渲染（none，
 *       无密码 + 原邮箱不可投递的账号靠这条自救，不得堵死改绑）；
 *       提交时把对应字段（password / old_email_code）放进请求体。
 *   P   改绑链 400 按 error_code 出文案（EMAIL_FORMAT / EMAIL_SAME / EMAIL_TAKEN /
 *       CODE_INVALID / CODE_EXPIRED / NEED_SECOND_FACTOR / PASSWORD_INVALID），
 *       不再把四种失败原因压成一句 e.message。
 *   验收 11  canVerifyByEmail 必须排除不可投递邮箱（profile.email_deliverable）。
 *   另外校 zh/en 两份 locale 的新增 key 齐备且 key 集合一致。
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

console.log('Issue #3469 改绑二次认证 / error_code 文案 — 静态断言测试\n');

const APP_JS = read('src/api/frontend/shell/js/app.js');
const ACCOUNT_VUE = read('src/api/frontend/app/src/views/AccountView.vue');
const ZH = read('src/api/frontend/shell/locales/zh/app.json');
const EN = read('src/api/frontend/shell/locales/en/app.json');

// ── 区域切片：避免断言命中文档/其他函数的同名关键字 ──
function slice(src, startMark, endMark) {
  const i = src.indexOf(startMark);
  assert.ok(i >= 0, '找不到起始锚点: ' + startMark);
  const j = src.indexOf(endMark, i + startMark.length);
  assert.ok(j > i, '找不到结束锚点: ' + endMark);
  return src.slice(i, j);
}

// shell 侧：从 CE_ERR_KEY 定义到绑定弹层注释（含 changeEmailErrMsg/_ceRenderFactor/
// showChangeEmail/sendChangeEmailCode/doChangeEmail 全部改绑代码）
const SHELL_REGION = slice(APP_JS, 'var CE_ERR_KEY = {', '// ══ 绑定弹层');
// Vue 侧：从 CE_ERR_KEY 定义到注销账号注释
const VUE_REGION = slice(ACCOUNT_VUE, 'const CE_ERR_KEY = {', '// ── 注销账号 ──');

const KNOWN_CODES = [
  'EMAIL_FORMAT',
  'EMAIL_SAME',
  'EMAIL_TAKEN',
  'CODE_INVALID',
  'CODE_EXPIRED',
  'NEED_SECOND_FACTOR',
  'PASSWORD_INVALID',
];

// ══════════════════ shell（src/api/frontend/shell/js/app.js）══════════════════

test('shell: CE_ERR_KEY 覆盖改绑链全部已知 error_code', () => {
  assert.ok(/var CE_ERR_KEY\s*=\s*\{/.test(SHELL_REGION), '缺少 CE_ERR_KEY 映射表');
  KNOWN_CODES.forEach((code) => {
    assert.ok(SHELL_REGION.indexOf(code + ':') >= 0, 'CE_ERR_KEY 缺 ' + code);
  });
});

test('shell: changeEmailErrMsg 按 e.error_code 取 I18N 文案，未知码回落 errMsg', () => {
  assert.ok(/function changeEmailErrMsg\(e\)/.test(SHELL_REGION), '缺少 changeEmailErrMsg');
  assert.ok(
    SHELL_REGION.indexOf("var code = (e && e.error_code) || ''") >= 0,
    'changeEmailErrMsg 没读 e.error_code'
  );
  assert.ok(
    SHELL_REGION.indexOf("var full = 'app.login.' + key") >= 0 &&
      SHELL_REGION.indexOf('I18N.t(full)') >= 0,
    'changeEmailErrMsg 没走 app.login.* I18N'
  );
  assert.ok(
    /return errMsg\(e\) \|\| I18N\.t\('app\.account\.error_failed'\)/.test(SHELL_REGION),
    '未知 error_code 未回落 errMsg'
  );
});

test('shell: send-code / change-email 的 catch 都改用 changeEmailErrMsg', () => {
  const hits = SHELL_REGION.split('showToast(changeEmailErrMsg(e))').length - 1;
  assert.strictEqual(hits, 2, 'send-code 与 change-email 两处 catch 应各调一次，实际 ' + hits);
  assert.ok(
    !/catch\(function\(e\) \{ showToast\(errMsg\(e\) \|\| I18N\.t\('app\.account\.error_failed'\)\); \}\);\s*\n\s*\}\s*\n\s*function doChangeEmail/.test(
      SHELL_REGION
    ),
    'send-code 的 catch 仍用旧的 errMsg 直通'
  );
});

test('shell: _ceRenderFactor 按 require_factor 三档渲染', () => {
  assert.ok(/function _ceRenderFactor\(factor\)/.test(SHELL_REGION), '缺少 _ceRenderFactor');
  assert.ok(
    /if \(factor === 'password'\)/.test(SHELL_REGION),
    "缺 password 档分支"
  );
  assert.ok(
    /else if \(factor === 'old_email_code'\)/.test(SHELL_REGION),
    '缺 old_email_code 档分支'
  );
  assert.ok(
    SHELL_REGION.indexOf('id="ceCurPwd"') >= 0,
    'password 档没渲染当前密码输入框（id=ceCurPwd）'
  );
  assert.ok(
    SHELL_REGION.indexOf('id="ceOldCode"') >= 0,
    'old_email_code 档没渲染原邮箱验证码输入框（id=ceOldCode）'
  );
  // none / 未知档必须渲染为空 —— 不得因「无可填因子」堵死改绑
  assert.ok(
    /\} else \{\s*\n\s*host\.innerHTML = ''/.test(SHELL_REGION),
    "none 档未渲染为空（会堵死不可投递邮箱账号的改绑自救）"
  );
  assert.ok(
    SHELL_REGION.indexOf("I18N.t('app.login.current_password')") >= 0,
    '密码框用的是硬编码中文而不是 I18N'
  );
  assert.ok(
    SHELL_REGION.indexOf("I18N.t('app.login.old_email_code')") >= 0,
    '原邮箱码框用的是硬编码中文而不是 I18N'
  );
});

test('shell: 弹层 step2 有 ceFactor 挂载点', () => {
  assert.ok(
    SHELL_REGION.indexOf('id="ceFactor"') >= 0,
    'showChangeEmail 的 step2 缺 #ceFactor 挂载点'
  );
});

test('shell: sendChangeEmailCode 把 require_factor 交给 _ceRenderFactor', () => {
  assert.ok(
    /\.then\(function\(d\)\s*\{[\s\S]*_ceRenderFactor\(d && d\.require_factor\)/.test(SHELL_REGION),
    'send-code 成功后未按 require_factor 渲染'
  );
});

test('shell: doChangeEmail 按渲染结果拼 password / old_email_code', () => {
  assert.ok(
    SHELL_REGION.indexOf("var body = {new_email: email, code: code}") >= 0,
    'doChangeEmail 未构造 body'
  );
  assert.ok(
    /if \(pwdEl\) \{[\s\S]*body\.password = curPwd/.test(SHELL_REGION),
    'doChangeEmail 未把 password 放进 body'
  );
  assert.ok(
    /if \(oldCodeEl\) \{[\s\S]*body\.old_email_code = oldCode/.test(SHELL_REGION),
    'doChangeEmail 未把 old_email_code 放进 body'
  );
  assert.ok(
    SHELL_REGION.indexOf("document.getElementById('ceCurPwd')") >= 0 &&
      SHELL_REGION.indexOf("document.getElementById('ceOldCode')") >= 0,
    'doChangeEmail 未探测两个因子框'
  );
});

test('shell: NEED_SECOND_FACTOR 回包会补渲染缺的因子框', () => {
  assert.ok(
    /e\.error_code === 'NEED_SECOND_FACTOR'/.test(SHELL_REGION),
    '未处理 NEED_SECOND_FACTOR'
  );
  assert.ok(
    SHELL_REGION.indexOf('e.detail && e.detail.require_factor') >= 0,
    '未从 e.detail.require_factor 取因子档位'
  );
});

// ══════════════════ Vue（AccountView.vue）══════════════════

test('vue: CE_ERR_KEY 覆盖改绑链全部已知 error_code', () => {
  assert.ok(/const CE_ERR_KEY = \{/.test(VUE_REGION), '缺少 CE_ERR_KEY 映射表');
  KNOWN_CODES.forEach((code) => {
    assert.ok(VUE_REGION.indexOf(code + ':') >= 0, 'CE_ERR_KEY 缺 ' + code);
  });
});

test('vue: changeEmailErrText 按 error_code（含 detail.error_code）出文案', () => {
  assert.ok(/function changeEmailErrText\(e\)/.test(VUE_REGION), '缺少 changeEmailErrText');
  assert.ok(VUE_REGION.indexOf('e.error_code || (e.detail && e.detail.error_code)') >= 0, '未读 error_code/detail.error_code');
  assert.ok(VUE_REGION.indexOf("'app.account.' + key") >= 0, '未走 app.account.* I18N');
});

test('vue: 改绑弹层 catch 不再把原因压成一句话', () => {
  const hits = VUE_REGION.split('G.app.showToast(changeEmailErrText(e))').length - 1;
  assert.strictEqual(hits, 2, 'send-code 与 change-email 两处 catch 应各调一次，实际 ' + hits);
  assert.ok(
    VUE_REGION.indexOf("G.app.showToast(e.message || '操作失败')") < 0,
    "改绑弹层仍有 e.message || '操作失败' 兜底"
  );
});

test('vue: renderChangeEmailFactor 按 require_factor 三档渲染', () => {
  assert.ok(/function renderChangeEmailFactor\(root, factor\)/.test(VUE_REGION), '缺少 renderChangeEmailFactor');
  assert.ok(/factor === 'password'/.test(VUE_REGION), '缺 password 档分支');
  assert.ok(/factor === 'old_email_code'/.test(VUE_REGION), '缺 old_email_code 档分支');
  assert.ok(VUE_REGION.indexOf('id="ceCurPwd"') >= 0, 'password 档没渲染当前密码框');
  assert.ok(VUE_REGION.indexOf('id="ceOldCode"') >= 0, 'old_email_code 档没渲染原邮箱验证码框');
  assert.ok(/\} else \{\s*\n\s*host\.innerHTML = ''/.test(VUE_REGION), 'none 档未渲染为空');
  assert.ok(VUE_REGION.indexOf("esc(t('current_password'))") >= 0, '密码框标签未走 t()');
  assert.ok(VUE_REGION.indexOf("esc(t('old_email_code'))") >= 0, '原邮箱码框标签未走 t()');
});

test('vue: 弹层 step2 有 ceFactor 挂载点 + send-code 后渲染', () => {
  assert.ok(VUE_REGION.indexOf("'<div id=\"ceFactor\"></div>'") >= 0, 'ceFactor 挂载点缺失');
  assert.ok(
    /renderChangeEmailFactor\(root, d && d\.require_factor\)/.test(VUE_REGION),
    'send-code 成功后未按 require_factor 渲染'
  );
});

test('vue: 提交时按渲染结果带 password / old_email_code', () => {
  assert.ok(/const body = \{ new_email: email, code: code \}/.test(VUE_REGION), '未构造 body');
  assert.ok(/body\.password = curPwd/.test(VUE_REGION), 'body 未带 password');
  assert.ok(/body\.old_email_code = oldCode/.test(VUE_REGION), 'body 未带 old_email_code');
  assert.ok(
    VUE_REGION.indexOf("root.querySelector('#ceCurPwd')") >= 0 &&
      VUE_REGION.indexOf("root.querySelector('#ceOldCode')") >= 0,
    '提交时未探测两个因子框'
  );
});

test('vue: 验收 11 canVerifyByEmail 排除不可投递邮箱', () => {
  assert.ok(
    /const canVerifyByEmail = hasEmail && !!profile\.value\.email_deliverable && emailConfigured\.value !== false/.test(
      ACCOUNT_VUE
    ),
    'canVerifyByEmail 未接 profile.email_deliverable'
  );
  assert.ok(
    ACCOUNT_VUE.indexOf('email_deliverable') >= 0,
    '全文没有 email_deliverable 判据'
  );
});

test('vue: DEFAULTS 兜底含新增标签与错误文案', () => {
  const need = [
    'current_password',
    'old_email_code',
    'err_email_format',
    'err_email_same',
    'err_email_taken',
    'err_code_invalid',
    'err_code_expired',
    'err_need_second_factor',
    'err_password_invalid',
  ];
  need.forEach((k) => {
    assert.ok(new RegExp('\\n\\s+' + k + ': ').test(ACCOUNT_VUE), 'DEFAULTS 缺 ' + k);
  });
});

// ══════════════════ locales（zh / en app.json）══════════════════

const zh = JSON.parse(ZH);
const en = JSON.parse(EN);

const NEW_KEYS = [
  'err_email_format',
  'err_email_same',
  'err_email_taken',
  'err_code_invalid',
  'err_code_expired',
  'err_need_second_factor',
  'err_password_invalid',
  'current_password',
  'old_email_code',
];

test('locale: zh/en app.login 含全部新增 key', () => {
  NEW_KEYS.forEach((k) => {
    assert.ok(k in zh.login, 'zh app.login 缺 ' + k);
    assert.ok(k in en.login, 'en app.login 缺 ' + k);
  });
});

test('locale: zh/en app.account 含全部新增 key（Vue 侧 t() 命名空间）', () => {
  NEW_KEYS.forEach((k) => {
    assert.ok(k in zh.account, 'zh app.account 缺 ' + k);
    assert.ok(k in en.account, 'en app.account 缺 ' + k);
  });
});

test('locale: zh/en 的 login / account key 集合一致（无单边遗漏）', () => {
  const diff = (a, b) => Object.keys(a).filter((k) => !(k in b));
  ['login', 'account'].forEach((ns) => {
    const zhOnly = diff(zh[ns], en[ns]);
    const enOnly = diff(en[ns], zh[ns]);
    assert.deepStrictEqual(zhOnly, [], ns + ' 仅 zh 有: ' + zhOnly.join(','));
    assert.deepStrictEqual(enOnly, [], ns + ' 仅 en 有: ' + enOnly.join(','));
  });
});

test('locale: 新增文案非空且中英不互抄', () => {
  NEW_KEYS.forEach((k) => {
    assert.ok(String(zh.login[k]).trim().length > 0, 'zh ' + k + ' 为空');
    assert.ok(String(en.login[k]).trim().length > 0, 'en ' + k + ' 为空');
    assert.notStrictEqual(zh.login[k], en.login[k], k + ' 中英文案相同，疑似漏翻');
  });
});

// ══════════════════ 收口 ══════════════════

console.log('\n通过 ' + passed + ' / 失败 ' + failed);
if (failed > 0) process.exit(1);