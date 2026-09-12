# Issue #45 任务执行规划（设计 → 实现 → MR）

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature | P1 | 状态：plan-ready
> 分支：`weekly-02`（实现分支建议 `weekly-02-lyz` 逐任务合入，见 §八）
> 日期：2026-09-08
> 范围：把 issue-45 四份设计文档转换为**可执行任务清单**，按依赖排序、明确每阶段退出标准。
> 输入：`issue-45-api-token-design.md` / `issue-45-openapi-api-design.md` / `issue-45-cli-design.md` / `issue-45-git-repos-api-design.md`（`skills-api-scan` 为迁移对照基线）。

## 一、交付物总览

| 交付物 | 形态 | 依赖 | 优先级 |
|--------|------|------|--------|
| A. Token 认证内核 | `auth_token.py` + 两表 + `deps` 三选一 | 无 | P0 |
| B. OpenAPI 对外暴露 | 开启 `/docs` + `openapi.py` + 对外路由切换 | A（security 依赖） | P0 |
| C. 会话 token 生命周期 | enqueue 签发 → env 注入 → 会话结束删除 | A | P0 |
| D. Git 仓库统一接口 | `git_repo_api.py` + `/git-credential` `/workspace` | A | P0 |
| E. CLI 独立包 | `cli/` 契约驱动命令 + git 透传 | B + D | P1 |
| F. 管理面 + 迁移 | Settings「API Tokens」面板 + 技能迁移 | A + D | P1 |

## 二、实施拓扑（依赖关系）

```
A Token 内核 ─┬─> B OpenAPI 暴露 ──> E CLI 包（读 /openapi.json）
              ├─> C 会话 token 生命周期（签发/失效）
              └─> D Git 仓库统一接口 ──> E（/git-credential）/ F（技能迁移）
F Settings 面板 ──> A（api_tokens 管理）
```

- **串行关键线**：A → B/E（CLI 依赖契约活起来）与 A → D（CLI git 依赖 credential 端点）。
- **可并行**：C 与 D 在 A 落地后可并行；F-技能迁移可在 D 冒烟后并行做；
- **建议实现顺序**：M0(A) → M1(B) → M2(C) ∥ M3(D) → M4(E) → M5(F)。

---

## 三、里程碑与任务分解

### M0：Token 认证内核（P0，无前置）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 0.1 | 建表 `session_api_tokens`（含 `session_key`/`git` 注释口径见 token 设计 §2.3）+ `api_tokens`（§3.2），随 `init_db` 迁移 | `src/api/db_setup.py` | 幂等建表；存量库升级不报错 |
| 0.2 | 新增 `auth_token.py`：session token issue/verify/revoke（`tk_`，绑定 repo_key+agent_type，chat 带 user_id，`session_key` 定位）+ api key generate/verify/revoke/list（`grape_`，sha256 落库） | `src/api/auth_token.py`（新） | T1-T9 用例通过（token 文档 §七） |
| 0.3 | `deps.py` 新增 `get_current_user_or_api_key`：三通道取 token（Bearer / PRIVATE-TOKEN / access_token）→ 前缀分流（`tk_`→session / `grape_`→api_key / 其余走 JWT），401 语义与现有 `get_current_user` 一致 | `src/api/deps.py` | JWT 会话不受影响；三通道均可达 |

**退出标准**：`tests/test_auth_token.py`（新建）全绿；644 起驱动 `pytest tests/test_auth_routes.py tests/test_api_routes.py` 回归绿。

> 依赖注记：0.3 依赖 0.2；0.1/0.2 可先合。

---

