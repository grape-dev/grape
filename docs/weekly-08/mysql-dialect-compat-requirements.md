# MySQL 方言兼容性缺陷修复需求（解绑 500 及全家族）

> **分支**：`fix/mysql-dialect-compat`
> **基线**：`grape-dev/weekly-08` (2d47f9f35)
> **状态**：已实施（代码 + 场景用例两层测试均通过），待全量回归与部署验证
> **类型**：Bug 修复 + 数据一致性加固 + 测试防回归

## 一、总览

### 1.1 背景

MySQL 后端实例上用户解绑 GitCode 报 500。全链路审计（AST 提取全库 1907 处
execute() SQL → 逐条经 mysql_compat 翻译 → MySQL EXPLAIN 解析验证 → 失败项
逐一定性 → 运行日志与真实 DB 交叉核验）确认：这不是孤立 bug，而是
`src/mysql_compat.py` 翻译层存在三条系统性漏网通道，共 11 个问题点；其中多数
被调用点 `except: pass` 吞掉，从 500 降级为静默错误数据。SQLite 后端全部
不可见，意味着所有只测过 SQLite 的环境都测不出这些问题。

### 1.2 系统性根因

翻译层为白名单正则机制，只翻译被明确列入的 SQLite 方言（`?` 占位符、无参数
`datetime('now')`、CREATE TABLE DDL），三条漏网通道：

| 编号 | 通道 | 后果 |
|------|------|------|
| R-1 | 字符串字面量被 KWP 关键字保护刻意保留 | 字符串内反斜杠方言炸弹原样放行（F1） |
| R-2 | `datetime('now')` 正则只匹配无参数形式 | 带修饰符变体漏翻（N1） |
| R-3 | 未列入翻译规则的方言透传 | CAST AS INTEGER / PRAGMA index_list / DISTINCT+ORDER BY（N2/N3/N4） |

放大器：调用点大量 `except: pass` 把漏网炸弹变成静默错误数据（比 500 更危险）。

### 1.3 问题清单

| 编号 | 级别 | 位置 | 问题 |
|------|------|------|------|
| F1 | P0 | grape_auth.py:1024/1035/1088/1111 | `LIKE ? ESCAPE '\'` MySQL 1064，解绑 500 |
| F2 | P1 | grape_auth.py:1088 | affected_count 被 except 吞 → 恒返回 0，用户决策数据错误 |
| F3 | P1 | grape_auth.py 解绑链路 | user_repos 清理在绑定软删后、审计前，无事务边界 → 半途状态风险 |
| N1a | P1 | feishu_onboard.py:314 | `datetime('now', ?)` 漏翻，stale 恒 False → 用户被 409 卡死 |
| N1b | P1 | event_pipeline.py:84 | 同上，session 活跃兜底恒 False |
| N1c | P1 | dashboard.py:1312 | 同上，events_week 指标静默错 |
| N2 | P1 | auth_token.py:132 | CAST AS INTEGER 漏翻 → 过期 token 清理静默失效，表只增不减 |
| N3 | P2 | im/handler_async.py:1508 | DISTINCT+ORDER BY 未选中列，ONLY_FULL_GROUP_BY 下 3065 → /repo 卡片中断 |
| N4 | P2 | db_setup.py:1885 | `PRAGMA index_list('issue')` 单引号参数不匹配兼容层 regex 字符类（`[`"\w]+` 无单引号）→ 不走 Cursor 特判，1064。遗留库唯一索引升级静默失败 |
| F4 | P2 | grape_auth.py:1035 | 路径 B 查询被吞 → 409 确认列表漏报 |
| F6 | P3 | grape_auth.py unbind 入口 | 无 provider 白名单，未知 provider 误清 um 列 |
| F7 | P2 | user/db.py:792 | 参数化 `sqlite_master WHERE type='table' AND name=?` 不匹配翻译层无参数正则 → MySQL 上 legacy 表清理（managed_repos_legacy/user_repos_legacy）静默跳过。有 try 兜底不崩，但清理功能失效（深度审查发现的参数化变体遗漏） |
| F8 | P2 | 前端 account.js:1057 | `affected_repos` 为 -1 时 `\|\| 0` 归零 + `> 0` 过滤 → "影响数未知"被显示为"无影响"。归前端分支（feat/unbind-repo-state-consistency 正在重写该函数）处理，本 MR 仅在描述注明关联 |
| F9 | P1 | db_setup.py:1887/1916 | 索引升级块在 MySQL 上走不通：1887 裸 `DROP INDEX`（无 ON，1064）；1916 裸 `CREATE UNIQUE INDEX (repo_key, issue_number)`（TEXT 列无前缀，1170）。净效果：遗留库触发升级时 DROP 成功、CREATE 失败 → **索引被删且不重建**（比不升级更糟）。基线遗留问题，N4 只修了 PRAGMA 探测环，升级动作未修（深度审查发现的验证盲区） |

