/*
 * Issue #316: 删除前端「场景·技能」页场景 tab 的「仓库场景」子 Tab — 前端回归测试
 *
 * 运行：node tests/js/test_issue_316_scene_repo_tab_removed.js
 *
 * 验证点：
 *   1. scene-market.js 不再包含 repo 二级子 Tab 入口（_switchSceneTab('repo') / scene_scope_repo）
 *   2. repo 列表加载/渲染函数与场景编排（编辑/删除/创建）死代码均已移除
 *   3. zh/en skills.json 均已删除 scene_scope_repo 及其配套死键
 *   4. official 路径（场景市场加载/渲染/应用/体验/点赞）保持完好
 *   5. index.html 引用版本号已更新（缓存失效）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/scene-market.js'), 'utf8');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/skills.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/skills.json'), 'utf8'));
const INDEX = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');

// 1. repo 子 Tab 入口移除
assert.ok(!JS.includes("_switchSceneTab('repo')"), '不应再有 repo 子 Tab 切换入口');
assert.ok(!JS.includes('scene_scope_repo'), '不应再引用 scene_scope_repo');
assert.ok(!JS.includes('仓库场景'), '不应再出现「仓库场景」文案');

// 2. repo 加载/渲染/编排死代码移除
['_loadCustomScenes', '_renderCustomSceneList', '_renderCustomSceneGrid',
 '_enterSceneEditor', '_editScene', '_deleteScene', '_showCardMenu',
 '_saveScene', '_renderSceneEditor', '_sceneEditorScope', '_skillSelectScope',
 'customSceneGrid', 'customSceneList'].forEach(function (name) {
  assert.ok(!JS.includes(name), '死代码应被移除: ' + name);
});
assert.ok(!/scope=repo/.test(JS), '不应再发起 scope=repo 请求');

// 3. locales 死键清理（zh/en 一致）
['scene_scope_repo', 'no_scenes_custom', 'create_scene', 'edit_scene', 'delete_scene',
 'confirm_delete_scene', 'delete_scene_tip', 'scene_id_placeholder', 'save_scene'].forEach(function (k) {
  assert.ok(!(k in ZH), 'zh skills.json 应删除死键: ' + k);
  assert.ok(!(k in EN), 'en skills.json 应删除死键: ' + k);
});

// 4. official 路径保持完好
['_loadSceneMarket', '_loadSceneMarketList', '_renderSceneMarketPage',
 '_renderOfficialSceneGrid', '_renderSceneCard', '_applyScene',
 '_experienceScene', '_toggleSceneLike', 'scene_scope_official'].forEach(function (name) {
  assert.ok(JS.includes(name), 'official 路径函数/键应保留: ' + name);
});
assert.ok('scene_scope_official' in ZH && 'scene_scope_official' in EN, 'scene_scope_official 键应保留');
assert.ok('goto_repo_scenes' in ZH && 'goto_repo_scenes' in EN, 'goto_repo_scenes（冲突弹窗仍用）应保留');

// 5. index.html 版本号刷新
assert.ok(/scene-market\.js\?v=(4[2-9]|[5-9]\d)/.test(INDEX), 'scene-market.js 缓存版本号应更新');

console.log('✅ Issue #316 前端回归测试全部通过');
