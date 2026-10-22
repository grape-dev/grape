# Spec: Issue #417 — 可观测性「设备与引擎」页 runtime 表已吊销设备仍显示

> 来源：Issue Agent 发布的 Spec（评论区），本文为开发侧落地副本。

## 目标
修复可观测性「设备与引擎」页 runtime 表中已吊销（revoked）设备仍残留显示的 Bug。

## 根因分析
- runtime 表数据源 = `/api/dashboard/ops/overview` → `src/api/routes/dashboard.py` 的 runtimes 查询（`agent_runtimes LEFT JOIN runtime_credentials`）。
- 吊销 = 物理删除（settings.py `revoke_runtime_credential`：删 `runtime_credentials` 行 + 级联删 `agent_runtimes`）。
- 残留路径 1（查询侧漏洞）：查询仅跳过 `rc.status == 'revoked'`；凭据行被物理删除后 LEFT JOIN 得 NULL，`NULL == 'revoked'` 为假 → 残留 `agent_runtimes` 行（历史孤儿行/级联删除遗漏行）继续显示最多 7 天（受 stale 过滤限制）。
- 残留路径 2（写入侧竞态）：`runtime_register` 的鉴权（`_auth_credential`）在 `db_lock` 之外；worker 通过鉴权后、拿到 `db_lock` 前，吊销完成删除 → register 仍以失效 `credential_id` UPDATE（0 行，无报错）/ INSERT `agent_runtimes` → 产生新孤儿行，被残留 worker 进程复活。

## 方案（与 Spec 一致：清理为主 + 过滤兜底）
1. 方案 A（吊销事件同步清理，补全）：
   - revoke 级联补删 `engine_capabilities`（rid_list 对应行），清理悬空能力标签；
   - `runtime_register` 在 `db_lock` 内复核凭据仍存在且 active，失效则 401，杜绝竞态复活。
2. 方案 B（查询侧过滤兜底）：
   - dashboard runtimes 查询新增 `rc.id AS cred_pk`，跳过 `cid>0 且 (status='revoked' 或凭据行缺失)` 的引擎行，孤儿行立即不再显示。

## 验收标准
- 吊销后立即查询，runtime 表不再出现该设备（含凭据行已删、agent_runtimes 残留的兜底场景）；
- 刷新页面后查询同样不出现；
- 竞态场景：吊销后 worker 再 register 被拒（401），不产生新残留行；
- revoke 级联清理 engine_capabilities；
- 现有测试全部通过，新增测试覆盖上述场景。
