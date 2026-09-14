# Issue #153: Worker 机器自动升级方案设计

## 0. 需求

Worker 机器在心跳中发现本地版本落后于服务端最新版本时，自动下载最新 worker 包并安装升级。

**核心约束**：
- 不中断运行中任务（待所有 active_tasks 完成后才升级）
- 不下包不中断任务（下载和安装仅在空闲时执行）
- 失败保持当前版本，指数退避降频重试
- 升级成功后重启加载新版本

**关联 Issue**：#152（原始需求）、#153（方案文档化 + 实施）

## 1. 现状分析

### 版本管理基础设施

| 组件 | 位置 | 说明 |
|------|------|------|
| `package_version()` | `src/worker_package.py:300` | 基于 git commit + manifest 指纹 + wheels 指纹 + python 指纹生成版本号 |
| `GET /api/runtime/install/latest` | `src/api/routes/runtime.py:846` | 返回当前最新包版本号（rta_ 鉴权） |
| `GET /api/runtime/install/package` | `src/api/routes/runtime.py:876` | 下发 worker 安装包 tar.gz（rta_ 鉴权） |
| `GET /api/runtime/grape-worker.tar.gz` | `src/api/routes/runtime.py:856` | 公开下发安装包（无鉴权） |
| `GET /api/runtime/install` | `src/api/routes/runtime.py:826` | 返回 install-worker.sh 脚本内容 |
| `install-worker.sh` | `scripts/install-worker.sh` | 幂等安装脚本：下载→解压→建 venv→写配置→重启 |
| `/etc/grape/worker.version` | worker 本地文件 | 安装脚本写入，记录当前 worker 版本号 |
| `_read_local_worker_version()` | `src/runtime/agent.py:1538` | 读取本地版本号 |

### 心跳机制

| 组件 | 位置 | 说明 |
|------|------|------|
| `POST /api/runtime/heartbeat` | `src/api/routes/runtime.py:208` | worker 定期心跳，当前响应 `{status, cancelled_tasks}` |
| `client.heartbeat()` | `src/runtime/client.py:109` | worker HTTP 客户端方法，返回完整响应 dict |
| 心跳处理 | `src/runtime/agent.py:1458` | `_run_loop` 中心跳+失败检测逻辑 |

### 当前版本自检（仅告警，不自动升级）

`src/runtime/agent.py:1631-1639`：worker 启动时调用 `client.latest_version()` 与本地版本对比，不一致时仅打印告警日志，不自动升级。

### Worker 主循环

`_run_loop()`（agent.py:1269）是 worker 主循环：
1. 定期重注册（防 id 静默失效）
2. Claim 任务 → 执行 → 回报
3. 心跳 + 失败检测
4. SID 看门狗（清理幽灵 session）

`active_tasks` dict 跟踪当前运行中任务（tid → sid 映射）。

## 2. 架构设计

### 整体流程

```
服务端                          Worker
  │                               │
  │  POST /heartbeat (响应含 latest_version)
  │ <─────────────────────────────│
  │                               │
  │  latest_version vs local_ver  │
  │  落后? → pending_upgrade=True │
  │                               │
  │  pending_upgrade &&           │
  │  active_tasks 空?             │
  │  → 下载包 + 执行安装          │
  │                               │
  │  install-worker.sh            │
  │  → 覆盖代码 → 重启 worker     │
  │                               │
  │  worker 重启 → 加载新版本     │
  │  → 版本一致 → 正常运行        │
```

### 数据流

1. **服务端心跳响应**追加 `latest_version` 字段（来源于 `package_version()`）
2. **Worker 心跳处理**解析 `latest_version`，与 `_read_local_worker_version()` 对比
3. **版本落后**时设置 `pending_upgrade=True`，记录目标版本
4. **升级执行**：`pending_upgrade=True` 且 `active_tasks` 为空时，调用 install-worker.sh
5. **install-worker.sh** 处理：下载包 → 解压 → 建 venv → 写配置 → 重启 worker
6. **失败重试**：install-worker.sh 返回非零退出码时，记录失败计数，指数退避

## 3. 状态机

