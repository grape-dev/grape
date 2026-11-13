/**
 * 令牌创建直达链接（手动绑定表单）— Vue 版前端测试
 *
 * 合入 arch-weekly-11 时移植：原测试对着 static/js/pages/account.js 跑，
 * 该文件已随 Vue 迁移删除 → 改为对 AccountView.vue + /auth/providers 做源码断言。
 *
 * 覆盖 T1-T5：
 *   T1. GitHub → settings/tokens/new?scopes=repo（预选仓库权限）
 *   T2. GitCode → setting/token-classic/create（官方实测页面）
 *   T3. 其余平台取 /auth/providers 的 pat_create_url（平台卡），没有就不渲染按钮
 *   T4. 按钮为通栏链接，target=_blank + rel=noopener noreferrer，文案带域名
 *   T5. 帮助文案按平台查表，未命中走 pat_token_help_generic 并带 host（修 D102 错站）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'frontend', 'app', 'src', 'views', 'AccountView.vue'),
  'utf8'
);
const CATALOG = fs.readFileSync(
  path.join(ROOT, 'src', 'domains', 'mgmt', 'auth', 'providers.py'),
  'utf8'
);
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src', 'api', 'frontend', 'shell', 'locales', 'zh', 'app.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src', 'api', 'frontend', 'shell', 'locales', 'en', 'app.json'), 'utf8'));

// app.json 的顶层就是模块内容（i18n 里 pack['app'] = 该文件），account 在顶层
function accountDict(d) {
  return (d && d.account) || {};
}

test('T1 GitHub 直达链接带 scopes=repo 预选', () => {
  assert.match(VIEW, /github: 'https:\/\/github\.com\/settings\/tokens\/new\?scopes=repo&description=Grape%20DevOps'/,
    'GitHub PAT 创建链接缺失或丢参数');
});

test('T2 GitCode 直达 classic 令牌新建页', () => {
  assert.match(VIEW, /gitcode: 'https:\/\/gitcode\.com\/setting\/token-classic\/create'/,
    'GitCode PAT 创建链接缺失');
});

test('T3 其他平台走平台卡 pat_create_url，取不到就不渲染', () => {
  assert.match(VIEW, /if \(providerInfo && providerInfo\.pat_create_url\) return providerInfo\.pat_create_url/,
    '未使用 /auth/providers 的 pat_create_url');
  assert.match(VIEW, /return ''\s*\n\}/, 'patCreateUrl 缺少空串兜底');
  assert.match(VIEW, /if \(createUrl\) \{/, '按钮未按 createUrl 存在性渲染');
  // 后端 /auth/providers 必须带出该字段
  assert.match(CATALOG, /pat_create_url/, '/auth/providers 未返回 pat_create_url');
  assert.match(CATALOG, /get_platform_card/, '/auth/providers 未从平台卡取 pat_create_url');
});

test('T4 按钮为通栏新窗口链接，文案带域名', () => {
  assert.match(VIEW, /target="_blank" rel="noopener noreferrer" class="bt"/, '按钮缺少新窗口/安全属性');
  assert.match(VIEW, /createUrl\.split\('\/'\)\[2\]/, '按钮文案没有取域名');
  assert.match(VIEW, /t\('open_token_page'\)/, '按钮文案未走 i18n');
});

test('T5 帮助文案按平台查表 + 通用兜底带 host（D102 错站修复）', () => {
  assert.match(VIEW, /PAT_HELP_KEYS = \{/, '缺少帮助文案查表');
  for (const k of ['github_token_help', 'gitcode_token_help', 'gitee_token_help']) {
    assert.ok(VIEW.includes(k), `帮助文案表缺少 ${k}`);
  }
  assert.match(VIEW, /pat_token_help_generic/, '缺少通用帮助文案兜底');
  assert.match(VIEW, /t\(helpKey, \{ host: createHost \|\| provider \}\)/, '通用文案没有传入 host');
});

test('T5b i18n 键 zh/en 对齐', () => {
  const zh = accountDict(ZH), en = accountDict(EN);
  for (const k of ['open_token_page', 'gitee_token_help', 'pat_token_help_generic', 'github_token_help', 'gitcode_token_help']) {
    assert.ok(zh[k], `zh 缺少 ${k}`);
    assert.ok(en[k], `en 缺少 ${k}`);
  }
  assert.match(zh.pat_token_help_generic, /\{\{host\}\}/, '通用文案缺 {{host}} 占位');
});