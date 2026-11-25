# Issue #3818 实现方案

## 模块与落点

| 模块 | 改动 |
|------|------|
| `src/domains/runtime/engine/payload_guard.py`（新增） | `normalize`（两轮 URL 解码/去零宽/空白折叠/小写）、`scan`（7 类规则，hard/soft 分级）、`redact`（预签名 query/口令/token/AK-SK/Bearer/私钥）、`payload_hash`、`metadata_for_log`、`instruction_from_autopilot_prompt` |
| `src/domains/runtime/engine/payload_dispatch_guard.py`（新增） | 熔断台账（runtime 表 `grape_t_autopilot_payload_guard`）：`record_dispatch` / `record_failure` / `guard_state`；阈值默认 3，`GRAPE_AUTOPILOT_PAYLOAD_DISPATCH_LIMIT` 可配，`GRAPE_AUTOPILOT_PAYLOAD_GUARD_ENABLED=0` 可回滚；台账故障 fail-open |
| `src/domains/mgmt/routes/autopilot.py` | 创建/更新指令入库前审查（命中 400 + 稳定 `reason_code=payload_rejected` + findings + 审计行，不落原文）；读侧 `dispatchable` 对载荷拦截状态返回 false |
| `src/domains/runtime/engine/scheduler.py` | 派发前闸门（审查 → 熔断），早于 `_create_execution`；手动触发同门；拦截写 `blocked_payload(_repeat)` + `[autopilot-alert]` |
| `src/domains/runtime/engine/direct_executor.py` | `prompt built` 改为只输出长度 + payload hash 前缀 + 技能；fake completion 降级 failed 时登记同载荷失败（仅 autopilot 合成 seq） |
| `src/domains/runtime/routes/task_logs.py` | 执行详情回显 prompt 同源脱敏（长度保留原值） |
| `src/domains/runtime/schema_ddl.py` / `src/api/db_setup.py` / `configs/table_ownership.json` | 新表 DDL + 存量迁移 `202611250004`（runtime 域，原 202611250001 撞号已 renumber；含 post_check）+ 归属登记 |

## 关键决策

1. **规则分级**：`curl|sh`、内联口令、内联私钥、公网 SSH + 跳 host key、读敏感目录后上传、
   打包后上传 → hard 拒；单独预签名链接 → soft 告警（避免误杀合法 OBS 运维任务）。
2. **熔断只由「拒绝」驱动**：fake completion 降级 failed 时登记同载荷失败；`dispatch_count` 仅作
   台账遥测。正常周期任务（同指令、从不被拒）不被停派，环境性失败由 #3800 通道治理。
   解除方式：修改指令（换新 payload hash，新台账行）或按运维流程清 `grape_t_autopilot_payload_guard` 行。
3. **worker/调度同键**：调度侧 hash 归一化 instruction；worker 侧从 autopilot prompt 的
   「任务指令」段反抽原文再 hash，保证两侧命中同一台账行。
4. **fail-open 边界**：仅「台账不可用」fail-open；命中规则一律硬拒（fail-closed）。

## 测试

`tests/runtime/test_issue_3818_payload_guard.py`：规则命中/绕行变体、脱敏与元信息、同键校验、
熔断阈值/配置/开关/故障降级、调度闸门、入库拒绝、读侧 dispatchable、环境性失败排除。

## 待决策（数值证据，B2 五源闸门）

| 数值 | 位置 | 缺哪种测量 | 现状 |
|---|---|---|---|
| `DEFAULT_LIMIT = 3`（同载荷连续失败阈值） | `src/domains/runtime/engine/payload_dispatch_guard.py` | 同载荷重复投递的失败间隔分布（P50/P95）与误熔断率 | 设计默认 3，`GRAPE_AUTOPILOT_PAYLOAD_DISPATCH_LIMIT` 可覆盖；未实测，登记为待决策 |
