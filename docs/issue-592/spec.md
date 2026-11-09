# Spec: Issue #592 — 引擎选择弹窗本机直连引擎数量偏少 + 状态误显失联

## 背景与现象

场景详情页编辑模式下，每个 Agent 的「选择引擎」弹窗（`dashboard.js _openEngineModalImpl`）中：

1. **本机直连引擎全部显示「失联」**：实际这些机器可正常下发执行任务，且与页面刷新无关。
2. **本机直连引擎数量偏少**：与引擎页（agent-runtimes 设备列表）实际注册的本机直连机器数不一致。

## 根因（代码级定位）

弹窗数据源 = `GET /api/dashboard/{repo}` 响应中的 `agent_engines[agent_type]`，由 `src/api/routes/dashboard.py` 组装：

### 根因 A：状态误显失联（dashboard.py:1286）

`agent_engines` 对**所有**引擎统一调用 `derive_runtime_health(status, last_seen_at, now)`——该口径是**远端 worker 心跳口径**（status=online 且 last_seen_at < 100s 才算 online，超时 → `recently_lost`/失联）。

而本机直连引擎（`credential_id=0`）**不参与 worker 心跳**，其 `last_seen_at` 仅由主控机周期重探循环（`main.py _local_probe_loop`，每 10 分钟）刷新。10 分钟 ≫ 100s 阈值 → 探活窗口内全部被判 `recently_lost`（失联）。引擎页（settings.py `_engine_health`）对本机直连早已有正确口径：`installed + healthy → online`，弹窗未复用，导致两处显示不一致。

### 根因 B：数量偏少（engine_select.py `query_engines` local 档 + agent_type 标签过滤）

弹窗列表按 `query_engines(repo_key, "local", agent_type=atype)` 查询，tag 过滤规则为「tags 包含 atype 或 all」。`save_agent_runtimes` 仅在**新建**本机直连行时默认写入 `engine_capabilities('all')`；历史遗留行（默认标签机制引入前创建）无 capabilities 行 → tags 为空 → 被过滤出弹窗列表。而引擎页设备列表不按 tag 过滤，仍显示这些行 → 弹窗数量 < 实际注册数量。派发侧（`resolve_local_engine`/`pick_engine`）也不依赖 tag 过滤即可本机直连执行，故这些机器「显示失联/不可见但实际可执行」。

## 修复方案

| # | 修复 | 位置 |
|---|------|------|
| 1 | 新增本机直连健康口径 `derive_local_runtime_health(installed, healthy)`（installed+healthy → online，否则 offline） | `src/runtime/health.py` |
| 2 | 弹窗 `agent_engines` 状态派生分流：credential_id=0 走本机直连口径，其余维持远端心跳口径 | `src/api/routes/dashboard.py` |
| 3 | 引擎页 `_engine_health` 复用新口径（消除重复实现，保持两处一致） | `src/api/routes/settings.py` |
| 4 | `query_engines` local 档 tag 过滤：本机直连行无 capabilities 标签时按默认 `all`（与注册默认行为及派发广播语义对齐） | `src/runtime/engine_select.py` |

## 验收标准

- 弹窗本机直连机器数量与引擎页实际注册数量一致（含无标签历史行）。
- 本机直连引擎状态按 installed/healthy 显示在线（非失联）；远端引擎口径不变。
- 选择本机直连引擎可正常下发执行（回归：派发链路零改动）。
- 新增测试覆盖上述场景，现有测试通过。
