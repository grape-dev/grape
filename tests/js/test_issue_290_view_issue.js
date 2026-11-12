/*
 * Issue #290 — 需求详情「查看 Issue」入口统一 /issues/（源码级断言）
 *
 * 2026-11-11 修复：Vue 版把 #290 的修复写回成 is_pr 分支，本次改回统一 /issues/。
 *   1. 「查看 Issue」按钮走 openIssueLink → 一律 /issues/{N}（无 is_pr 分支）
 *   2. PR 专属入口走 openPrLink → /pull/{N}（保留）
 *   3. 详情摘要行里的 #N ↗ 链接仍按 is_pr 区分（#290 明确 Out of Scope，不动）
 *   4. i18n detail.view_issue 文案存在
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/IssuesView.vue'), 'utf8');

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

check('查看 Issue 一律 /issues/（无 is_pr 分支）', function () {
  const b = fnBody('openIssueLink');
  assert.ok(/vcsUrl\(tk\.repo_key, '\/issues\/' \+ tk\.issue_number\)/.test(b), "应固定拼 '/issues/'");
  assert.ok(!/is_pr/.test(b), '不应再有 is_pr 分支');
});

check('PR 专属入口仍走 /pull/', function () {
  const b = fnBody('openPrLink');
  assert.ok(/vcsUrl\(tk\.repo_key, '\/pull\/' \+ tk\.issue_number\)/.test(b), 'openPrLink 应拼 /pull/');
});

check('摘要行 #N 链接保留 is_pr 区分（Out of Scope）', function () {
  assert.ok(/tk\.is_pr \? '\/pull\/' : '\/issues\/'/.test(VIEW), '摘要行链接应按 is_pr 区分（本次不动）');
});

check('view_issue 文案存在', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales', lg, 'issues.json'), 'utf8'),
    );
    assert.ok(d.detail && d.detail.view_issue, lg + ' 缺 detail.view_issue');
  }
});

console.log('\n#290 全部通过 (' + passed + '/4)');