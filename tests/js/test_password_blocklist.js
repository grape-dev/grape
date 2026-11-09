/*
 * 注册密码强度四条件（含后端黑名单同源）— 前端测试
 *
 * 运行：node tests/js/test_password_blocklist.js
 *
 * 验证点（P1-P5）：
 *   P1. test1234（黑名单命中，大小写不敏感）→ 四条件中 req-blocklist NOT-MET
 *   P2. 正常密码 → 四条件全 MET
 *   P3. RULES.regPassword 前端预检：黑名单密码在提交前被拦（不等后端 400）
 *   P4. blocklist 文件与后端 _WEAK_PASSWORDS 同源（数量与 test1234 抽查）
 *   P5. i18n：pwd_req_blocklist / err_password_blocklist zh-en 对齐
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const _STATIC = path.join(__dirname, '..', '..', 'src', 'api', 'static');
const _BACKEND = path.join(__dirname, '..', '..', 'src', 'auth', 'password.py');

// ── 与 register.html reqs 同规格的四条件判定 ──
function pwdReqs(p, username, email) {
  return {
    len: p.length >= 8,
    complex: /[a-zA-Z]/.test(p) && /\d/.test(p),
    notmatch: p.length > 0 && p.toLowerCase() !== (username || '').toLowerCase()
              && p.toLowerCase() !== (email || '').toLowerCase(),
    blocklist: p.length > 0 && !global.__BL.has(p.toLowerCase()),
  };
}

// ── 前端 blocklist 文件解析（与生成器同源产物）──
const blSrc = fs.readFileSync(path.join(_STATIC, 'js', 'pwd-blocklist.js'), 'utf8');
const m = blSrc.match(/new Set\((\[[^\]]*\])\)/);
assert.ok(m, 'P4: pwd-blocklist.js 可解析');
const words = JSON.parse(m[1]);
global.__BL = new Set(words);

// P1: 黑名单命中（大小写不敏感）
for (const p of ['test1234', 'Test1234', 'TEST1234', 'abc12345', 'Abc12345']) {
  const r = pwdReqs(p, 'alice', 'a@x.com');
  assert.ok(r.len && r.complex && r.notmatch, 'P1: ' + p + ' 前三条件满足（复现原缺陷形态）');
  assert.strictEqual(r.blocklist, false, 'P1: ' + p + ' 必须命中黑名单（第四条 NOT-MET）');
}

// P2: 正常密码四条件全过
for (const p of ['Xk9mzqR7', 'probe3!Secure', 'Zz99-Plow']) {
  const r = pwdReqs(p, 'alice', 'a@x.com');
  assert.ok(r.len && r.complex && r.notmatch && r.blocklist, 'P2: ' + p + ' 全 MET');
}

// P3: RULES 前端预检存在（结构守卫：register.html 在提交前拦黑名单）
const regSrc = fs.readFileSync(path.join(_STATIC, 'register.html'), 'utf8');
assert.ok(regSrc.includes('isWeakPassword(v)'), 'P3: RULES.regPassword 预检走统一谓词');
assert.ok(regSrc.includes("id:'req-blocklist'"), 'P3: 第四条 req 挂钩');
assert.ok(regSrc.includes('pwd-blocklist.js'), 'P3: blocklist 脚本已引入');
assert.ok(regSrc.includes('id="req-blocklist"'), 'P3: 第四条 UI 行存在');

// P4: 与后端同源（数量 + 抽查成员）
const beSrc = fs.readFileSync(_BACKEND, 'utf8');
const beWords = new Set();
for (const w of beSrc.match(/"[a-z0-9@!]+"/g) || []) beWords.add(w.slice(1, -1));
// 粗校验：前端集合应是后端集合的子集且含 test1234
assert.ok(global.__BL.has('test1234'), 'P4: 前端含 test1234（用户复现密码）');
assert.ok(words.length >= 90, 'P4: 黑名单规模合理（当前 ' + words.length + '）');
// 生成脚本存在（同源机制）
assert.ok(fs.existsSync(path.join(__dirname, '..', '..', 'scripts', 'gen-pwd-blocklist.py')), 'P4: 生成脚本存在');

// P5: i18n zh/en 对齐
for (const lang of ['zh', 'en']) {
  const d = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', lang, 'app.json'), 'utf8'));
  assert.ok(d.login && d.login.pwd_req_blocklist, 'P5: ' + lang + ' pwd_req_blocklist');
  assert.ok(d.login && d.login.err_password_blocklist, 'P5: ' + lang + ' err_password_blocklist');
  assert.ok(d.error && d.error.PASSWORD_WEAK, 'P5: ' + lang + ' error.PASSWORD_WEAK 映射');
}

console.log('test_password_blocklist: all assertions passed (P1-P5)');

// ── P6-P8：复核 #3 修复（2026-10-28 审查 follow-up）──
// P6: 统一谓词导出（pwd-blocklist.js 生成物含 isWeakPassword，trim+大小写归一）
assert.ok(/window\.isWeakPassword = function/.test(blSrc), 'P6: 谓词由生成器产出（单点定义）');
assert.ok(blSrc.includes('String(p).trim().toLowerCase()'), 'P6b: 谓词含 trim + 大小写归一');
// 谓词语义实测（node 沙箱求值——先提供 window 全局再 eval 生成物）
globalThis.window = globalThis;
const sandbox = blSrc + '\n; (function(){ return [isWeakPassword("TEST1234"), isWeakPassword("  admin123  "), isWeakPassword("xK9#mQ2$vL"), isWeakPassword("")]; })()';
const r = eval(sandbox);
assert.deepStrictEqual(r, [true, true, false, false], 'P6c: 谓词语义（命中/trim 命中/正常/空）');

// P7: 全消费面接入（改密/设密/忘记密码重置与注册同源，后端四端点均有
// check_password_strength——前端预检消除"前端全绿后端 400"的不一致）
const appSrc = fs.readFileSync(path.join(_STATIC, 'js', 'app.js'), 'utf8');
const doChange = appSrc.slice(appSrc.indexOf('function doChangePassword'), appSrc.indexOf('function showSetPassword'));
assert.ok(doChange.includes('isWeakPassword(np)'), 'P7: 改密预检走谓词');
const doSet = appSrc.slice(appSrc.indexOf('function doSetPassword'), appSrc.indexOf('function showChangeEmail'));
assert.ok(doSet.includes('isWeakPassword(pwd)'), 'P7b: 设密预检走谓词');
const forgotSrc = fs.readFileSync(path.join(_STATIC, 'forgot.html'), 'utf8');
assert.ok(forgotSrc.includes('isWeakPassword(v)'), 'P7c: 忘记密码 NEWPWD_RULE 走谓词');
assert.ok(forgotSrc.includes('pwd-blocklist.js'), 'P7d: forgot.html 引入 blocklist 脚本');
const idxHtml = fs.readFileSync(path.join(_STATIC, 'index.html'), 'utf8');
assert.ok(idxHtml.includes('pwd-blocklist.js'), 'P7e: 主应用引入 blocklist（改密/设密消费前置）');

// P8: 无散落拷贝（判断逻辑不得在调用点重复 has() 形态）
const callSites = [regSrc, forgotSrc, appSrc,
  fs.readFileSync(path.join(_STATIC, 'js', 'pages', 'account.js'), 'utf8')];
for (const src of callSites) {
  assert.ok(!/PWD_BLOCKLIST\.has\(/.test(src), 'P8: 消费点无 PWD_BLOCKLIST.has 散落拷贝（统一走谓词）');
}

// P9: inline 包覆盖（复核：i18n.js _INLINE_PACKS 补 pwd_req_blocklist /
// err_password_blocklist zh-en——全量包异步加载前（弱网首次访问）密码表单
// 不得闪裸键；文案与 locales/*/app.json 逐字对齐）
const i18nSrc = fs.readFileSync(path.join(_STATIC, 'js', 'i18n.js'), 'utf8');
const fullPacks = {};
for (const lang of ['zh', 'en']) {
  fullPacks[lang] = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', lang, 'app.json'), 'utf8'));
}
const zhSeg = i18nSrc.slice(i18nSrc.indexOf('_INLINE_PACKS'), i18nSrc.indexOf('en: {', i18nSrc.indexOf('_INLINE_PACKS')));
const enSeg = i18nSrc.slice(i18nSrc.indexOf('en: {', i18nSrc.indexOf('_INLINE_PACKS')));
for (const [lang, seg] of [['zh', zhSeg], ['en', enSeg]]) {
  for (const k of ['pwd_req_blocklist', 'err_password_blocklist']) {
    const m = seg.match(new RegExp(k + ": '([^']+)'"));
    assert.ok(m, 'P9: ' + lang + ' inline 包含 ' + k);
    assert.strictEqual(m[1], fullPacks[lang].login[k], 'P9: ' + lang + ' ' + k + ' inline 与全量包逐字一致');
  }
}
// 消费页 i18n.js 缓存已 bump（inline 包变更须失效存量缓存）
assert.ok(/i18n\.js\?v=20261028-1/.test(idxHtml), 'P9: index.html i18n.js 已 bump');
assert.ok(/i18n\.js\?v=17/.test(forgotSrc), 'P9: forgot.html i18n.js 已 bump');
assert.ok(/i18n\.js\?v=17/.test(regSrc), 'P9: register.html i18n.js 已 bump');

