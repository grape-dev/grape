# Plan: Issue #871 — 写方向反转实现方案

> **A2 收尾（2026-11-10，Phase B）— 历史设计说明**：下文的 `write_mode()`
> （new/legacy 灰度回滚）与"legacy 反向回写"已在 M2 删除；Phase B 后 legacy 三列与
> `global_im_bot_config` 整表已 DROP。写路径只有 `channel_installation` 一条
> （`engine_wiring.sync_installation` / `revoke_installation`）。

## 设计

### 1. 写方向开关（engine_wiring）
- `write_mode() -> 'new' | 'legacy'`，默认 **new**（channel_installation 主写）。
- 读取顺序：覆盖文件（`GRAPE_IM_WRITE_MODE_FILE`，默认 `data/im_write_mode`）> env `GRAPE_IM_WRITE_MODE` > 默认 new。与 `engine_mode()` 同款秒级切回机制，供灰度回滚。

### 2. 直写 + 反向回写（engine_wiring）
- `sync_legacy_installation`（保存/换 app 统一写桥，签名不变）：
  - 主写：upsert `channel_installation`（secret 快照入 config，幂等置 active）——两种模式都执行；
  - secret 兜底链：入参 → 新表已有 config → legacy 行 secret；全空则不建行（维持旧口径）；
  - new 模式追加 **legacy 反向回写**：user 轨回写 `chat_agent_session.im_*`，repo 轨回写/补建 `devops_agent_session` 行，并登记 `global_im_bot_config`；回写失败仅告警不回滚主写。
- `revoke_legacy_installation`（解绑）：
  - 主写：新表置 revoked（不硬删，留审计）；
  - new 模式追加反向回写解绑：清 legacy `im_*` 字段，`global_im_bot_config` 无其他引用时删除。

### 3. 运行期对账修复（ImEngine._reconcile_loop）
- new 模式下 `_reconcile_once` 检出 diff 后自动收敛：
  - `missing_in_new`（直改 legacy 的旧脚本）→ 从 legacy 行补 upsert 进新表；
  - `stale_in_new` → 从新表 config 反向回写 legacy。
- legacy 模式（回滚态）维持仅告警，行为与现状一致。

### 4. 对账脚本（scripts/im_reconcile_installations.py，新增）
- 只读比对：键集合 diff + secret 字段比对（legacy 行 secret vs 新表 config.app_secret）；
- 输出不一致明细，任何差异 exit 1；**仅发现，不自动修复**。

### 5. 调用点（agents.py / chat.py / feishu_onboard.py）
- 统一写桥入口不变（三动作已走 sync/revoke），更新注释为 #871 写方向反转语义；保存/换 app/解绑在请求内同步完成（不依赖扫描窗口）。

## 阶段
- 阶段一：对账脚本 + 直写/回写内核 + 测试基线。
- 阶段二：默认切 new 主写 + 对账循环自动收敛 + 灰度回滚测试。

## 风险
- 回写失败静默 → 回写独立事务 + 告警日志 + 对账脚本兜底。
- 回滚 → `data/im_write_mode` 写 `legacy` 即回旧派生同步语义。
