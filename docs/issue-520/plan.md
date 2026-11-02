# Issue #520 方案设计

## 变更点
| # | 文件 | 变更 |
|---|------|------|
| 1 | src/api/routes/runtime.py | RuntimeClaimBody + expected 成对字段（model_validator 校验成对）；新增 4 路由 |
| 2 | src/runtime/service.py | claim()/claim_by_tag() 加 expected 参数（原子校验分支，绝不回退 FIFO）；新增 lookup_task_by_event_seq() |
| 3 | tests/test_issue_520_bound_claim.py | 12 条验收用例 |

## 服务层设计（避免复制主路径逻辑）
- `claim(runtime_id, expected_task_id=None, expected_event_seq=None)`：
  - expected 为空 → 原 FIFO 选择代码**原样执行**（零改动）。
  - expected 非空 → 在同一 db_lock 内执行定向 SELECT：`id=? AND event_seq=? AND status='queued'
    AND (runtime_id=? OR (runtime_id=0 AND pool_agent_type != '')) AND NOT EXISTS(串行槽)`；
    未命中 → `db.commit(); return None`。pool_engine_ids 不含本引擎 → None（fail-closed）。
  - 命中后复用既有 UPDATE running / busy snapshot / 事件同步 / token 注入后处理链（不复制）。
- `claim_by_tag(tag, runtime_id, expected_task_id=None, expected_event_seq=None)`：
  - 复用既有 tag 能力校验、scope 解析、满载检查（主路径代码）。
  - bound 分支：在已构建的 scope SQL 上 `replace(" ORDER BY", " AND id=? AND event_seq=? ORDER BY")`
    追加 expected 过滤（占位符顺序 = 既有 params + expected 两个值）；未命中 → None；
    pool_engine_ids 不含本引擎 → None；不进入 FIFO 跳过/健康检查循环（while 顶部 bound 直接 break）。
- `lookup_task_by_event_seq(runtime_id, event_seq)`：纯 SELECT，候选骨架与 claim 定向分支一致 + `event_seq=?`，
  返回 `{task_id, issue_id, event_seq, agent_type, created_at}` 或 None。

## 路由层设计
- `_require_runtime_owned(runtime_id, credential_id)`：`SELECT 1 FROM agent_runtimes WHERE id=? AND credential_id=?`
  → 无行 403（他人 runtime）。
- `POST .../tasks/claim-bound`：`_require_credential` + 归属校验 + body 必须成对携带 expected（缺 → 400）
  → `service.claim(...)`，尾部 prefer_fallback/_attach_skill_obs/scan_request 与 claim 端点一致。
- `POST .../tasks/claim-by-tag-bound/{tag}`：同上走 `service.claim_by_tag(...)`。
- `GET .../tasks/claim-capabilities`：归属校验后返回 `{runtime_id, capabilities:{bound_claim:true, task_lookup:true}}`。
- `GET .../tasks/lookup?expected_event_seq=N`：归属校验；N 缺失或 <=0 → 400；否则 `{"task": <身份字段或 null>}`。
- 旧 claim/claim-by-tag 路由：body 成对携带 expected 时透传服务层（in-place 守卫）；未传保持 FIFO。
- 新路由不进对外 OpenAPI 契约（凭据级路由，与现有 claim 一致）。

## fail-closed 原则
- 未部署节点对 bound/lookup/capabilities 返回 404 → 客户端保持许可关闭。
- 服务端 bound 未匹配 → 无任务，绝不回退 FIFO、不修改其他任务。

## 测试方案（tests/test_issue_520_bound_claim.py）
沿用 test_runtime_protocol.py 模式：TestClient + rta_ 凭据种子 + `service.enqueue_task`，独立清理 fixture。
覆盖 spec 12 条用例 + claim-by-tag-bound 的 tag/scope 维度。

## 风险与回滚
- 主路径 SQL/控制流未动（expected=None 分支原样），风险集中在新分支。
- 回滚 = revert 单个 PR，无 schema/数据迁移。
