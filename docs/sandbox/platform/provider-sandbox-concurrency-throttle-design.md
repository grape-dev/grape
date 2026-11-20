# Provider 沙箱流控并发阈值设计

> 分支：feat/devbox-sandbox-integration
> 日期：2026-10-09
> 状态：已实施
> 依赖：云沙箱 Provider 插拔设计 v3

## 1. 需求分析

### 1.1 问题

现有并发控制只有 worker 级（max_concurrent=20，不区分 Provider）和 runtime 级（agent_runtimes.max_concurrent，单引擎并发）。缺少 per-provider 沙箱实例并发控制——同一个 Provider 的多个引擎（hermes + opencode）可能同时创建过多沙箱实例，超出云平台配额。

### 1.2 需求

| # | 需求 | 说明 |
|---|------|------|
| F1 | per-provider 沙箱并发阈值 | 每个 Provider 实例可配置最大并发沙箱数（max_sandboxes），防止超出云平台配额 |
| F2 | 创建前校验 | 沙箱创建前校验当前 Provider 的活跃沙箱数 < max_sandboxes，超限则等待或拒绝 |
| F3 | 实时计数 | 活跃沙箱数 = cloud_sandboxes 表中 provider_id + state IN ('creating','running') 的行数 |
| F4 | 系统默认值 + 用户可配 | 系统 Provider 预设默认值（如 10），用户 Provider 可自定义（上限 20） |

### 1.3 现有并发控制链路

```
任务入队 → claim_by_tag（runtime busy_slots < max_concurrent）→ worker run_loop（active_sessions < max_concurrent）→ _cloud_execute_task → sandbox_manager.prepare() → provider.create_sandbox() → __enter__() → _do_create() → SDK 创建沙箱
```

| 层级 | 控制点 | 维度 | 缺失 |
|------|--------|------|------|
| worker 级 | run_loop active_sessions >= max_concurrent | worker 进程总并发 | 不区分 Provider |
| runtime 级 | claim busy_slots >= max_concurrent | 单引擎并发 | 不区分 Provider |
| **Provider 级** | **无** | **per-provider 沙箱数** | **缺失** |

## 2. 数据模型

### 2.1 sandbox_provider_config 表新增字段

```sql
ALTER TABLE sandbox_provider_config ADD COLUMN max_sandboxes INTEGER DEFAULT 10;
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| max_sandboxes | INTEGER | 10 | 最大并发沙箱数（creating + running 状态）。系统 Provider 默认 10，用户可配 1-20 |

### 2.2 cloud_sandboxes 表

已有 provider_id 列（P1 已加）。已有 state 列（creating/running/killed/killed_failed/stale）。无需新增字段。

### 2.3 活跃沙箱计数

```sql
SELECT COUNT(*) FROM cloud_sandboxes
WHERE provider_id = ? AND state IN ('creating', 'running')
```

## 3. 全流程设计

### 3.1 配置流程

```
用户创建/编辑 Provider
  → POST/PUT /api/settings/sandbox-providers
  → 请求体新增 max_sandboxes 字段（可选，默认 10，范围 1-20）
  → 写入 sandbox_provider_config.max_sandboxes
  → 系统 Provider 预置默认值 10
```

### 3.2 下发流程

```
cloud_worker 拉取 Provider 配置
  → GET /api/runtime/provider-configs
  → 返回的 provider config 新增 max_sandboxes 字段
  → cloud_worker 缓存到 _provider_configs[provider_id]["max_sandboxes"]
```

### 3.3 注册流程

```
cloud_worker 注册 runtime
  → runtime_info 不含 max_sandboxes（流控不是 runtime 属性，是 Provider 属性）
  → 流控在沙箱创建时按 provider_id 查 DB 或从 _provider_configs 缓存取
```

### 3.4 沙箱创建流程——流控校验

```
_cloud_execute_task
  → sandbox_manager.prepare()
  → _select_provider_from_context（选 Provider）
  → provider.create_sandbox()
  → __enter__()
  → _do_create()——SDK 创建沙箱

流控校验插入点：prepare() 中 _select_provider_from_context 之后、provider.create_sandbox() 之前
```

#### 3.4.1 cloud_worker 端流控（推荐方案）

cloud_worker 在 _cloud_execute_task 中，prepare() 之前校验 per-provider 沙箱数：

```python
# _cloud_execute_task 中，prepare() 之前
_ctx = build_context(task)
_provider_id = _ctx.provider_id

if _provider_id:
    # 从 _provider_configs 缓存取 max_sandboxes
    _max_sandboxes = _provider_configs.get(_provider_id, {}).get("max_sandboxes", 10)
    # 查当前活跃沙箱数（通过 HTTP API，cloud_worker 无 DB）
    _active_count = await client.get_provider_active_sandboxes(_provider_id)
    if _active_count >= _max_sandboxes:
        # 流控触发：requeue 任务，等待其他沙箱释放
        await _log("throttle", f"provider_id={_provider_id} active={_active_count} max={_max_sandboxes}")
        await client.requeue(tid)
        return  # 不执行，任务回到队列
