/*
 * 飞书消息在 Web Chat 不产生重复气泡（源码级断言）
 *
 * 2026-11-11：Vue 版 ChatView 没有任何去重逻辑（前后端 grep dedupe/duplicate 均为 0），
 * 本次在渲染前加数据层去重 dedupeMessages：
 *   1. 同 id 的普通消息只保留首条
 *   2. 同 task_id 的 agent 回复只保留最新一条（流式重连后补的完整版）
 *   3. 两处服务端赋值（打开会话 / 兜底重拉）都经过去重
 *   4. 去重是纯函数，不修改入参顺序语义（非重复消息顺序不变）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ChatView.vue'), 'utf8');

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

check('按 id 去重（保留首条）', function () {
  const b = fnBody('dedupeMessages');
  assert.ok(/seenId\.has\(k\)/.test(b), '应按 id 判重');
  assert.ok(/if \(seenId\.has\(k\)\) return/.test(b), '重复 id 应跳过');
});

check('agent 回复按 task_id 保留最新', function () {
  const b = fnBody('dedupeMessages');
  assert.ok(/m\.role === 'agent' && m\.task_id != null/.test(b), '应识别 agent 回复');
  assert.ok(/out\[idx\] = m/.test(b), '同 task 应覆盖为最新');
});

check('两处服务端赋值都走去重', function () {
  const n = (VIEW.match(/dedupeMessages\(/g) || []).length;
  assert.ok(n >= 3, '定义 + 两处调用，实际 ' + n);
  assert.ok(/messages\.value = dedupeMessages\(msgs\)/.test(VIEW), '打开会话赋值应去重');
  assert.ok(/dedupeMessages\(d2\.messages \|\| \[\]\)/.test(VIEW), '兜底重拉赋值应去重');
});

check('去重保持非重复消息顺序', function () {
  const b = fnBody('dedupeMessages');
  assert.ok(/out\.push\(m\)/.test(b), '应顺序 push');
  assert.ok(!/\.sort\(/.test(b), '不应重排消息');
});

console.log('\nchat_task_bubble 全部通过 (' + passed + '/4)');