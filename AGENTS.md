# AGENTS.md — Agent 仓内协作约束（Grape dev 仓）

本仓所有 DevOps Agent（vod/issue/coding/qa/sre，direct 与 worker 双路径）共享以下**可执行约束**。违反 = 已复盘过的线上缺陷（Issue #3482 / #711 / #2245）。

## 1. 工位时序（Issue #3482 根因，强约束）

**先建 worktree，后物化技能。**

- direct 路径：`direct_executor._execute_agent_inner` 必须**先**调 `repogit.ensure_issue_worktree()` 拿到工作区，**再**调 `_materialize_workdir_and_skill_gate()`（`.skills`/`.project-info`/`.knowledge` 物化 + `_skill_consistency` 校验）。
- worker 路径：`agent._execute_task_impl` 同序（#711 方案 A）。
- 物化会把 `.skills` 等目录写进工位：先物化后建树 → 目标目录「已存在且无 `.git`」→ `git worktree add` 报 `fatal: '<path>' already exists`。
- **读侧随写侧**：消费 `evt["_skill_materialize"]` / `evt[SKILL_CONSISTENCY_KEY]`（agent_logs 标注）的代码必须在 `_materialize_workdir_and_skill_gate()` 物化**之后**执行——写在物化之前恒得空值，等于静默丢失审计标注（QA 第二轮复审 Required）。
- 回归看护：`tests/runtime/test_issue_3482_workspace_order.py`（源码顺序断言 + 行为级 fail-closed 用例），改顺序前先跑它。

## 2. direct 执行入口唯一实现（Issue #699 Phase 3 收尾）

`_execute_agent` / `_execute_agent_inner` / `start_direct_agent` / `_DIRECT_POOL` 的**唯一实现在 `src/domains/runtime/engine/direct_executor.py`**。

- `src/domains.runtime.pipeline.event_pipeline` **不得**再定义同名副本/第二套池/影子锁（副本会让旧入口跑过期实现、与唯一实现双轨分叉）。
- 新代码一律 `from src.domains.runtime.engine.direct_executor import ...` 直连；`src.api.event_pipeline` 兼容层仅为旧 import 路径 re-export 同对象。
- 看护：`tests/runtime/test_issue_596_p0_direct_executor_migration.py`、`test_issue_699_phase3_direct_executor.py`、`test_issue_3482_workspace_order.py::test_pipeline_exec_agent_delegates_to_direct_executor`。

## 3. 工作区失败语义（fail-closed，Issue #3482 C）

需要 git 工作区却没建起来（`ensure_issue_worktree` 返回 None）= **任务失败**，不是「只读会话」：

- direct：`fail_task` + `_fail_event(code=workspace_unavailable)`；
- worker：返回 `{"_error": ..., "_started": False}`；
- 禁止 fail-open 以只读 `completed` 收尾。只读语义仅覆盖「gitdir 存在但不可写」；`git dir not found` 标 `GRAPE_GIT_WORKSPACE_MISSING`，不置 `GRAPE_GIT_READONLY`。

## 4. 只读判定与降级（Issue #2245 / #3482 D）

- 判只读只用 errno 判据 `repogit.readonly_errno_reason(target, base)`（EROFS/EACCES/EPERM），**同时探测目标目录与共享基线**（`repo_base_dir`）；git stderr 文案只作日志线索，禁止据此降级。
- 「共享基线只读、目标目录可写」是 #2245 支持的部署形态 → 必须降级到会话级 gitdir（`_create_worktree_with_session_gitdir`）建树，不得退化为 `workspace_unavailable` 整任务失败。
- 其它失败（如分支被其它 worktree 占用）禁止降级（会制造同分支双工作树）→ 显式失败。

## 5. 删除同名副本前先盘点（Issue #3482 第四轮复盘）

删除迁移遗留的同名旧副本（如 event_pipeline 旧 `_execute_agent` 副本）前，必须机械盘点并随迁：

- **收尾钩子清单 diff**：逐条比对旧副本与唯一实现的收尾钩子（如 `finalize_issue_to_pending_review(source="exec")` 末节点人工闸），漏迁 = 三入口（a2a_dispatch/events redispatch/webhook）行为断链、direct 与 worker 口径分叉（#3482 第四轮 Required）。
- **源级守卫引用**：用 `rg "getsource.*<模块>" tests/` 类机械检索扫**对被删符号做 `inspect.getsource` 断言的测试**（如 `tests/kernel/test_d37_token_resolution_unify.py` 脱敏守卫），守卫生效面必须随实现迁移指向唯一实现，且该测试文件须补入影响面清单。
- **旧 import 路径可用性**：兼容层 re-export 不得依赖 import 顺序——canonical 模块用 PEP 562 `__getattr__` 惰性转发（或模块级显式 re-export），保证新进程直接 `from canonical import <旧名>` 不抛 ImportError（TC-3.2 源级守卫同时保持绿）。

## 6. 测试与门禁

- 改动 `src/domains/runtime/engine/`、`src/domains/runtime/pipeline/` 后运行：`python3 -m pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py tests/runtime/test_issue_2245_git_writable.py -q`，并跑 `make lint` 与 `python3 scripts/check-arch.py`（R5 禁止新增模块级环）。
- 影响面清单须按「引用被删符号 + 源级守卫（getsource 断言）」机械检索生成，不靠人工记忆圈定（第四轮 D37 守卫漏检教训）。