```

#### 3.4.2 服务端 API 新增

```
GET /api/runtime/provider-configs/{provider_id}/active-sandboxes
```

- 权限：rta_ credential（worker）
- 返回：`{"active_count": 3, "max_sandboxes": 10}`
- 查询：`SELECT COUNT(*) FROM cloud_sandboxes WHERE provider_id=? AND state IN ('creating','running')`

#### 3.4.3 流控触发时行为

流控触发时（active_count >= max_sandboxes）：
1. requeue 任务（status → queued，runtime_id → 0）
2. worker 跳过本次执行，继续 poll 下一个任务
3. 其他任务完成后沙箱释放（state → killed/stale），活跃数下降
4. 下次 poll 时重新 claim，如果活跃数 < max_sandboxes 则正常执行

不阻塞——worker 不等待，直接 requeue 继续下一轮。避免 worker 线程被阻塞导致其他 Provider 的任务也无法执行。

### 3.5 沙箱释放流程

```
任务完成/失败 → _cloud_execute_task finally → sandbox.__exit__() → sandbox.kill()
  → report_sandbox_status(state="killed")
  → cloud_sandboxes 表 state 更新为 killed
  → 活跃沙箱数减少
```

### 3.6 Provider 删除流程

```
删除 Provider
  → kill 该 Provider 下所有活跃沙箱（已有逻辑，§3.1.4）
  → cloud_sandboxes 表 state 更新为 killed
  → 活跃沙箱数归零
  → 级联删除模板/binding
```

### 3.7 沙箱状态上报流程（已有，无需改）

```
cloud_worker → report_sandbox_status(state="creating"/"running"/"killed")
  → cloud_sandboxes 表 state 更新
  → 活跃沙箱数实时变化
```

## 4. 改动点清单

### 4.1 DB 层

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | sandbox_provider_config 新增 max_sandboxes INTEGER DEFAULT 10 列 + 迁移 + 系统 Provider 预置 max_sandboxes=10 |

### 4.2 API 层

| 文件 | 改动 |
|------|------|
| `src/api/routes/settings.py` | Provider CRUD 请求体/响应新增 max_sandboxes 字段（校验范围 1-20） |
| `src/api/routes/runtime.py` | 新增 GET /provider-configs/{provider_id}/active-sandboxes 端点 + provider-configs 返回新增 max_sandboxes |

### 4.3 cloud_worker 层

| 文件 | 改动 |
|------|------|
| `src/runtime/cloud_worker.py` | _cloud_execute_task 中 prepare() 之前新增 per-provider 流控校验 |
| `src/runtime/client.py` | 新增 get_provider_active_sandboxes 方法 |

### 4.4 不受影响的模块

- sandbox_manager.py——流控在 cloud_worker 层做，prepare() 不改
- sandbox_context.py——不改
- backends/——不改
- engine_select.py / service.py——claim 不改（claim 级流控是 runtime 级，不是 Provider 级）
- runtime_credentials / agent_runtimes——不改

## 5. 安全分析

### 5.1 流控绕过

风险：worker 不校验 max_sandboxes，直接创建沙箱。

缓解：流控在 cloud_worker 端做（_cloud_execute_task），不在 sandbox_manager.prepare() 中做。如果 worker 代码被篡改跳过流控，沙箱仍会被创建——但 cloud_worker 是系统级进程，用户无法篡改。

### 5.2 计数不准

风险：cloud_sandboxes 表 state 滞后（沙箱已 kill 但 state 未更新）。

缓解：已有 reap_stale 逻辑（state='stale'）+ kill_attempts 机制。流控校验用 state IN ('creating','running')，stale/killed 不计入活跃数。

### 5.3 并发竞争

风险：两个 worker 同时查 active_count=9（max=10），都认为可以创建，实际创建 11 个。

缓解：cloud_worker 是单进程（一个 system credential），不存在并发竞争。如果未来多 worker 共享同一个 Provider，需要服务端原子计数（DB 事务 + INSERT 前检查）。当前设计为单 worker，不需要。

## 6. 验证矩阵

| 场景 | 验证点 |
|------|--------|
| Provider 配置 max_sandboxes | 创建/编辑 Provider 时设置 max_sandboxes=5 |
| 流控触发 | Provider 有 5 个活跃沙箱，第 6 个任务被 requeue |
| 流控释放 | 沙箱 kill 后活跃数下降，requeue 的任务重新 claim 执行 |
| 系统 Provider 默认值 | 系统 Provider max_sandboxes=10 |
| 用户 Provider 自定义 | 用户设 max_sandboxes=3，第 4 个任务被 requeue |
| Provider 删除 | kill 所有活跃沙箱，活跃数归零 |

## 7. 流控全流程

### 7.1 完整链路

```
任务入队（enqueue_task）
  → 任务状态 queued，runtime_id=0

