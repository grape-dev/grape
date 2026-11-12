/*
 * Issue #446 — 部署链接 XSS 防护（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试在沙箱里跑 legacy issues.js 的 _renderDeployLink；
 * Vue 迁移后部署链接改为模板属性绑定（:href），不再是字符串拼接 innerHTML，
 * 注入面天然消失。本用例改为结构 + 校验双端断言：
 *   1. 模板用 :href="deployUrl" 属性绑定（不做字符串拼接渲染）
 *   2. 部署链接不进入 v-html 渲染路径
 *   3. 前端保存时校验 http(s):// 前缀（saveDeployUrl）
 *   4. 后端 _validate_deploy_url 校验前缀 + 长度上限（双端防护）
 *   5. esc() 转义函数仍在（v-html 列表路径继续依赖它）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../../');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8',
);
const BACKEND = fs.readFileSync(
  path.join(ROOT, 'src/domains/mgmt/routes/repos.py'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('模板用 :href 属性绑定部署链接（无字符串拼接）', function () {
  assert.ok(/:href="deployUrl"/.test(VIEW), '应存在 :href="deployUrl" 绑定');
  assert.ok(
    !/innerHTML[\s\S]{0,80}deployUrl/.test(VIEW),
    '不得把 deployUrl 拼进 innerHTML',
  );
});

check('部署链接不走 v-html', function () {
  const lines = VIEW.split('\n').filter((l) => /v-html/.test(l));
  assert.ok(lines.length > 0, '列表/描述区仍应有 v-html（依赖 esc 转义）');
  assert.ok(
    !lines.some((l) => /deploy/i.test(l)),
    'v-html 行不应包含部署链接',
  );
});

check('前端 saveDeployUrl 校验 http(s):// 前缀', function () {
  const i = VIEW.indexOf('function saveDeployUrl(');
  assert.ok(i > 0, '应存在 saveDeployUrl');
  const body = VIEW.slice(i, i + 700);
  assert.ok(/startsWith\('http:\/\/'\)/.test(body), '应校验 http:// 前缀');
  assert.ok(/startsWith\('https:\/\/'\)/.test(body), '应校验 https:// 前缀');
  assert.ok(/showToast/.test(body), '非法 URL 应提示用户');
});

check('后端 _validate_deploy_url 校验前缀与长度', function () {
  assert.ok(/def _validate_deploy_url\(/.test(BACKEND), '后端应有 _validate_deploy_url');
  const i = BACKEND.indexOf('def _validate_deploy_url(');
  const body = BACKEND.slice(i, i + 900);
  assert.ok(/startswith\("http:\/\/"\)/.test(body), '后端应校验 http:// 前缀');
  assert.ok(/len\(url\) > 512/.test(body), '后端应限制长度 512');
});

check('esc() 转义函数仍在且覆盖 5 个危险字符', function () {
  const i = VIEW.indexOf('function esc(');
  assert.ok(i > 0, '应存在 esc()');
  const body = VIEW.slice(i, i + 500);
  for (const ch of ['&', '<', '>', '"', "'"]) {
    assert.ok(body.indexOf(ch) >= 0, 'esc 应转义字符 ' + ch);
  }
});

console.log('\n#446 全部通过 (' + passed + '/5)');