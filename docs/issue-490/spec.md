# Issue #490 — Worker 探针观测重复计数修复 · 需求文档

## 目标

修复 Worker 引擎探针观测被心跳重放导致服务端 `probe_fail_count` 虚增、Runtime 被误标 `healthy=0`、中央派发误判无可用引擎的问题。

## 背景根因

两个相互叠加的缺陷：

1. **启动重复探测**：注册成功后同步执行 `_worker_probe_once()`，进入 `_run_loop()` 后 `_worker_probe_loop()` 第一轮**不等待** `_PROBE_INTERVAL` 立即又探测一轮 → 启动阶段连续两轮完整探测。
2. **心跳重复计数**：心跳每次都携带缓存 `_worker_probe_results`；服务端每收到一次 `fail` 就 `probe_fail_count=probe_fail_count+1`，未按 `last_probe` 去重 → 同一观测重放 N 次被当 N 次独立失败，达阈值摘除 Runtime。

## 范围（In）

| 文件 | 改动 |
|------|------|
| `src/runtime/agent.py` | ① 复用注册即时探针结果（后台线程不再首轮立即探测） ② `_worker_probe_loop()` 首轮先 `_time_mod.sleep(_PROBE_INTERVAL)` 再探测 ③ 心跳成功后不重发同一观测（指纹 `engine_name+last_probe+status`） |
| `src/runtime/client.py` | `heartbeat()` 携带观测指纹字段（`probe_results` 内已含 `last_probe`，服务端按此幂等；client 层不额外加字段，复用 `last_probe`） |
| `src/api/routes/runtime.py` | `heartbeat` 按 `last_probe` 与已存 `probe_last` 幂等去重：相同 `last_probe` 重放只刷新 `last_seen/status`，不递增/重置 `probe_fail_count` |
| 新增单测 | 服务端幂等去重 + Worker 启动时序（首轮延迟） |

## 不在范围内（Out）

- 探针命令模板、探针超时时长调整
- 服务端 `engine_probe.py` 主动探测逻辑
- 注册/claim/重注册流程
- 历史 `git filter-repo` / 全历史扫描

## 验收标准

1. 同一失败观测连续 3 次心跳：`probe_fail_count` 只 +1，Runtime 不被摘除。
2. 两条 `last_probe` 不同的失败观测：计数达 2、Runtime 标记 `healthy=0`。
3. 新成功观测：计数清零、恢复 `healthy=1`。
4. 注册即时探针与后台首轮周期探针间隔 ≥ 一个 `_PROBE_INTERVAL`。
5. 单测覆盖服务端幂等与 Worker 启动时序；现有相关测试不回归。

## 技术方案（双层幂等）

**发送端（Worker）**：
- 注册后 `_worker_probe_once()` 结果写入 `_worker_probe_results`；后台 `_worker_probe_loop()` 启动后**先 sleep `_PROBE_INTERVAL`**，再首轮探测，避免与即时探针重叠。
- 心跳发送时记录「上次已发送成功的观测指纹」(`engine_name → last_probe`)；心跳 `status==ok` 后更新指纹，未变化的观测不再重复携带（`probe_results` 置空或仅发增量）。

**服务端防御（runtime.py）**：
- 对每个 `probe_results` 条目，先查已存 `probe_last`；仅当传入 `last_probe != 已存 probe_last` 时才走 `ok→重置计数` / `fail→递增计数` 分支；相同 `last_probe` 重放只刷新 `probe_status/probe_latency/probe_last/probe_error`（其实相同，幂等），**不动 `probe_fail_count` 与 `healthy`**。

## 命令

```bash
# 单测
python3 -m pytest tests/test_issue_490_probe_idempotent.py -q
# 回归
python3 -m pytest tests/test_runtime.py tests/test_engine_probe_codex.py tests/test_issue_417_revoked_runtime_residue.py -q
```

## 测试 seam

- **服务端幂等**：直接调用 `runtime_heartbeat(body, request, credential_id=cid)`（复用 `_require_credential` 依赖注入 seam，同 `test_issue_417` 模式），断言 DB `probe_fail_count` / `healthy`。
- **Worker 启动时序**：调用 `_worker_probe_loop()` 的单轮逻辑（抽函数或 monkeypatch `_time_mod.sleep` + `_discover_local_runtimes`/`_probe_engine_worker`），断言首轮先 sleep。

## 项目结构

```
src/runtime/agent.py         # Worker 探针 + 心跳
src/runtime/client.py        # 心跳 payload
src/api/routes/runtime.py    # 服务端 heartbeat 落库
tests/test_issue_490_probe_idempotent.py   # 新增单测
docs/issue-490/              # 本文档
```
