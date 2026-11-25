/*
 * Issue #3733 — Autopilot 任务页展示「已启用但不可派发」
 *
 * 后端已把空指令从「每 tick 一条 WARNING」改成「库内状态 + 状态变更留痕一次」，
 * 界面必须跟着把原因说清楚，否则运营只看到任务不跑、仍要翻日志。本文件锁三件事：
 *   1. 列表状态列：已启用且 dispatchable === false → ⚠ 徽标（title 用后端原因），并保留启停圆点
 *   2. 详情页：不可派发提示条（原因 + 「补齐指令」入口，复用 editTask）
 *   3. 词条齐备：zh/en 两个语言包都有 4 个新键，en 侧不含残留中文，视图引用的键都存在
 *
 * 运行：node tests/js/test_issue_3733_autopilot_not_dispatchable.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/AutopilotView.vue'),
  'utf8',
);
const LOCALES = path.join(ROOT, 'src/api/frontend/shell/locales');
const PACKS = {
  zh: JSON.parse(fs.readFileSync(path.join(LOCALES, 'zh', 'autopilot.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(LOCALES, 'en', 'autopilot.json'), 'utf8')),
};

const NEW_KEYS = [
  ['list', 'not_dispatchable'],
  ['detail', 'not_dispatchable_title'],
  ['detail', 'not_dispatchable_desc'],
  ['detail', 'not_dispatchable_fix'],
];

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('列表状态列按「已启用且指令为空」渲染 ⚠ 徽标', function () {
  const i = VIEW.indexOf('v-if="t.enabled && t.dispatchable === false"');
  assert.ok(i > 0, '状态列应判 t.enabled && t.dispatchable === false（暂停态不告警）');
  const block = VIEW.slice(i, i + 320);
  assert.ok(block.indexOf('apv-warn') >= 0, '应使用 apv-warn 徽标样式');
  assert.ok(block.indexOf('t.dispatch_state_reason') >= 0, 'title 应带后端原因');
  assert.ok(block.indexOf("tt('list.not_dispatchable')") >= 0, '无原因时回落词条');
  // 启停圆点不能被徽标挤掉（原有语义保留）
  assert.ok(block.indexOf('t.enabled') >= 0 && block.indexOf('apv-off') >= 0,
    '徽标之外仍要显示启停圆点');
});

check('详情页有不可派发提示条 + 补齐入口', function () {
  const i = VIEW.indexOf('v-if="detail.enabled && detail.dispatchable === false"');
  assert.ok(i > 0, '详情页应按 detail.enabled && detail.dispatchable === false 展示提示条');
  const block = VIEW.slice(i, i + 520);
  assert.ok(block.indexOf('apv-blocked') >= 0, '应使用 apv-blocked 提示条');
  assert.ok(block.indexOf('detail.dispatch_state_reason') >= 0, '提示条应带后端原因');
  assert.ok(block.indexOf("tt('detail.not_dispatchable_desc')") >= 0, '无原因时回落说明词条');
  assert.ok(block.indexOf("tt('detail.not_dispatchable_fix')") >= 0, '应提供补齐指令按钮文案');
  assert.ok(block.indexOf('editTask(detail.id)') >= 0, '补齐入口应复用编辑弹窗');
});

check('apv-warn / apv-blocked 有对应样式', function () {
  for (const sel of ['.apv-warn {', '.apv-blocked {', '.apv-blocked-txt {']) {
    assert.ok(VIEW.indexOf(sel) >= 0, '缺少样式 ' + sel);
  }
});

check('zh/en 语言包都有 4 个新键且非空', function () {
  for (const [group, key] of NEW_KEYS) {
    for (const loc of ['zh', 'en']) {
      const v = PACKS[loc][group] && PACKS[loc][group][key];
      assert.ok(typeof v === 'string' && v.trim().length > 0,
        `${loc}.${group}.${key} 缺失或为空`);
    }
  }
});

check('en 侧新词条无残留中文（资源要能直接给英文用户）', function () {
  for (const [group, key] of NEW_KEYS) {
    const v = PACKS.en[group][key];
    assert.ok(!/[\u4e00-\u9fff]/.test(v), `en.${group}.${key} 含中文: ${v}`);
  }
});

check('视图引用的词条键在语言包中真实存在（不留死引用）', function () {
  const used = VIEW.match(/tt\('(list|detail)\.[a-z0-9_]+'/g) || [];
  assert.ok(used.length > 0, '视图应有 tt() 引用');
  for (const raw of used) {
    const key = raw.slice(4, -1);            // 去掉 "tt('" 与结尾 "'"
    const [group, name] = key.split('.');
    if (group !== 'list' && group !== 'detail') continue;
    // 只校验本次新增键，避免把历史遗留缺口算进来
    if (!NEW_KEYS.some(([g, k]) => g === group && k === name)) continue;
    assert.ok(PACKS.zh[group][name] && PACKS.en[group][name],
      '语言包缺少视图引用的键 ' + key);
  }
});

console.log(`\ntest_issue_3733_autopilot_not_dispatchable.js: ${passed} checks passed`);
