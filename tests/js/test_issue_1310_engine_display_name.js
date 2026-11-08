/*
 * Issue #1310：引擎显示名统一取值函数 engineDisplayName 单测
 *
 * 运行：node tests/js/test_issue_1310_engine_display_name.js
 *
 * 契约（与 workerDisplayName #701 同源）：
 *   - 正常：返回「引擎名（凭据名）」（全角括号），引擎名取 display_name || engine_name || name || instance_name
 *   - 凭据名空/空白：占位符 i18n common.unnamed_worker（«未命名设备»）
 *   - 不回退设备固有名称：engine_name/name 缺省时兜底 instance_name（兼容匹配）
 *   - 本机直连：worker_name=host_name 直接拼接
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

function loadFns(i18nStub) {
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/js/worker-name.js'), 'utf8');
  const sandbox = { I18N: i18nStub };
  const keys = Object.keys(sandbox);
  const fn = new Function(...keys, src + '\n; return {workerDisplayName, engineDisplayName};');
  return fn(...keys.map(k => sandbox[k]));
}

console.log('Issue #1310：engineDisplayName 统一取值（引擎名+凭据名）');

check('正常：opencode(凭据名) 拼接', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(
    engineDisplayName({ name: 'opencode', display_name: 'opencode', worker_name: '我的工作机' }),
    'opencode（我的工作机）');
  assert.strictEqual(
    engineDisplayName({ name: 'hermes', worker_name: 'dev-box' }),
    'hermes（dev-box）');
});

check('引擎名优先 display_name，其次 name', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(
    engineDisplayName({ name: 'hwcloud', display_name: 'AIShell', worker_name: 'w1' }),
    'AIShell（w1）');
});

check('dashboard 数据源：engine_name 兜底（name 为 instance_name 时）', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(
    engineDisplayName({ name: 'opencode-worker-demo-0003', engine_name: 'opencode', worker_name: 'devki' }),
    'opencode（devki）');
});

check('凭据名空/空白/null → 「未命名设备」占位（i18n 键）', () => {
  const { engineDisplayName } = loadFns({ t: (k) => '«' + k + '»' });
  assert.strictEqual(engineDisplayName({ name: 'opencode', worker_name: '' }), 'opencode（«common.unnamed_worker»）');
  assert.strictEqual(engineDisplayName({ name: 'opencode', worker_name: '   ' }), 'opencode（«common.unnamed_worker»）');
  assert.strictEqual(engineDisplayName({ name: 'opencode' }), 'opencode（«common.unnamed_worker»）');
});

check('无 I18N 环境：兜底中文占位「未命名设备」', () => {
  const { engineDisplayName } = loadFns(undefined);
  assert.strictEqual(engineDisplayName({ name: 'opencode', worker_name: '' }), 'opencode（未命名设备）');
});

check('本机直连：worker_name=host_name 直接拼接，不出现设备固有名', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(
    engineDisplayName({ name: 'codex', worker_name: 'ctrl-host', instance_name: 'codex-ctrl-host' }),
    'codex（ctrl-host）');
});

check('兼容匹配：无 display/engine/name 时兜底 instance_name', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(
    engineDisplayName({ instance_name: 'opencode-worker-demo-0003', worker_name: 'w9' }),
    'opencode-worker-demo-0003（w9）');
});

check('空对象/null：仍返回占位，不抛错', () => {
  const { engineDisplayName } = loadFns({ t: (k) => k });
  assert.strictEqual(engineDisplayName({}), 'common.unnamed_worker');
  assert.strictEqual(engineDisplayName(null), 'common.unnamed_worker');
});

console.log('');
console.log('passed=' + passed + ' failed=' + failed);
if (failed > 0) process.exit(1);