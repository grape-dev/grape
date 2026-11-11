# 扫雷⑨：对象级权限（authz）审计报告

范围：`src/api/routes/*.py` 全部写端点（POST/PUT/DELETE/PATCH）+ 对象级读端点。
方法：逐端点核对鉴权依赖（`require_no_session_token` / `get_current_user_or_api_key` / `require_org_member` / `require_jwt_only`）+ 函数体内归属校验（`check_repo_access`、owner/created_by 比对、`_require_runtime_owned`）。

**实弹探测未能执行**：`example-user-a.token` / `example-user-b.token` 内容为占位符（`eyJhbG...oEIk`，非真实 JWT），调用 `/api/auth/me` 返回 401；且本机库 `grape-drill-db（自建演练库名，见部署配置）.grape_accounts` 中不存在上述测试账号（库内仅若干本地测试账号，已脱敏）。遵守不猜测凭据的规则，全部结论基于代码级审计。

## 鉴权模型速记

| 依赖 | 实际含义 |
|---|---|
| `require_no_session_token` | 任何 JWT / grape_ API Key 均放行（仅拦 tk_），**不含任何对象归属校验** |
| `get_current_user_or_api_key` | 三通道认证通过即放行，同样不含归属校验 |
| `check_repo_access(user, repo_key)` | JWT 用户必须 managed_repos 归属该仓；tk_/api_key 跳过（作用域限定） |
| `require_org_member` | 解析 org 上下文 + 成员资格；个人组织回退 → 无 org 过滤 |

---

## 越权矩阵（按风险降序）

### 🔴 高危

| # | 端点 | 对象 | 现状 | 可利用性 |
|---|---|---|---|---|
| H1 | `GET /api/repos/{owner}/{repo}/project-info` | 仓库项目知识库（**全文文件**） | `get_current_user_or_api_key`，仅 session_token 有 repo scope 比对；**JWT/API Key 无任何归属校验**（代码注释自认"JWT 维持现状，依赖 x-token-scope 的 user 通道"） | 任意登录用户传任意 owner/repo 即可拉取别家仓库全部知识基线文件。repos.py:2232-2250 |
| H2 | `POST /api/settings/runtime-credentials/{cid}/revoke` | 他人 Worker 凭据 | `require_no_session_token` 后**直接删除**，无 owner 比对（同文件 scope 端点有 `_owner != _uname → 403`，rename/revoke 漏配） | 任意用户枚举/传入 cid → 删除他人凭据 + 级联删引擎行、清场景 engine_runtime_ids。settings.py:2018-2062 |
| H3 | `PUT /api/runtime/runtimes/{runtime_id}/tags`、`/max-concurrent`、`/scene-tags`、`/alias`；`GET /runtimes/{runtime_id}/...` | 他人引擎（agent_runtimes） | `require_no_session_token` + 仅查 runtime 存在，无归属校验（runtime.py 有 `_require_runtime_owned` 但只用于 rta_ worker 通道） | 任意用户可改任何引擎的并发上限/标签/别名（max_concurrent 可设 1 → 打瘫他人引擎；tags 影响任务路由）。runtime.py:1866-2005 |
| H4 | `GET /api/runtime/admin/event-bus`、`/admin/machine-health[/{rid}]`、`POST /admin/batch-recover` | 全局 admin 面 | 名为 admin，实为 `require_no_session_token`（普通 JWT 即过）；batch-recover 触发全局 stale 任务恢复 | 任意用户可读全局机器健康/事件总线、触发全局恢复操作。runtime.py:527-572 |
| H5 | `GET /api/task-logs/{task_id}/info`、`/messages`、`/stream` | 任意任务 | `require_no_session_token`，按 task_id 直接查，无归属校验 | 泄露任意任务完整 prompt（8000 字）、引擎 last_ip、owner_name、repo_key。task_logs.py:35-102 |

### 🟠 中危

