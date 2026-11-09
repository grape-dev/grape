# 远端 Chat 任务失败提示优化设计（chat-remote-failure-hint-design.md）

> Status：plan-ready（待实施，TDD）
> Branch：`weekly-09`（目标部署分支 weekly-09-lyz）
> Related：worker 引擎崩溃导致「查询失败」无原因（2026-10-29 实锤 opencode CLI/DB schema 冲突）· `src/runtime/service.py` fail_task · `src/api/routes/chat.py` 远端轮询
> Sensitive info: this doc contains no real IP, domain, password, account or token (all `<xxx>` placeholders)

---

## 〇、Problem / Observed behavior

远端 worker 引擎执行失败时，chat agent 只回一个光秃秃的「查询失败」，**真实原因在链路中丢失**，
用户既不知道发生了什么，也不知道下一步该怎么做。本次事故是 opencode CLI 1.17.7 与共享
`opencode.db`（被 Desktop 1.18.18 迁移）schema 不兼容，每次 `opencode run` 崩溃 exit=1：

1. **失败原因被存错列**：worker 上报失败时带原因文本（`agent.py:2434`
   `engine exit_code=1, errors: SQLiteError...`）→ `client.fail`（`client.py:261`）→
   `service.fail_task`（`service.py:2512`）→ `update_task_status(..., errors=json.dumps([error]))`（`service.py:2562`）
   **只写 `errors` 列，不写 `result_text`**。
2. **chat 轮询读错列**：远端轮询收敛展示只读 `result_text`——`chat.py:1969`、`chat.py:2245`、`chat.py:2357`
   三处 `_final_text = f"查询失败: {tk.get('result_text', '')}"`。失败路径 `result_text` 恒为空
   → 用户看到 `查询失败: `（空尾巴）。**数据在这次会话里丢了，不是网络丢**。
3. **即便带出原因，原始文本也不该直接暴露给用户**：`engine exit_code=1, errors: SQLiteError: no such column...`
   是排障串，无可行动性；而用户需要的是「发生了什么 + 下一步点哪里/检查什么」。
4. **同类盲区不止 chat**：事件 redispatch、dashboard 同样消费 `result_text`/`errors`，没有统一「失败→友好提示」映射。

## 一、Target semantics

| 分面 | 语义 |
|------|------|
| 数据修复 | 失败原因在服务端落库时**两种途径都可见**：`result_text` 有兜底原因（不再恒空），`errors` 保留原始排障详情 |
| 友好提示 | 展示层按统一分类把失败映射为「发生了什么 + 行动建议」；原始 `errors`/`stderr` 不进用户可见文案 |
| 未分类兜底 | 命中不了任何已知分类的特例 → 用原始原因截断展示 + 明确标注"请参考任务详情/worker 日志" |
| 端到端 | worker→服务端→chat 三处展示点（submit 轮询 / 同步 wait / SSE DB 兜底）同一套口径 |
| 不破坏 | worker 协议、DB schema、事件/重派路径、401 检测（`detect_and_mark_expired`）全部保持现状 |
| 运维可见 | `errors` 列继续保留完整原因，Dashboard/任务详情排障不受影响 |

## 二、方案设计

### 2.1 数据修复：失败原因双写

`service.fail_task`（`service.py:2512`）在写 `errors` 的同时，把可读原因也写入 `result_text`
（此前失败路径 `result_text` 恒空）：

```python
update_task_status(task_id, "failed", exit_code=exit_code,
                   result_text=(error or "")[:500],          # 新增：兜底可读原因
                   errors=json.dumps([str(error)] if error else []))
```

- 幂等保护（already completed/failed / stale report）逻辑不变，均在 `update_task_status` 之前；
- 500 字符上限防撑爆展示；完整 `errors` 仍由 `json.dumps` 保留；
- `complete_task` 不受影响（正常完成仍写结构化结论）。

### 2.2 分类函数：失败 → 友好提示

新增 `friendly_failure_hint(task: dict) -> tuple[str, str]`（建议放 `service.py`，与 `_R16_ERROR_MESSAGES`
同文件），输入 `get_task` 的行 dict，输出 `(hint, action)`：

| 判定特征（输入字段） | hint（发生了什么） | action（下一步） |
|------|------|------|
| `errors`/`result_text` 含 `engine exit_code=` 且非 0，或含 `no such column`/`databaseMigrations`/`SQLiteError` 等 CLI/DB 初始化错误 | `引擎执行异常（exit_code=N）：本地引擎 CLI 可能损坏或与数据版本不兼容` | `请检查所选 worker 上的引擎是否正常，或更换引擎后重试` |
| `exit_code < 0` 或含 `task timed out` / `timeout` | `任务执行超时` | `请稍后重试，或简化问题后重发` |
| 命中 `_R16_ERROR_MESSAGES` 的 token 类（复用现有 `detect_and_mark_expired` 结果） | `登录/授权状态失效` | `请重新授权后重试` |
| `worker 离线/未认领`（入队侧 `_remote_runtime_ready(rid)==False`，见 2.3） | `所选引擎当前不可达` | `请确认 worker 在线或更换引擎后重试` |
| 兜底（以上均不命中） | 原始 `errors`/`result_text` 截断 200 字 | `请参考任务详情或 worker 日志排障` |

