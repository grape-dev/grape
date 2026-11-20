/*
 * 「已完成需求 → 回到处理中」入口守卫（2026-11-20 主人需求，零依赖源码断言）
 *
 * 背景：误触「确认完成」后，前端对 completed 任务把确认完成/取消那排按钮整块隐藏
 * （v-if="status !== 'completed'"），页面上没有任何入口能继续该需求 → 需求卡死在完成态。
 *
 * 口径 B（主人选定）：新增专用端点 POST /issues/{id}/reopen（只回 status=processing、
 * 清完成信息、清空 stage），**不自动重派**；不把 completed→processing 放进通用流转表
 * _TASK_STATUS_FLOWS（那条通用写路径不清 stage，会让 UI 继续显示「已完成」）。
 *
 * 本守卫钉住这几条契约，防止按钮/端点/文案被悄悄改回去：
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8'
);
const ROUTES = fs.readFileSync(
  path.join(ROOT, 'src/domains/runtime/routes/issues.py'),
  'utf8'
);
const FLOWS = fs.readFileSync(
  path.join(ROOT, 'src/domains/runtime/issues_center.py'),
  'utf8'
);

let count = 0;
function ok(cond, msg) {
  count++;
  assert.ok(cond, msg);
}

// 1. 前端有回退函数，且打的是专用端点（不是通用 PUT /status）
const fn = (() => {
  const i = VIEW.indexOf('function reopenTask(');
  assert.ok(i >= 0, '缺少 reopenTask()');
  let d = 0, started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') { d++; started = true; }
    else if (VIEW[j] === '}') { d--; if (started && d === 0) return VIEW.slice(i, j + 1); }
  }
  assert.fail('reopenTask 括号不闭合');
})();
ok(
  /API\(\)\.post\('\/issues\/' \+ tk\.id \+ '\/reopen'\)/.test(fn),
  'reopenTask 必须 POST /issues/{id}/reopen（专用端点）'
);
ok(/grapeConfirm\(/.test(fn), '回到处理中必须二次确认（防误触）');
ok(/withTaskLoading\(/.test(fn), 'reopenTask 必须走统一的 loading/toast 包装');

// 2. 列表行 + 详情栏都要有入口（当前页面两处任务操作区）
const reopenButtons = VIEW.match(/v-if="(sel|detailView)\.status === 'completed'"/g) || [];
ok(
  reopenButtons.length >= 2,
  '已完成任务的回退按钮应同时出现在列表行与详情栏，实际 ' + reopenButtons.length
);
ok(
  (VIEW.match(/@click="reopenTask\((sel|detailView), \$event\)"/g) || []).length >= 2,
  '两处按钮都要绑 reopenTask'
);

// 3. 通用流转表不得被放开 completed→processing（口径 B 只走专用端点）
const flows = (() => {
  const i = FLOWS.indexOf('_TASK_STATUS_FLOWS = {');
  assert.ok(i >= 0, '未找到 _TASK_STATUS_FLOWS');
  const j = FLOWS.indexOf('}', i);
  return FLOWS.slice(i, j + 1);
})();
ok(
  !/completed['"]?\s*:\s*\([^)]*processing/.test(flows),
  'completed 不得在通用流转表里放行 processing（会绕过 stage 清空）'
);
ok(
  /completed['"]?\s*:\s*\(\s*['"]pending_review['"]\s*,?\s*\)/.test(flows),
  'completed→pending_review（#1408 受控撤销）必须保留'
);

// 4. 后端端点：只认 completed、清 stage 与完成信息、不自动派发
const ep = (() => {
  const i = ROUTES.indexOf('async def reopen_issue(');
  assert.ok(i >= 0, '缺少 reopen_issue 端点');
  // 切到下一个 @router 装饰器为止，避免把相邻端点（pipeline 详情）算进来
  const j = ROUTES.indexOf('\n@router.', i);
  return ROUTES.slice(i, j < 0 ? i + 4000 : j);
})();
ok(/@router\.post\("\/\{issue_id\}\/reopen"/.test(ROUTES), '端点必须是 POST /{issue_id}/reopen');
ok(/cur_status != "completed"/.test(ep), '必须拒绝非 completed 需求（400）');
ok(
  /UPDATE issue SET status='processing', stage='', completed_at=NULL,/.test(ep),
  '必须回 status=processing 并清空 stage/completed_*（否则前端仍判「已完成」）'
);
ok(
  !/urlopen|resolve_a2a_base|api\/a2a/.test(ep),
  '口径 B：端点内不得出现任何 A2A 自动派发（派发语句，不是注释里的「不派发」）'
);
ok(/user_id=\? OR user_id=0/.test(ep), '必须按 owner 过滤（他人需求 404）');

// 5. 文案：zh/en 三键齐备，且中文文案要写明「不会自动重派」
for (const lang of ['zh', 'en']) {
  const loc = JSON.parse(
    fs.readFileSync(
      path.join(ROOT, 'src/api/frontend/shell/locales', lang, 'tasks.json'),
      'utf8'
    )
  );
  for (const k of ['reopen_task', 'reopen_task_message', 'task_reopened_toast']) {
    ok(typeof loc[k] === 'string' && loc[k].length > 0, lang + ' 缺文案键 ' + k);
  }
}
const zh = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/tasks.json'), 'utf8')
);
ok(
  /不会自动重派/.test(zh.reopen_task_message),
  '中文确认文案必须写明「不会自动重派」（否则用户以为点了就自动续跑）'
);

console.log('PASS issue reopen guard (' + count + ' assertions)');