已排除的嫌疑（不修，防止重复排查）：16 处 sqlite_master/SHOW TABLES/SHOW
COLUMNS/SHOW STATUS/SET SESSION 均有后端分支或 try-fallback 守卫，属设计内
兼容写法；issues.py:472、bot_lease.py:362 为审计工具假参数误报。

## 二、需求范围

**设计原则（已按评审确认）**：统一使用通用标准 SQL 写法。业务代码一律写
标准参数化 SQL（`?` 占位符、应用层预计算的时间值、无方言函数）；兼容层只为
"MySQL 对标准 SQL 的偏差"做吸收（如 CAST AS INTEGER→SIGNED），不为 SQLite
私有方言打补丁。

| 编号 | 需求 | 类型 |
|------|------|------|
| M-1 | grape_auth.py 四处改纯参数化 LIKE，删 ESCAPE 与 _escape_like_pattern | Bug 修复 |
| M-2 | unbind 入口加 provider 白名单 {gitcode, github} | 防呆加固 |
| M-3 | 解绑写步骤顺序调整：可失败的读/校验前置；user_repos 清理失败 → log.error + 响应 partial 标记（顺序执行+告警，不做真回滚） | 数据一致性 |
| M-4 | 三处 `datetime('now', …)` 带参调用点（feishu_onboard:314 / event_pipeline:84 / dashboard:1312）改为应用层预计算 cutoff + 标准参数化 SQL（`created_at < ?`）；兼容层只保留无参 `datetime('now')` 翻译，带参变体不再翻译（设计原则：不为 SQLite 私有函数打补丁） | 调用点标准 SQL |
| M-5 | mysql_compat 新增 CAST AS INTEGER → AS SIGNED 翻译（CAST AS INTEGER 本身是 SQL 标准，MySQL 只认 SIGNED——此条属"吸收 MySQL 偏差"，符合兼容层本职） | 兼容层补齐 |
| M-6 | PRAGMA index_list/index_info regex 接受单引号参数（db_setup:1885 带引号形式不再 1064），Cursor 特判剥引号 | 兼容层容差 |
| M-7 | handler_async.py:1508 排序列（last_active_at）加入 SELECT 列表 | Bug 修复 |
| M-8 | 用户决策数据路径的 except: pass → log.warning + 保守值（affected_count 失败 → -1 表未知，禁止静默归零；探测类 fallback 不动） | 吞错治理 |
| M-9 | mysql_compat 模块文档追加团队规则五条（见 §四） | 规范 |
| M-10 | 场景用例代码实现：tests/test_mysql_dialect_compat.py（pytest 双方言行为用例，SQLite 测试库，含 AST 防回归守卫）+ tests/test_unbind_http_mock.py（mock HTTP 全流程）。MySQL 方言级验收（S1-U1 等需真实 MySQL 的场景）由部署验收文档约定，验收工具不入库 | 测试实现 |
| M-11 | F7 修复：翻译层 sqlite_master 正则支持参数化变体（`AND name=?`），Cursor 特判特换为 information_schema 查询，使 legacy 表清理在 MySQL 上生效 | 兼容层补齐 |
| M-12 | F8 跟踪：MR 描述注明 affected_repos=-1/partial 的前端适配归属前端 MR；本分支后端语义保持（-1=未知，不改回 0） | 文档关联 |
| M-13 | F9 修复：db_setup 升级块 1887 行 DROP 补 `ON issue`；1916 行裸 CREATE 改走兼容层 `CREATE UNIQUE INDEX IF NOT EXISTS` 形态（Cursor 特判自动为 TEXT 列加前缀长度）——遗留库升级路径端到端打通 | Bug 修复 |

