# Issue #727 需求：worker hermes 引擎 LLM 用量采集对 hermes 0.9.0 的兼容修复（需求文档）

> 来源：用户反馈 #502（hermes 0.9.0 现场三处报错/少计）。
> 修复方案见 `issue-727-hermes-usage-compat-design.md`。

## 背景

worker 的 hermes 引擎 LLM 用量采集（`src/runtime/usage_collect.py` + `src/agent/hermes_acp.py`）
对 hermes 0.9.0 的 state.db 表结构与 ACP usage 回传形态假设不符：

1. `_hermes_rows()` 假设 state.db 存在 `session_model_usage` 表——hermes 0.9.0 没有该表
   （0.10+ 才有），SQL 直接报 `no such table`，用量采集报错；
2. `_cleanup()` 删除临时目录后未复位 `_tmpdir`——worker 侧 `_report_llm_usage`
   对 hermes 有「首采为空 → 1.5s 后二次 finish」的短重试，二次 finish 时
   `_workdir()` 返回已删除目录，拷账本报 `FileNotFoundError`（Errno 2）；
3. hermes ACP 的 `session/prompt` PromptResponse 其实携带 `result.usage`
   （token 计数），但 `hermes_acp.py` 未解析、未入账——用量 0 条落盘。

## 需求（Spec 对齐）

- **治本**：`hermes_acp.py` 解析 PromptResponse `result.usage`，经事件流
  （`("usage", {...})` 事件）喂给 `UsageCollector`；`agent.py` 的 feed 路由
  把该事件只喂采集器、不进消息流；
- **兜底**：`_hermes_rows()` 缺 `session_model_usage` 表时回退读 `sessions`
  表（同是累计值、按 session 取差值），告警降级一次、不中断采集；
- **幂等**：`_cleanup()` 复位 `_tmpdir`，二次 finish 不再 Errno 2；
- **测试**：`tests/test_obs_llm_usage.py` 覆盖上述三处回归；
- **契约**：明确 worker 对 hermes 版本的用量采集兼容矩阵（见设计文档 §5）。

## 验收标准

1. hermes 0.9.0 跑任务用量采集零异常（不抛 `no such table` / Errno 2）；
2. 用量落盘条数 > 0（PromptResponse 直报路径）；
3. 缺表时降级告警一次、采集继续（回退 sessions 表）；
4. `finish()` 幂等：连续两次调用行为一致、无异常；
5. 既有用例（`tests/test_obs_llm_usage.py`）全部通过。