```
                    ┌──────────────┐
                    │   NORMAL     │ ← 正常运行
                    │ (版本一致)   │
                    └──────┬───────┘
                           │ heartbeat 响应
                           │ latest_version != local
                           ▼
                    ┌──────────────┐
                    │ PENDING_UPGRADE│ ← 待升级
                    │ 等待任务完成  │
                    └──────┬───────┘
                           │ active_tasks 为空
                           │ && 退避间隔已过
                           ▼
                    ┌──────────────┐
                    │  UPGRADING   │ ← 升级中
                    │ 执行安装脚本  │
                    └──────┬───────┘
                           │
                   ┌───────┴────────┐
                   │                │
           安装成功(进程被杀)    安装失败(返回非零)
                   │                │
                   ▼                ▼
            ┌────────────┐  ┌──────────────┐
            │  RESTARTED │  │ UPGRADE_FAILED│
            │ (新版本启动)│  │ 退避等待重试  │
            └────────────┘  └──────┬───────┘
                                   │ 退避时间过后
                                   ▼
                            ┌──────────────┐
                            │ PENDING_UPGRADE│
                            └──────────────┘
```

### 状态说明

| 状态 | 条件 | 行为 |
|------|------|------|
| NORMAL | local_version == latest_version | 正常 claim/heartbeat |
| PENDING_UPGRADE | 版本不一致 | 正常 claim/heartbeat，等待 active_tasks 为空 |
| UPGRADING | pending_upgrade && active_tasks 空 && 退避已过 | 执行 install-worker.sh |
| RESTARTED | 安装成功 | worker 被 install-worker.sh 重启，加载新版本 |
| UPGRADE_FAILED | 安装失败 | 清 pending_upgrade，记录失败计数，退避后回到 PENDING_UPGRADE |

## 4. 接口契约

### 4.1 心跳响应（修改）

**端点**：`POST /api/runtime/heartbeat`

**响应**（追加 `latest_version` 字段）：

```json
{
  "status": "ok",
  "cancelled_tasks": ["task-123", "task-456"],
  "latest_version": "git-a8a7dde-w12345678-p87654321"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| status | string | "ok" |
| cancelled_tasks | list[string] | 已取消任务 ID（现有） |
| latest_version | string | 当前服务端最新 worker 包版本号（新增） |

**版本号为空时的处理**：`package_version()` 失败时返回空字符串，worker 侧跳过升级判断。

### 4.2 升级执行（内部，无新端点）

Worker 通过 `GET /api/runtime/install` 下载 install-worker.sh 脚本，本地执行：

```bash
bash install-worker.sh -- --token <rta_...> --server <https://server> --dir <install_dir>
```

install-worker.sh 幂等：复用已有目录/venv，仅更新代码与配置并重启。

## 5. 实现设计

### 5.1 服务端改动（`src/api/routes/runtime.py`）

心跳端点 `runtime_heartbeat()` 返回值追加 `latest_version` 字段：

```python
# 在 return 之前计算 latest_version
from src.worker_package import package_version
_latest_ver = ""
try:
    _latest_ver = package_version()
except Exception:
    pass

return {"status": "ok", "cancelled_tasks": cancelled_tasks, "latest_version": _latest_ver}
```

### 5.2 Worker 改动（`src/runtime/agent.py`）

#### 新增函数：`_perform_upgrade`

```python
async def _perform_upgrade(server: str, token: str) -> bool:
    """下载并执行 install-worker.sh 升级 worker。

    成功时 worker 被 install-worker.sh 重启（本进程被杀，不会返回）。
    失败时返回 False，worker 继续运行当前版本。
    """
```

实现要点：
1. 通过 `GET /api/runtime/install` 下载 install-worker.sh 到临时文件
2. 通过 `asyncio.to_thread` 在线程中执行 `subprocess.run`
3. 传入 `--token`、`--server`、`--dir`（当前安装目录）参数
4. 如果 subprocess.run 返回（未被杀），说明安装失败
5. 如果 worker 被杀（安装成功触发重启），函数不返回

#### `_run_loop` 改动

在 `_run_loop` 函数中新增升级状态变量：

```python
# 自动升级状态
pending_upgrade = False
upgrade_target_version = ""
upgrade_fail_count = 0
last_upgrade_attempt = 0.0
UPGRADE_BACKOFF_BASE = float(os.environ.get("RUNTIME_UPGRADE_BACKOFF_BASE", "60"))
UPGRADE_BACKOFF_MAX = float(os.environ.get("RUNTIME_UPGRADE_BACKOFF_MAX", "1800"))
```

在心跳处理逻辑之后追加版本对比+升级触发：

```python
# ---- 版本对比 + 自动升级 ----
_latest = (ok or {}).get("latest_version", "")
if _latest and _latest != _read_local_worker_version():
    if not pending_upgrade:
        log.info(f"Version mismatch detected: local={_read_local_worker_version()} server={_latest}, "
                 f"will upgrade when idle")
    pending_upgrade = True
    upgrade_target_version = _latest