## 三、不在范围

- 不重构整个 mysql_compat 兼容层（只补确认的三个盲区）
- 不引入事务框架改造（M-3 零危害原则：检测+报告，破坏性操作留给管理台）
- 不动已排除的 16 处守卫型兼容写法
- 不动 EXPLAIN 不支持的 SHOW/SET 白名单跳过项
- 前端不改（本次全部为后端 SQL 层问题）
- dashboard.py:430 strftime→TIMESTAMPDIFF 的 except 降级链保持现状（设计内 fallback，MySQL 路径可用；仅提示：以异常做控制流，每次面板调用都先抛一次 1305，可顺手改后端分支，非必须）

## 四、团队规则（写入 mysql_compat 文档）

1. SQL 字符串字面量内禁止反斜杠；
2. 前缀匹配 = `LIKE ?` + 常量前缀参数，不引入 ESCAPE/转义助手；
3. 用户输入进 LIKE 模式时用 `ESCAPE '|'`（双方言安全）+ 转义函数；
4. DISTINCT + ORDER BY 的排序列必须在 SELECT 列表内；
5. 统一使用通用标准 SQL：时间比较用应用层预计算值 + `?` 参数，禁止
   `datetime('now', 带参)` 等 SQLite 私有函数变体（无参 `datetime('now')` 为
   历史存量形态，由兼容层翻译；新代码优先标准写法）；确需方言分支时用
   `is_mysql` 显式分支。

## 四点五、场景用例代码实现（M-10 落地说明）

两层结构（pytest conftest 强制 SQLite 测试库隔离、绝不连 RDS，MySQL 方言
验证不能走默认 pytest）：

- tests/test_mysql_dialect_compat.py —— pytest 双方言行为用例（默认跑）：
  S1-U3/U5、S2-U1/U2、S3 参数化断言、F3 partial 语义、两条 AST 防回归守卫
  （无守卫的 datetime 带参 SQL 常量零残留、ESCAPE 反斜杠零残留）；
- tests/test_unbind_http_mock.py —— 解绑全流程 mock 用例（S1-U1~U5 完整
  HTTP 闭环）：FastAPI TestClient + dependency_overrides 免登录，
  tests/mock_db.py 的 MockDB 按 SQL 子串注册结果集，数据访问层函数打桩。
  不连任何数据库、不起服务进程；同时在 mock 层锁定"发出的 SQL 必须是
  标准参数化 LIKE（无 ESCAPE 反斜杠）"的方言行为断言。
- （验收工具已移出仓库：方言级验收 SQL 清单见本文件 S1/S3-S9 用例，部署验收时按清单在目标环境执行）
  （S1-U1/U3、S3-U1、S4-U1/U2、S5-U1、S6-U1/U2、S7-U1、S8-U2 负样本）：
  直连 configs/db-backend.json 的 MySQL 后端，退出码 0 = 全过。
  仅手动执行（部署验收），pytest/CI 不收集不执行。

用例数据注意：managed_repos.webhook_id 列为数值型（'' 在 MySQL 语义即 0），
测试数据须用非零数值才会命中 `webhook_id != ''` 条件。

## 五、场景与场景用例（Acceptance Criteria）

### 场景 S1：用户解绑 GitCode（核心触发场景）

- [ ] S1-U1 MySQL 后端，有在管仓库的用户解绑 confirm=false → 409，warehouses
      完整列出路径 A（user_repos 关联）与路径 B（host_user_id）仓库
- [ ] S1-U2 confirm=true → 200；DB 核验：external_identities status=0、token
      清空、unbound_at 有值；user_repos 无 `<provider>:%` 残留；auth_audit 有
      unbind 记录
- [ ] S1-U3 返回 affected_repos 为真实数量（非 0）
- [ ] S1-U4 无密码单绑定账号解绑 → 400 need_set_password（守卫回归）
- [ ] S1-U5 `DELETE /api/auth/bind/<未知provider>` → 400/404，且
      unified_user_mapping.gitcode_user 保持原值（白名单）
- [ ] S1-U6 SQLite 后端 S1-U1~U5 行为完全一致（双方言回归）

