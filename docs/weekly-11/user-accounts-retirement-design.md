# 设计：user_accounts 读路径归零

> 需求：`user-accounts-retirement-requirements.md`｜代码：本轮 7 个文件的删除与改向
> 场景用例：`tests/mgmt/test_user_accounts_retirement_scenarios.py`（S1–S7）

## 1. 判定原则

```
一个字段只有一个真源：
  账号 id ↔ 用户名        → grape_accounts（权威）
  用户名 ↔ 外部身份       → external_identities
  凭据（AK/SK、PAT）      → user_credentials / grape_t_repo_credentials

旧表 user_accounts 已不是任何一项的真源 ⇒ 任何对它的 SQL 都是"必然返回空"的死读；
死读不是兼容，是让后来者以为它还能用。所以处理方式不是"加守卫"，而是**删掉**。
```

## 2. 改动面（逐处）

| # | 位置 | 原状 | 处理 |
|---|---|---|---|
| 1 | `mgmt/public` 契约键 `user_account_username_by_id` | `SELECT username FROM user_accounts WHERE id=?` | 改读 `grape_accounts`，键名与函数名保留 |
| 2 | `mgmt/public` 契约键 `user_accounts_e2eb04` | 旧表按 id 取用户名 | 无消费方，删除 |
| 3 | `mgmt/routes/sandbox_admin.py` | 超管兜底 `JOIN user_accounts` | 改 `JOIN grape_accounts` |
| 4 | `db_setup._migrate_create_personal_orgs` | 旧表取用户列表建个人组织 | 函数删除（调用点早已注释） |
| 5 | `db_setup._migrate_data_to_personal_orgs` | 旧表按 username 取 id 迁凭据/技能 | 函数删除（同上） |
| 6 | `db_setup` default 账户迁移块 | 读旧表 default 账户搬 OAuth 配置 | 删除（现网恒零命中） |
| 7 | `db_setup` publish 列 / last_scene 补列 | 对旧表 `_ensure_column` | 删除 |
| 8 | `mgmt/migrations_ddl` | 4 个旧表查询常量 + 5 条旧表 ALTER | 删除；`ALTERS_USER_AND_KNOWLEDGE_COLUMNS` 只留知识表列 |
| 9 | `kernel/db.py` 表注册表 | 列了 `user_accounts` | 删除（否则与归属清单打架） |
| 10 | `configs/table_ownership.json` | 归属 + 跨域可见项 | 删除两项 |

上一批（c29056bdd）已删除的：5 个 deprecated 读函数、装饰器、`_USER_ACCOUNTS_DDL` 及其建表注册、
三段 init_db 死迁移、legacy 模块的补列与重映射、`cleanup_orphan_credentials` 尾部的 users 映射。

## 3. 关键取舍

```
① 为什么保留函数名 user_account_username_by_id_any
   调用点在 runtime/pipeline/account_env，改函数名要动 3 个域；名字虽然带历史包袱，
   但它现在读的是权威表。语义变化只写进 docstring，不动调用面。

② 为什么删函数而不是改 SQL
   个人组织迁移的调用点已被 Issue #343 注释，函数体留着=死代码；
   把死代码改成"读新表"会复活一条没人调用的迁移路径，反而增加维护面。

③ 为什么删注册表/归属清单项
   table_ownership.owner_map() 是四进程 DDL 守卫的唯一真源；
   代码已不建这张表，清单还列着它 → 守卫会继续"保护"一张无人创建的表，
   与 kernel 注册表也会互相矛盾（有测试对账）。

④ 旧 id 解析为空是允许的
   现网旧表 0 行，此前同样返回空；id 归一由数据侧完成。
   代码层面不引入"猜 id 属于哪一代"的逻辑（猜错就会张冠李戴）。
```

## 4. 场景与实现映射

```
S1  AST 扫描 src/：只检查真正传给 execute/read_one/read_one_any 的字面量 SQL，
    不看注释与 docstring（否则历史说明会误报）
S2  _READ_QUERIES["user_account_username_by_id"] 文本必须含 grape_accounts、
    且不含 "FROM user_accounts"
S3  行为：monkeypatch read_one_any 捕获键与参数 → 命中/未命中/非法 id 三种分支
S4  read_one("user_accounts_e2eb04") 必 KeyError
S5  sandbox_admin.py 文本断言：有 "JOIN grape_accounts"，无 "JOIN user_accounts"
S6  TABLE_REGISTRY 与 manifest 双向断言
S7  migrations_ddl 常量值扫描 + db_setup 文本断言（不再有那两个函数名与旧表补列）
S8  复用 tests/mgmt/test_user_accounts_write_freeze_guard.py 的 GC1/GC2/GC3
```

## 5. 风险与回滚

```
风险 1  生产上仍有旧小整数 id 的引用 → 显示名变空（与现状相同，非新增退化）
回滚    整批是删除+改表名，git revert 单提交即可恢复；不涉及数据变更
风险 2  sandbox_admin 无 username 分支的行为变化：旧 id 不再能映射到超管
        → 与现状一致（旧表 0 行本来就映射不到）；数据归一后按新 id 生效
```

## 6. 非目标（明确不做）

```
· 不 DROP TABLE（代码已解耦，删表随时可做，属运维动作）
· 不回填 created_by/user_id（数据侧）
· 不动与旧表无关的老库升级语句（保留兼容）
· 不改 token 签发结构、HTTP 契约、三库四进程拓扑
```