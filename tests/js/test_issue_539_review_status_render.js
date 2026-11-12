/*
 * Issue #539 — 需求详情页流水线卡片：需求级状态横幅 + SRE 验收结论块（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy issues.js；Vue 迁移后为 review_status 相关模板 + i18n。
 *   1. 待人工确认横幅：review_status.pending_review_hint（阶段完成 ≠ 需求完成）
 *   2. SRE 结论三态：在途 / 无结论 / 任务引用
 *   3. 数据来自 GET /issues/{id}/pipeline（review_status / sre_result），不是前端臆造
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/IssuesView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('待人工确认横幅文案存在', function () {
  assert.ok(/review_status\.pending_review_hint/.test(VIEW), '应有 pending_review_hint');
  assert.ok(/review_status/.test(VIEW), '应消费 review_status 字段');
});

check('SRE 结论三态齐备', function () {
  for (const k of ['sre_pending', 'sre_unavailable', 'sre_task_ref', 'sre_result_title']) {
    assert.ok(VIEW.indexOf('review_status.' + k) >= 0, '应有 ' + k);
  }
});

check('数据来源是 pipeline 端点', function () {
  assert.ok(/\/issues\/' \+ tk\.id \+ '\/pipeline'/.test(VIEW), '应 GET /issues/{id}/pipeline');
  assert.ok(/sre_result/.test(VIEW), '应消费 sre_result');
});

check('i18n 键在 zh/en 都有值', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales', lg, 'issues.json'), 'utf8'),
    );
    const s = JSON.stringify(d);
    for (const k of ['pending_review_hint', 'sre_pending', 'sre_unavailable']) {
      assert.ok(s.indexOf(k) >= 0, lg + ' 缺 ' + k);
    }
  }
});

console.log('\n#539 全部通过 (' + passed + '/4)');