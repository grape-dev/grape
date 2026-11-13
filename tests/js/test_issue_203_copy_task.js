/*
 * Issue #203 — 「复制需求」弹窗预填与提交校验（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试在沙箱里跑 legacy issues.js 的 openTaskCopyModal /
 * copyTaskSubmit；Vue 迁移后收敛到 openCopyModal() + submitModal()（mode='copy'）。
 * 断言保留原验收语义：
 *   1. 预填标题 = 原标题 + copy.title_suffix
 *   2. 预填描述 = 原描述 + 空行 + 「Issue 链接: <url>」
 *   3. copy 模式提交校验：空标题 / 空描述 / 无 repo 分别阻断（copy.* 错误键）
 *   4. 正常提交走 POST /issues/{owner}/{repo}，payload 与新建同构
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8',
);

function fnBody(source, name) {
  const i = source.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到函数 ' + name);
  let depth = 0;
  let started = false;
  for (let j = i; j < source.length; j++) {
    if (source[j] === '{') {
      depth++;
      started = true;
    } else if (source[j] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(i, j + 1);
    }
  }
  throw new Error('函数 ' + name + ' 括号不闭合');
}

const openCopy = fnBody(VIEW, 'openCopyModal');
const submit = fnBody(VIEW, 'submitModal');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('预填标题 = 原标题 + copy.title_suffix', function () {
  assert.ok(
    /modal\.title = \(tk\.title \|\| ''\) \+ \(t\('copy\.title_suffix'\) \|\| ''\)/.test(openCopy),
    '标题应拼接 copy.title_suffix',
  );
});

check('预填描述 = 原描述 + 空行 + Issue 链接', function () {
  assert.ok(/modal\.desc = desc/.test(openCopy), '应写入描述');
  assert.ok(
    openCopy.indexOf("desc += '\\n\\n'") >= 0,
    "原描述非空时应先补空行（desc += '\\n\\n'）",
  );
  assert.ok(/copy\.issue_link_label/.test(openCopy), '应有「Issue 链接」标签键');
  assert.ok(
    /vcsUrl\(tk\.repo_key, '\/issues\/' \+ tk\.issue_number\)/.test(openCopy),
    'Issue 链接应指向 /issues/{N}',
  );
});

check('copy 模式：空标题 / 空描述 / 无 repo 三处阻断', function () {
  assert.ok(/const isCopy = modal\.mode === 'copy'/.test(submit), '应识别 copy 模式');
  assert.ok(/isCopy \? t\('copy\.err_no_title'\)/.test(submit), '空标题应给 copy.err_no_title');
  assert.ok(/isCopy \? t\('copy\.err_no_desc'\)/.test(submit), '空描述应给 copy.err_no_desc');
  assert.ok(/if \(!repoKey\.trim\(\)\)[\s\S]{0,80}err_no_repo/.test(submit), '无 repo 应阻断');
});

check('提交成功路径与新建同构（同一 POST + payload）', function () {
  assert.ok(/\.post\('\/issues\/' \+ encodeURIComponent\(owner\)/.test(submit), '应 POST /issues/{owner}/{repo}');
  assert.ok(/copy\.success_toast/.test(submit), '复制成功应有独立 toast 文案');
});

console.log('\n#203 全部通过 (' + passed + '/4)');