| # | 端点 | 对象 | 现状 | 可利用性 |
|---|---|---|---|---|
| M1 | `GET /api/issues/{issue_id}/status`（GET） | 任意 issue | `get_current_user_or_api_key`，无 owner/repo 校验（同组 PUT/start 均有 `row[0]!=user["id"]→403`，唯独 GET 漏） | 读任意 issue 的 repo_key/编号/标题/状态（信息侦察跳板）。issues.py:516-532 |
| M2 | `GET /api/repos/{owner}/{repo}/scene-recommend-config` + `PUT`（写） | 仓库推荐配置 | `require_no_session_token`，repo_key 直接解析落库，无 `check_repo_access` | 任意用户可**改写**别家仓库的场景推荐路由配置（写操作）。repos.py:2284-2324 |
| M3 | `GET /api/scenes/{scene_id}` + `PUT` / `DELETE /{scene_id}`（`repo=` 参数） | 仓库场景 | 归属粒度=repo_key 而非 user；`repo` 为可选参数且缺省回退 `app_current_repo()`，未校验调用者对该 repo 的托管权 | 传 `?repo=他人仓库` 即可改/删他人仓库场景（`_validate_scene_engine_ids` 不做权限）。scenes.py:1813-1893 |
| M4 | `PUT /api/scenes/{scene_id}/advanced-options`、`/deploy-url`（`repo=` 参数） | 仓库场景配置 | 同 M3，无 repo 归属校验 | 跨仓改写场景部署地址/高级选项。scenes.py:2180-2420 |
| M5 | `POST /api/scenes/apply/{owner}/{repo}`、`GET/POST /branches/{owner}/{repo}` | 他人仓库 | `require_no_session_token`，未调 `check_repo_access` | 对任意 managed 仓库应用场景 / **创建分支**（写平台远端仓库）。scenes.py:1079-1211、1895 |
| M6 | `GET /api/chat/image/{conv_id}/{filename}` | 他人会话图片 | **完全无鉴权**（注释自认"No auth"，靠文件名不可猜） | conv_id 可枚举/泄露即读图。chat.py:114-151 |
| M7 | `GET /api/observability/tokens/tasks`、`/tokens/issues/{issue_id}/stages` | 全局 token 用量 | `require_org_member`，个人组织（默认）时 `_filter_org_id=0` → 无 org 过滤 | 个人组织用户可看**全平台** token 消耗/任务明细（dashboard `/ops/*` 同型：overview/timeline/error-logs 个人组织无过滤）。observability.py:635、dashboard.py:111 |
| M8 | `GET /api/repos/bot-token/{owner}/{repo}`、`GET /deploy-url/{owner}/{repo}` | 他人仓库 | `require_no_session_token`，无归属校验（PUT 方向有 check_repo_access ✓） | 探测别家仓库 bot token 存在性 / 读 deploy-url。repos.py:1744、2012 |
| M9 | `GET /api/repos/{owner}/{repo}/workspace` | 服务器路径 | `get_current_user_or_api_key` 无归属校验 | 泄露任意仓库的服务器 workspace 绝对路径。repos.py:2472 |
| M10 | `PUT /api/settings/gitcode-repo/{owner}/{repo}`（写） | 仓库配置 | `require_no_session_token`，未见 repo 归属校验（GET 同） | 跨仓改写 gitcode 仓库配置。settings.py:778/834 |
| M11 | `POST /api/knowledge/{knowledge_id}/refs`、`/refs/batch`、`PUT /ref/{ref_id}` | 知识引用 | `require_no_session_token`，无创建者/仓库校验（knowledge 本体 PUT/DELETE 有 `created_by` 校验 ✓，refs 系漏配） | 给他人知识条目挂引用/改引用。knowledge.py:701/763/847 |

### 🟡 低危 / 备忘

| # | 端点 | 说明 |
|---|---|---|
| L1 | `POST /api/repos/bot-token`（PUT） | check_repo_access ✓，但 L1778 `_is_admin = ... or auth_type in (session_token, api_key)` 把 **API Key 一律当 admin**，绕过"仅托管人"的补充校验 |
| L2 | `POST /api/runtime/tasks/{task_id}/cancel`、`/converge-event`、`GET /tasks/{task_id}/status` | `require_no_session_token`，task_id 直接操作（用户侧触发路径未验归属；task 属 agent_tasks 全局命名空间） |
| L3 | `GET /api/repos/{owner}/{repo}/git-credential` | tk_ 会话才可用 ✓，但仅比对 session token 的 repo 前缀时**未比对**（`_repo_platform_token` 只验 auth_type==session_token，不比对 user.repo_key 与路径仓）→ 绑 A 仓的 tk_ 可换 B 仓凭据 |
| L4 | `GET /api/patrol/findings` | 全局巡检发现列表，`get_current_user`，无 scope 过滤（泄露他人仓库问题） |
| L5 | 组织接口（orgs.py） | 已按 Issue #381/#395 修复：PUT/DELETE/members 全部 `require_path_org_admin` 绑定路径 org_id ✓ |
| L6 | skills.py DELETE/update、knowledge PUT/DELETE、events terminate/redispatch、agents PUT/clean、autopilot CRUD、dashboard `/{owner}/{repo}`、repos remove/repair-host/migrate | 均有归属/`check_repo_access` 校验 ✓ |

## 正面清单（校验到位的模式，可作修复范本）

- `check_repo_access()`（deps.py:573）：managed_repos 归属 + 双前缀候选 + tk_/api_key 作用域豁免。
- `require_path_org_admin()`（Issue #381/#395）：路径绑定型组织鉴权。
- `_require_runtime_owned()`（runtime.py:593）：runtime 凭据归属 fail-closed。
- skills/knowledge 的 `owner_id`/`created_by` 比对。

## 修复建议（按序）

1. **H1**：project-info 对 JWT/API Key 增加 `check_repo_access(user, repo_key)`（或明确产品口径为公开知识库并去掉文件全文）。
2. **H2/H3/H4**：runtime-credentials rename/revoke 补 `_owner == username` 校验；runtime 标签/并发/别名端点补 runtime 归属（org owner 或 credential owner）；`/admin/*` 改 `require_admin` 或至少 require_org_member + org 过滤。
3. **H5**：task_logs 三端点按 task.repo_key 走 `check_repo_access`。
4. **M 组**：scenes/issues/knowledge-refs/settings gitcode-repo 统一补 `check_repo_access`；chat image 加会话签名或登录态。
5. 收敛 `require_no_session_token` 的语义误导：它不是"管理端点鉴权"，仅是"tk_ 禁用"——大量端点误把它当权限墙。
