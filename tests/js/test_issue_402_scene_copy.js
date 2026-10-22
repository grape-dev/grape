/**
 * Issue #402: 项目场景详情页「复制场景」按钮 — 纯前端实现（后端接口零改动）验证测试。
 *
 * 运行：node tests/js/test_issue_402_scene_copy.js
 *
 * 验证点：
 * 1. _renderLocalSceneDetailPage 头部含「复制场景」按钮（不在 isCustomScene 条件内）
 * 2. window._copySceneFromDetail 已定义：弹窗名称预填 <源场景名>-copy（40 字符截断）且可编辑
 * 3. 提交走既有 POST /scenes?repo=…（无新增后端端点），body 含 name/nodes/scope
 * 4. 引擎清洗：engine_runtime_ids 按当前用户可选引擎（agentEngines）过滤；SRE 空引擎节点置停用
 * 5. 名称冲突自动追加序号（行为断言：连续复制两次第二个副本为 <名>-copy2，≤40 字符）
 * 6. 成功后跳转 app.nav('scene-detail-local', 新id)
 * 7. i18n 文案 key 中英齐全
 * 8. 语法完整（Function 构造可解析）
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const dashSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/dashboard.js'), 'utf8').replace(/\r/g, '');
const zhJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/dashboard.json'), 'utf8'));
const enJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/dashboard.json'), 'utf8'));

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

function copyFnSection() {
  const start = dashSrc.indexOf('window._copySceneFromDetail = function');
  assert(start >= 0, 'window._copySceneFromDetail 未找到');
  const end = dashSrc.indexOf('\n  window._', start + 10) > 0 ? dashSrc.indexOf('\n  window._', start + 10) : dashSrc.indexOf('\n}', start);
  return dashSrc.slice(start, end);
}

// 加载 dashboard.js 中提取的 window._generateCopySceneName（沙箱执行，返回可调用函数）
function loadGenerateCopySceneName() {
  const start = dashSrc.indexOf('window._generateCopySceneName = function');
  assert(start >= 0, 'window._generateCopySceneName 未找到（应作为顶层可测函数定义）');
  const end = dashSrc.indexOf('\n};', start);
  assert(end > start, '_generateCopySceneName 函数体未闭合');
  const sandboxWindow = {};
  new Function('window', dashSrc.slice(start, end + 2))(sandboxWindow);
  assert(typeof sandboxWindow._generateCopySceneName === 'function', '沙箱内 _generateCopySceneName 不可调用');
  return sandboxWindow._generateCopySceneName;
}

console.log('Issue #402 复制场景（纯前端实现）— 前端回归测试\n');

test('dashboard.js 语法完整', () => {
  new Function(dashSrc);
});

test('后端零改动：scenes.py / pipeline_config.py 无 copy 相关新增', () => {
  const scenesPy = fs.readFileSync(path.join(ROOT, 'src/api/routes/scenes.py'), 'utf8');
  assert(!scenesPy.includes('scenes_copy'), 'scenes.py 不应包含新增 copy 端点（scenes_copy）');
  assert(!scenesPy.includes('_clean_scene_engine_ids_for_copy'), 'scenes.py 不应包含 copy 清洗助手');
  const pcPy = fs.readFileSync(path.join(ROOT, 'src/api/pipeline_config.py'), 'utf8');
  assert(!/def _generate_copy_scene_name/.test(pcPy), 'pipeline_config.py 不应包含 copy 命名助手');
});

test('详情页头部含「复制场景」按钮', () => {
  const start = dashSrc.indexOf('function _renderLocalSceneDetailPage');
  assert(start >= 0, '_renderLocalSceneDetailPage 未找到');
  const end = dashSrc.indexOf('\nfunction ', start + 10);
  const section = dashSrc.slice(start, end);
  assert(section.includes('_copySceneFromDetail()'), '缺失复制按钮 onclick=_copySceneFromDetail()');
  const btnLine = section.split('\n').find(l => l.includes('_copySceneFromDetail()'));
  assert(btnLine && !btnLine.includes('isCustomScene ?'), '复制按钮不应限定 isCustomScene');
});

test('_copySceneFromDetail 定义于详情页渲染函数内（闭包捕获 scene/nodes/agentEngines/owner/name/dashData）', () => {
  const start = dashSrc.indexOf('function _renderLocalSceneDetailPage');
  const end = dashSrc.indexOf('\nfunction ', start + 10);
  const section = dashSrc.slice(start, end);
  assert(section.includes('window._copySceneFromDetail = function'), 'window._copySceneFromDetail 未定义于详情页渲染函数内');
});

test('弹窗名称预填 <源场景名>-copy 且 40 字符截断', () => {
  const section = copyFnSection();
  assert(section.includes("sn + '-copy'"), "名称预填应为 <源场景名> + '-copy'");
  assert(section.includes('.slice(0, 40)'), '名称预填应截断到 40 字符');
});

test('弹窗名称输入可编辑（maxlength=40 且非只读）', () => {
  const section = copyFnSection();
  const inputLine = section.split('\n').find(l => l.includes('copySceneNameInput') && l.includes('<input'));
  assert(inputLine, '缺失名称输入框 copySceneNameInput');
  assert(inputLine.includes('maxlength="40"'), '输入框应 maxlength=40');
  assert(!/readonly|disabled/.test(inputLine), '输入框不应只读/禁用');
});

test('提交复用既有 POST /scenes 创建接口（不新增后端端点）', () => {
  const section = copyFnSection();
  assert(section.includes("API.post('/scenes?repo='"), "应调用既有 POST /scenes?repo=…（创建场景接口）");
  assert(!section.includes("/copy?repo="), '不应调用新增的 /scenes/{id}/copy 端点');
  assert(section.includes('name:'), 'POST body 应含 name 字段');
  assert(section.includes('nodes:'), 'POST body 应含 nodes 字段');
  assert(section.includes("scope: 'repo'"), "POST body scope 应为 'repo'");
});

test('源配置优先取 dashData._scenes（含 icon/PR 订阅/关联仓库）', () => {
  const section = copyFnSection();
  assert(section.includes('dashData._scenes'), '应从 dashData._scenes 读取源场景完整配置');
  assert(section.includes('icon: src.icon'), '副本应携带源场景 icon');
  assert(section.includes('linked_repos: src.linked_repos'), '副本应携带源场景关联仓库');
  assert(section.includes('pr_subscription_enabled:'), '副本应携带 PR 订阅开关');
});

test('引擎清洗：按 agentEngines 过滤 engine_runtime_ids，SRE 空引擎节点置停用', () => {
  const section = copyFnSection();
  assert(section.includes('agentEngines'), '应基于 agentEngines（当前用户可选引擎）过滤');
  assert(/n\.enabled = false/.test(section), 'SRE 节点无可用引擎时应置 enabled=false');
  assert(section.includes("tag === 'sre'"), '应针对 sre 节点特判');
});

test('名称冲突自动追加序号（提交路径调用 _generateCopySceneName）', () => {
  const section = copyFnSection();
  assert(section.includes('existing[') || section.includes('existing.'), '应基于已有场景名判定冲突');
  assert(section.includes('window._generateCopySceneName(want, existing)'), '提交命名应调用 window._generateCopySceneName(want, existing)');
});

test('行为断言：连续复制两次，第二个副本名称为 <名>-copy2（≤40 字符）', () => {
  const gen = loadGenerateCopySceneName();
  const sn = '源场景展示名';
  // 模拟既有场景名集合（与 dashboard.js 相同的 existing 构造方式）
  const existing = {};
  existing[sn] = 1;
  // 第一次复制：默认名 <名>-copy，无冲突
  const firstWant = (sn + '-copy').slice(0, 40);
  const firstName = gen(firstWant, existing);
  assert.strictEqual(firstName, firstWant, '第一次副本名应为 <名>-copy');
  assert.ok(firstName.length <= 40, '第一次副本名 ≤40 字符');
  // 第一次复制成功后占用 <名>-copy（与 dashboard.js 一致：创建成功即进入场景列表）
  existing[firstName] = 1;
  // 第二次复制同名源场景：默认名仍为 <名>-copy，但已被占用 → 应追加序号
  const secondName = gen(firstWant, existing);
  assert.strictEqual(secondName, sn + '-copy2', `第二个副本名应为 ${sn}-copy2，实际为 ${secondName}`);
  assert.ok(secondName.length <= 40, '第二个副本名 ≤40 字符');
});

test('行为断言：序号连续递增（-copy / -copy2 / -copy3），全占用时不返回已占用名', () => {
  const gen = loadGenerateCopySceneName();
  const sn = 'S';
  const existing = {};
  existing[sn] = 1;
  const want = sn + '-copy';
  const n1 = gen(want, existing); existing[n1] = 1;
  const n2 = gen(want, existing); existing[n2] = 1;
  const n3 = gen(want, existing);
  assert.strictEqual(n1, 'S-copy', '第 1 个副本名应为 S-copy');
  assert.strictEqual(n2, 'S-copy2', '第 2 个副本名应为 S-copy2');
  assert.strictEqual(n3, 'S-copy3', '第 3 个副本名应为 S-copy3');
  assert.ok(!existing[n3], '返回的名称不应与已占用名重复');
});

test('行为断言：超长名称冲突追加序号后仍 ≤40 字符', () => {
  const gen = loadGenerateCopySceneName();
  const longSn = '很'.repeat(40); // 40 字符源场景名
  const want = (longSn + '-copy').slice(0, 40); // 40 字符
  const existing = {};
  existing[want] = 1; // <名>-copy 已被第一次副本占用
  const second = gen(want, existing);
  assert.strictEqual(second.length, 40, '追加序号后应恰为 40 字符（截尾补序号）');
  assert.ok(second.endsWith('2'), '应以序号 2 结尾');
  assert.notStrictEqual(second, want, '不应返回已被占用的 40 字符原名');
  assert.ok(!existing[second], '不应与已占用名重复');
});

test('复制成功后跳转新场景详情', () => {
  const section = copyFnSection();
  assert(section.includes("app.nav('scene-detail-local', r.scene_id)"), "成功后应 app.nav('scene-detail-local', 新id)");
});

test('i18n 文案 key 中英齐全', () => {
  for (const k of ['copy_scene', 'confirm_copy_scene', 'copy_scene_hint', 'copy_scene_name_label', 'scene_copied', 'copy_engines_dropped', 'copy_failed']) {
    assert(zhJson[k], `zh/dashboard.json 缺失 ${k}`);
    assert(enJson[k], `en/dashboard.json 缺失 ${k}`);
  }
  assert(String(zhJson.copy_failed).includes('{{msg}}'), 'zh copy_failed 应含 {{msg}} 插值');
});

console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
process.exit(failed ? 1 : 0);
