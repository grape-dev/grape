/**
 * 托管仓库平台路由修复 — 前端 hostAndSelect provider 透传回归（docs/weekly-08/host-repo-provider-routing-fix-design.md）。
 *
 * 运行：node tests/js/test_repos_host_provider.js
 *
 * 断言 hostAndSelect 依据 repoKey 前缀推导 ?provider=github|gitcode，body 保持 {full_name}。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const src = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/repos.js'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}
console.log('hostAndSelect provider 透传 — 回归测试\n');

function bodyOf(marker) {
  const i = src.indexOf(marker);
  if (i < 0) throw new Error('未找到 ' + marker);
  return src.substring(i, i + 800);
}

const b = bodyOf('function hostAndSelect');

test('FS-01 源码含从 repoKey 推导 provider（github:/gitcode: 前缀）', () => {
  assert.ok(b.indexOf("'github:'") >= 0 || b.indexOf('"github:"') >= 0, '应判断 github: 前缀');
  assert.ok(b.indexOf("'gitcode:'") >= 0 || b.indexOf('"gitcode:"') >= 0, '应判断 gitcode: 前缀');
  assert.ok(b.indexOf('_provider') >= 0, '应使用 _provider 变量');
});

test('FS-02 URL 模板按 _provider 拼 ?provider=', () => {
  assert.ok(b.indexOf("'/repos/host'") >= 0, '应基于 /repos/host');
  assert.ok(b.indexOf("?provider=") >= 0 && b.indexOf('_provider') >= 0,
    'URL 应拼 ?provider=_provider');
});

test('FS-03 body 恒为 {full_name: fullName}', () => {
  const idx = b.indexOf('API.post');
  const post = b.substring(idx, idx + 80);
  assert.ok(post.indexOf('full_name') >= 0 && post.indexOf('fullName') >= 0, 'body 应含 full_name/fullName');
});

test('FS-04 API.post 使用拼好的 _url 变量', () => {
  assert.ok(/API\.post\(_url/.test(b), '应以 _url 发起请求');
});

test('FS-05 无前缀时 URL 回退 /repos/host（不带 provider）', () => {
  // 三元拼接：_provider 为空 → '' → URL = '/repos/host'
  const tt = b.match(/\/repos\/host' \+\s*\(_provider \?[^)]*:\s*''\)/);
  const tt2 = b.match(/_url\s*=\s*'\/repos\/host'/);
  assert.ok(tt2, '应定义 _url = \'/repos/host\'');
  assert.ok(tt, '无前缀时应拼回 /repos/host（条件为空串）');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);