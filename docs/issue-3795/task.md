# Issue #3795 任务拆解

| # | 任务 | 文件 | 状态 |
|---|---|---|---|
| 1 | 新增 `ChannelWsClosed` 契约异常（close_code + `ws closed code=`） | `src/domains/runtime/im/channel/channel.py`、`channel/__init__.py` | done |
| 2 | 飞书 `_ws_failure()` 带关闭码归一为 `ChannelWsClosed` | `src/domains/runtime/im/adapters/feishu/channel.py` | done |
| 3 | 关闭码有界归因键 `ws_close_cause()` + `record_ws_close()` | `src/domains/runtime/im/engine/metrics.py` | done |
| 4 | supervisor WARNING 分支 + 泄漏 `ConnectionClosed` duck-typing 兜底 | `src/domains/runtime/im/engine/supervisor.py` | done |
| 5 | 单测（适配器 / supervisor / 分桶 / 取码 / 真 bug 守护） | `tests/runtime/im/test_issue_3795_ws_close_attribution.py` | done |
| 6 | 回归 + 门禁（`tests/runtime/im`、`make lint`、`check-arch.py`、silent-except ratchet） | — | done |
| 7 | 建 PR + Issue 评论 + A2A QA | — | pending |
| 8 | 发布后 LogQL 观测验证（`channel crashed (unexpected)` = 0） | 由 sre 执行 | out of scope |