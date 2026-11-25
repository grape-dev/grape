/*
 * Issue #3433 — 事件详情 failed 态透出结构化失败原因（源码级断言）
 *
 *   1. 读取 events.detail 返回的 failure_info，渲染原因 / 阶段 / 类型 / 引擎
 *   2. failed 详情页提供「重试」「联系人工」入口
 *   3. 事件序号支持一键复制
 *   4. 历史事件缺 failure_info 时展示降级提示
 *   5. zh/en locale 均包含新增文案
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/EventView.vue'),
  'utf8'
);
const ZH = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/shell/locales/zh/events.json'), 'utf8')
);
const EN = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/shell/locales/en/events.json'), 'utf8')
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('读取 failure_info 并分字段渲染', function () {
  assert.ok(/d\.failure_info/.test(VIEW), '应读取 failure_info');
  assert.ok(/fi\.phase_label/.test(VIEW) && /fi\.phase/.test(VIEW), '应展示失败阶段');
  assert.ok(/fi\.error_type/.test(VIEW), '应展示异常类型');
  assert.ok(/fi\.runtime/.test(VIEW) && /fi\.agent/.test(VIEW), '应展示 runtime/agent');
});

check('failed 态有重试与联系人工入口', function () {
  assert.ok(/data-ev-act="failretry"/.test(VIEW), '应有重试按钮');
  assert.ok(/data-ev-act="failfeedback"/.test(VIEW), '应有联系人工按钮');
  assert.ok(/act === 'failretry'/.test(VIEW), '重试点击应打开重派发');
  assert.ok(/act === 'failfeedback'/.test(VIEW) && /openFeedback\(\)/.test(VIEW), '联系人工应打开反馈');
});

check('事件序号一键复制', function () {
  assert.ok(/function copySeq\(\)/.test(VIEW), '应有 copySeq');
  assert.ok(/class="ev__seqcopy"/.test(VIEW), '序号旁应有复制按钮');
  assert.ok(/seq_copied/.test(VIEW), '复制成功应有提示');
});

check('历史事件缺字段降级提示', function () {
  assert.ok(/fail_legacy_hint/.test(VIEW), '应使用降级提示文案');
  assert.ok(/else \{\s*html \+=/.test(VIEW), '无 failure_info 时走降级分支');
});

check('zh/en locale 文案齐备', function () {
  const keys = ['seq_copy_title', 'seq_copied', 'seq_copy_failed', 'fail_phase', 'fail_type',
    'fail_engine', 'fail_legacy_hint', 'fail_actions', 'btn_fail_retry', 'btn_contact_human'];
  keys.forEach(function (k) {
    assert.ok(ZH[k], 'zh 缺 ' + k);
    assert.ok(EN[k], 'en 缺 ' + k);
  });
});

console.log('\n#3433 全部通过 (' + passed + '/5)');