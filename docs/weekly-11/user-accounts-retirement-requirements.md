# 需求：旧账号表 user_accounts 清退（读路径归零）

> 触发：现网数据已搬完（user_accounts 0 行、grape_accounts 232 行、归档 94 行可回滚），
> 仓库里仍残留旧表读路径与旧表 DDL，读到的只能是空结果。
> 目标：让**代码不再触达这张表**，把"账号 id → 用户名"统一到权威表 grape_accounts。

## 1. 事实基线（运维给出，2026-11-22）

```
生产 user_accounts             0 行
生产 grape_accounts            232 行
归档 grape_system_db.user_accounts 94 行（回滚用；用户名 94/94 在新表可命中）
gitcode_id 76 → external_identities 176 行；github token 20 个
孤儿检查：user_credentials 悬空 0 / external_identities 悬空 0
密码：84 个 hash 一致 + 10 个 bcrypt（迁移后自改）
昵称 92/94 一致；last_active_at 全保留
```

## 2. 需求

```
R1  零 SQL 触达：src/ 内不得出现任何以 user_accounts 为目标的
    SELECT / INSERT / UPDATE / DELETE / ALTER / JOIN（注释与历史说明不算）
R2  账号名解析统一：id → username 只读 grape_accounts；契约函数名保持不变，
    调用点不改（runtime/pipeline/account_env、沙箱列表）
R3  死键清理：registry 里没有消费方的旧表查询键必须删除
R4  旧表 DDL/迁移归零：建表注册、补列、旧字段搬迁、default 账户迁移、
    个人组织迁移里依赖旧表的分支全部删除
R5  注册表与归属清单一致：kernel 表注册表 与 configs/table_ownership.json
    都不得再列 user_accounts
R6  写冻结铁律不回退：GC1/GC2/GC3（src 全域零写）继续成立
R7  语义可解释：旧小整数 id 只存在于旧表时解析为空 —— 与现网此前表现一致，
    id 归一属数据侧任务，不在代码里做兼容猜测
```

## 3. 场景（用例编号与代码一一对应）

```
S1  源码 AST 扫描：所有 execute/read_one/read_one_any 的字面量 SQL 都不含 user_accounts
S2  契约键 "user_account_username_by_id" 的 SQL 指向 grape_accounts
S3  user_account_username_by_id_any：命中返回用户名；未命中返回空；id 非法不查库
S4  死键 "user_accounts_e2eb04" 已删除（访问必 KeyError）
S5  sandbox_admin 超管兜底的 JOIN 指向 grape_accounts（文件级断言，禁止回退成旧表）
S6  kernel 表注册表 与 table_ownership.json 均无 user_accounts
S7  migrations_ddl 无旧表查询常量/ALTER；db_setup 无旧表补列与个人组织迁移函数
S8  写冻结守卫仍为 0（复用既有 GC1/GC2/GC3 用例，不重复实现）
```

## 4. 验收

```
A1  S1–S7 全绿；A2 既有 GC1/GC2/GC3 全绿
A3  既有失败用例数量与父提交一致（不得新增红）
A4  grep 结果只剩注释/文档串（可人工复核）
A5  不改 token 签发、不改 HTTP 契约、不改 3库4进程拓扑
```

## 5. 非目标

```
· 不执行 DROP TABLE（代码已与表解耦，删表是运维动作，可随时做）
· 不回填历史 created_by/user_id（数据侧负责）
· 不改 db_setup 里与旧表无关的老库升级路径
```