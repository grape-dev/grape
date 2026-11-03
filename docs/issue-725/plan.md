# Issue #725 Plan — 实现方案设计

## 改动点
1. `src/api/routes/issues.py` `issue_pending_review`：
   - 在 `completed/cancelled` 短路分支前新增 `pending_review` 短路分支，返回 200 + `idempotent: true`。
   - `completed/cancelled` 短路响应补 `idempotent: true` 字段。
   - 路由 `description` 增补幂等语义说明（重复调用 → 200 幂等短路）。
2. `src/api/routes/issues.py` `_session_devops_issue`：
   - 修复 #672 number 兜底的解析优先级：主键命中且会话作用域匹配时以主键行为权威，
     number 兜底仅在主键 miss / scope 不匹配时生效（原实现无条件被 number 兜底覆盖，
     内部卡片 id 与同仓 issue_number 撞号时解析到错误卡片——新增幂等测试暴露该存量缺陷，
     表现为同套件顺序敏感失败）。
3. `help/api/11-issues.md` pending-review 小节：
   - 出参表补 `idempotent` 字段；「返回」行补 200 幂等语义；增补「重复调用处理指引」段落。
3. `tests/test_issue_pending_review.py`：
   - 新增 `test_duplicate_pending_review_idempotent_200`：已是 pending_review → 200 + `idempotent=true` + `changed=false`，且无会话/通知副作用（conversation_id 不被写、notify 不被调度）。
   - 调整既有 `test_terminal_or_duplicate_changed_false` 断言幂等字段。

## 实现顺序（TDD）
1. RED：先写幂等分支测试，确认现实现缺 `idempotent` 字段而失败。
2. GREEN：实现短路分支，测试通过。
3. REFACTOR：文档同步 + 全量回归 `tests/test_issue_pending_review.py`。

## 风险与回退
- 短路发生在鉴权/解析之后，不改变 400/403/404 行为；无 DB 写路径变化，风险极低。
- 回退：revert 单 commit 即可。
