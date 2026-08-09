# Web Chat 无回复事故复盘（2026-08-09）

> 日期：2026-08-09
> 状态：已解决（528c414 + 回归测试看护）
> 关联：`opencode-incident-20260808.md`（opencode 管道/DB 类故障的首次复盘）

---

## 一、现象

- Web chat 发送消息**无回复**，前端最终显示兜底文案（后改为「⚠️ 未收到回复，连接可能中断，请重试」）
- 飞书 chat bot **正常**回复
- opencode 日志显示 agent 在正常处理（loop step 0/1、执行工具），但 grape 侧拿不到结果

## 二、根因（两个叠加，缺一不可）

### 根因 A：opencode 只在 stdout 为「带控制终端的 TTY」时输出 NDJSON

- opencode（Bun 二进制）`run --format json` 在 stdout 为**纯管道**时**不输出任何 NDJSON**（挂到超时）
- grape 的 `subprocess.Popen(stdout=PIPE)` 正是纯管道 → 拿不到输出
- **修复**：POSIX 下用 `script -qec '<cmd>' /dev/null` 包装，给 opencode 分配**完整控制终端**，其 stdout 是管道，grape 照常 `readline` 读取
- 手写 `pty.openpty()` 只给 TTY stdout **不够**（缺控制终端），实测仍不输出；`script` 才分配控制终端（验证：带/不带 TOKEN/SK env、完整 web prompt、主线程/线程均正常）

### 根因 B：`send_message_stream` / `send_message` 的 agent_worker 日志对 dict 切片崩溃

- `stream_agent` 的 `tool_use` 事件自 M1 起 yield **dict**（`{"tool": ..., "input": ...}`）
- agent_worker 日志 `log.info(f"  🔧 {data[:150]}")` 对 dict 切片 → `KeyError: slice(None, 150, None)` → **agent_worker 异常中止** → 无后续事件、无 done → web chat 无回复
- 飞书走 `consume_stream_async`（无此日志 bug）→ 一直正常
- **修复**：改 `str(data)[:150]`

### 两者关系

- 根因 B 一直存在，但根因 A（管道拿不到 NDJSON）掩盖了它——连 step_start 都到不了
- script 修复（A）让 NDJSON 到达后，agent 读到 SKILL.md（第一个 tool_use）即触发根因 B

## 三、排障过程（含误判，供后续少走弯路）

| 阶段 | 观察 | 误判/动作 |
|---|---|---|
| 1 | web chat 无回复、飞书正常 | 误判为「差异在参数/env/prompt」；SSH 直测全挂，误判 opencode 坏了 |
| 2 | 裸管道 `opencode run > file` 0 字节；PTY(`script`)正常 | **正确定位根因 A**（需控制终端） |
| 3 | 手写 pty.openpty 仍不输出 | 误判「PTY 不够是玄学」，后确认缺**控制终端** |
| 4 | script 修复后：飞书仍通、web 只到 step_start 后停 | 反复查线程/残留进程/Semaphore，均不是 |
| 5 | 加全链路日志 | **当场抓到** `agent_worker EXC: KeyError: slice(None,150,None)` → 根因 B |

**教训**：当「SSH 直测正常但 grape 里异常」时，不要反复猜上下文，应**在 grape 内加日志**一次性定位。第 4 步浪费了大量时间。

## 四、修复清单

1. `src/agent/executor.py` `_run_once`：POSIX 用 `script -qec '<cmd>' /dev/null` 包装 opencode（分配控制终端），非 POSIX 回退纯管道
2. `src/api/routes/chat.py`：`tool_use` 日志 `data[:150]` → `str(data)[:150]`（`send_message` + `send_message_stream` 两处）
3. Web chat 前端兜底文案 `'Done'` → 明确提示（`chat.js`，此前已改）
4. Web chat SSE 60s 无响应看门狗（`chat.py`，此前已加）

## 五、回归测试看护

`tests/test_web_chat_tooluse.py`（3 用例）：

| 用例 | 守护 |
|---|---|
| `test_send_stream_handles_dict_tool_use` | mock `stream_agent` 产出 dict tool_use → `/send/stream` 不得出现「后端错误」，须含 done 事件 |
| `test_send_handles_dict_tool_use` | 同上，`/send` 返回正确 reply |
| `test_tool_use_logging_uses_str` | 源码中不得再出现 `data[:150]` 裸切片 |

**验证方式**：将 `str(data)[:150]` 改回 `data[:150]`，上述用例必失败（agent_worker 崩溃 → 「后端错误」）。

## 六、后续建议

- 排查「grape 内异常 vs SSH 正常」类问题：第一时间在 grape 内加日志，不要外部反复复现
- 日志对事件 data 统一 `str(data)`，避免对可变结构（dict/list）做切片/索引假设
- 保留 executor 的「reader EOF total_lines」「done 摘要」日志，便于后续判断是「没输出」还是「输出没读到」