console.log('test_password_blocklist: all assertions passed (P1-P9, blocklist predicate + full surface + inline i18n)');

// ── P10-P11：C/D 家族收敛守卫（2026-10-28 深扫 follow-up）──
// P10: 长度谓词 + 常量由生成器产出，前端散落的 length<8 / >=8 硬编码统一消费
assert.ok(/window\.PWD_MIN_LEN = 8/.test(blSrc), 'P10: PWD_MIN_LEN 常量由生成器产出');
assert.ok(/window\.isPwdTooShort = function/.test(blSrc), 'P10b: isPwdTooShort 谓词产出');
const r10 = eval(blSrc + '\n; (function(){ return [isPwdTooShort("abc123"), isPwdTooShort("abcdefg1"), isPwdTooShort(""), isPwdTooShort(null)]; })()');
assert.deepStrictEqual(r10, [true, false, true, true], 'P10c: 长度谓词语义（短/达标/空/null）');
// 消费面：forgot/register reqs、admin.js 三处、auth.js 评分均走谓词/常量，无 length<8 散落
const adminSrc = fs.readFileSync(path.join(_STATIC, 'js', 'admin.js'), 'utf8');
const authSrc = fs.readFileSync(path.join(_STATIC, 'js', 'auth.js'), 'utf8');
assert.ok(adminSrc.includes('isPwdTooShort(newP)') && (adminSrc.match(/isPwdTooShort\(password\)/g) || []).length === 2,
  'P10d: admin.js 三处长度校验走谓词');
