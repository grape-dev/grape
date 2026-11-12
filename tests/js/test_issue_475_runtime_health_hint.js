/*
 * Issue #475 — 引擎健康「?」帮助提示 + 异常描边
 *
 * 2026-11-11 修复：Vue 迁移时整块丢失（后端 health_reason 一直在返回、i18n 6 个 hint 键
 * 成了孤儿）。本次按旧 runtimes.js _healthHelpHint/_helpBtn 规格补回：
 *   1. healthHintKey 优先级：状态类（about_to_gc/offline/recently_lost）→ 后端 health_reason
 *      → health_abnormal===false 直接无提示 → 连续失败 → 心跳失联 → 健康分 <50
 *   2. 引擎卡与设备卡都在健康徽标旁渲染 ? 提示（title 走 i18n，不渲染原始 key）
 *   3. 健康引擎返回空 key → 不渲染按钮（避免噪音）
 *   4. i18n runtimes.health.hint.* 6 个键在 zh/en 都有值
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') {
      d++;
      started = true;
    } else if (VIEW[j] === '}') {
      d--;
      if (started && d === 0) return VIEW.slice(i, j + 1);
    }
  }
  throw new Error(name + ' 括号不闭合');
}

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('hint 优先级与后端口径', function () {
  const b = fnBody('healthHintKey');
  const order = [
    "h === 'about_to_gc'",
    "h === 'offline'",
    "h === 'recently_lost'",
    'rt.health_reason',
    'rt.health_abnormal === false',
    'health_consecutive_failures',
    'health_heartbeat_alive === false',
    'health_score < 50',
  ];
  let last = -1;
  for (const frag of order) {
    const i = b.indexOf(frag);
    assert.ok(i >= 0, '应包含判断 ' + frag);
    assert.ok(i > last, '判断顺序错误（' + frag + '）');
    last = i;
  }
});

check('健康引擎返回空字符串（不渲染按钮）', function () {
  const b = fnBody('healthHintKey');
  assert.ok(/if \(rt\.health_abnormal === false\) return ''/.test(b), '后端判定健康应直接返回空');
  assert.ok(/return ''\s*\}\s*$/.test(b.trim()), '末尾应兜底返回空');
});

check('引擎卡与设备卡都渲染 ? 提示且走 i18n', function () {
  const n = (VIEW.match(/class="rtv-hhelp"/g) || []).length;
  assert.ok(n >= 2, '引擎卡与设备卡都应渲染 ? 提示，实际 ' + n);
  assert.ok(/\.rtv-hhelp \{/.test(VIEW), '应有 .rtv-hhelp 样式');
  const hints = (VIEW.match(/:title="t\(healthHintKey\(/g) || []).length;
  assert.ok(hints >= 2, '引擎卡与设备卡都应绑定提示，实际 ' + hints);
  assert.ok(!/:title="healthHintKey\(/.test(VIEW), '不应把原始 key 直接当文案');
});

check('i18n 6 个 hint 键 zh/en 齐全', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales', lg, 'runtimes.json'), 'utf8'),
    );
    const hint = (d.health || {}).hint || {};
    for (const k of ['consecutive_failures', 'heartbeat_lost', 'offline', 'recently_lost', 'about_to_gc', 'low_score']) {
      assert.ok(hint[k] && String(hint[k]).trim().length, lg + ' 缺 hint.' + k);
    }
  }
});

console.log('\n#475 全部通过 (' + passed + '/4)');