### M1：OpenAPI 对外暴露 + 对外路由切换（P0，依赖 M0）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 1.1 | 开启 `docs_url/redoc_url/openapi_url`（当前 L244-246 关） | `src/api/main.py` | `/docs` `/redoc` `/openapi.json` 可访问 |
| 1.2 | 新增 `openapi.py`：`custom_openapi` 注入三通道 securitySchemes；遍历 `app.routes` 收集带对外依赖的 operationId（`get_current_user_or_api_key`）；过滤内部路由（`get_current_user`）；`version="2.1.0"` | `src/api/openapi.py`（新） | 契约仅含 8 个对外模块端点；operationId 完整；内部路由不出现在 arena |
| 1.3 | 对外路由认证切换：tasks/repos/agents/chat/skills/scenes/knowledge/events 的 Depends 换 `get_current_user_or_api_key` | 对应 `src/api/routes/*.py` | 三通道调用均通过；管理类仍仅 JWT |
| 1.4 | 对外接口 declaration 规范补漏：缺 `tags`/`operation_id`/`response_model`/`summary` 的端点补齐（契约友好，openapi 设计 §3.5） | 对外 routes | 契约中每个 operation 有 operationId + summary |

**退出标准**：`tests/test_openapi.py`（新建）：契约含安全方案、仅对外路由、operationId 全、版本为 2.1.0；三选一认证对 8 模块抽样通过。

---

### M2：会话 token 生命周期（P0，依赖 M0）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 2.1 | 任务入队时签发：`enqueue_task` 统一签发（session_key=task_id）+ `env_extra["GRAPE_SESSION_TOKEN"]` 注入 | `src/runtime/service.py`（L311 enqueue_task，统一覆盖 pipeline/direct/chat 各入队路径） | enqueue 后 `session_api_tokens` 有行；`GRAPE_SESSION_TOKEN` 进任务 env |
| 2.2 | chat 会话签发：enqueue 时按 env USER_ID 绑定 user_id（repo_key=`chat:{uid}`，agent_type=chat）；direct 模式 `_execute_agent_inner` 按 task_id 或 `d_{seq}` 签发 | `src/runtime/service.py` / `src/api/event_pipeline.py`（_execute_agent/_execute_agent_inner） | chat 会话 token 带 user_id；direct 执行结束失效 |
| 2.3 | 会话结束失效：任务终态（complete/fail/cancel 经 `update_task_status`）按 task_id revoke；direct finally revoke | `src/runtime/service.py` update_task_status / `src/api/event_pipeline.py` | 再调用 401；并发同类会话不受影响（T3/T5/T8） |
| 2.4 | TTL 兜底：过期行清理（签发时 `expires_at`，main 活跃追踪循环每 5 分钟 purge） | `src/api/main.py` / `auth_token.py` | 过期 token 校验 401；脏数据可收敛（T4） |
| 2.4 | TTL 兜底：过期行清理（签发时 `expires_at`，启动/周期任务 PURGE） | `auth_token.py` | 过期 token 校验 401；脏数据可收敛（T4） |

**退出标准**：会话生命周期用例（token 文档 T1-T5、T8）全绿；`test_executor_token_env.py` 回归（devops 不注入 USER_TOKEN，仅 GRAPE_SESSION_TOKEN）。

> 注：`enqueue_task`（`src/runtime/service.py` L311）已支持 `env_extra` 透传，无需改签名，仅需在 2.1/2.2 的组装处新增字段。

---

### M3：Git 仓库统一接口（P0，依赖 M0）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 3.1 | 新增 `git_repo_api.py`：白名单 14 类资源端点 dispatch（provider 头/query → 复用 `repo_manager` gitcode client），出参/入参对齐 `docs/openapi/10-git-repos.md` | `src/api/git_repo_api.py`（新） | 白名单端点可用；白名单外透传被拒（401/404） |
| 3.2 | 挂载路由 + provider 判定 + 认证（对外 Depends） | `src/api/main.py` / `src/api/git_repo_api.py` | `/api/git-repo/{o}/{r}/...` 三通道可调 |
| 3.3 | `GET /api/git-repo/{o}/{r}/git-credential`：短期平台凭据（TTL 600s、优先 bot token、回退调用者 token、可审计）；`GET /api/git-repo/{o}/{r}/workspace` | `git_repo_api.py` | git-credential 返回 username/password/clone_url/expires_in；CLI 进程内使用 |
| 3.4 | **收敛现状泄露点（随 F-2 技能迁移同步做，推迟到 M5 同批）**：devops 不再注入 `BOT_GITCODE_TOKEN` env + askpass 废弃。原因：技能仍引用 `$BOT_GITCODE_TOKEN`，单独移除会中断执行 | `src/agent/executor.py`（L209 `_assemble_env`）、`src/runtime/repogit.py`（L168 askpass） | 技能迁移完成后移除 env，`test_executor_token_env.py` 断言 devops 无 GITCODE token |

