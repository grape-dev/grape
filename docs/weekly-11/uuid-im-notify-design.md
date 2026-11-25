# IM 通知 UUID 账号兼容设计（Issue #3510）

## 背景

`grape_accounts.id` 自 D105 起是应用层 UUIDv7 字符串，但通知链多处仍按 `int` 处理
`user_id`。UUID 触发 `ValueError` 后被各层 `except` 吞掉，表现为「通知没发但调用方
以为成功」，站内信降级通道（Issue #3469 F 项）对 UUID 账号静默丢失。

## 根因

- `src/domains/runtime/im/outbox.py` `out_track_id()`：`int(user_id or 0)` 对 UUID 抛错。
- `src/domains/runtime/im/notify.py` `notify_user()` / `schedule_notify()`：入口裸 `int()`。
- `vod_handler_async.py` / `handler_async.py` 的用量采集 `MasterRun` attr 同样裸 `int()`。

## 方案

新增 `src.kernel.user_ids.norm_uid()`（与 Issue 中提到的 `_norm_uid()` 同口径）：
纯数字串转 `int`，其余原样保留字符串。

> 说明：`fix/issue-3469-email-verification-hardening` 分支上的私有 `_norm_uid()` /
> `enqueue_user_notify()` 尚未合入 `weekly-mys`，因此本修复把归一口径收敛到 D105 的
> `src/kernel/user_ids.py`，供通知链复用，避免复制私有实现。

- `outbox.out_track_id()`：数字仍以 `int` 参与 sha1（既有单号不漂移），UUID 用字符串。
- `notify.notify_user()` / `schedule_notify()`：入口走 `norm_uid()`，不再抛 `ValueError`；
  `schedule_notify()` 返回是否已创建投递任务（UUID/数字入队即 `True`，满足 AC）。
- `notify._issue_owner()`：`issue.user_id` 为 TEXT（`schema_ddl.py:217`，D105 后可为 UUID），
  返回改为 `norm_uid(row[0] or 0)`，注解 `tuple[int | str, str, str]`，避免 UUID 归属 Issue
  经 `notify_issue_event()` 时被裸 `int()` 吞成 uid=0；`notify_issue_event()` 对 UUID uid 正常扇出。
- `notify._make_payload()`：`user_id` 统一归一（数字 int / UUID str），消费侧原样使用。
- 两处 `MasterRun` attr（`handler_async._usage_user_id()` / `vod_handler_async._usage_user_id()`）：
  `norm_uid()` 替代裸 `int()`。

### 取舍说明：payload `user_id` 数字仍为 int

Spec 技术路径第 3 条写的是「payload `user_id` 统一序列化为字符串」。本实现**保留数字为 int、
UUID 为 str**，属于有意取舍：

- 既有消费侧按 int 读取数字账号，统一转 str 会改动其类型契约，扩大回归面；
- 归一为 `int | str` 已消除裸 `int()` 抛错，UUID 通知能力不受影响；
- 显式用例锁定「数字 payload 仍为 int」，避免后续误改。

### 范围说明：`handler_async.py` 保留 `norm_uid` 的依据

`handler_async.py` 该处 `user_id` 取自 `get_user_id_by_app_id(ctx.app_id)` 的数字域，前序复核
（报障用户，handle 已脱敏）已判定**非本缺陷**；`norm_uid` 对数字输入为恒等（int→int），不改变行为，故保留以
统一口径，并在本 PR 说明中注明该依据。

## 测试

`tests/runtime/im/test_issue_3510_uuid_notify.py`：

- 数字 id 的 `out_track_id` 与旧 sha1 口径逐字节一致；
- UUID 账号 `notify_user` 经真实 `out_track_id` 落 outbox（仅打桩目标解析与引擎）；
- `schedule_notify` 对 UUID 返回 `True`、不抛错并真实入队；
- `_issue_owner()` 对 UUID 归属 Issue 保留 str、数字仍归一 int，`notify_issue_event()` 不丢通知；
- 数字账号 payload `user_id` 仍为 int（消费侧兼容）；
- 两处 handler `_usage_user_id()` 的行为/类型断言 + 源码守卫。

## 验收对照

- UUID 账号 `schedule_notify` 返回 `True` 且 outbox 有入队行；
- 数字账号行为与单号不变；
- UUID 归属 Issue 的 `notify_issue_event()` 不再静默丢失；
- 所有现有 IM 通知 / outbox 用例通过。
