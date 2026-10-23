# Spec: Issue #441 — worker hermes 引擎探针 sessionId 写死为空导致 ACP 拒答

> 依据 Issue #441 正文与 VOD/Issue Agent 已发布 Spec（Issue 评论）整理；目标分支 `功能分支`。

## 1. 需求

### 目标
- 修复 `src/runtime/agent.py` `_probe_hermes_acp` 探针在未调用 session/new 的情况下直接发 session/prompt（sessionId 写死 `""`）导致 hermes ACP 服务端返回 `stopReason=refusal`、监控全量 refused 的问题。
- 探针语义与真实任务执行路径一致：initialize → session/new（获取真实 sessionId）→ session/prompt 端到端验证。
- 用户：平台运维/监控使用者。#423（refusal 如实上报）修复暴露的老问题。

### 范围
- **In Scope**（本 PR 改动）
  - `src/runtime/agent.py` `_probe_hermes_acp`：prompt 前先发 session/new 获取真实 sessionId（对齐 `_probe_hwcloud_acp` 既有先例，方案 A）。
  - 探针会话清理（如有 dispose 语义则对齐 hwcloud 探针做法——hwcloud 探针无 dispose，见 §2）。
  - 新增测试覆盖探针 session/new 路径（可 mock ACP 会话）。
- **Out of Scope**（明确不做）
  - hermes ACP 服务端（外部 hermes-agent 仓库）对空 sessionId 自动建会话（方案 B，不采用）。
  - 9 台外部 worker 探活 fail（各自环境问题，Issue 已注明不在本单范围）。
  - `service.py` 可达性判定改动（另行处理，尚未合入主干）。
  - `src/observability/engine_probe.py`（主服务侧探针，仅 initialize 验证、不走 prompt，无 sessionId 依赖，不受本问题影响；Out of Scope 保持不改）。

### 关键功能点
1. `_probe_hermes_acp` 在 initialize 之后、prompt 之前插入 session/new，取回真实 sessionId（`result.sessionId`，与任务链路 `hermes_acp.py:_extract_session_id` 的顶层字段口径一致；仅取顶层，探针不发 _meta 协议字段）。
2. prompt 请求使用真实 sessionId；stopReason 判定沿用 #423 之后的口径（refused 仍如实上报）。
3. session/new 失败（无响应/error）正确归类为 fail（对齐 hwcloud 探针分支），不得崩溃探针线程。

## 2. 技术方案

### 现状（缺陷代码）
`src/runtime/agent.py:206-208`：
```python
_send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
       "params": {"sessionId": "",
                  "prompt": [{"type": "text", "text": "hi"}]}})
```
hermes ACP 服务端 `prompt()` 对空/不存在的 sessionId 查不到会话 → 返回 `stopReason=refusal` → #423 后 refusal 独立标记 refused → 全量 refused。

### 修改方案（方案 A，对齐 `_probe_hwcloud_acp` 先例）
探针流程改为与 hwcloud 探针同构的三步：
1. 启动 `hermes acp` 子进程（不变）
2. initialize 握手（id=1，不变）
3. **session/new**（id=2）：`{"cwd": <临时目录>, "mcpServers": []}`；`cwd` 用轻量临时目录（对齐 hwcloud 探针理由：家目录在 devspace/网络挂载场景可能不可写或初始化慢，探活不应受其拖累）；响应取 `result.sessionId`。
4. session/prompt（id=3）：使用真实 sessionId；"hi" 轻量 prompt。
5. 清理：terminate 子进程（既有 finally，覆盖会话清理——hwcloud 探针同样仅 terminate、无 session/dispose，保持同构）。

失败分支（全部对齐 hwcloud 探针分支处理）：
- session/new 无响应/error → `fail`，错误前缀 `ACP session/new: ...`。
- prompt 无响应/error → `fail`。
- stopReason=refusal → `refused`（#423 口径不变）。
- 其他 stopReason → `ok`。

### 输出口径
探活成功 output 文案同步为三步形态：`ACP initialize+session/new+prompt ok (stopReason=...)`（与 hwcloud 探针 output 同构，监控端按 status 而非文案解析，无兼容性风险）。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 修复位置 | A: worker 探针先 session/new；B: ACP 服务端空 sessionId 自动建会话 | A | 治本、无外部依赖；探针语义与真实任务链路一致；hwcloud 探针已有先例 |
| sessionId 提取 | 仅顶层 `result.sessionId` / 兼容 `_meta.hermes.sessionProvenance` | 顶层 `result.sessionId`，取不到则 fail | hwcloud 探针同款取法（`(_new_resp.get("result") or {}).get("sessionId", "")`）；探针场景无需 resume/持久化语义 |
| session/new 超时 | 复用 `_ACP_PROBE_TIMEOUT` | 复用 15s | 建会话无模型推理，与 initialize 同量级（hwcloud 探针同款） |
| cwd | 家目录 / 临时目录 | 临时目录 | 对齐 hwcloud 探针：家目录在 devspace/网络挂载可能不可写/初始化慢，拖累探活 |

