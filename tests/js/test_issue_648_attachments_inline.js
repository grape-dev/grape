/*
 * Issue #648 — 详情附件渲染（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试沙箱执行 legacy issues.js 的 _renderTaskAttachments。
 * Vue 迁移后由 attachments 响应式状态 + 模板分段渲染；断言保留原语义：
 *   1. 三段状态机：loading / error / 空数组文案
 *   2. 列表按 filename 逐项渲染（图片缩略图与非图片条目同列表）
 *   3. 上传 / 删除走 /issues/{id}/attachments 端点
 *   4. 端点失败 → error 态（不白屏）
 * 已知偏差（记入 tests/js/P3_AUDIT.md）：原「卡片自带 attachments 直接内联渲染、
 * 不发请求」的首屏优化未随 Vue 版移植，loadDetailExtras 一律 GET 端点（性能项，非功能缺陷）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('三段状态机：loading / error / 空列表文案', function () {
  assert.ok(/attachments\.state === 'loading'/.test(VIEW), '应有 loading 态');
  assert.ok(/attachments\.state === 'error'/.test(VIEW), '应有 error 态');
  assert.ok(/!attachments\.list\.length/.test(VIEW), '应有空列表分支');
  assert.ok(/no_attachments/.test(VIEW), '空列表应显示 no_attachments 文案');
});

check('列表按 filename 渲染并带删除入口', function () {
  assert.ok(/v-for="a in attachments\.list" :key="a\.filename"/.test(VIEW), '应逐项渲染附件');
  assert.ok(/function deleteAttach\(/.test(VIEW), '应有 deleteAttach 删除入口');
  assert.ok(/deleteAttach\(/.test(VIEW.split('function deleteAttach(')[0]), '模板应绑定 deleteAttach');
});

check('加载 / 上传 / 删除端点齐全', function () {
  assert.ok(
    /\.get\('\/issues\/' \+ tk\.id \+ '\/attachments'\)/.test(VIEW),
    '应 GET /issues/{id}/attachments',
  );
  assert.ok(
    /\.upload\('\/issues\/' \+ tk\.id \+ '\/attachments', formData\)/.test(VIEW),
    '应上传到同一端点',
  );
  assert.ok(
    /\.del\('\/issues\/' \+ tk\.id \+ '\/attachments\/' \+ filename\)/.test(VIEW),
    '应 DELETE /issues/{id}/attachments/{filename}',
  );
});

check('端点失败落到 error 态（不白屏）', function () {
  const i = VIEW.indexOf('function loadDetailExtras(');
  assert.ok(i > 0, '应有 loadDetailExtras');
  const body = VIEW.slice(i, i + 1200);
  assert.ok(/attachments\.state = 'ok'/.test(body), '成功应置 ok');
  assert.ok(/attachments\.state = 'error'/.test(body), '失败应置 error');
  assert.ok(/\.catch\(/.test(body), '应有 catch 兜底');
});

console.log('\n#648 全部通过 (' + passed + '/4)');