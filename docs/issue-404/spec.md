# Spec: Issue #404 — 场景绑定引擎全部离线缺乏告警

> 本文件为本 Issue 开发本地说明，权威 Spec 已由 Issue Agent 发布在 Issue #404 评论。
> 此处仅做开发落地映射，不重复全文；与 Issue 评论一致。

## 目标
为"场景绑定引擎全部离线"补齐主动发现与自愈：离线即告警（1h 静默去重）、
恢复后一键重派联动、场景配置离线校验。纯主控侧改动，不改 worker，无 schema 变更。

## 范围（In Scope）
1. **离线主动告警**：`event_pipeline._fail_scene_all_offline` 触发 `_emit_scene_offline_alert`，
   内存 TTL 字典去重（1h，Redis 降级为内存——本仓未引入 Redis），走现有 `_notify_webhook` /
   飞书发送器（`src/observability/alerts.py`）。
2. **恢复后一键重派**：`engine_probe._probe_loop` 心跳恢复（fail→ok）钩子 +
   `events.py` 新增 `GET /events/redispatchable` 列表入口；实际重派复用现有
   `POST /events/{seq}/redispatch`（events.py:379/762），其内部已重新校验 pool。
3. **场景配置校验**：`scenes.py update_scene` 保存前查询绑定引擎离线情况，返回 `warnings`
   字段（仅提示，不阻断保存）。
4. **5 条验收单测**：见 tests/test_issue_404_scene_offline_alert.py。

## 范围（Out of Scope）
- worker 侧改动（告警与重派均在主控侧）。
- runtime_id 快照失配误判离线（属 #362 范畴）。
- 派发层拒绝入队逻辑（#259 已闭环，不回退）。
- 默认池（无场景绑定路径）的"全部离线"误报。
- DB schema 变更。

## 技术决策
| 决策点 | 选择 | 理由 |
|--------|------|------|
| 告警去重存储 | 进程内字典 + TTL（Redis 降级为内存） | 本仓未引入 Redis；内存字典重启丢失可接受（spec 设计决策已允许降级） |
| 静默期 | 1h | 对齐验收标准第 3 条 |
| 重派触发 | 手动一键（GET 列表 + 现有 POST redispatch） | 避免恢复瞬间雪崩；用户保留控制权 |
| 场景校验 | 仅 warning，不阻断 | 不阻断正常配置流 |
| 告警通道 | 复用 `_notify_webhook`（OBS_ALERT_WEBHOOK_URL）+ 飞书 recipients | 走现有发送器，不新建通道 |

## 受影响模块
| 模块 | 改动 | 说明 |
|------|------|------|
| src/api/event_pipeline.py | 改 | 新增 `_emit_scene_offline_alert` + TTL 字典；`_fail_scene_all_offline` 接入 |
| src/observability/engine_probe.py | 改 | `_probe_loop` 新增 fail→ok 转换检测 + `_on_engine_recovery` 钩子 |
| src/api/routes/events.py | 改 | 新增 `GET /events/redispatchable` 列表端点 |
| src/api/routes/scenes.py | 改 | `update_scene` 返回 `warnings` 字段（绑定引擎离线提示） |
| tests/test_issue_404_scene_offline_alert.py | 新增 | 5 条验收单测 |

## 验收标准（与 Issue 一致）
1. 引擎全部 offline → 事件 failed 且 result 含 scene id / agent
2. 恢复 online 后一键重派 → 新任务派发成功且原事件终态不变
3. 同场景 1h 内重复离线 → 告警只发一次
4. pool 有兜底引擎 → claim 正常不受影响
5. 空场景走默认池 → 不误报全部离线
