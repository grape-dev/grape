/*
 * 令牌创建直达链接（手动绑定表单）— 前端测试
 *
 * 运行：node tests/js/test_token_create_deeplink.js
 *
 * 验证点（T1-T5）：
 *   T1. GitHub → 直达 https://github.com/settings/tokens/new?scopes=repo（预选 repo 权限）
 *   T2. GitCode → 直达 https://gitcode.com/setting/token-classic/create
 *      （官方帮助文档路径：个人设置→访问令牌(经典)→新建；浏览器实测为真实页面）
 *   T3. 帮助文案保持纯文本（单一直达入口：令牌创建链接只在通栏按钮出现一次，
 *      帮助步骤第 1 步与按钮同目标属重复入口，已移除链接化）
 *   T4. 表单顶部有"前往令牌创建页面"按钮，链接域名与 provider 匹配
 *   T5. i18n 键 zh/en 对齐；文案第一步格式可被正则命中
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const _STATIC = path.join(__dirname, '..', '..', 'src', 'api', 'static');
const _zh = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', 'zh', 'app.json'), 'utf8'));
const _en = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', 'en', 'app.json'), 'utf8'));

// ── 与 account.js _accountManualBindForm 同规格提取（i18n 版）──
function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function buildForm(provider, lang) {
  const L = lang === 'zh' ? _zh : _en;
  const t = (k) => L.account[k.replace('app.account.', '')];
  const tokenCreateUrl = provider === 'github'
    ? 'https://github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps'
    : 'https://gitcode.com/setting/token-classic/create';
  const tokenCreateLabel = provider === 'github' ? 'github.com' : 'gitcode.com';
  let helpSteps = t(provider + '_token_help');
  const createLinkBtn = '<a href="' + tokenCreateUrl + '" target="_blank" rel="noopener noreferrer" class="bt" '
    + 'style="display:block;text-align:center;text-decoration:none;margin:0 0 12px;color:var(--a);border:1px solid var(--a)">'
    + t('open_token_page') + ' ' + _esc(tokenCreateLabel) + '</a>';
  return { url: tokenCreateUrl, label: tokenCreateLabel, helpSteps, createLinkBtn };
}

// T1/T2: 直达 URL 正确
const gh = buildForm('github', 'zh');
assert.strictEqual(gh.url, 'https://github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps', 'T1: github deeplink with scopes');
const gc = buildForm('gitcode', 'zh');
assert.strictEqual(gc.url, 'https://gitcode.com/setting/token-classic/create', 'T2: gitcode deeplink');

// T3: 帮助文案保持纯文本（单一直达入口，链接只在通栏按钮出现）
for (const [p, f] of [['github', gh], ['gitcode', gc]]) {
  assert.ok(!f.helpSteps.includes('<a '), 'T3: help steps contain no link (' + p + ')');
  assert.ok(/^1\.\s*[^<]+/.test(f.helpSteps), 'T3: step1 remains plain text (' + p + ')');
}

// T4: 按钮存在、域名匹配、URL 不含用户可控注入（_esc 处理 label）、无重复 ↗ 符号
for (const [p, f] of [['github', gh], ['gitcode', gc]]) {
  assert.ok(f.createLinkBtn.includes(f.url), 'T4: button href is deeplink (' + p + ')');
  assert.ok(f.createLinkBtn.includes(_esc(f.label)), 'T4: button shows domain (' + p + ')');
  assert.ok(!f.label.includes('"') && !f.label.includes('<'), 'T4: label is static trusted text');
  assert.ok(!f.createLinkBtn.includes('↗</a>'), 'T4: no duplicated arrow glyph (' + p + ')');
}

// T5: i18n 键 zh/en 对齐 + 第一步文案可被正则命中（4 组文案全验）
assert.ok(_zh.account.open_token_page && _en.account.open_token_page, 'T5: open_token_page exists both langs');
for (const lang of ['zh', 'en']) {
  const L = lang === 'zh' ? _zh : _en;
  for (const p of ['github', 'gitcode']) {
    const step1 = L.account[p + '_token_help'].split('<br>')[0];
    assert.ok(/^1\.\s*[^<]+/.test(step1), 'T5: step1 pattern matchable (' + lang + '/' + p + '): ' + step1);
  }
}

// 结构守卫：account.js 实际包含直达 URL 与注入逻辑
const accSrc = fs.readFileSync(path.join(_STATIC, 'js', 'pages', 'account.js'), 'utf8');
assert.ok(accSrc.includes('https://github.com/settings/tokens/new?scopes=repo'), 'guard: github deeplink in account.js');
assert.ok(accSrc.includes('https://gitcode.com/setting/token-classic/create'), 'guard: gitcode deeplink in account.js');
assert.ok(accSrc.includes("I18N.t('app.account.open_token_page')"), 'guard: button uses i18n');

console.log('test_token_create_deeplink: all assertions passed (T1-T5)');
