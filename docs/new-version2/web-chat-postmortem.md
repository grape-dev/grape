# Web Chat 无回复事故复盘（2026-08-09）

> 日期：2026-08-09
> 状态：已解决（528c414 + 回归测试看护）
> 关联：`opencode-incident-20260808.md`（opencode 管道/DB 类故障的首次复盘）

---

## 一、现象

- Web chat 发送消息**无回复**，前端最终显示兜底文案（后改为「⚠️ 未收到回复，连接可能中断，请重试」）
- 飞书 chat bot **正常**回复
- opencode 日志显示 agent 在正常处理（loop step 0/1、执行工具），但 grape 侧拿不到结果

## 二、根因

### 真正根因（唯一代码回归）：`send_message_stream` / `send_message` 的 agent_worker 日志对 dict 切片崩溃

- `stream_agent` 的 `tool_use` 事件自 M1（`d4ab258`）起 yield **dict**（`{"tool": ..., "input": ...}`）
- agent_worker 日志 `log.info(f"  🔧 {data[:150]}")` 对 dict 切片 → `KeyError: slice(None, 150, None)` → **agent_worker 异常中止** → 无后续事件、无 done → web chat 无回复
- 飞书走 `consume_stream_async`（无此日志 bug）→ 一直正常
- **修复**：改 `str(data)[:150]`（`send_message` + `send_message_stream` 两处）

### 曾误判的「根因 A」：opencode 需控制终端 / script 包装 —— 已证伪并移除

- 排障初期观察到「裸 `opencode run > file` 得 0 字节」，误判 opencode 在纯管道不输出，加了 `script` 包装分配控制终端
- **证伪依据**：飞书一直走 grape `Popen(stdout=PIPE)` 纯管道正常回复 → **opencode 在管道下可输出**，管道不是问题
- pre-script 的「web 无输出」实为 **opencode.db 被反复测试污染**（环境）：16:53 重建 DB 后 16:57 飞书立即恢复
- **结论**：`script` 包装是误判下的多余修复，且**远端 worker 无 util-linux `script` 会退化**（queue_remote 隐患），已移除（`9e2745b`）
- 纯管道 + 仅 tool_use 修复，web chat 验证正常

## 三、排障过程（含误判，供后续少走弯路）

| 阶段 | 观察 | 误判/动作 |
|---|---|---|
| 1 | web chat 无回复、飞书正常 | 误判为「差异在参数/env/prompt」；SSH 直测全挂，误判 opencode 坏了 |
| 2 | 裸管道 `opencode run > file` 0 字节 | **误判根因 A**（opencode 需控制终端），加 script 包装 |
| 3 | script 后：飞书仍通、web 只到 step_start 后停 | 反复查线程/残留进程/Semaphore，均不是 |
| 4 | 加全链路日志 | **当场抓到** `agent_worker EXC: KeyError: slice(None,150,None)` → 真正根因 |
| 5 | 移除 script、回归纯管道 | 纯管道 + tool_use 修复验证正常 → **根因 A 证伪** |

**教训**：
1. 当「SSH 直测正常但 grape 里异常」时，不要反复猜上下文，应**在 grape 内加日志**一次性定位。
2. **对照组要覆盖关键反例**：飞书一直纯管道正常，本应第一时间推翻"管道不输出"假设。
3. 修复前先想清「远端/跨机」影响：script 依赖 util-linux，远端 worker 不可用。

## 四、修复清单

1. `src/api/routes/chat.py`：`tool_use` 日志 `data[:150]` → `str(data)[:150]`（`send_message` + `send_message_stream` 两处）——**唯一代码修复**
2. `src/agent/executor.py`：**保持纯管道**（与 main 一致），移除误加的 script 包装（`9e2745b`）
3. Web chat 前端兜底文案 `'Done'` → 明确提示（`chat.js`，体验改善）
4. Web chat SSE 60s 无响应看门狗（`chat.py`，体验改善）
5. 运维：反复测试污染 opencode.db 时重建（`~/.local/share/opencode/opencode.db*`）恢复

## 五、回归测试看护

`tests/test_web_chat_tooluse.py`（3 用例）：

| 用例 | 守护 |
|---|---|
| `test_send_stream_handles_dict_tool_use` | mock `stream_agent` 产出 dict tool_use → `/send/stream` 不得出现「后端错误」，须含 done 事件 |
| `test_send_handles_dict_tool_use` | 同上，`/send` 返回正确 reply |
| `test_tool_use_logging_uses_str` | 源码中不得再出现 `data[:150]` 裸切片 |

**验证方式**：将 `str(data)[:150]` 改回 `data[:150]`，上述用例必失败（agent_worker 崩溃 → 「后端错误」）。

另在 `tests/test_agent_executor.py` 新增 `TestExecutorPipeInvocation`：

| 用例 | 守护 |
|---|---|
| `test_popen_uses_direct_command_and_pipe` | mock `subprocess.Popen` 捕获启动命令 → 断言命令是直接 agent CLI（无 `script` 包装）、stdout 为纯管道——防止误判再引入 script 包装（远端隐患） |

## 六、后续建议

- 排查「grape 内异常 vs SSH 正常」类问题：第一时间在 grape 内加日志，不要外部反复复现
- 日志对事件 data 统一 `str(data)`，避免对可变结构（dict/list）做切片/索引假设
- 对照组要覆盖关键反例（如飞书一直纯管道正常），先证伪"机制性假设"再修
- 修复前评估「远端/跨机」影响（如 script 依赖 util-linux，远端 worker 不可用）
- 保留 executor 的「reader EOF total_lines」「done 摘要」日志，便于后续判断是「没输出」还是「输出没读到」