### 场景 S2：飞书引导任务过期判定（N1a）

- [ ] S2-U1 MySQL 后端，构造 created_at 超过 STALE_TTL 的 active 任务 → 判
      stale，允许开始新任务（修复前恒 409 卡死）
- [ ] S2-U2 未超 TTL 的 active 任务 → 仍 409（防误作废，回归）

### 场景 S3：session 活跃兜底检测（N1b）

- [ ] S3-U1 devops_agent_session 最近 60s 有活跃行 → _check_session_active
      返回 True（修复前恒 False）

### 场景 S4：过期 session token 清理（N2）

- [ ] S4-U1 插入 expires_at 已过期的行 → purge 后被删除（修复前静默保留）
- [ ] S4-U2 未过期行不被误删

### 场景 S5：IM /repo 兜底卡片（N3）

- [ ] S5-U1 MySQL（ONLY_FULL_GROUP_BY 默认 sql_mode）下未绑定用户触发 /repo
      兜底 → 卡片正常返回，无 3065

### 场景 S6：唯一索引升级（N4 + F9/M-13）

- [ ] S6-U1 db_setup 重跑：MySQL 上无 PRAGMA index_list 1064 告警
- [x] S6-U2 遗留库场景端到端：人工造同名非唯一索引 → 触发 db_setup 真实
      升级代码路径（PRAGMA 探测 → DROP → CREATE）→ 升级为 UNIQUE，无索引
      丢失窗口。**双栈实现**：DROP INDEX 的 ON 子句 MySQL 必须/SQLite 不允许
      （实测 SQLite 对 `DROP INDEX x ON tbl` 直接 syntax error），按
      `_is_mysql_backend` 分支；CREATE 统一 IF NOT EXISTS 形态（MySQL 走
      兼容层特判自动加 TEXT 前缀，SQLite 原生合法）。MySQL e2e 实测
      PASS（legacy 0 → upgrade 1），SQLite 分支实测 PASS
- [ ] S6-U3 健康库零触碰：索引已是 UNIQUE 时重跑 db_setup 升级块，
      不执行任何 DDL（幂等跳过）
- [x] S6-U2(旧) 注：此前"verified live"验证的是 indexes 循环的 IF NOT EXISTS
      语句（兼容层特判路径），未覆盖 db_setup 升级块裸 CREATE 路径——
      深度审查发现的验证盲区，本版修正验收口径并补修（M-13）

### 场景 S7：dashboard 指标（N1c）

- [ ] S7-U1 MySQL 后端 events_week 返回真实 7 天事件数（有数据时 > 0）

### 场景 S9：legacy 表清理参数化查询（F7/M-11）

- [ ] S9-U1 MySQL 后端：`SELECT name FROM sqlite_master WHERE type='table' AND name=?`
      经翻译层可执行（information_schema 等价），真实查询 managed_repos_legacy
      与 SHOW TABLES 判存一致；legacy 表存在时清理流程能真正 DROP
- [ ] S9-U2 SQLite 后端：同 SQL 原生可执行（零回归）
- [ ] S9-U3 翻译层单测：参数化 sqlite_master 形态产出 MySQL 合法 SQL

### 场景 S8：CI 防回归（M-10）

- [ ] S8-U1 审计脚本对修复后代码全库执行 → 0 个非预期解析失败
      （1146/1054 动态 schema 类豁免）
- [ ] S8-U2 人为注入一条 `ESCAPE '\'` SQL → CI fail（负样本验证）

## 六、实施批次与风险

批次一（解绑止血，单文件）：M-1/M-2/M-8(grape_auth 部分) → S1
批次二（翻译层补齐）：M-4/M-5/M-6/M-7 + M-9 → S2/S3/S5/S6/S7
批次三（吞错治理）：M-8 全库定界 → S4
批次四（CI 防回归）：M-10 → S8；建议 M-10 提前到批次二之前落地兜底

风险：
- 翻译规则改动影响面大 → CI 全量解析兜底（故建议先行）；
- datetime 翻译后返回类型变化 → 调用点做字符串字典序比较的需保持
  CAST(NOW() AS CHAR) 语义（与现有无参翻译行为对齐）；
- M-3 不做真回滚 → 半途状态风险降级为"可见的部分失败"，符合零危害原则。
