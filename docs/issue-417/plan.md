# Plan: Issue #417

## 改动点（原子提交）

### C1 查询侧兜底过滤 — `src/api/routes/dashboard.py`
- runtimes 查询 SELECT 增加 `rc.id AS cred_pk`；
- 过滤条件由 `rcred_status == 'revoked'` 扩为：`cid>0 且 (rcred_status=='revoked' 或 cred_pk 为 None)`（凭据行已物理删除的孤儿行不再显示）。
- 注意：凭据行存在但 status 为 NULL 的历史合法行**不**误伤（与 user/db.py、instances.py 的 `rc.status IS NULL OR rc.status='active'` 语义对齐）。

### C2 吊销级联清理补全 — `src/api/routes/settings.py`
- `revoke_runtime_credential` 在删除 `agent_runtimes` 前后补删对应 `engine_capabilities` 行（rid_list 为空时跳过）。

### C3 注册竞态防护 — `src/api/routes/runtime.py`
- `runtime_register` 进入 `db_lock` 后先 `SELECT id FROM runtime_credentials WHERE id=? AND status='active'` 复核；查不到 → 401（与 `_auth_credential` 同语义），阻断「鉴权通过后凭据被吊销」的竞态 INSERT。

## 测试（TDD）
`tests/test_issue_417_revoked_runtime_residue.py`：
1. T1 兜底过滤：插入凭据+引擎行 → 物理删凭据行（模拟残留孤儿）→ 调 dashboard ops overview → runtime 表不含该引擎；
2. T2 revoked 过滤回归：凭据 status='revoked' → 不显示；
3. T3 正常设备不误伤：active 凭据（含 status NULL 合法历史行）→ 显示；
4. T4 注册竞态：吊销后再 register（持失效 token 语义，直接调用端点用已删凭据的鉴权返回）→ 401、不产生新 agent_runtimes 行；
5. T5 revoke 级联清理 engine_capabilities。

## 风险
- 过滤条件扩展可能误伤：仅当「凭据行缺失」才跳过，凭据存在（NULL/active）不受影响；
- register 复核放在 db_lock 内，不改变正常路径行为。
