/**
 * Issue #1740：需求中心「视图模式持久化 + 详情返回看板」回归测试（Vue 版）
 *
 * 合入 arch-weekly-11 时从 tests/api/js 的同名旧测试移植：原来 vm 沙箱执行
 * src/api/static/js/pages/issues.js（该文件已随 Vue 迁移删除），改为对
 * IssuesView.vue 做源码级断言 —— 覆盖 T1/T2/T3/T4/T5/T7。
 *
 * 运行：node tests/js/test_issue_1740_view_mode_back_board.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'frontend', 'app', 'src', 'views', 'IssuesView.vue'),
  'utf8'
);

function fnBody(name) {
  const i = SRC.indexOf('function ' + name + '(');
  assert.ok(i > -1, `missing function ${name}`);
  let depth = 0, started = false, out = '';
  for (let j = i; j < SRC.length; j++) {
    const ch = SRC[j];
    out += ch;
    if (ch === '{') { depth++; started = true; }
    else if (ch === '}') { depth--; if (started && depth === 0) break; }
  }
  return out;
}

test('T1 视图模式从 localStorage 初始化，非法值回退 mine', () => {
  assert.match(SRC, /const VIEW_MODE_KEY = 'tasks_view_mode'/, '视图模式持久化 key 丢了');
  const body = fnBody('loadViewMode');
  assert.match(body, /localStorage\.getItem\(VIEW_MODE_KEY\)/, 'loadViewMode 没读 localStorage');
  assert.match(body, /saved === 'mine' \|\| saved === 'repo_all'/, '没做合法值校验');
  assert.match(body, /return 'mine'/, '非法缓存值没有回退 mine');
  assert.match(SRC, /const viewMode = ref\(loadViewMode\(\)\)/, 'viewMode 未从 loadViewMode 初始化');
});

test('T2 切换视图模式写入 localStorage（仅合法值）', () => {
  const save = fnBody('saveViewMode');
  assert.match(save, /mode === 'mine' \|\| mode === 'repo_all'/, 'saveViewMode 未校验取值');
  assert.match(save, /localStorage\.setItem\(VIEW_MODE_KEY, mode\)/, 'saveViewMode 没写 localStorage');
  const sw = fnBody('switchViewMode');
  assert.match(sw, /saveViewMode\(mode\)/, 'switchViewMode 未持久化选择');
});

test('T3/T4 详情返回走缓存渲染 + merge 校准（不走全局重载）', () => {
  assert.match(SRC, /loadBoardAll\(\{ merge: true \}\)/, '看板返回缺少 merge:true 校准');
  assert.match(SRC, /loadTasks\(\{ merge: true \}\)/, '列表返回缺少 merge:true 校准');
  // merge 路径必须由返回路径触发，且不能退化成全量 loadTasks()
  assert.match(SRC, /boardPagedView\.value\) loadBoardAll\(\{ merge: true \}\)/, '看板分支判断缺失');
});

test('T5 关闭详情清理详情态', () => {
  const close = fnBody('closeDetail');
  assert.match(close, /detailView\.value = null/, 'closeDetail 未清空 detailView');
  assert.match(close, /prQaHtml\.value = ''/, 'closeDetail 未重置 PR QA 面板');
});

test('T7 详情返回按钮统一走 closeDetail（含场景详情分支）', () => {
  assert.match(SRC, /@click="closeDetail"/, '详情返回按钮没有绑 closeDetail');
  const back = fnBody('backFromDetail');
  assert.match(back, /_taskDetailFromScene/, '场景详情来源判断缺失');
  assert.match(back, /closeDetail\(\)/, 'backFromDetail 未回到 closeDetail');
});

test('轮询在详情返回路径上恢复（不残留）', () => {
  assert.match(SRC, /clearInterval\(pollTimer\)/, '缺少轮询清理');
  assert.match(SRC, /pollTimer = setInterval/, '缺少轮询重建');
});