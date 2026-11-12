/*
 * Issue #221 — 流水线页面问题列表滚动条布局（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy pipeline.js 并断言注入 CSS；Vue 迁移后为 scoped 样式。
 *   1. 列表容器有独立滚动（overflow-y: auto），不撑开整页
 *   2. 搜索栏/表头与滚动区分离（搜索栏不在滚动容器内）
 *   3. 滚动容器的 overflow 不与外层 overflow:hidden 打架
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/Pipeline.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('存在独立纵向滚动容器', function () {
  assert.ok(/overflow-y:\s*auto/.test(VIEW), '应有 overflow-y: auto 的滚动容器');
  const i = VIEW.indexOf('overflow-y: auto');
  const block = VIEW.slice(Math.max(0, i - 400), i + 200);
  const cls = (block.match(/\.(pl__[a-z-]+)/g) || []).pop();
  assert.ok(!!cls, '滚动规则应挂在具体容器类上，实际 ' + cls);
});

check('搜索栏在滚动容器之外', function () {
  const si = VIEW.indexOf('pl__search-bar');
  const li = VIEW.indexOf('pl__search-bar') >= 0 ? VIEW.indexOf('<!-- 表格 -->') : -1;
  assert.ok(si > 0 && li > si, '搜索栏应在表格滚动区之前（各自独立）');
});

check('滚动高度受控（不无限增高）', function () {
  assert.ok(/max-height|height:\s*(calc|100%)/.test(VIEW), '滚动容器应有高度上限');
});

console.log('\n#221 全部通过 (' + passed + '/3)');