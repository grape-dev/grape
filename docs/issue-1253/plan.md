# Plan: Issue #1253 — Hermes 静默杀死阈值 300s → 600s

## 方案
1. **hermes_acp.py**：`_silent_timeout_for` 中默认串 `"300"` → `"600"`，异常回退 `300.0` → `600.0`；docstring 与调用处注释同步。
2. **runtime/agent.py**：`_DEFAULT_SILENT_WINDOW = 300` → `600`；#378 注释中「其余默认 300s」「300s 会在正常进行中误杀」同步为 600s 语义（coding/qa=1200s 不变）。
3. **executor.py**：`os.environ.get("AGENT_SILENT_WINDOW", str(min(300, max_exec_seconds)))` → `min(600, ...)`；同步「与 hermes ACP _SILENT_TIMEOUT 对齐」注释与超时总账示例（600+600+10=1210s → 2×2420s < 3600s 仍安全）。
4. **tests/test_agent_silent_timeouts.py**：默认与非法回退断言 300.0 → 600.0，测试名同步；executor 用例注释 `min(300,0.1)` → `min(600,0.1)`（max_exec=0.1 时结果仍 0.1，行为不变）。

## 测试 seam
- 沿用现有单测 seam：直接调用 `ha._silent_timeout_for(env)`、断言 `ra._DEFAULT_SILENT_WINDOW`、`stream_agent` 行为级测试（env 注入 1200 不被强杀 / 默认 0.1s 强杀）。
- 不新增 seam；全量跑 `tests/test_agent_silent_timeouts.py`。

## 回归风险
- executor 默认窗口提升到 600s 后，`min(600, max_exec)` 仍保证不超过任务上限；`test_default_silent_window_kills_fast`（max_exec=0.1）行为不变。
- 总账示例仍满足 `RUNTIME_TASK_TIMEOUT=3600s ≥ 2×(最坏单次)`，无超时回归。