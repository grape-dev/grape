# D105b 账号域 user_id 外键列 TEXT 归一设计——修绑定转移票 410 与 uuid 用户写入断链（2026-11-09）

## 一、需求（用户裁定）

D105 第二批把 `grape_accounts.id` 切到 UUIDv7（TEXT）后，**全库引用账号 id 的外键列仍声明 INTEGER**——新 uuid 用户一注册就会在写入/比较两面炸。用户实弹踩响第一例：绑定转移确认秒回 410「确认会话已过期，请重新操作」。要求从根上修：**账号域所有 user_id 族外键列 TEXT 化**，同根因一次清完，不留第二批式欠账。

## 二、根因（实弹定位链）

1. 会话身份：`grape_accounts.id` TEXT → JWT sub / `user["id"]` = 字符串（`'24'` 或 uuid 串）
2. 票据存储：`binding_tickets.initiating_user_id INTEGER NOT NULL`（db_setup.py 建表 DDL）→ SQLite INTEGER 亲和落盘 int 24
3. 校验：`validate_binding_ticket` Python 层 `row[0] != initiating_user_id` → `24 != '24'` 恒真 → 本人票据判 mismatch → 410（uv3000.log 实锤 `ticket_user=24, session_user=24` 仍 mismatch）
4. 系统性面：实查 PRAGMA 全库扫出 **30 张表 / 31 列** `*user_id` 族 INTEGER 亲和外键。SQLite 侥幸仅限"纯数字字符串 id"（亲和隐式转换）；**uuid 用户写入时 SQLite 存出 int/text 混型同列（等值查询互相查不中），MySQL INT 列严格模式直接 1265/1365 写崩**。

## 三、修复设计（三层，同根因一次收口）

### L1 存储层——迁移 202611091000（safe，幂等）

- `_normalize_account_fk_user_id_text`：对枚举清单（30 表 31 列，见附录）做 TEXT 亲和归一，复用 D105-5 沉淀的 `_rebuild_issue_text_columns` 重建手法（sqlite_master 原文改造防表达式默认值丢失、索引重建、CAST 保值、幂等：已 TEXT 且无混型零改动）。
- 存量 int 值字符串化保值（`24` → `'24'`），uuid 用户写入后同列恒 text，无混型。
- 新装库 DDL：db_setup.py / user/db.py / persistence 模型的这些列声明同步 `INTEGER` → `TEXT`（新库直接正，无迁移依赖）。

### L2 访问层——Python 类型口径归一

- 比较边界（票据校验、归属判定）统一 `str()` 双侧归一（fff452153 先行修复 validate_binding_ticket，本批全量清扫同族）。
- `int(<user id>)` / `isdigit()` 数值假设清扫；排序语义 `ORDER BY id` 数值依赖处改 `created_at`（仅确认会因 uuid 损坏时才动，最小化）。

### L3 MySQL 翻译层

- `mysql_compat` 按声明类型自动翻译：DDL TEXT → VARCHAR 通道（键列 VARCHAR(36/191)），INT 写崩面随之消除；无按列名特判需求。

## 四、场景用例（新增，映射见 13-uc-test-mapping UC57-UC59）

| # | 场景 | 前置 | 动作 | 预期 |
|---|------|------|------|------|
| UC57 | uuid 用户绑定转移全链 | 账号 id 为 36 字符 uuid | save_binding_ticket → validate_binding_ticket(str id) | 校验通过不 410，转移成功 |
| UC58 | 混型存量库迁移保值 | 列含 int 行（24）+ uuid 行 | 跑 202611091000 | 全列 typeof=text、'24' 保值、幂等二次零改动、str 参数命中 int 存量行 |
| UC59 | 新装库 DDL 正确性 | 全新库 | 建表 | 全部枚举列声明 TEXT，写入 uuid 无混型 |

## 五、验证与部署

- 定向：test_transfer_bind + test_issue_376_mysql_transfer_bind（基线 3 个 SQLText 失败双侧 diff 排除）+ 新增回归族。
- 实弹：:3000/:3001 重启后用户重走 PAT 绑定转移确认成功（用户验收）。
- 部署注意：迁移 safe 幂等可随滚动重启自动盖章；MySQL 生产按 deploy-checklist（集群守则见 D105 设计稿第八节）。

## 六、边界（不做的）

- 不动非账号域的数值 id 列（issue_number/parent_issue_number 走 D105-5/0004 口径，organization/org_id 属第三批另排）。
- 不回填 audit_logs 等历史整型 id 值（列 TEXT 化后旧值保形，语义不变）。

## 七、机制固化（D105-7，2026-11-09）

枚举与人工同步防不住「下次加表忘改」——固化两道自动化闸门（checklist 常驻项）：

1. **不变式守卫 `tests/test_schema_fk_type_invariant.py`**：
   - 清单真源 import `db_setup._ACCOUNT_FK_USER_ID_COLS`（不硬拷贝，枚举扩充即自动覆盖）；
   - 断言①：清单内每列（含 D105-7 追补散列）声明类型全 TEXT（→ TEXT 亲和）；
   - 断言②：每列 INSERT uuid7 形态 + 纯数字串（存量保值形态）双探针后，同列 typeof 恒 text 无混型（INTEGER 亲和翻回 int 的根因必红）；
   - **以后每次加表/加列引用 user id 而漏同步清单或声明 INTEGER，本守卫必红**，倒逼同步。
   - 残留登记：`grape_t_patrol_findings.owner`、`issue.release_approved_by`（0=unowned 整型哨兵语义）修复需动 src/，暂以 `_KNOWN_INTEGER_SENTINEL` 显式在册看护（uuid 串写入 typeof 底线仍锁）；修一列删一项自动落入强断言面，全部清零后登记表与看护用例退役。
2. **新用户 e2e `tests/test_new_user_uuid_lifecycle.py`**：uuid7 注册 → save/validate(consume=False/True)/consume binding_ticket 全链 → 跨用户与存量 '24' 混型隔离 → external_identities 绑定 → credential_usage_log 审计 → 触达外键列 typeof 终检；验收口径零异常零 410。OAuth HTTP 握手需真实 IdP 不可行处不 mock，以 grape_account_db DB 直调等价覆盖（见该文件尾注）。

部署/验收 checklist 增补：见 deploy-checklist-design.md「注册新账号走核心链路」验证步（注册新号→绑平台→触发一次凭据使用，观察零 410/零 500、审计行 typeof=text）。
