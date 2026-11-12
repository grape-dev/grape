/*
 * Issue #329 — 跨流水线移交（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy issues.js；Vue 迁移后为 cross 模式弹窗 + doCrossPipelineSubmit。
 *   1. 目标场景下拉：去重（同 scene_id 只出现一次），来自 qa_history
 *   2. 提交前二次确认（cross_pipeline.confirm_message，含需求号与场景名）
 *   3. 提交 POST /issues/{id}/handoff {target_scene_id}；被拦截的 skipped 三态各有文案
 *   4. 成功 toast 带目标场景名
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'), 'utf8');

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

check('目标场景来自后端 targets 接口', function () {
  assert.ok(/modal\.crossTargets = d\.targets \|\| \[\]/.test(VIEW), '应取后端 targets');
  assert.ok(/crossTargets\.length|v-for="s in modal\.crossTargets"/.test(VIEW), '模板应渲染目标场景');
});

check('提交前二次确认（含需求号/场景名）', function () {
  const b = fnBody('submitCrossPipeline');
  assert.ok(/grapeConfirm/.test(b), '应二次确认');
  assert.ok(/confirm_message/.test(b), '确认文案走 i18n');
  assert.ok(/replace\('\{issue\}'/.test(b) && /replace\('\{scene\}'/.test(b), '文案应替换需求号与场景');
});

check('提交端点与载荷', function () {
  const b = fnBody('doCrossPipelineSubmit');
  assert.ok(/'\/issues\/' \+ tk\.id \+ '\/handoff'/.test(b), '应 POST /issues/{id}/handoff');
  assert.ok(/target_scene_id/.test(b), '载荷应含 target_scene_id');
});

check('skipped 三态与成功 toast', function () {
  const b = fnBody('doCrossPipelineSubmit');
  for (const k of ['skipped_dup', 'skipped_no_vod', 'skipped']) {
    assert.ok(b.indexOf('cross_pipeline.' + k) >= 0, '应有 ' + k + ' 文案');
  }
  assert.ok(/success_toast/.test(b), '成功应有 toast');
  assert.ok(/replace\(\s*'\{scene\}'/.test(b), '成功文案应带场景名');
});

console.log('\n#329 全部通过 (' + passed + '/4)');