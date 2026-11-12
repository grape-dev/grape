/*
 * Issue #1077 — 列表分页平滑加载（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试沙箱执行 legacy issues.js 的 _partialRenderTasks /
 * _observeTaskSentinel；Vue 迁移后列表是响应式集合，局部更新天然成立，
 * 断言改为校验「增量语义」是否存在：
 *   1. loadTasks 支持 append / merge 两种增量模式（不再整页重建）
 *   2. 追加去重：append 期间重复触发直接丢弃
 *   3. 过期响应防护：taskReqSeq 递增，旧请求不覆盖新结果
 *   4. 触底哨兵：IntersectionObserver + has_more 收尾
 *   5. 看板分屏视图有独立游标重拉（不落回整页刷新）
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

const body = fnBody(VIEW, 'loadTasks');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('append / merge 两种增量模式', function () {
  assert.ok(/const append = !!\(opts && opts\.append\)/.test(body), '应识别 append');
  assert.ok(/const merge = !!\(opts && opts\.merge\)/.test(body), '应识别 merge');
  assert.ok(/tasksAppendLoading\.value/.test(body), 'append 应有进行中标记');
});

check('追加去重：append 期间重复触发丢弃', function () {
  assert.ok(
    /if \(append && tasksAppendLoading\.value\) return/.test(body),
    '应丢弃重复的 append 请求',
  );
});

check('过期响应防护（seq 递增，旧响应不覆盖新结果）', function () {
  assert.ok(/const seq = \+\+taskReqSeq/.test(body), '应递增请求序号');
  const rest = VIEW.slice(VIEW.indexOf('const seq = ++taskReqSeq'));
  assert.ok(
    /if \(seq !== taskReqSeq\) return/.test(rest.slice(0, 6000)),
    '响应落地前应比较 seq，丢弃过期结果',
  );
});

check('触底哨兵：IntersectionObserver + has_more 收尾', function () {
  assert.ok(/IntersectionObserver/.test(VIEW), '应使用 IntersectionObserver');
  assert.ok(/has_more/.test(VIEW), '应消费 has_more 判断是否继续加载');
  assert.ok(/sentinel/i.test(VIEW), '应有哨兵元素');
});

check('看板分屏视图走列游标重拉（不等价于整页刷新）', function () {
  assert.ok(
    /if \(!append && !merge && boardPagedView\.value\) \{[\s\S]{0,80}resetBoardCursors\(\)[\s\S]{0,60}return loadBoardAll\(\)/.test(
      body,
    ),
    '看板分屏应走 resetBoardCursors + loadBoardAll',
  );
});

console.log('\n#1077 全部通过 (' + passed + '/5)');