## 3. 任务拆解

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 修改 `_probe_hermes_acp`：插入 session/new（id=2），prompt 改 id=3 + 真实 sessionId | `src/runtime/agent.py` | ✅ |
| 2 | 失败分支对齐 hwcloud：session/new 无响应/error → fail；prompt 错误前缀保持 | `src/runtime/agent.py` | ✅ |
| 3 | 新增测试：fake ACP CLI 断言探针发出 session/new 且 prompt 携带真实 sessionId | `tests/test_probe_hermes_session_new_441.py` | ✅ |
| 4 | 回归：`tests/test_probe_acp_read_response_423.py` 全量通过（fake CLI 已支持 session/new） | 测试通过 | ✅ |
| 5 | 文档：Spec/Plan/Task 三件套落 docs/issue-441/ | 本目录 | ✅ |

## 4. 测试

### 新增（tests/test_probe_hermes_session_new_441.py）
- **T1 happy path**：fake CLI（initialize → session/new 返回 `sessionId="s-1"` → prompt stopReason=end_turn）→ `ok`，且探针 stdin 发出的第 3 个请求 method=session/prompt、params.sessionId="s-1"（端到端验证真实 sessionId 被传递，非空字符串）。
- **T2 session/new 无响应**（fake CLI 静默不回 session/new）→ `fail`，error 含 `session/new`。
- **T3 session/new error**（fake CLI 返回 JSON-RPC error）→ `fail`，error 含 `session/new`。
- **T4 sessionId 缺失**（session/new 成功但 result 无 sessionId）→ `fail`（不把空 sessionId 带进 prompt 重蹈覆辙）。
- **T5 refusal 路径回归**：stopReason=refusal → `refused`（#423 口径不软化）。
- **T6 通知流形态**：session/new 前 34 行通知 → 仍 ok（deadline 驱动不回归）。

### 回归
- `tests/test_probe_acp_read_response_423.py` 全量（hermes/hwcloud/refused 分支）。
- 既有测试套件相关文件不引入失败。

### 手动验证口径（真机，非本 PR 范围）
- 升级到修复版本的 worker hermes 探活不再全量 refused（Issue 验收标准，需 worker 常规版本发布后观察监控页）。

## 5. 验收标准对照（Issue）

| Issue 验收项 | 本 PR 落实 |
|---|---|
| `_probe_hermes_acp` 在发 prompt 前先调用 session/new 获取真实 sessionId | ✅ 探针三步流程 + T1 端到端断言请求序列 |
| hermes 引擎可用时探活返回 ok（非 refused/fail） | ✅ T1/T6（stopReason=end_turn → ok） |
| hermes 引擎不可用时探活仍正确返回 fail/refused | ✅ T2/T3/T4 → fail；T5 → refused |
| 探针语义与真实任务执行路径一致（session/new → prompt） | ✅ 与 `hermes_acp.py` 任务链路同款 session/new + 真实 sessionId prompt |
| 所有现有测试通过；新增测试覆盖探针 session/new 路径（可 mock ACP 会话） | ✅ #423 回归套件全过 + 新增 6 个用例 |

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| session/new 失败需正确归类为 fail 而非崩溃 | 中 | 对齐 hwcloud 探针异常分支；T2/T3/T4 覆盖 |
| hermes session/new 响应字段位置与 hwcloud 不同（_meta 嵌套） | 低 | 任务链路 `hermes_acp.py` 实测顶层 `sessionId` 可用（`_extract_session_id` 优先顶层）；探针取不到即 fail，fail 安全侧 |
| worker 需升级后监控才恢复 | 低 | 修复随常规 worker 版本发布；监控侧 refused=物理可达口径已对齐 |

# Task 执行记录

> 拆解与勾选见 plan.md §Task；spec.md §3 含任务表（含状态列）。

## 变更清单

- src/runtime/agent.py：_probe_hermes_acp 三步探活（initialize → session/new → prompt）
- tests/test_probe_hermes_session_new_441.py：新增 6 用例
- docs/issue-441/：spec.md / plan.md / task.md
