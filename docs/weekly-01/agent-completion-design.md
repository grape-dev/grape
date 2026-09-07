# Grape Agent 任务完成判定机制方案

## 1. 现状与问题

### 1.1 当前架构

```
主控(service.py)                    Worker(agent.py)
    │                                    │
    │──── dispatch task ────────────────>│
    │                                    │── stream_execute(prompt)
    │<─── report_messages(step_*) ───────│   │
    │                                    │   ├── hermes ACP / opencode / codearts
    │<─── report_messages(text/tool) ────│   │
    │                                    │   │
    │<─── complete(result_text, ─────────│<── done {text, exit_code, session_id}
    │         exit_code)                 │
    │                                    │
    │  parse_agent_output_json(result_text)
    │  ├─ 有结构化 JSON → completed
    │  └─ 无结构化 JSON → PREMATURE_END_TURN → requeue
    │                                    │
```

### 1.2 当前的问题

| 问题 | 根因 | 影响 |
|------|------|------|
| JSON 被 ACP chunk 拆分，提前截断 | `_on_message` 检测到部分 JSON 字段就 `prompt_done.set()`，hermes 进程被中断，JSON 未输出完整 | `parse_agent_output_json` 解析失败 → PREMATURE_END_TURN → 无效重试 |
| 两端 JSON 检测标准不一致 | worker 端 `_detect_structured_output` 用字符串匹配（宽松），主控端 `parse_agent_output_json` 用 `json.loads`（严格） | worker 认为完成了，主控认为没完成 |
| 单步 vs 多步判定混乱 | hermes ACP 原设计是单步（一个 prompt → end_turn → done），LLM 有时在分析完就 end_turn 不执行动作 | 任务被判定为未完成，service.py 反复重试 |
| attempt 2/3 未执行 | `collected_text` 未在 attempt 间清空 + `reader_done` 残留 → 内循环误 break | 新进程重试机制形同虚设，实际靠 service.py 重试 + LLM 随机性 |

### 1.3 与 opencode 的对比

| 维度 | opencode | hermes ACP |
|------|----------|------------|
| 进程模型 | 单进程，内部循环 | 单进程，end_turn 即结束 |
| 工具回传 | 进程内部，LLM 拿到结果继续 | 正常（单 turn 内可多次调用） |
| 多步驱动 | 引擎自己决定（进程不退出就继续） | 需要外部驱动（Grape 侧循环） |
| 完成判定 | 进程退出 = 完成 | 需要检查结构化输出 |
| 续步方式 | 不需要 | 新进程 + session/resume |

## 2. 方案：单步优先 + 结构化完成信号

### 2.1 核心原则

**一个任务尽量在一个 step 内完成。** agent 在单个对话轮次内完成：读文件 → 分析 → 执行动作 → 输出结构化 JSON → end_turn。

多步（新进程重试）仅作为兜底——当 LLM 提前 end_turn 且无结构化输出时，才触发重试。

### 2.2 完成判定标准（统一）

**唯一的完成信号：agent 输出中包含可被 `json.loads` 成功解析的 JSON 块，且包含 `actions` / `conclusion` / `issue_summary` 中的任意一个字段。**

worker 端和主控端使用**同一个函数、同一个标准**判断：

```python
# 统一判定函数（放在共享模块中，worker 和主控都调用）
def is_task_complete(text: str) -> bool:
    """判断 agent 输出是否包含完整的结构化 JSON 完成信号。"""
    if not text:
        return False
    # 1. 去掉 markdown code fence
    clean = text.replace("```json", "").replace("```", "")
    # 2. 提取最后一个 JSON 块（greedy match 最后一个 {...}）
    matches = list(re.finditer(r'\{[^{}]*\}', clean, re.S))
    if not matches:
        # 尝试嵌套 JSON
        m = re.search(r'\{.*\}', clean, re.S)
        if not m:
            return False
        matches = [m]
    raw = matches[-1].group(0)  # 取最后一个
    # 3. 直接解析
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and any(k in obj for k in ("actions", "conclusion", "issue_summary")):
            return True
    except Exception:
        pass
    # 4. 去空白后解析（修复 chunk 拆分）
    try:
        compact = re.sub(r'\s+', '', raw)
        obj = json.loads(compact)
        if isinstance(obj, dict) and any(k in obj for k in ("actions", "conclusion", "issue_summary")):
            return True
    except Exception:
        pass
    return False
```

### 2.3 worker 端流程（hermes_acp.py）

```
stream_execute(prompt):
    attempt 1:
        启动 hermes 进程
        session/new
        发 prompt
        内循环等 end_turn / turn_end
        ├─ 收到 turn_end → 正常结束
        ├─ 15s 静默 → 假设结束（兜底）
        └─ 检测到完整 JSON（含闭合 }）→ 假设结束（兜底）
        
        flush 所有 text
        
        if is_task_complete(full_text):
            → exit_code=0, yield done, 结束
        else:
            → 记录 text 摘要，准备 attempt 2
    
    attempt 2 (兜底):
        关闭旧进程
        启动新进程（复用 iso_home）
        session/resume（恢复上下文）
        发 continuation prompt（带上一步摘要）
        内循环等 end_turn
        flush
        
        if is_task_complete(full_text):
            → exit_code=0, yield done, 结束
        else:
            → 空步检测，准备 attempt 3
    
    attempt 3 (最后机会):
        同 attempt 2
    
    yield done (exit_code=0 或 -1)
```

