# Spec: Issue #871 — IM 配置权威收尾：保存/解绑直接写 channel_installation

> **A2 收尾（2026-11-10，Phase B）— 历史设计说明**：下文的 `write_mode()`
> （new/legacy 灰度回滚）与"legacy 反向回写"已在 M2 删除；Phase B 后 legacy 三列与
> `global_im_bot_config` 整表已 DROP。写路径只有 `channel_installation` 一条
> （`engine_wiring.sync_installation` / `revoke_installation`）。

> 摘自 Issue #871 Spec（评论 198029102）。目标分支 weekly-09-IM。

## Objective
反转 IM 配置写入权威方向：UI 保存/解绑/换 app 直写 `channel_installation`（含 secret 快照入 config），legacy 双轨表转为反向兼容回写，保留灰度回滚能力，消除「页面显示已绑定、引擎实际没连」的一致性漂移。

## Scope
- `src/im/engine_wiring.py`：`sync_legacy_installation` / `revoke_legacy_installation` 由派生同步改为直写 + legacy 反向回写兼容层
- `agents.py` / `chat.py` / `feishu_onboard`：写桥调用点语义更新（统一写桥入口不变）
- 对账脚本：比对 legacy 双轨表与 channel_installation，不一致行告警（仅发现不自动修复）
- 灰度开关：写方向可配置（new=channel_installation 主写 / legacy=旧派生同步），可回滚

## Out of Scope
- #870 / #869；IM 引擎连接逻辑重构；全量历史数据一次性清洗

## Acceptance Criteria
- UI 保存/解绑/换 app 后 channel_installation 立即正确（事务内，不依赖 ≤30s 扫描窗口）
- 对账脚本零差异；直改 legacy 的旧脚本仍可用（回写/修复生效）；灰度可回滚
- pytest tests/im 全绿，新增测试覆盖直写/回写/换 app/对账
