# 方案设计：Issue #1229 能效看板

## 后端：`GET /api/dashboard/efficiency`（src/api/routes/dashboard.py 新增）

### 端点契约
```
GET /api/dashboard/efficiency?repo=<owner/repo 或 platform:owner/repo>&limit=50
鉴权：Depends(require_org_member)；repo 提供时 check_repo_access（与 /{owner}/{repo} 一致）
响应：
{
  "repo": "gitcode:owner/repo",          # 空串=未指定仓库
  "total_issues": N, "completed_issues": N, "total_tokens": N,
  "contributors": [
    {"user": "<创建人>", "issues": n1, "completed": n2,
     "input_tokens": t1, "output_tokens": t2, "total_tokens": t3}
  ]   # 按 issues DESC, total_tokens DESC, user ASC 排序；limit 截断（1-200）
}
```

### 聚合算法（两条独立读路径，Python 侧合并）
1. **提需求/完成数**（`issue` 单表，C3 权威）：
   - `SELECT COALESCE(NULLIF(created_by,''), assignee) AS user, COUNT(*), SUM(status='completed')`
   - `WHERE repo_key IN (gitcode:X, github:X)`（跨平台双前缀，同 `get_dashboard` 口径）
   - 排除：bot 用户名（`get_all_bot_usernames()`）与子任务行（`is_subtask_mode=1`）
2. **token 用量**：`obs_llm_usage`（`run_kind='task'` 且 repo 前缀匹配）按 `task_id` SUM
   五项 token，再以 `agent_tasks.issue_id` 回链 `issue`（同仓库）取创建人，Python 累加。
   - 口径对齐 `src/observability/tokens.py`：total = input+output+cache_read+cache_write（不含 reasoning）。
   - issue 行不存在 / 创建人为 bot → 丢弃该任务用量（bot 驱动的执行不属任何协作者）。
   - task_id 先聚合再回链：SQL 返回行数 = 任务数，非用量行数（大数据量可控）。

### 错误纪律
- 任一查询异常 → 记 warning + 该路径按空结果处理，绝不 500（用量/统计不能拖垮页面）。
- 未指定 repo → 返回空 contributors + 空态（看板是仓库维度视图）。

## 前端：「任务与日志」新增「能效看板」子 Tab（src/api/static/js/pages/observability.js）

- `_OBS_SECTIONS.inspect.subs`：`['tasks', 'events', 'errorlogs']` → `['tasks', 'efficiency', 'events', 'errorlogs']`
  （能效看板紧跟任务 Tab，符合「任务与日志模块内新增」的入口语义）。
- 渲染：`renderObsContent()` 新增 `_obsTab === 'efficiency'` 分支 → `renderObsEfficiency(c)`：
  - 仓库维度 KPI 行（复用 `section()` + `_kpiRowHTML()`）：总需求 / 已完成 / 总 Token；
  - 协作者排行：表格（协作者 / 提需求 / 完成 / Token），Token 列内嵌占比条形（纯 CSS，不引图表库）；
  - 空态：无协作者记录时渲染 `empty` 文案卡片；
  - 取数：`API.get('/dashboard/efficiency?repo=' + currentRepo.fullName)`，手动刷新按钮复用；
  - 10s 自动刷新轮询对 efficiency Tab 静默重取（与 tasks Tab 同模式）。
- HTML 转义复用 `_escHtml()`；用户名/数字全部转义后渲染（防 XSS）。

## i18n（locales/zh + en 的 observability.json 同步新增）
- `tab.efficiency`（子 Tab 名「能效看板」/「Efficiency」）
- `eff.total_issues / eff.completed / eff.total_tokens / eff.th_user / eff.th_issues / eff.th_completed / eff.th_tokens`
- `eff.empty / eff.load_failed / eff.refresh`
- zh/en 键集合必须一致（既有 T12 parity 守卫风格）。

## 不改动的相邻面
- `_OBS_VIEWS` 顺序、大 Tab、侧栏结构不动；`/api/dashboard/ops/*` 既有端点不动。
- usage_collect.py 只读复用（经 obs_llm_usage 台账），不改采集路径。
