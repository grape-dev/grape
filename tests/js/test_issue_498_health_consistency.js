/*
 * Issue #498 — 健康口径一致（后端 health_abnormal 优先）+ worker 版本落后独立标签
 *
 * 2026-11-11 修复：迁移丢失后本次补回，规格取自旧 runtimes.js：
 *   1. isAbnormalEngine：非 online 一律异常；后端给了 health_abnormal 就以其为准
 *      （在线 + 偶发失败已衰减 → 不异常）；无字段时回退派生信号
 *   2. abnormalBorderColor：about_to_gc/offline 恒红；有 health_abnormal 时 true→红、false→无框
 *   3. 卡片边框按上述结论落地（cardLeftBorder 第二参传 rt）
 *   4. worker_version_stale 走独立琥珀标签，不参与异常描边
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/RuntimesView.vue'),
  'utf8',
);

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

check('isAbnormalEngine 以后端 health_abnormal 为准', function () {
  const b = fnBody('isAbnormalEngine');
  assert.ok(/if \(h !== 'online'\) return true/.test(b), '非在线一律异常');
  assert.ok(/health_abnormal !== undefined && rt\.health_abnormal !== null/.test(b), '应检测字段是否给出');
  assert.ok(/return rt\.health_abnormal === true/.test(b), '给了字段就以其为准');
  assert.ok(/health_consecutive_failures/.test(b) && /health_heartbeat_alive/.test(b), '无字段时回退派生信号');
});

check('abnormalBorderColor 与应用规则', function () {
  const b = fnBody('abnormalBorderColor');
  // 2026-11-20：红色由字面量 var(--r) 收口到语义变量 --bar-bad
  // （浅色档 = var(--r) 纯红，与原口径等价；暗色档压暗到 color-mix(--r 55%, 卡片)，
  //  否则深底上 #ff453a 整条 3px 纯红刺眼）。契约不变：红/灰态恒红、false 不给红框。
  assert.ok(
    /h === 'about_to_gc' \|\| h === 'offline'\) return 'var\(--bar-bad\)'/.test(b),
    '红/灰态恒红（走 --bar-bad 语义变量）',
  );
  assert.ok(/rt\.health_abnormal === true \? 'var\(--bar-bad\)' : ''/.test(b), 'false 时不给红框');
});

check('卡片边框消费异常判定（两处传入 rt）', function () {
  const b = fnBody('cardLeftBorder');
  assert.ok(/isAbnormalEngine\(rt\)/.test(b), '边框应看 isAbnormalEngine');
  assert.ok(/abnormalBorderColor\(rt\)/.test(b), '颜色应取 abnormalBorderColor');
  const n = (VIEW.match(/cardLeftBorder\([^)]*,\s*(rt|d)\)/g) || []).length;
  assert.ok(n >= 2, '引擎卡与设备卡都要把对象传进去，实际 ' + n);
});

check('worker_version_stale 独立琥珀标签、不参与描边', function () {
  const b = fnBody('workerVersionStale');
  assert.ok(/worker_version_stale/.test(b), '应识别该字段');
  const cbb = fnBody('cardLeftBorder');
  assert.ok(!/worker_version_stale/.test(cbb), '版本陈旧不应触发异常描边');
  assert.ok(/class="rtv-vstale"/.test(VIEW), '应有独立琥珀标签');
});

console.log('\n#498 全部通过 (' + passed + '/4)');