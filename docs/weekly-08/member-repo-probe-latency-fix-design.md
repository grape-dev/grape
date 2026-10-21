# 方案：/repos/member 接口时延优化 —— 仓库探活从请求路径脱离

> 状态：**已实施（TDD：红→绿）** ｜ 分支：`weekly-08-lyz`
> 关联：`/api/repos/member` 在仓库规模较大时耗时数秒（实测 `167` 托管仓库 ≈ `3.5s`，缓存命中仍慢）
> 敏感信息：本文仅含占位符（`<xxx>`）与代码标识符（文件:行/函数名），不含任何真实 IP、域名、口令、密钥、账号、仓库名。

---

## 〇、问题现象（Observed behavior）

- `/api/repos/member`（仓库列表/托管大堂）在**托管仓库规模较大**的环境下，接口耗时稳定在数秒级；
  实测某环境（单账号名下托管仓库较多）：
  - 单平台 `gitcode`：≈ `1.6s`
  - 默认全平台（前端默认路径）：**≈ `3.5s`**
  - **缓存命中时仍 ≈ `3.2~4.0s`**（说明慢点不在 VCS 全量拉取）
- cProfile 定位：耗时集中在 `_probe_stale_repos` 内部的 `concurrent.futures.as_completed(...)`（并发探测 webhook）+ 主请求对后台线程/事件循环的等待。
- 该函数**执行完整性/返回结果无人消费**（调用处 `return []`），仅为"清理失效托管记录"。

### 位置（当前分支 `weekly-08-lyz`）

| 项 | 位置 |
|----|------|
| 探活函数 | `src/api/routes/repos.py:144` `_probe_stale_repos` |
| 并发探测实现 | `repos.py:214-216` `ThreadPoolExecutor` + `as_completed(timeout=15)` |
| 请求内触发点 | `repos.py:310-326`（member 缓存路径外触发）与 `repos.py:453-464`（缓存过期路径触发） |

> 该缺陷在 `main` 分支同源存在（`repos.py:143` / `:216` / `:322,463`），本次先在 `weekly-08-lyz` 修复后评估同步。

---

## 一、目标语义（Target semantics）

| 项 | 现状 | 目标 |
|----|------|------|
| 请求延迟 | 缓存命中仍 `3.2~4.0s`（等探活完成） | **缓存命中时请求纵向切面秒级内返回**（探活完全脱离请求生命周期） |
| 探活触发 | 每次 `/member` 都启动，且结果回传到请求路径 | **真正 fire-and-forget**：发起即返回，后端后台线程自行完成，请求不等它 |
| 探测范围 | 对**全部**托管仓库逐个 VCS 探活 | 仅探测**值得探**的托管仓库（见 Step 3 收敛条件），降低总耗时 |
| 探测频率 | 无频控，依赖接口被调用 | 增加**最小间隔节流**（同一用户短时间不重复全量探测） |
| 语义保持 | 失效托管记录清理（删仓库 webhook 兜底） | **行为不变**：仍会清理，只是异步完成 |

---

## 二、方案（Plan）

### Step 1：探活结果彻底脱离请求返回链（核心修复）

`list_member_repos` 内启动探活处（`repos.py:310-326` 与 `453-464`）改为**不等待、不 join、不取结果**：

- 用**独立常驻后台线程池/独立线程**执行探活，且**不在请求线程路径上调用** `as_completed().result()` / `future.result()`。
- 具体：把 `_probe_stale_repos` 的调用从「请求内 `threading.Thread(...).start()`」提升为**模块级后台调度器**（如独立 `Timer`/daemon 线程或每用户带锁的单飞线程），请求只做「登记一次异步任务」后立即返回。
- `as_completed` 必须在**非请求**线程内消费结果（探活本身用 `ThreadPoolExecutor(max_workers=...)` 内部并发即可；宿主线程只管启动）。

