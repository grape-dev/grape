/*
 * Issue #364: Workers 设备卡片"承载引擎"点击不弹详情修复 — 前端测试
 *
 * 运行：node tests/js/test_issue_364_engine_detail_fallback.js
 *
 * 验证点：
 *   1. runtimes.js 含模块级 _rtEngineIndex 索引 + renderRuntimes 中重建调用
 *   2. openEngineDetail 多数据源查找（Engines 缓存 → 设备索引 → 异步回退端点）
 *   3. 设备来源条目补齐 worker/scope/owner 字段并标记 _partial（_upgradeEngineDetail 后台补全）
 *   4. 异步回退走 GET /settings/agent-runtimes/{rid}，失败有 toast 反馈（非静默 return）
 *   5. 后端 settings.py 新增详情端点（复用 get_agent_runtimes 可见性口径，404 分支）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');
const SETTINGS_PY = fs.readFileSync(path.join(ROOT, 'src/api/routes/settings.py'), 'utf8');

// ── 与 runtimes.js _rebuildRtEngineIndex 保持一致的逻辑提取 ──
function rebuildIndex(runtimeData) {
  const index = {};
  ((runtimeData && runtimeData.runtimes) || []).forEach(rt => {
    if (rt && rt.id !== undefined) index[rt.id] = rt;
  });
  ((runtimeData && runtimeData.devices) || []).forEach(d => {
    (d.runtimes || []).forEach(rt => {
      if (!rt || rt.id === undefined || index[rt.id]) return;
      index[rt.id] = Object.assign({}, rt, {
        credential_id: d.credential_id,
        worker_name: d.name || d.device_name || '',
        worker_ip: d.worker_ip || '',
        scope: d.scope || 'global',
        owner: d.owner || '',
        is_local: d.credential_id === 0,
        _partial: true,
      });
    });
  });
  return index;
}

// ── 与 runtimes.js openEngineDetail 保持一致的查找顺序提取 ──
function lookup(runtimeData, index, runtimeId) {
  const allRuntimes = (runtimeData && runtimeData.runtimes) || [];
  let rt = allRuntimes.find(r => r.id === runtimeId);
  if (!rt) rt = index[runtimeId];
  return rt;  // undefined → 异步回退端点
}

// ── 测试 ──

function testIndexCoversDeviceEngines() {
  // 场景：设备承载引擎不在 Engines 分页缓存（Issue #364 根因场景）
  const data = {
    runtimes: [{ id: 1, name: 'opencode', scope: 'global' }],
    devices: [
      { credential_id: 5, name: 'worker-a', worker_ip: '10.0.0.1', scope: 'global', owner: 'bob', runtimes: [
        { id: 21, name: 'hermes', healthy: true },
      ]},
      { credential_id: 0, name: '本机直连', worker_ip: '127.0.0.1', scope: 'local', owner: '', runtimes: [
        { id: 99, name: 'codex-local', healthy: true },
      ]},
    ],
  };
  const index = rebuildIndex(data);

  assert.ok(index[1], 'Engines 缓存条目进索引');
  assert.ok(index[21], '设备承载引擎（不在 Engines 页）进索引（Issue #364 修复点）');
  assert.ok(index[99], '本机直连引擎进索引');
  assert.strictEqual(index[21].worker_name, 'worker-a', '设备来源条目补齐 worker_name');
  assert.strictEqual(index[21].worker_ip, '10.0.0.1', '补齐 worker_ip');
  assert.strictEqual(index[21].scope, 'global', '补齐 scope');
  assert.strictEqual(index[21].owner, 'bob', '补齐 owner');
  assert.strictEqual(index[21].credential_id, 5, '补齐 credential_id');
  assert.ok(index[21]._partial, '设备来源条目标记 _partial（触发后台补全）');
  assert.strictEqual(index[99].is_local, true, 'credential_id=0 → is_local');
  assert.ok(!index[1]._partial, 'Engines 缓存条目不标记 _partial');

  console.log('  PASS: 索引覆盖 Engines 缓存 ∪ 设备承载引擎，设备来源条目字段补齐');
}

function testLookupOrderEnginesFirst() {
  const data = { runtimes: [{ id: 1, name: 'opencode-full', health_score: 90 }], devices: [] };
  const index = { 1: { id: 1, name: 'opencode-partial', _partial: true } };
  const rt = lookup(data, index, 1);
  assert.strictEqual(rt.name, 'opencode-full', 'Engines 缓存优先于设备索引（行为不变）');
  console.log('  PASS: 查找顺序 Engines 缓存优先（不回归）');
}

function testLookupMissFallsBackAsync() {
  const data = { runtimes: [{ id: 1 }], devices: [] };
  const index = rebuildIndex(data);
  const rt = lookup(data, index, 777);
  assert.strictEqual(rt, undefined, '未命中 → 走异步详情端点回退（不再静默 return）');
  console.log('  PASS: 两级未命中 → 异步回退（Issue #364 修复点）');
}

function testSourceContract() {
  // 前端源码契约：索引、重建调用、多源查找、异步回退、toast
  assert.ok(/let _rtEngineIndex = \{\}/.test(RUNTIMES_JS), '存在模块级 _rtEngineIndex');
  assert.ok(/_rebuildRtEngineIndex\(\);/.test(RUNTIMES_JS), 'renderRuntimes 中重建索引');
  assert.ok(/_rtEngineIndex\[runtimeId\]/.test(RUNTIMES_JS), 'openEngineDetail 查设备引擎索引');
  assert.ok(/_openEngineDetailAsync/.test(RUNTIMES_JS), '存在异步回退函数');
  assert.ok(/\/settings\/agent-runtimes\/' \+ runtimeId/.test(RUNTIMES_JS), '异步回退调用单引擎详情端点');
  assert.ok(/_upgradeEngineDetail/.test(RUNTIMES_JS), '_partial 条目后台补全');
  assert.ok(/app\.showToast\(\(I18N\.t\('common\.load_failed'\)/.test(RUNTIMES_JS), '失败 toast 反馈');
  assert.ok(!/if \(!rt\) return;/.test(RUNTIMES_JS), '原静默 return 已移除');
  // Workers 设备承载引擎点击绑定仍在
  assert.ok(/openEngineDetail\(\$\{rt\.id\}\)/.test(RUNTIMES_JS), '设备承载引擎点击绑定保持');

  // 后端源码契约：详情端点 + 可见性复用 + 404
  assert.ok(/get_agent_runtime_detail/.test(SETTINGS_PY), 'settings.py 存在详情端点函数');
  assert.ok(/operation_id="settings_agent_runtime_detail"/.test(SETTINGS_PY), '端点 operation_id');
  assert.ok(/get_agent_runtimes\(user=user, page=1, page_size=0/.test(SETTINGS_PY), '复用列表可见性口径');
  assert.ok(/status_code=404/.test(SETTINGS_PY), '不可见/未命中 404');

  console.log('  PASS: 前后端源码契约（索引/多源查找/异步回退/toast/404）');
}

// ── 执行 ──
const tests = [
  testIndexCoversDeviceEngines,
  testLookupOrderEnginesFirst,
  testLookupMissFallsBackAsync,
  testSourceContract,
];
let failed = 0;
console.log(`Issue #364 engine detail fallback tests (${tests.length} suites)`);
tests.forEach(t => { try { t(); } catch (e) { failed++; console.error('  FAIL:', e.message); } });
console.log(`\n${tests.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
