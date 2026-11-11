# Issue #699 Phase 1 需求规格（spec）

> 依据：docs/weekly-09/grape-unified-implementation-design.md（v2.1）§3.3/§3.7 域归属对照表、§12 Phase 1 步骤、§14 TC-1.1~TC-1.6。
> 基线：feat/issue-699（Phase 0 已完成，PR !836）。

## 目标

将 `src/` 重组为「kernel 底座 + 三域四层」的物理结构，并保持**旧 import 路径全部可用**（re-export 兼容层）：

1. 提取 `src/kernel/`：18 个顶层共享模块（设计文档表格列出全部）+ `auth_sdk/`（jwt_utils / auth_token / cookie_utils）+ `db_migrations/` 空壳。
2. 域内聚 `src/domains/{mgmt,runtime,insight,contracts}/`：按 §3.7 域归属对照表整包迁移（auth/user/admin → mgmt；runtime/agent/im/vcs/storage + event_pipeline → runtime；observability → insight；contracts 空壳）。
3. 路由文件按域归入 `domains/*/routes/`（§3.7 映射的 29 个路由文件）。
4. `src/` 顶层旧路径创建 re-export 兼容 shim（外部/历史调用方零破坏）。
5. 新建 `scripts/check-arch.py`（R1~R6 架构约束 + 白名单）+ `tests/arch/` 守卫。

## 范围（以 §3.7 域归属对照表为权威）

| 类别 | 迁移项 |
|------|--------|
| kernel 模块 | config, session_manager, lock, repo_key, repo, repo_cleanup, repo_migrate, mysql_compat, token_service, token_resolver, prompt_builder, worker_package, localfs, outbound_ledger, scene_custom_env, featured_order, help_docs_sync, help_page_gen |
| kernel/auth_sdk | src/api/jwt_utils.py, src/api/auth_token.py, src/auth/cookie_utils.py |
| mgmt | src/auth/（其余）, src/user/, src/admin/ + routes: auth, grape_auth, orgs, settings, feedback, scenes, skills, templates, repos, community, feishu_onboard, feishu_card, autopilot, agents, devstage |
| runtime | src/runtime/ → engine/, src/agent/ → agents/, src/im/, src/vcs/, src/storage/, src/api/event_pipeline.py → pipeline/ + routes: webhook, events, issues, runtime, tasks, chat, pipeline, task_logs |
| insight | src/observability/ + routes: dashboard, observability, search, latency, health |
| 不迁移 | src/api/ 其余文件（main/routing/deps/event_store/…，Phase 4 处理）、src/api/routes/knowledge.py（对照表未映射）、src/project_info/ |

## 关键决策

1. **旧路径全量保留为 shim**（`from <canonical> import *` + `globals().update`）：仓库内部 import **全部改写为 canonical 新路径**（避免 shim 双实例 / 部分初始化环 / mock.patch 打在 shim 上失效三类隐患），shim 仅供外部兼容与验收用例（TC-1.5）。
2. **worker 入口不变**：worker 仍以 `python -m src.runtime.agent` 启动；`src/runtime/agent.py` shim 额外透传 `main()` 与 `__main__` 块，worker_package MANIFEST 在保留旧条目（现为 shim）基础上新增 `src/kernel/`、`src/domains/`。
3. **`__file__` 相对路径修正**：kernel 化后模块深度 +1，config/session_manager/mysql_compat/worker_package/featured_order/help_docs_sync/help_page_gen 的根目录推导同步 +1 层，语义不变。
4. check-arch R1~R6：R1 kernel 禁 import domains、R2 kernel 禁 import src.api、R3 domains 禁 import apps（预留）、R4 跨域 import 白名单管控（当前存量较多，先报告+白名单，Phase 5 收口）、R5 kernel/domains 无 import 环、R6 kernel/domains 禁用旧路径 shim import。R1/R2/R5/R6 为硬红牌（exit 1）。

## 验收（TC-1.1~TC-1.6）

- TC-1.1 `from src.kernel.config import log` 与 `from src.config import log` 均可用
- TC-1.2 `src/kernel/auth_sdk/jwt_utils.py` 存在且 JWT 生成/验证正常
- TC-1.3 domains/{mgmt/auth, runtime/engine, insight/observability} 就位，`from src.api.main import app` 可加载
- TC-1.4 `python scripts/check-arch.py` 运行并输出 R1~R6 报告，白名单生效
- TC-1.5 `from src.auth.audit import *`（旧）与 `from src.domains.mgmt.auth.audit import *`（新）均可用
- TC-1.6 `pytest tests/` 全量通过率 ≥ Phase 0 基线（100 passed），无新增 import 错误
