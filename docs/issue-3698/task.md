# Issue #3698 任务拆解

## 切片与提交序列（TDD：先红后绿）

| # | 提交 | 内容 | 红/绿 |
|---|------|------|-------|
| 1 | `test(#3698): …失败用例` | 契约新增 `ChannelRegistrationConflict`（additive）+ 飞书/退出 Supervisor 两组必红用例 | 红 |
| 2 | `fix(#3698): 飞书接收循环/握手传输层异常归一` | `_ws_close_code` / `_ws_failure` + `connect()` 外层 try/except | 绿（飞书 5 例） |
| 3 | `fix(#3698): …归因/退避/单连接` | Supervisor 专用分支、`next_backoff`、`_backoff_sleep` seam、app 去重、`FLAP_CAUSES` 加桶 | 绿（Supervisor 7 例） |
| 4 | `docs(#3698): …` | 本目录 spec/plan/task | — |

## 验收对照

| Spec 验收项 | 落点 |
|---|---|
| 断连不再打 `crashed (unexpected)` | `adapters/feishu/channel.py::connect` 外层归一；`tests/.../test_feishu_channel_lifecycle.py` 5 例 |
| 3001 独立归因 + WARNING | `engine/supervisor.py::_run` 专用分支；`test_supervisor_register_conflict.py::test_register_conflict_attributed_not_unexpected_crash` |
| 退避突破 15s + 升级告警 | `next_backoff(..., register_conflict=True)`；`test_conflict_backoff_breaks_legacy_15s_cap`、`test_register_conflict_escalates_to_error_alert`、`test_next_backoff_caps_per_cause` |
| 同 app_id 单连接/单租约 | `Supervisor._ensure_channel` app 去重 + `_drop_app_ref`；`test_duplicate_app_id_keeps_single_registration`、`test_same_installation_rescan_is_not_treated_as_duplicate`、`test_skipped_duplicate_takes_over_after_owner_released` |
| 现有测试全绿 | `python3 -m pytest tests/runtime/im -q`（含按 `FLAP_CAUSES` 校正的 stale 断言） |

## 复核要点（给 QA）

- `_ws_close_code` 只认 websockets 关闭对象的 `int` 关闭码（`ConnectionClosed` / `frames.Close`），
  沿 cause/context 上溯一次即止环；非 WS 异常的同名 `.code` 不参与判定，不可退化为日志文本匹配。
- 冲突计数重置依据**上一轮 connect() 的真实存活时长**（`last_session_secs`），不能用本轮
  attempt 的 elapsed（冲突分支恒为握手期毫秒级失败）。
- Supervisor 分支顺序：`PermanentChannelError` → `ChannelRegistrationConflict` → `ChannelError`
  → `Exception`（子类必须先于父类，否则 3001 又落回通用分支）。
- app 去重在 `try_acquire` **之前**返回，绝不出现「持租约但不建连接」。
- app 身份只探测适配器真实读取的键（`app_id`/`bot_id`/`ilink_bot_id`/`bot_token`）；
  日志标识白名单外一律摘要（Slack/Telegram token 位、企微 `bot_id`、微信 `ilink_bot_id`）。