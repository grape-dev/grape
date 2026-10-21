# Spec: Issue #376 — transfer-bind MySQL 8.0 必现失败修复

> 来源：Issue Agent Spec Document（可开发性评级 ready，2026-10-19）| 目标分支: 功能分支

## 目标
修复 transfer-bind 在 MySQL 8.0 后端 100% 失败（500）及失败后 binding_ticket 被连带作废（重试必然 410）的问题。

## 根因
1. **主因（必现 500）**：`src/auth/grape_account_db.py` `transfer_business_data()` 对 `user_repos` 的两条 SQL 使用 SQLite 同表子查询语法，MySQL 报 1093（`You can't specify target table 'user_repos' for update in FROM clause`）→ `transfer_business_data` 返回 False → 事务回滚 → 500。
2. **体验放大器（重试必然 410）**：`validate_binding_ticket()` 校验时即 DELETE 票据，首次 500 后票据已删且不恢复 → 重试永远 410。

## 范围（In）
1. `transfer_business_data()`：user_repos 两条 UPDATE/DELETE 改为派生表包裹，消除同表子查询（SQLite/MySQL 双兼容）。
2. 同类排查命中：`src/api/routes/latency.py:43` `DELETE FROM api_latency_log WHERE id IN (SELECT id FROM api_latency_log ...)` 一并修复；`src/runtime/table_maintenance.py` SQLite 分支清理 SQL 同步统一写法。
3. `validate_binding_ticket()` 拆分校验/消费两步：新增 `consume=False` 参数（只校验不删）与 `consume_binding_ticket()`（独立消费）；transfer-bind 端点票据 DELETE 移到 `_ga_db.commit()` 成功后。
4. 双后端（SQLite/MySQL 8.0）回归。

## Out of Scope
- 解绑「至少保留一种登录方式」409 规则（合理安全设计，不改）。
- binding_tickets 表结构变更、票据加密存储（后续独立 Issue）。
- 前端确认页交互改版。

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 消除同表子查询写法 | A. 派生表包裹 / B. JOIN 改写 / C. 先 SELECT 后按主键操作 | A | 语义与原语句一致，改动最小，SQLite/MySQL 双兼容 |
| 票据消费时机 | A. 校验即删（现状）/ B. 事务成功后删 / C. 失败回写 | B | 免回写补偿复杂度；失败路径票据自然保留剩余有效期 |
| latency.py 同类 SQL | A. 一并修 / B. 单独 Issue | A | 同源同类病，拆开重复排查成本 |

## 验收标准
- MySQL 8.0 后端占位账号有业务数据场景 transfer-bind 返回 200（业务数据迁移+绑定转移+占位账号 merged 标记生效）。
- 首次失败后同票据有效期内重试不再 410。
- 全库无 `UPDATE/DELETE ... IN/NOT IN (SELECT ... FROM 同表)` 直接写法。
- SQLite 回归不破坏。
- 现有测试全通过 + 新增测试覆盖。

## 风险与缓解
| 风险 | 影响 | 缓解 |
|------|------|------|
| 派生表包裹在极端旧版 SQLite 不支持 | Low | 项目 SQLite 最低版本远高于支持线；回归覆盖 |
| 票据消费后移引入重放窗口 | Med | 窗口=事务执行期（秒级）且需 JWT 会话匹配；保留 initiating_user_id 校验 |
| MySQL 环境缺失无法本地实测 | Med | SQL 语法断言测试（含 translate_sql 翻译层断言）覆盖 1093 形态消除 |
