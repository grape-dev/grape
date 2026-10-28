/*
 * Issue #487 回归测试：智能推荐规则弹窗加载竞态防护 + 保存失败提示
 *
 * 运行：node tests/js/test_issue_487_scene_recommend_save.js
 *
 * 验证点（与 src/api/static/js/pages/dashboard.js _openSceneRecommendModal 一致）：
 *   1. 弹窗初始「添加规则 / 保存」按钮 disabled
 *   2. 场景列表加载成功后按钮启用
 *   3. 场景列表为空 / 加载失败 → 按钮保持禁用并 toast 提示
 *   4. 场景未加载完成时 _saveSceneRecommendConfig 直接拦截（不发 PUT）
 *   5. _addRecRuleRow 在 sceneOptions 为空时不添加规则行
 *   6. dashboard.js 源码包含关键守卫（disabled 初始态 / sceneListLoaded / 启用逻辑）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// ── 提取弹窗加载竞态防护逻辑（与 dashboard.js 一致）──

function createModalState() {
  // 弹窗初始状态：按钮 disabled（dashboard.js modal.innerHTML 中 recAddRuleBtn/recSaveBtn 均 disabled）
  return {
    sceneOptions: '',
    sceneListLoaded: false,
    buttons: { recAddRuleBtn: { disabled: true }, recSaveBtn: { disabled: true } },
    toasts: []
  };
}

function toast(state, msg) { state.toasts.push(msg); }

// 场景列表加载完成回调（dashboard.js API.get('/scenes/with-pipeline').then 内逻辑）
function onScenesLoaded(state, scenes) {
  state.sceneOptions = (scenes || []).map(function (s) {
    return '<option value="' + s.id + '">' + (s.display_name || s.name || s.id) + '</option>';
  }).join('');
  if (!scenes.length) {
    toast(state, '当前仓库暂无可用场景，无法配置智能推荐规则');
    return null; // 空场景：保持按钮禁用，跳过配置回显
  }
  state.sceneListLoaded = true;
  ['recAddRuleBtn', 'recSaveBtn'].forEach(function (id) { state.buttons[id].disabled = false; });
  return true;
}

// 加载失败回调（dashboard.js .catch 逻辑：保持禁用 + toast）
function onScenesLoadFailed(state, e) {
  toast(state, '加载推荐配置失败' + (e && e.message ? ': ' + e.message : ''));
}

// _addRecRuleRow 守卫（dashboard.js：sceneOptions 为空时不添加行）
function addRecRuleRow(state, sceneId) {
  if (!state.sceneOptions && sceneId === undefined) {
    toast(state, '场景列表尚未加载完成，请稍后再添加规则');
    return false;
  }
  return true;
}

// _saveSceneRecommendConfig 守卫（dashboard.js：sceneListLoaded 为 false 时直接 return）
function saveSceneRecommendConfig(state) {
  if (!state.sceneListLoaded) {
    toast(state, '场景列表尚未加载完成，请稍后保存');
    return false;
  }
  return true;
}

// ── 测试 ──

console.log('Issue #487: 智能推荐规则弹窗竞态防护测试\n');

// 1. 初始禁用
let st = createModalState();
assert.ok(st.buttons.recAddRuleBtn.disabled, 'add-rule button should start disabled');
assert.ok(st.buttons.recSaveBtn.disabled, 'save button should start disabled');
assert.strictEqual(saveSceneRecommendConfig(st), false, 'save must be blocked before scenes loaded');
console.log('  PASS: 弹窗初始按钮禁用，保存被拦截');

// 2. 加载成功后启用 + 保存放行
st = createModalState();
onScenesLoaded(st, [{ id: 'scene-a', name: 'A' }, { id: 'scene-b', name: 'B' }]);
assert.strictEqual(st.buttons.recAddRuleBtn.disabled, false, 'add-rule enabled after load');
assert.strictEqual(st.buttons.recSaveBtn.disabled, false, 'save enabled after load');
assert.ok(st.sceneOptions.indexOf('scene-a') >= 0);
assert.strictEqual(saveSceneRecommendConfig(st), true, 'save allowed after load');
console.log('  PASS: 场景加载完成后按钮启用、保存放行');

// 3. 场景列表为空 → 保持禁用 + 提示
st = createModalState();
assert.strictEqual(onScenesLoaded(st, []), null);
assert.ok(st.buttons.recSaveBtn.disabled, 'buttons stay disabled when scene list empty');
assert.ok(st.toasts.some(m => m.indexOf('暂无可用场景') >= 0), 'empty-scene toast shown');
console.log('  PASS: 场景列表为空时保持禁用并提示');

// 4. 加载失败 → 保持禁用 + 提示
st = createModalState();
onScenesLoadFailed(st, new Error('network'));
assert.ok(st.buttons.recAddRuleBtn.disabled && st.buttons.recSaveBtn.disabled);
assert.ok(st.toasts.some(m => m.indexOf('加载推荐配置失败') >= 0));
console.log('  PASS: 加载失败保持禁用并提示');

// 5. _addRecRuleRow 空选项守卫
st = createModalState();
assert.strictEqual(addRecRuleRow(st), false, 'add-rule blocked when sceneOptions empty');
assert.ok(st.toasts.some(m => m.indexOf('尚未加载完成') >= 0));
onScenesLoaded(st, [{ id: 'scene-a' }]);
assert.strictEqual(addRecRuleRow(st), true, 'add-rule allowed after options ready');
console.log('  PASS: 规则行添加在选项就绪前被拦截、就绪后放行');

// 6. dashboard.js 源码守卫存在性
const src = fs.readFileSync(path.join(__dirname, '../../src/api/static/js/pages/dashboard.js'), 'utf8');
assert.ok(/id="recAddRuleBtn"[^>]*disabled/.test(src), 'add-rule button starts disabled in source');
assert.ok(/id="recSaveBtn"[^>]*disabled/.test(src), 'save button starts disabled in source');
assert.ok(src.indexOf('sceneListLoaded') >= 0, 'sceneListLoaded flag present');
assert.ok(src.indexOf("'recAddRuleBtn', 'recSaveBtn'") >= 0, 'enable logic present');
assert.ok(/b\.disabled = false/.test(src), 'button enable assignment present');
console.log('  PASS: dashboard.js 源码包含禁用/启用守卫');

console.log('\nAll tests passed.');