**退出标准**：`tests/test_git_repo_api.py`（新建）provider 分派、白名单、credential 一次性全通过；devops 任务 env 无 GITCODE token 暴露。

> 3.4 与技能迁移（F-2）有协同：先改技能引用到 `/api/git-repo`，再移除 env 注入，避免执行中断。

---

### M4：CLI 独立包（P1，依赖 M1 + M3）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 4.1 | `cli/` 骨架：独立 `pyproject.toml`（name=`grape`）、`grape_cli/` 入口、config/client（配置优先级、三通道认证） | `cli/`（新） | `pip install .` 后 `grape --help` 可用；A1-A5、A2b 用例绿 |
| 4.2 | `contracts.py`：`/openapi.json` 拉取 + 缓存（`~/.grape/openapi-<version>.json`）+ 版本检测 + `refresh` | `cli/grape_cli/contracts.py` | H1-H6 用例绿 |
| 4.3 | `registry.py`：operationId → typer 命令动态注册（tag/动词拆分、query/path/body/security 参数映射、`--json`） | `cli/grape_cli/registry.py` | R1-R9 用例绿（含 **R7 新增契约零 CLI 改动**） |
| 4.4 | 帮助系统：`--help` / 组 help / `--list` / `commands` / `--version` | `registry.py`/`cli.py` | H7-H13 用例绿 |
| 4.5 | `commands/gitrepo.py` 说明 | git-repo REST 命令由**契约自动生成**（`gitrepo_*` → `git-repo` 组 + 嵌套动词链），无需手写（cli 设计 §二）；仅 `git` 手写 | `cli/grape_cli/registry.py` | `grape git-repo issues list --owner o --repo r` 可用 |
| 4.6 | `commands/git.py`：git 原生命令透传（`ignore_unknown_options`）+ `/git-credential` 进程内注入（GIT_CONFIG env，base64，不进 argv/磁盘） | `cli/grape_cli/commands/git.py` | C1-C7 用例绿（**C3/C4 token 零暴露为关键验收**） |
| 4.6 | `commands/git.py`：git 原生命令透传 + `/git-credential` 进程内 credential helper（token 不进命令行/argv/磁盘/env） | `cli/grape_cli/commands/git.py` | C1-C7 用例绿（**C3/C4 token 零暴露为关键验收**） |
| 4.7 | `api` 兜底命令（白名单验证方法/路径） | `cli/grape_cli/commands/api.py` | D4 白名单用例 |

**退出标准**：CLI 测试 A-E 全绿（cli 设计 §五）；对本地 mock 服务端到端可用（E2）。

---

### M5：管理面 + 技能迁移 + 收尾（P1，依赖 M0/M3）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 5.1 | Settings「API Tokens」面板：用户级 API Key 列表/创建（明文仅一次）/吊销/重命名；**仅方式 B**，会话认证，`/docs` 不暴露（内部路由） | `src/api/routes/settings.py` + 前端 | 创建→查列表→吊销闭环；无自举 create/revoke 的 public 端点 |
| 5.2 | 技能迁移：`gitcode-api-reference.md` `api.gitcode.com` → `{{SERVER_URL}}/api/git-repo/...`、`$BOT_GITCODE_TOKEN` → grape token；逐技能按 `issue-45-skills-api-scan.md` 清单核对 | skills 文档/技能包 | 技能不再直连 gitcode 平台；问答无 `api.gitcode.com` 引用 |
| 5.3 | 全量回归 + 覆盖率：`pytest tests/ -v` + `--cov`（行覆盖 ≥ 70%） | `tests/` | 全绿；覆盖达标 |
| 5.4 | 文档收口：`docs/openapi/` 按新契约同步；README/安装说明（CLI） | docs | 口径与实现一致 |

**退出标准**：issue-45 四份验收清单全部勾满。

