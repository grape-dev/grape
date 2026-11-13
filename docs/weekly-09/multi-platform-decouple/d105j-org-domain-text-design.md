# D105 第三批：org 域 id TEXT 化设计（2026-11-09 定稿）

## 背景

D105 前两批已把账号域主键 uuid7 化 + `*user_id` 外键列 TEXT 归一，组织域明确
排除（d105b 第 47 行"organization/org_id 属第三批另排"）。活病灶实锤：
`_migrate_create_personal_orgs`（db_setup.py）把 `user_accounts.id`（现为 TEXT
uuid/保值 str）写入 `organization.owner_id INTEGER`——SQLite 混型列互查不中
（binding_tickets 410 同族病）；MySQL 严格模式写崩 1366 / 非严格截 0 错归属。

用户裁定：全量做（org.id + 全部 org_id 外键 + 用户 id 列），一次性清完。

## 口径（两个实现批次共同遵守，不得偏离）

1. **列形态**：对齐第二批——SQLite TEXT 亲和，MySQL VARCHAR(36)
   - `organization.id`：TEXT 主键，新建用 `uuid7_str()`，存量 int 保值 → str（'1'→'1'）
   - `organization.owner_id`：TEXT NOT NULL DEFAULT ''
   - `org_member.org_id`、`org_invitation.org_id`：TEXT
   - `org_invitation.inviter_id`、`org_member.invited_by`：TEXT DEFAULT ''
   - `_ORG_ID_TABLES`（db_setup.py 现有清单，13 张核心表）的 `org_id`：TEXT DEFAULT ''
2. **哨兵语义**：`org_id=0`（全局/未归属）→ `''`（空串），对齐 `owner` 列
   ''=unowned 口径（issue 表 202611091100 先例）。MySQL TEXT 列与 0 数值比较
   会把非数字串强转 0 误匹配（'abc'=0 TRUE），**所有 SQL 哨兵必须改字面量**：
   `org_id=0 OR org_id=?` → `org_id='' OR org_id=?`；`org_id > 0` → `org_id != ''`
3. **Python 签名**：org_db.py / deps.py / 路由 / pipeline 全部 int→str；
   `get_personal_org_id` 返回 str；`create_org` 显式生成 uuid7_str 作为 id，
   退役 lastrowid 依赖；跨边界用户 id 传入侧 `str()` 归一（对齐第二批 15 强转清零口径）
4. **迁移**：新版本号 `202611091300`（扩展已盖章迁移必须开新号——铁律3），
   safe 幂等：
   - SQLite：organization/org_member/org_invitation 表重建保值（复用
     _rebuild_issue_text_columns 手法），核心表 org_id 列亲和重建
   - MySQL：ALTER MODIFY VARCHAR(36)（202611091000 widen 同口径）
   - **名单预警**（handoff 挂账项）：迁移执行时 log 输出受影响表列清单与
     保值行数，供现网执行前人工核对
5. **前端**：本批不动（org_id 仅作不透明 id 透传，JS 无数值假设即不改；
   若发现 `org_id === 0` 之类判断一并改 `=== ''` 并 bump `?v=` + grep tests/ 同步 pin——铁律1）

## 验收

- 守卫测试：迁移保值（int 1 → '1'）、哨兵归零、uuid 写入 owner_id 可查、幂等二次零改动
- 既有 org 相关测试全绿；api_routes 定向回归
- 59 RDS grape-drill-db（自建演练库名，见部署配置） 演练库实跑：零失败、org 域 id 保值、二次 0 pending
- 分支收尾 squash 单提交 force push（沿用 3161a423d 收口流程）
