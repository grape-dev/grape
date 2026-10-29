# Issue #470 — 方案设计与任务计划

> 关联：`spec.md` | 分支 `feat/issue-470` ← base `local-liufei`

## 1. 总体设计

新增统一配置模块 `src/config.py` 内嵌（复用 CONFIG_DIR / log / read_server_config 基建）新增 `runtime-config.json` 读取层 + 管理台 meta 覆盖层，输出 5 个 getter：

```python
# src/config.py 新增（单一配置源，所有调用点收敛到这里）
RUNTIME_CONFIG_PATH = os.path.join(CONFIG_DIR, "runtime-config.json")

def get_runtime_task_timeout() -> int:        # 超时家族单一来源
    # env RUNTIME_TASK_TIMEOUT > meta runtime_task_timeout > runtime-config.json > 3600
def get_stuck_threshold_minutes() -> int:     # 派生：默认 ceil(timeout/60 * 22/3600)≈timeout*11/18000，回退 22
def get_orphan_threshold_minutes() -> int:    # 派生：默认 timeout 分钟数的 6 倍（3600s→360min），回退 360
def get_devstage_template_id() -> str         # env GRAPE_DEVSTAGE_TEMPLATE_ID? 不新增 env，用既有约定
def get_devstage_flavor() -> dict             # flavor_id/cores/memory/arch
```

**优先级（Spec 要求）**：env 覆盖 > 管理台值（DB meta）> 配置文件（runtime-config.json）> 代码默认值。
**默认值全部与现状一致**（3600s / 22min / 6h / 3c6g / 现有模板 ID / ARM）。

### 1.1 为什么不用 server-config.json

`chat_stream_timeout` 先例是「单一标量、无管理台入口」；本 Issue 明确要求**管理台可改**（meta 表），且超时家族 + DevStage 是两组语义，独立 `runtime-config.json` + `.example.json` 遵循 configs/ 既有约定（engine-selection.json 同模式），不污染 server-config 的部署基址语义。

### 1.2 worker 侧生效链路（远端 worker 无本地 DB/文件）

服务端 `enqueue_task`（`src/runtime/service.py`）把 `get_runtime_task_timeout()` 以 `RUNTIME_TASK_TIMEOUT` 键 setdefault 进 `env`（env_extra JSON 列）→ claim 下发 → worker `_execute_task` `env.update(env_extra)` → `stream_agent(env_extra)` → ACP `stream_execute(env=...)`：
- `agent.py:2340` 看门狗：改为 `env.get("RUNTIME_TASK_TIMEOUT")` 优先 → `os.environ` → 默认（新任务经服务端 setdefault 注入后任务级值生效，worker 本地 os.environ 仅在任务 env 无该键时兜底——本地直跑/旧任务，保持运维兜底能力）
- `hermes_acp._global_timeout_for` / `hwcloud_acp._global_timeout_for`：签名加 `env: dict | None = None`，`env 优先 → os.environ → 默认`（与同文件 `_silent_timeout_for(env)` 模式一致，#378/#416 先例）

worker 端 `runtime.env` 显式设置仅在任务 env 未注入该键时兜底（本地直跑/旧任务）；对服务端新派发任务，任务级注入值生效（管理台值经 enqueue setdefault 下发）。

### 1.3 服务端派生阈值

- `observability/__init__.py:462` `_STUCK_MINUTES = 22` → 模块级常量改为函数调用 `get_stuck_threshold_minutes()`（采样器 8s TTL，每轮读配置，管理台改后 ≤8s 生效）
- `dashboard.py:134` `timedelta(minutes=22)` → 同 getter
- `cleanup.py:23` `_LONG_RUNNING_MINUTES` 保留 env `SWEEPER_LONG_RUNNING_MINUTES` 兜底，默认值改从 getter 取（派生关系：6×timeout）

派生规则（不新增独立配置项，跟随超时语义派生——Spec 明确「派生阈值由统一超时配置派生或纳入同一配置组」）：
- stuck = max(1, round(timeout_s/60 × 22/60))≈timeout 的 36.7%（3600s→22min）
- orphan = timeout 分钟 × 6（3600s→360min）

`runtime/service.py:306` `_WORKER_TASK_TIMEOUT_DEFAULT`（启动期耦合告警用）同步改 getter。

### 1.4 DevStage 收敛

`src/api/routes/devstage.py`：
- 模板 ID 两处重复 → 常量 `GRAPE_AI_DEVSPACE_TEMPLATE` 定义处改为 `get_devstage_template_id()` 的**函数级单一定义**（模块级函数 `_devstage_template_id()`），`list_environments` 与 `create_environment` 都调它；模块级 `GRAPE_AI_DEVSPACE_TEMPLATE` 保留为函数别名（tests/test_devstage_filter.py 引用它构造过滤断言）
- `create_environment` 内 flavor_id/cores/memory/arch 四项改从 `get_devstage_flavor()` 取（内部：env GRAPE_DEVSTAGE_* > meta > runtime-config.json > 默认——现有 env 覆盖语义原样保留）

### 1.5 管理台（admin 配置中心）

