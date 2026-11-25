/*
 * Issue #342 — 事件详情页 running 状态局部轮询（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy events.js；Vue 迁移后为 startPoll/stopPoll。
 *   1. 只在非终态（running/pending）启动轮询
 *   2. 终态（completed/failed/cancelled）立即 stopPoll
 *   3. 无 seq 不轮询（避免空转）
 *   4. 轮询定时器统一清理（离开页面/切换事件不泄漏）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/EventView.vue'), 'utf8');
// Issue #3812：轮询器封装（stop 的真实清表动作在这里）
const GUARD = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/lib/pollGuard.js'), 'utf8');

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

check('终态立即停止轮询', function () {
  const b = fnBody('startPoll');
  assert.ok(/st === 'completed' \|\| st === 'failed' \|\| st === 'cancelled'/.test(b), '应识别终态');
  assert.ok(/stopPoll\(\)[\s\S]{0,20}return/.test(b), '终态应 stopPoll 并返回');
});

check('无 seq 不轮询', function () {
  const b = fnBody('startPoll');
  assert.ok(/if \(!seq\)/.test(b), '应有 seq 守卫');
  assert.ok(/stopPoll\(\)/.test(b), '无 seq 应停止');
});

check('定时器统一清理', function () {
  assert.ok(/function stopPoll\(/.test(VIEW), '应有 stopPoll');
  const b = fnBody('stopPoll');
  // Issue #3812：清表动作下沉到会话感知轮询器（createSessionPoller.stop → clearTimer），
  // 视图侧改为委托；这里同时校验委托点与封装内真实的 clearInterval，守卫生效面不下移。
  assert.ok(
    /clearInterval|clearTimeout|detailPoller\.stop\(\)/.test(b),
    '应清理定时器（或委托轮询器 stop）'
  );
  assert.ok(
    /function clearTimer\(\) \{[\s\S]{0,200}?clearInterval\(timer\)/.test(GUARD),
    '轮询器 stop 必须真实 clearInterval（封装不得变成空转）'
  );
  assert.ok(/onBeforeUnmount/.test(VIEW), '组件卸载应停轮询');
});

check('轮询不整页刷新（只更新当前事件状态）', function () {
  assert.ok(/setConvTask|status =|apply|update/.test(VIEW), '只更新事件状态字段');
  assert.ok(!/location\.reload/.test(VIEW), '不得整页 reload');
});

console.log('\n#342 全部通过 (' + passed + '/4)');