---

## 四、实施顺序与并行建议

```
M0（单线，含 3 个 commit 上限）→ 合入
  ├─ M1 → 合入
  │     └─ M4（CLI，依赖契约活）——并行时用 M1 合入后起点
  └─（M0 后）M2 ∥ M3（可并行，互不阻塞）
        └─ M3 冒烟 → M4 的 git 部分可并行
M5 收尾（技能迁移与 3.4 协同，见 §三 M3 注）
```

- **每里程碑独立可合入**，避免大 PR；合入即回到 `weekly-02` 基线，供其他人消费（尤其 M1 的契约）。
- M3 的 3.4 与 F 技能迁移必须**同批推进**（先改引用后撤 env），中间态会触发技能 401。

## 五、关键接缝（实现时需对齐）

| 接缝 | 对齐方 |
|------|--------|
| 对外路由判定 | `openapi.py` 用 Depends 函数身份区分对外/内部——对外路由**禁止**再引 `get_current_user`，否则契约漏收录或误暴露 |
| `session_key` 语义 | token 文档 §2.1：管理键 ≠ 授权身份；revoke 按 key 不按 (repo,agent) 宽删 |
| devops 无 user：chat 有 user | `GRAPE_SESSION_TOKEN` 注入点分别取 task_id / conv_id 作 session_key；user_id 处 0 或 chat 用户 id |
| CLI token 优先级 | `GRAPE_SESSION_TOKEN`（任务级）> `GRAPE_API_TOKEN`（用户级）——worker 内自动优先任务 token |
| git 透传 token 零暴露 | 只用进程内 credential helper / `GIT_CONFIG` env；禁 argv/落盘（C3/C4） |

## 六、风险与依赖协调

| 风险 | 缓解 |
|------|------|
| 开启 `/docs` 暴露内部端点 | openapi.py 只渲染带对外依赖路由；内部路由仍 JWT 会话 |
| 技能迁移未完成即撤 `BOT_GITCODE_TOKEN` env | 先改技能引用（F-2）→ 再撤 env（3.4）；中间态在 dev 环境验证 |
| `session_key` 与现有任务清理链路耦合 | 在 enqueue 返回/任务终态回调处集中 revoke，避免散落 |
| CLI 契约与实现不同步 | `version` 字段驱动拉新 + `refresh`；operationId 规范在 M1 统一补齐 |
| 覆盖率门禁 | 测试随里程碑建（每里程碑自带 tests），避免 M5 突击 |

## 七、MR 规划

按里程碑提 MR 到 `weekly-02`，标题示例：

```bash
create_mr.sh docs/issue-45-token-core      weekly-02 "feat(api): token auth core (M0)" "部 M0 Token 认证内核"
create_mr.sh feat/issue-45-openapi         weekly-02 "feat(api): openapi exposure (M1)" "Closes #45 子任务 M1"
create_mr.sh feat/issue-45-session-token   weekly-02 "feat(api): session token lifecycle (M2)" ...
create_mr.sh feat/issue-45-git-repos       weekly-02 "feat(api): git repos unified api (M3)" ...
create_mr.sh feat/issue-45-cli             weekly-02 "feat(cli): contract-driven CLI (M4)" ...
create_mr.sh feat/issue-45-settings-migrate weekly-02 "feat(ui): api tokens panel + skill migration (M5)" ...
```

> 每个 MR 附带该里程碑的退出标准自检（可勾选清单），便于评审对照设计文档验收。

## 附：涉及文件速查

| 类型 | 路径 |
|------|------|
| 修改 | `src/api/main.py`、`src/api/deps.py`、`src/api/db_setup.py`、`src/api/event_pipeline.py`、`src/api/routes/chat.py`、`src/api/routes/settings.py`、`src/agent/executor.py`、`src/runtime/repogit.py`、对外 8 个 routes |
| 新增 | `src/api/auth_token.py`、`src/api/openapi.py`、`src/api/git_repo_api.py`、`cli/`（含 `grape_cli/*`）、`tests/test_{auth_token,openapi,git_repo_api,cli_*}.py` |