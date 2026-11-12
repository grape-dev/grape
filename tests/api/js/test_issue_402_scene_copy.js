/**
 * Issue #402 场景复制按钮 + Issue #1844 服务端复制改造 — 前端回归测试。
 *
 * 运行：node tests/js/test_issue_402_scene_copy.js
 *
 * 验证点：
 * 1. _renderLocalSceneDetailPage 头部含「复制场景」按钮（不在 isCustomScene 条件内）
 * 2. window._copySceneFromDetail 已定义：弹窗名称预填 <源场景名>-copy（40 字符截断）且可编辑
 * 3. 提交走 POST /scenes/{id}/copy?repo=…（Issue #1844 服务端复制），body 只带 name
 * 4. 复制明细由服务端响应渲染（redacted_fields / dropped_engines / cleared_skills / pr_subscription_disabled）
 * 5. 成功后跳转 app.nav('scene-detail-local', 新id)
 * 6. 名称去重工具行为不变（服务端同样兜底）
 * 7. skill_pending 提示渲染、重选技能后清空、保存节点时保留
 * 8. i18n 文案 key 中英齐全
 * 9. 语法完整（Function 构造可解析）
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../../..');
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

console.log('Issue #1844 场景复制（服务端复制）— 前端回归测试\n');

test('dashboard.js 语法完整', () => {
  new Function(dashSrc);
});

test('后端已新增服务端复制端点（scenes_copy）', () => {
  const scenesPy = fs.readFileSync(path.join(ROOT, 'src/domains/mgmt/routes/scenes.py'), 'utf8');
  assert(scenesPy.includes('operation_id="scenes_copy"'), 'scenes.py 应包含服务端复制端点 scenes_copy');
  const pcPy = fs.readFileSync(path.join(ROOT, 'src/api/pipeline_config.py'), 'utf8');
  assert(/advanced_options/.test(pcPy), 'pipeline_config.py 应支持 advanced_options 落库');
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

test('_copySceneFromDetail 定义于详情页渲染函数内（闭包捕获 scene/owner/name）', () => {
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

test('提交走 POST /scenes/{id}/copy?repo=…（服务端复制）', () => {
  const section = copyFnSection();
  assert(section.includes("API.post('/scenes/' + encodeURIComponent(scene.id) + '/copy?repo='"), '应调用 POST /scenes/{id}/copy?repo=…');
  assert(!section.includes("API.post('/scenes?repo='"), '不应再复用既有 POST /scenes?repo=… 创建接口');
});

test('请求体只带 name（取值矩阵由服务端执行）', () => {
  const section = copyFnSection();
  assert(/\{\s*name:\s*want\s*\}/.test(section), '请求体应只带 { name: want }');
  assert(!section.includes('nodes: copyNodes'), '前端不应再提交 nodes');
  assert(!section.includes('linked_repos: src.linked_repos'), '前端不应再提交 linked_repos');
});

test('复制明细由服务端响应渲染', () => {
  const section = copyFnSection();
  for (const k of ['redacted_fields', 'dropped_engines', 'cleared_skills', 'pr_subscription_disabled']) {
    assert(section.includes(k), '应读取响应字段 ' + k);
  }
  for (const k of ['copy_fields_redacted', 'copy_engines_dropped', 'copy_skills_cleared', 'copy_pr_sub_disabled']) {
    assert(section.includes(k), '应展示明细文案 ' + k);
  }
});

test('复制成功后跳转新场景详情', () => {
  const section = copyFnSection();
  assert(section.includes("app.nav('scene-detail-local', r.scene_id)"), "成功后应 app.nav('scene-detail-local', 新id)");
});

test('行为断言：名称去重工具 -copy / -copy2 / -copy3', () => {
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
});

test('skill_pending 提示渲染（查看模式与编辑模式）', () => {
  assert(dashSrc.includes('node.skill_pending'), '应渲染 node.skill_pending');
  assert(dashSrc.includes('skill_pending_hint'), '应使用 skill_pending_hint 文案');
  assert((dashSrc.match(/skill_pending_hint/g) || []).length >= 2, '查看/编辑两种卡片都应提示需重选技能');
});

test('重选技能后清空 skill_pending', () => {
  assert((dashSrc.match(/node\.skill_pending = '';/g) || []).length >= 2, '编辑与创建两处 selectAgentSkill 都应清空 skill_pending');
});

test('保存节点 payload 保留 skill_pending', () => {
  assert((dashSrc.match(/skill_pending: n\.skill_pending \|\| ''/g) || []).length >= 2, '编辑/创建保存 payload 都应保留 skill_pending');
});

test('i18n 文案 key 中英齐全', () => {
  for (const k of ['copy_scene', 'confirm_copy_scene', 'copy_scene_hint', 'copy_scene_name_label',
                   'scene_copied', 'copy_engines_dropped', 'copy_fields_redacted',
                   'copy_skills_cleared', 'copy_pr_sub_disabled', 'skill_pending_hint', 'copy_failed']) {
    assert(zhJson[k], `zh/dashboard.json 缺失 ${k}`);
    assert(enJson[k], `en/dashboard.json 缺失 ${k}`);
  }
  assert(String(zhJson.copy_failed).includes('{{msg}}'), 'zh copy_failed 应含 {{msg}} 插值');
  assert(String(zhJson.skill_pending_hint).includes('{{skill}}'), 'zh skill_pending_hint 应含 {{skill}} 插值');
});

function addCustomFieldSections() {
  const sections = [];
  let idx = dashSrc.indexOf('window._addCustomFieldInline = function');
  const marker = 'window._addCustomFieldInline = function';
  while (idx >= 0) {
    const nextWindow = dashSrc.slice(idx + 10).search(/\n\s*window\._/);
    const end = nextWindow > 0 ? idx + 10 + nextWindow : dashSrc.indexOf('\n};', idx);
    sections.push(dashSrc.slice(idx, end));
    const next = dashSrc.indexOf(marker, idx + marker.length);
    if (next < 0) break;
    idx = next;
  }
  return sections;
}

test('高级选项 name/value 渲染前转义（防存储型 XSS，覆盖两份实现）', () => {
  const sections = addCustomFieldSections();
  assert(sections.length >= 2, `应同时覆盖全局与创建模式两份 _addCustomFieldInline，实际 ${sections.length} 份`);
  sections.forEach((section, i) => {
    assert(section.includes('_skillEsc(name'), `第 ${i + 1} 份实现 name 应经 _skillEsc 转义`);
    assert(section.includes('_skillEsc(_val'), `第 ${i + 1} 份实现 value 应经 _skillEsc 转义`);
    assert(section.includes('_skillEsc(_desc'), `第 ${i + 1} 份实现 description 应经 _skillEsc 转义`);
  });
});

test('离开创建模式恢复全局 _addCustomFieldInline', () => {
  assert(dashSrc.includes('_addCustomFieldInlineGlobal = window._addCustomFieldInline'), '应保存全局实现引用');
  const start = dashSrc.indexOf('function _resetCreateMode()');
  assert(start >= 0, '_resetCreateMode 未找到');
  const section = dashSrc.slice(start, dashSrc.indexOf('}', start));
  assert(section.includes('window._addCustomFieldInline = window._addCustomFieldInlineGlobal'), '退出创建模式时应恢复全局实现');
});

test('skill_pending_hint 文案不自带 ⚠（模板已前置，避免双图标）', () => {
  assert(!String(zhJson.skill_pending_hint).includes('⚠'), 'zh skill_pending_hint 不应自带 ⚠');
  assert(!String(enJson.skill_pending_hint).includes('⚠'), 'en skill_pending_hint 不应自带 ⚠');
});

console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
process.exit(failed ? 1 : 0);
