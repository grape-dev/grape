/*
 * Issue #202 — 同名需求不应被本地查重阻断提交（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试从 legacy issues.js 提取 createTaskSubmit 在沙箱执行；
 * Vue 迁移后提交逻辑收敛到 submitModal()。改为源码断言，保留原验收语义：
 *   1. 提交路径不存在「同名标题」本地查重拦截（无 err_dup / dup_title 分支）
 *   2. 必填校验仍保留：无 repo / 空标题 会阻断并给出对应错误
 *   3. 正常提交调用 API.post('/issues/{owner}/{repo}')，body 含 repo_key/title/description/scene_id
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

const body = fnBody(VIEW, 'submitModal');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('提交路径无同名标题查重拦截', function () {
  assert.ok(!/err_dup/.test(body), 'submitModal 不应含 err_dup 分支');
  assert.ok(!/dup_title/.test(body), 'submitModal 不应含 dup_title 分支');
  assert.ok(
    !/tasksAll[\s\S]{0,80}title[\s\S]{0,40}===/.test(body),
    '不应在提交前遍历已有任务做同名比对',
  );
});

check('必填校验保留：无 repo → 阻断', function () {
  assert.ok(/if \(!repoKey\.trim\(\)\)/.test(body), '应保留 repo 必填校验');
  assert.ok(/err_no_repo/.test(body), '应给出 err_no_repo 错误');
});

check('必填校验保留：空标题 → 阻断且不提交', function () {
  const guard = body.indexOf('if (!modal.title.trim())');
  const post = body.indexOf(".post('/issues/'");
  assert.ok(guard >= 0, '应保留标题必填校验');
  assert.ok(post > guard, '标题校验必须在提交之前');
  assert.ok(/err_no_title/.test(body), '应给出 err_no_title 错误');
});

check('正常提交：POST /issues/{owner}/{repo} 且 body 字段齐全', function () {
  assert.ok(
    /\.post\('\/issues\/' \+ encodeURIComponent\(owner\) \+ '\/' \+ encodeURIComponent\(rname\), payload\)/.test(
      body,
    ),
    '应 POST /issues/{owner}/{repo}',
  );
  for (const key of ['repo_key:', 'title:', 'description:', 'scene_id:']) {
    assert.ok(body.indexOf(key) >= 0, 'payload 应含 ' + key);
  }
});

console.log('\n#202 全部通过 (' + passed + '/4)');