# ---- 升级执行（待升级 && 无活跃任务 && 退避已过）----
if pending_upgrade and not active_tasks:
    _now = asyncio.get_running_loop().time()
    _backoff = min(UPGRADE_BACKOFF_BASE * (2 ** upgrade_fail_count), UPGRADE_BACKOFF_MAX)
    if _now - last_upgrade_attempt >= _backoff:
        last_upgrade_attempt = _now
        log.info(f"Starting auto-upgrade to version {upgrade_target_version} "
                 f"(attempt {upgrade_fail_count + 1})")
        _upgrade_ok = await _perform_upgrade(
            os.environ.get("RUNTIME_SERVER_URL", ""),
            os.environ.get("RUNTIME_TOKEN", "")
        )
        if not _upgrade_ok:
            upgrade_fail_count += 1
            pending_upgrade = False
            log.warning(f"Auto-upgrade failed (fail_count={upgrade_fail_count}), "
                        f"will retry in {_backoff * 2:.0f}s")
        # 如果 _upgrade_ok 为 True，不会执行到这里（worker 已被重启）
```

### 5.3 退避策略

| 失败次数 | 退避间隔 | 计算 |
|---------|---------|------|
| 0（首次） | 60s | 60 * 2^0 |
| 1 | 120s | 60 * 2^1 |
| 2 | 240s | 60 * 2^2 |
| 3 | 480s | 60 * 2^3 |
| 4 | 960s | 60 * 2^4 |
| 5+ | 1800s（30min） | min(60 * 2^n, 1800) |

退避参数可通过环境变量调整：
- `RUNTIME_UPGRADE_BACKOFF_BASE`：基础退避秒数（默认 60）
- `RUNTIME_UPGRADE_BACKOFF_MAX`：最大退避秒数（默认 1800）

## 6. 风险评估

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| 升级过程中 worker 重启导致短暂不可用 | Med | High | 仅在无运行任务时执行；重启后自动恢复心跳（systemd/nohup 自动重启） |
| install-worker.sh 执行时间过长阻塞主循环 | Med | Low | `asyncio.to_thread` 在线程中执行，不阻塞事件循环；设置 600s 超时 |
| 下载大文件失败 | Low | Med | install-worker.sh 内部有 curl 重试；失败后指数退避降频重试 |
| 版本对比异常（格式不匹配） | Low | Low | 版本号为空时跳过升级判断；版本格式不一致即触发升级（幂等安装） |
| 升级失败后无限重试 | Med | Low | 指数退避（最大 30 分钟），不放弃重试但降频 |
| install-worker.sh 不存在或下载失败 | Med | Low | install-worker.sh 从服务端 `/api/runtime/install` 下载，服务端保证可用 |
| 并发升级（多引擎同时触发） | Low | Low | 升级在 _run_loop 中单线程执行，同一 worker 只有一个升级进程 |

## 7. 测试计划

| 测试用例 | 验证点 |
|---------|--------|
| `test_heartbeat_response_contains_latest_version` | 心跳响应包含 latest_version 字段 |
| `test_version_mismatch_sets_pending_upgrade` | 版本不一致时设置 pending_upgrade |
| `test_version_match_no_upgrade` | 版本一致时不触发升级 |
| `test_pending_upgrade_waits_for_tasks` | 有活跃任务时不执行升级 |
| `test_pending_upgrade_triggers_when_idle` | 无活跃任务时触发升级 |
| `test_upgrade_failure_exponential_backoff` | 失败后指数退避 |
| `test_upgrade_success_restarts_worker` | 成功升级后 worker 重启 |
| `test_empty_latest_version_skips_upgrade` | latest_version 为空时跳过升级 |

## 8. 不做项（Out of Scope）

- Worker 启动时版本自检逻辑改造（保持现有告警行为）
- 服务端 worker 包构建/发布流程（已有 `package_version` / `build_package` 基础设施）
- 回滚机制（失败保持当前版本即可）
- 手动触发升级接口
- 升级进度通知（日志记录即可）

---
*Related Issue: #152（原始需求）| Design Doc: docs/weekly-03/issue-153-worker-auto-upgrade-design.md*