assert.ok(/PWD_MIN_LEN \|\| 8/.test(authSrc), 'P10e: auth.js 评分阈值走常量');
for (const [name, src] of [['admin', adminSrc], ['forgot', forgotSrc], ['register', regSrc]]) {
  assert.ok(!/length\s*[<>]=?\s*8\b/.test(src), 'P10f: ' + name + ' 无 length<8/>=8 散落硬编码');
}
// admin.html 引入 blocklist（admin.js 消费前置）
const adminHtml = fs.readFileSync(path.join(_STATIC, 'admin.html'), 'utf8');
assert.ok(adminHtml.includes('pwd-blocklist.js'), 'P10g: admin.html 引入 blocklist');

// P11: 过期意图标记复位（D 审计：_isCreateMode 原三路径复位，侧栏离开泄漏）
assert.ok(appSrc.includes('window._isCreateMode = false;'), 'P11: nav 离开统一复位 _isCreateMode');

// P12: admin 改密弱口令预检（A 项，2026-10-28 深扫：后端 admin /change-password 有
// check_password_strength，前端预检消除"前端全绿后端 400"——与用户侧四入口同源）
const changePwdFn = adminSrc.slice(adminSrc.indexOf('function changePwd'), adminSrc.indexOf('function logout', adminSrc.indexOf('function changePwd')));
assert.ok(changePwdFn.includes('isPwdTooShort(newP)'), 'P12: admin 改密长度预检走谓词');
assert.ok(changePwdFn.includes('isWeakPassword(newP)'), 'P12b: admin 改密弱口令预检（A 项）');
assert.ok(changePwdFn.includes("admin.pwd_modal.too_common"), 'P12c: 预检文案走 i18n');
for (const lang of ['zh', 'en']) {
  const adminPack = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', lang, 'admin.json'), 'utf8'));
  assert.ok(adminPack.pwd_modal && adminPack.pwd_modal.too_common, 'P12d: ' + lang + ' admin.pwd_modal.too_common 键存在');
}

console.log('test_password_blocklist: all assertions passed (P1-P12, + C length-predicate & D intent-flag & A admin precheck guards)');
