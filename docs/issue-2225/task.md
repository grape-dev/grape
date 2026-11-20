# Task: Issue #2225 任务拆解

- [x] T1 新增 `session_tmpdir` helper（下发/创建/回退/清理）
- [x] T2 平台入队注入 `TASK_TMPDIR`/`SESSION_DIR`（queue + direct）
- [x] T3 worker 执行前 ensure（缺失回退确定性会话目录 + 不可写再回退 mktemp + 告警）
- [x] T4 会话结束清理自身子目录（成功/异常/吊销路径）
- [x] T5 sweeper 兜底清理 `/tmp/opencode` 存量平铺与超 TTL 会话子目录
- [x] T6 新增 `release_guard`（会话任务 Issue 解析 + 一致性断言 + 显式覆盖）
- [x] T7 接入 `issues update` / `pulls create` / `pulls update`
- [x] T8 单测：并发隔离、缺失回退、断言拒绝/放行、清理
- [x] T9 目标测试 + 自审查 + pre-pr 增量检查
- [x] T10 提交/推送/建 PR/Issue 评论/A2A QA/tasks complete

## QA 复审第 1 轮修复（PR #1295）

- [x] R1 Required：`test_issue_2225_session_tmpdir.py` / `test_issue_2225_release_guard.py`
      接入 `.github/workflows/windows-smoke.yml` 的 `linux-backend` 门禁（先例 #2133/#1982/#1613），
      回归不再只靠人工 pytest
- [x] R2 Required：`release_guard` 把缺省 `issue_id` 占位 `#0`/`0` 归为 None（无 Issue 上下文），
      不再解析成 `expected=0` 把该会话所有正文写入/PR 标题引用拦成 409
- [x] R3 Required：sweeper 单测把扫描根收敛到 `tmp_path`（`_sweep_opencode_tmpdirs(now, roots=...)`），
      共享 worker 上跑 pytest 不再删除其他会话的活跃暂存目录
- [x] N1：`ensure_session_tmpdir` 补齐 docstring 声明的第二档（worker 本地确定性会话目录），
      优先级与实现一致
- [x] N2：`session_tmpdir.py` / `release_guard.py` / 两个用例文件补齐行尾换行
- [x] N3：sweeper 对仍处 queued/dispatched/running 的任务跳过其会话子目录（长任务不写暂存不被误清）
- [x] N4：`allow_cross_issue` 同步进 `help/api/openapi-baseline.json`（issues update / pulls create /
      pulls update 三个操作的 query 参数）
- [x] N5：`release_guard` 读 `issue_id` 改用只读连接（`mode=ro`），写请求不再逐一持全局 `db_lock`
- [x] N6：新增用例覆盖 R2 占位 0、R3 扫描根收敛、运行中任务跳过、只读连接不持锁

## 基线合入（arch-weekly-11-zj 8bbc56f53）

- [x] 把 8 个 #2225 提交 rebase 到当前基线 `8bbc56f53`：基线在此期间已并入 #2133/#2245/#2292 等，
      原分支与基线冲突（GitCode `mergeable=false`），按「只重放本 Issue 提交」方式合入，
      不携带其他 Issue 的旧基线内容
- [x] 冲突处置：`cleanup.py`（保留基线 `db_registry`/`contextlib` 导入 + 新增 `tempfile`）、
      `git_repo_api.py`（保留基线 `update_issue` 路由签名，叠加 `allow_cross_issue` query 参数）、
      `windows-smoke.yml`（基线 #2245 步骤与 #2225 步骤并存）
- [x] QA 复审 Optional 收口：`_sweep_opencode_tmpdirs(roots=[])` 显式表达「不扫任何根」，
      不再回落生产扫描根（`roots is not None`），并补 `test_sweeper_empty_roots_scans_nothing`
- [x] QA 复审 Optional 收口：`test_cleanup_by_key_covers_direct_path` 落点收敛到 `tmp_path`
- [x] 静默 except ratchet：`cleanup.py` 新增 3 处 `except: pass` 改为告警日志，
      不再触发 `tests/arch/test_silent_except_ratchet.py` 回潮
- [x] 验证：`tests/runtime/test_issue_2225_*.py` 25 passed；OpenAPI 端点完整性
      `scripts/check-openapi-endpoints.py --require-baseline` OK（284 paths / baseline 255）

## QA 裁定增量（P1/P2，2026-11-16，PR #1295 head 412783cb9）

- [x] 基线再次前进至 `abd6278eb`（#2314/#827 九提交）：按复审建议先实际 rebase 重放
      本 Issue 全部 9 个提交（0 冲突），再叠加本轮增量
- [x] P1(a)：`issues_comments_create`（create_issue_comment）补 `_release_guard_reject`
      接线（`target_issue=number`、传入 body、`allow_cross_issue` 逃生口）
- [x] P1(b)：`pulls_comments_create`（create_pull_comment）补接线（传入 body、
      `allow_cross_issue` 逃生口）；PR 号与任务 Issue 号属不同号段空间不做 target 比对
      ——PR 号恒不等于任务 Issue 号，比对无判别力且会误杀全部正常 PR 评论，
      伪造跨 Issue 正文由 body 主语锚点拦（与 pulls create/update 同口径）
- [x] P1 回归用例：伪造跨 Issue body → 断言 409（`ISSUE_ASSERT_MISMATCH`）且上游
      零调用；含目标 Issue 比对、chat 代发放行、正常 PR 评论放行共 8 例
- [x] P2（口径已裁定纳入）：`issues_create` 接正文主语锚点校验——无目标号可比时比对
      body 首个 `Issue #N` 锚点，无锚点放行；回归用例 3 例（伪造/无锚点/锚点一致）
- [x] `allow_cross_issue` query 参数同步 `help/api/openapi-baseline.json`（issues create /
      issues comments create / pulls comments create 三个操作，外科式刷新）
- [x] help/api/10-git-repos.md 三端点补一致性断言说明
- [ ] P3（QA 已裁定 follow-up，不阻塞本 PR）：guard fail-open 补 blocked/skipped
      计数或统计日志，随办注明，另行落地

验证：`tests/runtime/test_issue_2225_*.py` → 35 passed（25 存量 + 10 新增）；
`scripts/check-openapi-endpoints.py --require-baseline` OK（284 paths）；
`test_issue_372_deep_analysis`（6 例）与 `test_issue_754_pr_comment_fallback::test_post_result_to_pr_405_fallback`
在改动前后复跑为同一组失败，系基线既有红灯，与本轮无关。
