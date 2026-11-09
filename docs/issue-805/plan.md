# Plan: Issue #805

## 改动路径（依赖顺序）
1. `src/api/db_setup.py`：启动迁移 `_migrate_issue_created_by`（_ensure_column，SQLite/MySQL 共用）兜底补列。
2. `src/api/issues_center.py`：
   - `ensure_issue_task(..., creator="")`：INSERT 写 `created_by`；复用路径仅空补写（幂等）。
   - `_query_tasks()`：SELECT 追加 `created_by`，返回 dict 加 `creator`（user_id 索引后移）。
   - 新增 `_build_creator_filter()`（与 `_build_assignee_filter` 并列，created_by 列）。
3. `src/api/event_pipeline.py`：issue 建档分支按优先级提取创建人，传 `creator=`；仅 open 类事件做 VCS 兜底（避免每事件一次 API）。
4. `src/api/routes/issues.py`：
   - `/issues/mine`、`/issues/{owner}/{repo}` 新增 `creators` Query 参数（单值 = / 多值 IN）。
   - `_issue_stage_map` SELECT 加 `created_by`；`_assemble_issue` 透出 `creator`；详情 VCS 兜底分支返回 `creator`。
   - `_sync_missing_issue_row` 从 VCS data 提取 user.login 传 creator。
   - 列表 PR 伪卡片 setdefault `creator`。
5. `scripts/backfill_issue_creator.py`：按 repo 分批查 created_by 空值行 → fetch_issue_via_vcs → UPDATE 仅空行；限流 + `--dry-run`/`--limit`。
6. `src/api/static/js/pages/issues.js`（本分支实际前端源码）：
   - 筛选值/计数/二次过滤/请求参数 `assignees=` → `creators=`（变量名保留 `_taskFilterAssignees` 兼容内联 onclick）。
   - 列表副标题、看板卡片、详情「创建人」行 → `t.creator`；PR 伪卡片副标题保持 `t.assignee`（触发者语义）。
7. 测试：`tests/test_issue_805_creator.py`（写 creator / 幂等不覆盖 / creator 过滤）。

## 风险
- MySQL created_by 列：db_setup _ensure_column 兜底 + 上线核查（运维）。
- 前端缓存：静态 JS 由 FastAPI StaticFiles 直出，无版本号构建链；本次不改缓存策略（超范围）。