> 关键约束：**任何 `as_completed`/`future.result()` 不得出现在请求链路的栈上**（cProfile 已证明这就是阻塞源）。

### Step 2：缓存命中路径不再为探活取 token

`repos.py:310-322` 当前在**缓存命中**分支也先 `with_oauth_token` 同步取 token（供探活），再判定缓存。若 Step 1 将探活异步化，则该 token 预热应**推迟到后台任务内部**自行获取，请求路径不因探活触达 OAuth 刷新（避免叠加 token 刷新网络往返）。

### Step 3：探测范围收敛 + 节流（控制总工作量）

`_probe_stale_repos` 内：

1. **范围**：仅对 `webhook_id IS NOT NULL`（真实托管）的仓库探测；跳过仅 join/普通成员仓库；每仓探测前跳过「最近 `<PROBE_INTERVAL>` 内已探测」的记录（增量，而非全量每次全部探）。
2. **节流**：模块级 `{user_id: last_probe_ts}` 字典（带锁）+ 环境变量 `REPO_PROBE_INTERVAL`（建议默认 `<300>`s）——同用户短间隔内不重复全量探测。

### Step 4（可选，后续）同步 `main`

确认 weekly-08-lyz 修复稳定后，将同一手法应用到 `main` 分支 `repos.py:143` 等位置。

---

