// Issue #3229：SKILL.md 首部 UTF-8 BOM 导致 frontmatter 正则失配的前端回归测试。
//
// 运行：node tests/js/test_issue_3229_skill_bom.js
//
// 从 SkillsView.vue 抽取 stripLeadingBom / parseYAMLFrontMatter / escapeHtml /
// renderSkillContent 纯函数，用桩替换 renderMarkdown 后验证：
//   1. 带 BOM 与不带 BOM 的同一 SKILL.md 渲染结果一致（BOM 被剥离，frontmatter 正常成表）
//   2. 带 BOM 时正文不再泄漏裸 frontmatter 文本
//   3. 无 BOM 行为不变（原样渲染 frontmatter + 正文）
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const src = fs.readFileSync(
  path.join(__dirname, '../../src/api/frontend/app/src/views/SkillsView.vue'),
  'utf8'
);

const startMarker = '// Issue #3229：剥除首部 UTF-8 BOM';
const endMarker = '// ── 上传 ──';
const start = src.indexOf(startMarker);
const end = src.indexOf(endMarker, start + startMarker.length);
assert(start >= 0, 'stripLeadingBom marker not found in SkillsView.vue');
assert(end > start, 'renderSkillContent block end marker not found in SkillsView.vue');

const extracted = src.slice(start, end);
const markdownCalls = [];
const sandbox = {
  window: {
    renderMarkdown: function (text) {
      markdownCalls.push(text);
      return '<MD>' + text + '</MD>';
    },
  },
};
const api = new Function(
  'G',
  extracted + '\nreturn { stripLeadingBom: stripLeadingBom, renderSkillContent: renderSkillContent };'
)(sandbox);

const FRONTMATTER =
  '---\n' +
  'name: demo-skill\n' +
  'version: 1.0.0\n' +
  'description: |\n' +
  '  demo description\n' +
  '---\n\n' +
  '# Body\n\n' +
  'Content line.\n';

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log('ok - ' + name);
  } catch (e) {
    failures++;
    console.log('FAIL - ' + name + ': ' + e.message);
  }
}

check('stripLeadingBom removes single/multiple BOM only at start', function () {
  assert.strictEqual(api.stripLeadingBom('\uFEFF---\nname: x\n'), '---\nname: x\n');
  assert.strictEqual(api.stripLeadingBom('\uFEFF\uFEFF---\nname: x\n'), '---\nname: x\n');
  assert.strictEqual(api.stripLeadingBom('---\nname: x\n'), '---\nname: x\n');
  assert.strictEqual(api.stripLeadingBom('body \uFEFF tail'), 'body \uFEFF tail');
});

check('no-BOM content renders frontmatter table then body (unchanged behavior)', function () {
  markdownCalls.length = 0;
  const html = api.renderSkillContent(FRONTMATTER);
  assert(html.indexOf('<table class="fm-table">') === 0, 'frontmatter should render as table first');
  assert(html.indexOf('demo-skill') >= 0, 'frontmatter name should appear in table');
  assert(html.indexOf('1.0.0') >= 0, 'frontmatter version should appear in table');
  assert(html.indexOf('<MD>') >= 0 && html.indexOf('# Body') >= 0, 'body should be rendered by markdown');
  assert.strictEqual(markdownCalls.length, 1, 'markdown should be called once for body only');
});

check('BOM content renders identically to no-BOM content', function () {
  markdownCalls.length = 0;
  const expected = api.renderSkillContent(FRONTMATTER);
  const withBom = api.renderSkillContent('\uFEFF' + FRONTMATTER);
  assert.strictEqual(withBom, expected, 'BOM and non-BOM output must be identical');
});

check('BOM content does not leak raw frontmatter into rendered body', function () {
  markdownCalls.length = 0;
  const html = api.renderSkillContent('\uFEFF' + FRONTMATTER);
  assert(html.indexOf('<table class="fm-table">') === 0, 'frontmatter should be stripped into table');
  markdownCalls.forEach(function (text) {
    assert(text.indexOf('name: demo-skill') === -1, 'raw frontmatter leaked into markdown body');
    assert(text.indexOf('description: |') === -1, 'raw YAML block leaked into markdown body');
  });
});

if (failures > 0) {
  console.error(failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('all passed');
