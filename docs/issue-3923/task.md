# Issue #3923 任务拆解与完成状态

| # | 任务 | 状态 | 载体 |
|---|------|------|------|
| 1 | 复现：HEAD 指 `refs/remotes/*` 时 `worktree add` 必报 `HEAD not found` | ✅ | 本地 bare 仓实测 |
| 2 | 归因三态（resolved/none/unavailable/unattributed） | ✅ | `src/kernel/token_resolver.py` |
| 3 | 裸仓 HEAD 校验 + 自愈（建树前） | ✅ | `repogit.ensure_bare_head` / `ensure_repo_base` |
| 4 | `worktree add` 失败自愈重试一次 | ✅ | `repogit.ensure_issue_worktree` |
| 5 | 失败现场 + 错误面透传 git 原文与阶段 | ✅ | `worktree_failure_error` / `agent.worktree_block_result` |
| 6 | 删除猜测性文案 | ✅ | `worktree_credential_error` |
| 7 | 新增 19 条用例（自愈/重试/三态/反向回归/并发现场/阶段措辞/单次解析） | ✅ | `tests/runtime/test_issue_3923_worktree_head_and_attribution.py` |
| 8 | 既有相关测试回归 | ✅ | 122 passed（含 #3713 双入口、#2245、#2514） |
| 9 | 自审查（五轴）+ 开源前置增量检查 | ✅ | 见 PR 说明 |
| 10 | 建 PR + Issue 评论 + A2A 交办 QA | ✅ | PR #2099 |
| 11 | 审查 R1：仓级失败现场 `pop` → `get` + TTL，并发任务均阻断 | ✅ | `repogit.ensure_issue_worktree` / `worktree_last_failure` |
| 12 | 审查 R2：失败阶段按事实映射措辞（repo_base ≠ worktree add） | ✅ | `_WORKTREE_STAGE_FAILURE_TEXT` |
| 13 | 审查 Optional/Nit：单次阻断只解析一次凭据链；用例名与断言对齐 | ✅ | `agent.worktree_block_result` / 测试改名与补断言 |
| 14 | 复审 Required：`ensure_repo_base` 的 init/remote add/except 失败路径同样写失败现场并阻断 | ✅ | `repogit.ensure_repo_base` + `test_base_init_failure_records_scene_and_blocks` / `test_base_exception_failure_records_scene_and_blocks` |
| 15 | 复审 Optional：issue/agent 下传 `ensure_repo_base`，失败直接写三元组键，去掉「仓级 → 三元组」复制 | ✅ | `repogit.ensure_issue_worktree` + 并发用例改为各自读自己的 git 原文 |
| 16 | 复审 Nit：`test_non_head_failure_is_not_recorded_as_head_repair` 构造真实非 HEAD 失败，断言 `stage=worktree_add` 且无 head_repair | ✅ | `tests/runtime/test_issue_3923_worktree_head_and_attribution.py` |
| 17 | 复审合并阻塞：分支落后基线（lint.yml 缺失、non_ff）→ 变基到 `arch-weekly-11-zj` 并复跑回归 | ✅ | 变基后 124 passed + `make lint` 通过 |
| 18 | 三轮 Required：错误面先读现场再问凭据；空仓断言仅在无 `repo_base` 现场时成立；失败现场按阶段优先级保留 | ✅ | `repogit.worktree_credential_error` / `worktree_failure_error` / `_record_worktree_failure`（`WORKTREE_STAGE_PRIORITY`） |
| 19 | 三轮 Required：建树自身异常出口写现场并阻断（`os.makedirs` + `ensure_issue_worktree` except + `agent` 宽 except） | ✅ | `WORKTREE_STAGE_BUILD_EXCEPTION` / `agent._execute_task_impl` |
| 20 | 三轮 CI 覆盖：lint.yml pytest 步骤纳入本 Issue 用例与 `test_repogit.py` | ✅ | `.github/workflows/lint.yml` |