## 三、模块改动（Module changes）

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/api/routes/repos.py` | `list_member_repos`：探活触发改为「缓存过期后再异步启动」；新增 `_start_stale_probe_async`（节流 + fire-and-forget）；缓存命中不再启动探活取 token | ✅ 已实施 |
| `src/api/routes/repos.py` `_probe_stale_repos` | 保留后台线程并发探测（`as_completed` 已在后台线程内，请求栈不含）；范围收敛（仅 `webhook_id` 托管仓）延续；探测日志 `info→debug` 降噪；`REPO_PROBE_INTERVAL` 节流 | ✅ 已实施 |
| `src/api/routes/repos.py` 常量区 | `REPO_PROBE_INTERVAL`（env 可配，默认 `<300>`s）+ 模块级节流守卫 | ✅ 已实施 |
| `src/api/static/index.html` | 若改前端无必要（纯后端），可不改；此轮不涉及前端 | — |
| `tests/test_member_probe_deferred.py` | 新增 4 用例（UT-01/02/03/05，TDD 红→绿） | ✅ 已实施 |
| `main` 分支 | 后续同步（可选） | ⬜ 待实施 |

---

## 四、关键实现要点（Key implementation notes）

| 点 | 依据 |
|----|------|
| 阻塞源已实测定位 | cProfile：`concurrent.futures._base.as_completed` cumtime `6.9s`、`threading.wait` `6.2s`、`anyio.run_sync_from_thread` `3.5s`——均在 `/repos/member` 请求栈上 |
| 探活结果无人用 | `list_member_repos` 内探活调用包在 `_probe(...)` 返回 `[]`（`repos.py:313-314`），无需同步等待 |
| 线程特点 | `threading.Thread(daemon=True)` 仅保证**进程退出不阻塞**，不保证请求不等它；只要宿主线程 join/future 等待，事件循环就阻塞 |
| token 刷新叠加 | `get_user_token_with_refresh` 每次调用检查临期会同步 `refresh_oauth`（实测单次 `0.12~0.32s`），请求路径应避免为探活触发 |
| 并发参数 | 探测内 `ThreadPoolExecutor(max_workers=10)` 可保留（后台线程内并发），只要**宿主不在请求栈消费结果** |
| 兜底语义 | 失效托管清理仍执行（后台），`host_status`/webhook 检测逻辑（`dashboard.py:983`）不变 |

---

## 五、边界 / 风险（Boundaries / Risks）

| 项 | 说明 |
|----|------|
| 探活变为异步 | 页面打开瞬间"失效托管清理"可能稍后才落库；影响：同名重建仓库时极短窗口内前端仍显示"已托管"——可接受（原本也是尽力而为） |
| 后台线程生命周期 | 用模块级单飞 + 锁保证同用户并发时只执行一次；daemon 线程避免阻塞进程退出 |
| 节流失效 | 若 `REPO_PROBE_INTERVAL` 设 `0` 视为不节流（兼容）；默认启用 |
| 多实例 | 节流字典为进程内；多 worker 场景各自节流（可接受，非核心） |
| 缓存过期全量拉取 | 该路径探活同样后台化；`_fetch_and_cache`（VCS 拉 167 仓）本身仍可能 `1~3s`，但属缓存过期必要成本，与本次「缓存命中却慢」缺陷不同 |
| 与既有测试 | `test_api_routes` repo 相关用例若依赖同步清理，需按新语义显式修订（见六） |

---

## 六、测试用例设计（Test case design，TDD 先红后绿）

| 编号 | 用例 | 前置 | 断言 |
|------|------|------|------|
| UT-01 | 缓存命中时接口**即时返回**（不再等探活） | 预置 `user_repos` 缓存未过期 + 托管仓库多条 | `GET /api/repos/member` 完成时间 << 探活耗时（mock 探活耗时提高阈值也能秒回）；响应含 `_cached` |
| UT-02 | 探活异步：请求返回后仍执行清理 | mock `_probe_stale_repos` 记录调用 | 请求返回后，后台最终调用到探活（轮询断言），`host_status`/无效托管被清理 |
| UT-03 | 请求栈不含 `as_completed` | mock `concurrent.futures.as_completed` 抛错 | `/member` 请求不受影响（探活异常不冒泡到接口） |
| UT-04 | 缓存命中不取 token | mock `with_oauth_token` 计数 | 缓存命中路径 `with_oauth_token` 调用次数为 `0` |
| UT-05 | 节流生效 | 连续两次 `/member` | 两次请求只触发一次全量探测（`REPO_PROBE_INTERVAL` 内） |
| UT-06 | 范围收敛 | 托管仓库含「无 webhook_id」记录 | 探测文件仅覆盖有 `webhook_id` 的托管仓库 |
| UT-07 | 环境变量可配 | 设置 `REPO_PROBE_INTERVAL` 非常用值 | 节流阈值按环境值生效 |
| PERF-01 | 规模化回归（环境 2） | 部署后实测 | 缓存命中 `/member` 耗时应显著下降（目标到亚秒~1s 级） |

> **红阶段**（修复前）：UT-01/02/05 失败（缓存命中仍启动探活/取 token、无节流）；UT-03 通过（缓存过期触发探活）。
> **绿阶段**（修复后）：`tests/test_member_probe_deferred.py` 4/4 通过（UT-01/02/03/05）。
> 回归：`tests/test_lobby_managed_repos.py` 通过；`tests/test_api_routes.py` TestRepos 除 `test_host_no_permission`（pre-existing 401 测试凭据问题）外全过；`tests/test_token_scope_guard.py` 4 例失败为改动前既有（stash 对比确认）；TestKnowledge 16 例为 pre-existing baseline。

---

## 七、参考（References）

- `src/api/routes/repos.py:144-271`：`_probe_stale_repos`（范围/探测/并发）
- `src/api/routes/repos.py:279-393`：`list_member_repos`（缓存命中/过期路径）
- `src/api/routes/repos.py:310-326`、`:453-464`：探活触发点（`threading.Thread` + `with_oauth_token`）
- `src/api/oauth_token.py:49`：`with_oauth_token`（同步取 token/刷新）
- `src/api/deps.py` `get_user_token_with_refresh`：预刷新逻辑（临期触发 `refresh_oauth`）
- `src/api/routes/dashboard.py:983-1006`：webhook 存在性检测（探活清理的旁侧消费方）
- `docs/weekly-08/DOCUMENTATION-GUIDE.md`：本文遵守的文档红线与 TDD 约定