- 函数纯判定、无副作用，可供 chat / redispatch / dashboard 复用；
- token 401 检测保持唯一入口（`fail_task` 内既有逻辑），分类函数只读已落库结果，不重复测量。

### 2.3 入队前预防：引擎不可达早期提示（可选增强）

chat submit 远端路径已有 `_remote_runtime_ready(rid)` 兜底判定（`chat.py:1772`）。当前不可达时
走的是「换 `_resolve_chat_runtime_id` 重解析」；若仍无可用引擎则回 `⚠️ 未配置远端引擎`。
本方案**不改这条行为**，仅在最终落定使用某个远端 rid 前，若该 rid `_remote_runtime_ready==False`
可输出更精确的 `hint`（见 2.2 第 4 行），避免用户明明配了引擎却只看到"查询失败"。

### 2.4 展示层改造（`chat.py` 三处）

统一封装 `_format_chat_failure_text(task_id, enqueue_tid)`：

```python
def _format_chat_failure_text(enqueue_tid):
    from src.runtime.service import get_task, friendly_failure_hint
    tk = get_task(enqueue_tid) or {}
    hint, action = friendly_failure_hint(tk)
    return "查询失败：" + hint + "；" + action
```

- `chat.py:1969`（submit 远端 `remote_agent_fn`）、`chat.py:2245`（`_poll_worker`）、
  `chat.py:2357`（`_execute_chat_task` 同步 wait）三处替换原 `f"查询失败: {result_text}"`；
- 原始 `errors`/`stderr` 不进该文案，但 `get_task` 仍可被排障端读取；
- text 类事件/DB 落库内容即友好提示，前端零改动。

## 三、模块改动

| 模块 | 改动 |
|------|------|
| `src/runtime/service.py` | `fail_task` 失败原因双写 `result_text`；新增 `friendly_failure_hint(task)` |
| `src/api/routes/chat.py` | 新增 `_format_chat_failure_text`；替换 1969/2245/2357 三处展示 |
| `src/runtime/agent.py` | **不改**（worker 上报协议保持不变，避免双端升级联动） |
| `src/runtime/client.py` | **不改** |
| `tests/` | 见 §六（TDD，先红后绿） |

## 四、关键实现要点

- **只读展示，不改状态**：`friendly_failure_hint` 不写库、不触发 401 检测，纯纯函数；
- **顺序与幂等不动**：`fail_task` 的 completed/failed/stale 三重幂等保护都在写库之前，双写只影响
  失败任务列内新增 `result_text`，不影响这些守卫；
- **截断安全**：`result_text` 落库 500 字、展示兜底 200 字，防长栈撑爆聊天记录/卡片；
- **单点兜底**：未命中任何分类时，兜底文案必须能自解释（含原始片段 + "参考任务详情/worker 日志"），
  严禁再次出现「查询失败: 」空尾。

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| 失败任务 `result_text` | 从「恒空」变为「有可读原因」；仅影响失败路径，正常完成不受影响 |
| 事件 redispatch | `events.py` 读 `result_text` 展示失败原因的地方会同时拿到兜底原因（行为更友好，无破坏） |
| worker 协议 | 零改动，老 worker（err 为空）仍走兜底分类 |
| DB schema | 零迁移 |
| 文案 i18n | 友好提示当前为中文硬编码；与现有「查询失败」「⚠️ 未配置远端引擎」等文案一致，暂不做字典（可后续统一） |

## 六、测试（测试先行，TDD）

落地文件 `tests/test_chat_failure_hint.py`：

| ID | 场景 | 断言 |
|----|------|------|
| QA-01 | `fail_task` 收到 `"engine exit_code=1, errors: ..."` 后 `get_task` | `result_text` 非空且含原因；`errors` 保留原始 JSON |
| QA-02 | `fail_task` 幂等：already completed / already failed / stale report | 守卫行为不变（复用既有断言） |
| QA-03 | `friendly_failure_hint` 命中 exit_code≠0 且 errors 含 SQLite/DB 关键 | 返回"引擎/CLI 不兼容类" hint + 行动建议 |
| QA-04 | `friendly_failure_hint` 命中超时（exit_code<0 / timed out） | 返回超时 hint |
| QA-05 | `friendly_failure_hint` 命中 `_R16_ERROR_MESSAGES` token 类 | 返回鉴权失效 hint（复用既有 401 用例数据） |
| QA-06 | `friendly_failure_hint` 完全未知错误 | 兜底：原始片段截断 + "参考任务详情/worker 日志" |
| QA-07 | `_format_chat_failure_text` 三处展示点 | 输出恒为 `查询失败：<hint>；<action>`，不再出现空尾 |
| QA-08 | 回归：正常 completed 任务 | `result_text` 为结构化结论，不经过失败分类 |

## References

- `src/runtime/service.py`：`fail_task`(2512) / `update_task_status`(1712) / `get_task`(1347) / `_R16_ERROR_MESSAGES`(18)
- `src/runtime/agent.py`：`_do_report`(2392) / 失败文本组装(2419-2445)
- `src/runtime/client.py`：`fail`(261)
- `src/api/routes/chat.py`：1969 / 2245 / 2357（三处「查询失败」展示点）、`_remote_runtime_ready` 兜底(1772)
- 事故复盘：2026-10-29 opencode CLI 1.17.7 ↔ Desktop 1.18.18 共享 `opencode.db` schema 冲突 → worker 引擎 exit=1 → 「查询失败」无原因