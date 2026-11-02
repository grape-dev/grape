/*
 * Issue #701：worker 显示名统一取值函数 workerDisplayName 单测
 *
 * 运行：node tests/js/test_issue_701_worker_display_name.js
 *
 * 契约：rc.name（runtime_credentials.name）为唯一来源；
 *   - 正常名：原样返回（trim）
 *   - 空名/空白名：返回统一占位符「未命名设备」（i18n common.unnamed_worker）
 *   - 不回退 wk.device_name / wk.host_name
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

function loadFn(i18nStub) {
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/js/worker-name.js'), 'utf8');
  const sandbox = { I18N: i18nStub };
  const keys = Object.keys(sandbox);
  const fn = new Function(...keys, src + '\n; return workerDisplayName;');
  return fn(...keys.map(k => sandbox[k]));
}

console.log('Issue #701：workerDisplayName 统一取值（rc.name 唯一来源 + 空名占位）');

check('正常自定义名：trim 后原样返回', () => {
  const f = loadFn({ t: (k) => k });
  assert.strictEqual(f({ name: '我的工作机', device_name: 'worker-os-host' }), '我的工作机');
  assert.strictEqual(f({ name: '  dev-box  ' }), 'dev-box');
});

check('空名/空白名/null：返回占位符，不回退 device_name/host_name', () => {
  const f = loadFn({ t: (k) => '«' + k + '»' });
  assert.strictEqual(f({ name: '', device_name: 'worker-os-host' }), '«common.unnamed_worker»');
  assert.strictEqual(f({ name: '   ', device_name: 'worker-os-host' }), '«common.unnamed_worker»');
  assert.strictEqual(f({ name: null, host_name: 'ctrl-host' }), '«common.unnamed_worker»');
  assert.strictEqual(f({}), '«common.unnamed_worker»');
  assert.strictEqual(f(null), '«common.unnamed_worker»');
});

check('无 I18N 环境：兜底中文占位「未命名设备」', () => {
  const f = loadFn(undefined);
  assert.strictEqual(f({ name: '' }), '未命名设备');
  assert.strictEqual(f({ device_name: 'worker-os-host' }), '未命名设备');
});

check('i18n 双语：占位键存在且中英文值正确', () => {
  const zh = JSON.parse(fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/locales/zh/common.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/locales/en/common.json'), 'utf8'));
  assert.strictEqual(zh.unnamed_worker, '未命名设备');
  assert.strictEqual(en.unnamed_worker, 'Unnamed device');
});

console.log('');
console.log('passed=' + passed + ' failed=' + failed);
if (failed > 0) process.exit(1);