cloud_worker 主循环（run_loop）
  → claim_by_tag（runtime 级 busy_slots < max_concurrent 校验）
  → claim 成功，任务状态 dispatched，runtime_id=引擎ID
  → _cloud_execute_task 执行

_cloud_execute_task
  → 1. GET /tasks/{task_id}/env（取 LLM key + AK/SK + user_gitcode_token）
  → 2. 构建 env（chat/devops 互斥注入 token）
  → 3. 签发 proxy token
  → 4. build_context(task) → _ctx.provider_id 从 runtime_info 提取

  → 4b. per-provider 沙箱流控校验 ★新增★
    → client.get_provider_active_sandboxes(provider_id)
      → GET /api/runtime/provider-configs/{provider_id}/active-sandboxes
      → 服务端查 cloud_sandboxes COUNT(*) WHERE provider_id=? AND state IN ('creating','running')
      → 服务端查 sandbox_provider_config.max_sandboxes
      → 返回 {"active_count": N, "max_sandboxes": M}
    → active_count >= max_sandboxes？
      → 是：requeue 任务（status → queued，runtime_id → 0）+ 返回（不阻塞 worker）
           → worker 继续处理其他任务
           → 其他沙箱完成后释放（state → killed），活跃数下降
           → 下次 poll 时重新 claim，active_count < max_sandboxes 则正常执行
      → 否：继续

  → 5. sandbox_manager.prepare() → _select_provider_from_context → provider.create_sandbox()
  → 6. __enter__() → _do_create() → SDK 创建沙箱
       → report_sandbox_status(state="creating") → cloud_sandboxes 表写入
  → 7. _init_sandbox() → 沙箱初始化
       → report_sandbox_status(state="running") → cloud_sandboxes 表更新
  → 8. dispatch_execution → agent 执行
  → 9. 执行完成 → sandbox.__exit__() → sandbox.kill()
       → report_sandbox_status(state="killed") → cloud_sandboxes 表更新
       → 活跃沙箱数减少
  → 10. 任务终态（complete/fail）
```

### 7.2 流控触发时序

```
Provider A (max_sandboxes=5) 有 5 个活跃沙箱

T0: cloud_worker claim 到 task X（Provider A 的引擎）
T1: _cloud_execute_task → build_context → provider_id=A
T2: get_provider_active_sandboxes(A) → {"active_count": 5, "max_sandboxes": 5}
T3: 5 >= 5 → 流控触发
T4: client.requeue(task_X) → task_X status=queued, runtime_id=0
T5: _cloud_execute_task 返回（不执行）
T6: cloud_worker 继续处理其他任务（可能属于 Provider B，不受影响）

T7: Provider A 的 task_Y 执行完成 → sandbox kill → state=killed → 活跃数=4
T8: 下次 poll → cloud_worker claim 到 task_X（重新 claim）
T9: get_provider_active_sandboxes(A) → {"active_count": 4, "max_sandboxes": 5}
T10: 4 < 5 → 流控通过 → 正常创建沙箱执行
```

### 7.3 三级并发控制对照

| 层级 | 控制点 | 维度 | 校验时机 | 超限行为 |
|------|--------|------|---------|---------|
| Provider 级 | _cloud_execute_task 流控校验 | per-provider 沙箱数 | 沙箱创建前 | requeue 任务 |
| runtime 级 | claim_by_tag busy_slots | 单引擎并发 | claim 时 | 跳过（不 claim） |
| worker 级 | run_loop active_sessions | worker 进程总并发 | claim 后执行前 | 跳过（continue） |

三级控制互不干扰：
- Provider 级控制沙箱实例数（防止超出云平台配额）
- runtime 级控制单引擎并发（防止引擎过载）
- worker 级控制进程总并发（防止 worker 资源耗尽）

### 7.4 实施改动点

| 文件 | 改动 | 状态 |
|------|------|------|
| `src/api/db_setup.py` | sandbox_provider_config 新增 max_sandboxes 列 + SQLite DDL + MySQL DDL + 迁移语句 | 已实施 |
| `src/api/routes/runtime.py` | provider-configs 端点返回 max_sandboxes + 新增 GET /provider-configs/{provider_id}/active-sandboxes 端点 | 已实施 |
| `src/api/routes/settings.py` | Provider CRUD 请求体/响应加 max_sandboxes（校验 1-20）+ INSERT/UPDATE SQL 加列 | 已实施 |
| `src/runtime/cloud_worker.py` | _cloud_execute_task 中 build_context 后、prepare 前新增流控校验（~15 行） | 已实施 |
| `src/runtime/client.py` | 新增 get_provider_active_sandboxes 方法 | 已实施 |