**关键规则：**
- `_on_message` 中的 JSON 提前检测：必须同时满足 `is_task_complete` + 有闭合 `}`，才触发 `prompt_done`
- 不再单独用 `_detect_structured_output`（字符串匹配），统一用 `is_task_complete`（json.loads）
- attempt 间清空 `collected_text` / `_text_buf` / `_think_buf`
- `reader_done.clear()` 在新进程启动后、内循环前

### 2.4 主控端流程（service.py）

```
complete_task(task_id, result_text, exit_code):
    if exit_code != 0:
        → status=failed, 结束
    
    if is_task_complete(result_text):
        → 解析 JSON → 写 event.json 档案
        → status=completed, 结束
    
    # exit_code=0 但无结构化输出
    if attempts < 2:
        → PREMATURE_END_TURN
        → requeue (attempts+1)
    else:
        → status=failed
        → result_text += "[PREMATURE_END_TURN] 最大重试次数已达"
```

**关键规则：**
- 主控端不再用 `parse_agent_output_json`（只做 JSON 解析），改用 `is_task_complete`（与 worker 一致）
- 如果 `is_task_complete` 返回 True，再用 `parse_agent_output_json` 提取 JSON dict 写档案
- PREMATURE_END_TURN 只在 `is_task_complete` 返回 False 时触发

### 2.5 continuation prompt 设计

```
你在上一轮回复的末尾说了：
"{上一步 text 输出末尾 800 字}"

但你没有实际执行这些操作（未检测到结构化处理记录）。
请立即调用工具执行上述操作，不要重新分析，不要再说"Let me..."。
如果你需要读取 SKILL.md 或项目知识，请先读取再执行。
完成后在回复末尾输出处理记录 JSON 块：
{"actions": [...], "conclusion": "...", "issue_summary": "..."}
```

### 2.6 prompt_builder.py 系统提示词

保留"执行纪律"段落，强化单步完成引导：

```
## 执行纪律

你必须在单个对话轮次内完成全部操作：读取文件 → 分析 → 执行动作
（发评论/加标签/A2A 交办等）→ 输出处理记录 JSON → 结束。

⚠️ 禁止"分析完就结束"：如果你在回复中说"Let me..."或"现在我将..."
但实际没有调用工具执行该操作，任务将被判定为未完成并重新执行。

⚠️ 必须调用工具执行实际动作：发评论用 execute_code 调 GitCode API，
不要只输出评论内容就 end_turn。

⚠️ 完成信号：所有动作执行完毕后，必须在回复末尾输出一个完整的 JSON 块：
{"actions": [...], "conclusion": "...", "issue_summary": "..."}
没有这个 JSON 块，系统无法识别任务完成。
```

## 3. 不改的部分

| 组件 | 原因 |
|------|------|
| opencode/codearts 路径 | 进程内循环，不存在此问题 |
| service.py 的 attempts 上限（3 次） | 合理兜底 |
| service.py 的 `_detect_business_failure` | 独立逻辑，不冲突 |
| worker 的 report_messages 机制 | 消息上报正常，只是完成判定逻辑需要统一 |

## 4. 验证矩阵

| 场景 | 预期行为 | 验证点 |
|------|----------|--------|
| agent 单步完成 + 输出完整 JSON | attempt 1 完成，status=completed | `is_task_complete` 返回 True |
| agent 单步完成但 JSON 被 chunk 拆分 | attempt 1 完成，status=completed | 去空白后 `json.loads` 成功 |
| agent 分析完 end_turn，无 JSON | attempt 1 失败 → attempt 2 resume + continuation → 完成 | attempt 2 有 tool_use + JSON |
| agent 两轮都不干活 | 空步检测 → attempt 3 → 仍然无 JSON → failed | `_consecutive_no_tool >= 2` |
| agent 输出部分 JSON 后静默 | 15s 静默超时 → `is_task_complete` 检查 → False → requeue | 不误判为完成 |
| agent 输出完整 JSON 后静默 | `is_task_complete` + `}` 检查 → True → 完成 | 不截断 JSON |

## 5. 实施步骤

1. 创建共享函数 `is_task_complete`（放在 `src/api/event_store.py` 或独立模块）
2. hermes_acp.py：`_detect_structured_output` → 改用 `is_task_complete`
3. hermes_acp.py：`_on_message` JSON 检测 → 改用 `is_task_complete` + `}` 检查
4. service.py：`complete_task` 中的完成判定 → 改用 `is_task_complete`
5. service.py：`parse_agent_output_json` 保留用于档案解析（不改完成判定）
6. 验证：找一个 issue agent 任务跑一遍，确认 attempt 1 完成 + status=completed