`src/admin/config_store.py` + `src/admin/routes.py` + `src/api/static/admin.html` / `js/admin.js` / `locales/*/admin.json`：
- ConfigBody 增加字段：`runtime_task_timeout: int = 0`、`devstage_template_id: str = ""`、`devstage_flavor_id: str = ""`、`devstage_cores: int = 0`、`devstage_memory: int = 0`、`devstage_arch: str = ""`（0/空 = 未提交不改，沿用「只保存非空字段」语义）
- 保存分流：写入 meta 表 `runtime_task_timeout` / `devstage_template_id` / `devstage_flavor_id` / `devstage_cores` / `devstage_memory` / `devstage_arch`（非敏感，明文回显）
- read_public_config 增加回显这些字段（明文，非脱敏类）+ `runtime_task_timeout_effective`（当前生效值，便于管理员核对 env 覆盖）
- 前端 admin.html 新卡片「运行参数」：超时输入 + DevStage 6 字段；admin.js `_renderCfg`/`saveConfig` 增量处理；zh/en 两个 locales 补 key

### 1.6 配置文件

`configs/runtime-config.json`（gitignored 真实值不入库）+ `configs/runtime-config.example.json`（入库）：
```json
{
  "runtime_task_timeout": 3600,
  "devstage": {
    "template_id": "4ef59a2b4d3d4909aacd80f6007c00b2",
    "flavor_id": "23ebdfe3b2d34cc4b34a1aaf03d27c54",
    "cores": 3,
    "memory": 6,
    "arch": "ARM"
  }
}
```
`.gitignore` 补 `configs/runtime-config.json`。**文件缺省即全默认**——不部署文件也完全兼容现状。

## 2. 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/config.py` | 新增 RUNTIME_CONFIG_PATH + 5 个 getter（含 60s TTL 缓存 meta 读 + 非法值回退 + env 优先） |
| 2 | `src/runtime/service.py` | `enqueue_task` 注入 RUNTIME_TASK_TIMEOUT（setdefault）；`_WORKER_TASK_TIMEOUT_DEFAULT` 改 getter |
| 3 | `src/runtime/agent.py:2340` | 看门狗 env 优先 |
| 4 | `src/agent/hermes_acp.py` | `_global_timeout_for(max_exec_seconds, env=None)` env 优先 |
| 5 | `src/agent/hwcloud_acp.py` | 同上 |
| 6 | `src/observability/__init__.py` | `_STUCK_MINUTES` → getter |
| 7 | `src/api/routes/dashboard.py:134` | 22 → getter |
| 8 | `src/runtime/cleanup.py:23` | 默认值 → getter（env 兜底保留） |
| 9 | `src/api/routes/devstage.py` | 模板 ID/flavor 收敛到 getter；两处重复消除 |
| 10 | `src/admin/config_store.py` | 字段清单 + meta 读写 |
| 11 | `src/admin/routes.py` | ConfigBody 字段 |
| 12 | `src/api/static/admin.html` / `js/admin.js` / `locales/{zh,en}/admin.json` | 运行参数卡片 |
| 13 | `configs/runtime-config.example.json` + `.gitignore` | 示例配置 |
| 14 | `tests/test_runtime_config_470.py`（新） | 配置读取/非法回退/优先级/派生 |
| 15 | `tests/test_admin_runtime_config.py`（新） | 管理台 GET/PUT 回显 |

## 3. 测试 seam 声明

- 配置层：`src.config.get_runtime_task_timeout` 等公开 getter（monkeypatch env / meta / 文件三源验证优先级与回退）
- ACP 层：`_global_timeout_for(max_exec, env)` 纯函数（沿用 test_hermes_global_timeout.py 模式，新增 env 优先用例）
- 路由层：admin `/api/admin/config` GET/PUT（沿用 test_admin_whitelist_user_crud.py 的 TestClient+真实 SQLite 模式）
- worker 层：`agent.py` 看门狗取值函数纯断言（不起 worker 进程）

## 4. 已知预存问题（非本改动范围）

- `tests/test_devstage_filter.py` 6 个用例在基线即红：历史提交 812e8933d 把 `list_environments` 从 `async def` 改成 `def`，测试仍用 `asyncio.run()` 调用 → `ValueError: a coroutine was expected`。基线 `origin/local-liufei`（fe563608f）复现一致，与本 Issue 无关。本 Issue 不修（超范围），PR body 注明。

## 5. 任务拆解（TDD 红/绿切片）

- T1 红：test_runtime_config_470.py 配置读取/优先级/非法回退/派生（全红）
- T2 绿：src/config.py getter 实现 + runtime-config.example.json + .gitignore
- T3 红：hermes/hwcloud `_global_timeout_for(env)` env 优先 + agent.py 看门狗 env 优先 + service.py 注入 的失败测试
- T4 绿：四处实现
- T5 红：observability/dashboard/cleanup 派生阈值 getter 化 失败测试
- T6 绿：三处实现
- T7 红：devstage 收敛 + 管理台字段 失败测试
- T8 绿：实现 + 前端卡片 + locales
- T9 回归：相关模块全套 + 敏感扫描 + 自审查
