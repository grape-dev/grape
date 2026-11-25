# Issue #4045 Spec：引擎 revoke 后 chat 配置 runtime_id 悬空

> 需求权威来源：Issue #4045 正文 + issue agent 已发布的 Spec 评论（三个开放点已定稿）。
> 目标合入分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-4045`。

## 目标

引擎 revoke / 物理删除后，`chat_agent_session` 不再残留指向已删 `agent_runtimes` 的悬空引用：
删除动作内**同事务**收敛引用，存量悬空行一次性清扫；受影响用户拿到「可用引擎」或「明确未绑定」，
不再静默回落本机直连。成功标准：1 小时窗口 `get_chat_config: runtime_id=.*不存在` WARNING = 0。

## 现场事实

- 生产 2026-11-23 22:00–23:00 CST：`get_chat_config: runtime_id=1091 不存在（引擎行已删除），按未配置处理`
  27 次、`1202` 28 次、`1080` 10 次，合计 65 条/小时；4 小时趋势 69/59/47/54（持续存量，非抖动）；
- 命中点：`src/kernel/session_manager.py` `_chat_config_row_engine()`（引擎行缺失 → WARNING +
  `usable=False`）→ `_pick_chat_config_row()` 候选全不可用 → `runtime_id` 置 0 →
  `get_chat_config()` 按「未配置」返回 → 上层 `chat.py` / `handler_async.py` 本机直连/空兜底；
- 删除侧缺口：`src/domains/runtime/public/__init__.py::purge_runtime_credential()` 只摘绑
  `devops_agent_session`，**未覆盖 `chat_agent_session`**；#250 只收敛了场景侧
  `engine_runtime_ids`，chat 会话配置表未覆盖；
- `engine_uid` 双写（#868 / T10）已存在，但读路径「uid 优先」只用于**解析**，解析不到可用引擎时
  仍回落告警的 `runtime_id`，且不把重解析结果回写 → 悬空引用不因 T10 消除。

## 关键决策（issue agent 定稿，本实现照此执行）

| 决策点 | 结论 |
|---|---|
| 删除侧语义 | 同事务 `runtime_id=0`，**保留 `engine_uid` 不清空**（可审计/可重绑定线索不可逆丢失风险） |
| 未绑定显式化 | 出参新增 `bound` + `reason`（`engine_missing` / `unbound`），保留 `runtime_id=0` 兼容 |
| 存量清理 | 盖章迁移 `scripts/migrations/009_*`（幂等、可重放、多库一次到位），非启动期隐式自愈 |

## 范围

### In

1. **删除侧同事务清理**（`src/domains/runtime/public/__init__.py::purge_runtime_credential`）：
   `UPDATE chat_agent_session SET runtime_id=0 WHERE runtime_id IN (...)`（不写 `engine_uid`），
   与既有 `devops_agent_session` 摘绑、能力删除、引擎行删除、凭据行删除同一事务；
   场景侧引用沿用既有 `cleanup_scene_engine_bindings` / `_cleanup_scene_engine_runtime_ids`。
2. **读路径**（`src/kernel/session_manager.py`）：
   - 悬空命中且 `engine_uid` 非空 → 按 uid 重解析可用引擎，并**回写** `runtime_id`（限行级读路径）；
   - 解析不到 → `usable=False, reason=engine_missing`；`engine_uid` 为空的历史悬空行 → `reason=unbound`；
   - `runtime_id=0` 且无 uid → `reason=unbound`（真未配置）；
   - `get_chat_config()` 出参新增 `bound` / `reason`，`runtime_id` 语义不变（保留兼容）；
   - 悬空 WARNING 按 `runtime_id` 进程内去重（上限 256），首条 `warning`、其后 `debug`。
3. **存量清扫**（`scripts/migrations/009_chat_config_dangling_runtime.py`）：
   扫描 `runtime_id>0 AND runtime_id NOT IN (SELECT id FROM agent_runtimes)` 的行置 0、
   保留 `engine_uid`；默认 dry-run，`--apply` 前整表备份，二次执行 0 变更。
4. **API 出参**（`src/domains/runtime/routes/chat.py::chat_config_get`）透出 `bound` / `reason`。
5. **测试**：删除侧 / uid 重解析回写 / bound+reason 三态 / WARNING 去重 / 迁移幂等。

### Out

- #3351（重注册导致 uid 漂移、并发配置丢失）：成因不同，不合并；
- 不改引擎删除策略（仍物理删除）、不改前端 UI 版式（只约定字段语义）；
- 不改 chat 兜底策略（本机直连兜底保留，只是可辨识化）；
- 生产 LogQL 1 小时窗口复核由 sre 发布后执行，不在本 PR 范围。

## 验收标准

- [x] 删除被 chat 绑定的 runtime 后，`chat_agent_session.runtime_id=0`、`engine_uid` 保留，
      同事务完成（失败整体回滚），`purge_runtime_credential` 报告含 chat 摘绑行数；
- [x] 悬空命中且 `engine_uid` 非空 → 重解析到可用引擎并回写 `runtime_id`（DB 可见），
      `get_chat_config` 返回 `bound=True`；
- [x] 悬空且 uid 不可解析 → `bound=False, reason=engine_missing`；真未配置 → `bound=False, reason=unbound`；
- [x] 同一 `runtime_id` 仅首次 WARNING，后续 debug；
- [x] 删除侧新形态（`runtime_id=0` + 悬空 `engine_uid`）的 uid 分支告警同样去重，且该行 `usable=False`（不遮蔽可用遗留行）；
- [x] 引用对账（reconcile）只清扫 `runtime_id>0` 的孤儿 uid，不得抹掉 `runtime_id=0` 行保留的 `engine_uid`；
- [x] 读路径回写 `runtime_id` 带旧值 CAS 守卫，不覆盖并发 `chat_config_upsert_uid` 新写入；
- [x] 迁移 009 幂等（重复执行 0 变更），清扫后 audit 口径 0 悬空，`engine_uid` 未被清空；
- [x] 现有测试无新增失败（基线唯一红用例 `tests/api/test_credential_write_primitive.py::test_insert_runtime_credential_returns_id` 前后一致）；
      `make lint`、`python3 scripts/check-arch.py` 通过。
- [x] 迁移 009 对「表不存在 / 0 行」的疑似连错库显式非 0 退出，不输出假绿。