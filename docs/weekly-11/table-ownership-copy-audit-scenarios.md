# 表归属副本审计与安全清理（S1–S10）

> 2026-11-21 / 分支 `feat/clone-endpoint-single-source` / 关联 `configs/table_ownership.json`
> 起因：本轮两次把 `grape_t_repo_credentials` 从 mgmt 库查成「0 行 → 该仓没配 bot」，
> 实际数据在 insight 库。这是**契约层与 DDL 层的落差**，不是偶发查错库。

## 1. 为什么会存在副本

```
① 单库时代     每个进程都跑 db_setup 建全量表 → 每库一份同名副本
② 拆库后迁移   _migration_conn(domain, default) 在目标域 DSN 不可用时回落进程库
               → 「给 insight 建表」的迁移建在了 mgmt 里
③ 守卫只拦新建 _owned_skip 只管「别家域的表不建/不改」，
               对已存在的副本无效；CREATE TABLE IF NOT EXISTS 也不删/重建
```

结论：**契约层是单点的，DDL 层带历史副本**。代码走契约是对的（`mgmt.get_repo_credential_meta`
→ `insight.public.repo_credential_meta` → insight 自己的 DSN），错的是举证时的裸 SQL。

## 2. 术语

```
owner           configs/table_ownership.json 里声明的归属域（唯一权威副本应在该域库）
BASE TABLE 副本  非 owner 库里物理存在的同名实表 —— 裸 SQL 能查到，值可能为空或陈旧
VIEW 副本        非 owner 库里的视图，转发到 owner 表 —— 正确形态（<host_b> 那套的 mgmt/insight 就是）
empty_copy       副本 0 行 → 数据在 owner 库，可安全清理
divergent_copy   副本非 0 行 → 可能该部署真源就在这份里（实测 <host_b>：rc 基表在 runtime），
                 绝不能自动删，必须人工裁决后再迁
```

## 3. 场景表

| 编号 | 场景 | 期望 | 用例 |
|---|---|---|---|
| S1 | 非 owner 库、BASE TABLE、0 行 | `empty_copy` | `test_s1_empty_copy_is_classified_empty` |
| S2 | 非 owner 库、BASE TABLE、非 0 行 | `divergent_copy`，`drop_empty_copy` 拒绝 | `test_s2_divergent_copy_is_not_deletable` |
| S3 | 非 owner 库、VIEW | 不出现（正确形态） | `test_s3_view_is_not_reported` |
| S4 | owner 自己的表 | 不出现 | `test_s4_owner_table_is_not_reported` |
| S6 | 清理 0 行副本 | 删除成功 | `test_s6_drop_empty_copy_succeeds` |
| S7 | 清理非 0 行副本 | 抛错拒绝，不删 | `test_s7_drop_refuses_non_empty` |
| S8 | 非法表名（注入形态） | 抛 `ValueError`，不执行 SQL | `test_s8_drop_refuses_unsafe_table_name` |
| S9 | 迁移默认运行 | 只告警，**不删任何表** | `test_s9_migration_warns_without_touching_data` |
| S10 | `GRAPE_TABLE_COPY_CLEANUP=1` | 只删 `empty_copy`，`divergent` 保留 | `test_s10_cleanup_switch_only_drops_empty_copies` |

用例文件：`tests/kernel/test_table_ownership_copy_audit.py`（10 passed）。

启动路径**不做**副本检测（2026-11-21 撤除 `db_setup._foreign_copy_warning`）：建表时
逐表查 `information_schema` 属运维诊断，不是启动该干的活，且与下面的迁移审计重复。
需要时由运维/脚本直接调用原语：

```python
from src.kernel.table_ownership import audit_foreign_copies, drop_empty_copy
items = audit_foreign_copies(conn, "<库名>")      # 只发现不改；empty_copy / divergent_copy
drop_empty_copy(conn, "<表名>")                   # 只删 0 行副本，非 0 行/非法名一律拒绝
```

## 4. 真库证据（本机 <host_a>，2026-11-21 18:46）

`<db_mgmt>`（<mgmt_db_host>）的别域副本 —— 11 张：

```
agent_runtimes                    owner=runtime  rows=0    empty_copy
branch_guard_state                owner=runtime  rows=0    empty_copy
grape_t_cloud_sandboxes           owner=runtime  rows=0    empty_copy
grape_t_credential_usage_log      owner=runtime  rows=0    empty_copy
grape_t_engine_lifecycle_audit    owner=runtime  rows=0    empty_copy
grape_t_merge_gate_log            owner=insight  rows=0    empty_copy
grape_t_repo_credentials          owner=insight  rows=0    empty_copy   ← 两次误判的来源
grape_t_system_credentials        owner=runtime  rows=0    empty_copy
meta                              owner=runtime  rows=4    divergent_copy
scene_category                    owner=runtime  rows=0    empty_copy
takeover_sessions                 owner=runtime  rows=0    empty_copy
```

`meta` 的 4 行：`issue343_jwt_invalidated` / `issue95_migrate_done` / `m0_backup_done` /
`server_base_url` —— runtime 库里**四个键都存在**，当前值一致（`server_base_url` 两边都是
`http://<server_ip>`）。但这条路径历史上踩过坑：往 mgmt 的同名 `meta` 写键**完全不生效**
（服务只读 runtime 那份）。它是「双写路径残留」，一旦哪次只写 mgmt 就会分叉 ——
这正是它被归为 `divergent_copy`、禁止自动删的原因。

启动日志（mgmt 重启后，原句）：

```json
{"level":"WARNING","message":"[TABLE_OWNERSHIP_AUDIT] <db_mgmt>: 别域副本 11 张 (空 10 / 非空 1)；裸 SQL 会读到空表或旧数据且不报错，断言数据面请走 owner 域 public 原语"}
{"level":"WARNING","message":"[TABLE_OWNERSHIP_AUDIT] `meta` owner=runtime 本库 4 行 → 非空副本，可能该部署真源在此，需人工裁决后再迁，禁止自动删"}
```

迁移盖章：

```
<db_mgmt>.grape_t_schema_migrations
202611210005  table-ownership-copy-audit  2026-11-21 18:46:47
```

四端口 38080–38083 `/health` 全部 200。

## 5. 契约（写死，别改）

```
① 审计只发现不修改：audit_foreign_copies() 不改任何表结构、不动数据
② 默认不清理：迁移只告警；非 0 行副本永远不删（数据可能只在这份里）
③ 清理必须显式开启：GRAPE_TABLE_COPY_CLEANUP=1，且只作用于 0 行副本
④ 表名白名单：^[A-Za-z0-9_]{1,64}$，表名虽来自 information_schema 仍校验
⑤ 判域只靠 configs/table_ownership.json + 库名后缀，不靠印象
```

## 6. 运维手册

```sql
-- 任意表在三库里是基表还是视图
SELECT TABLE_SCHEMA, TABLE_TYPE FROM information_schema.TABLES
 WHERE TABLE_NAME='grape_t_repo_credentials' AND TABLE_SCHEMA LIKE 'grape_%';

-- 命中 VIEW 就顺定义找基表
SELECT TABLE_SCHEMA, LEFT(VIEW_DEFINITION,200) FROM information_schema.VIEWS
 WHERE TABLE_NAME='grape_t_repo_credentials' AND TABLE_SCHEMA LIKE 'grape_%';
```

```
裸 SQL 前先自证：  SELECT DATABASE(), @@hostname;
断言数据面：       get_repo_credential_meta(rk,'bot')   ← 永远读 owner 库
清理 0 行副本：    GRAPE_TABLE_COPY_CLEANUP=1 重启该域进程（只删 empty_copy）
```

## 7. 待办（不在本 PR）

```
① <host_b> 那套：rc 基表在 runtime（清单声明 insight）→ 先迁后删，需停机窗口
② 本机 mgmt：10 张 0 行副本可开开关清理一次，但 meta 那 4 行必须先确认
   （当前与 runtime 一致，删除前需证明无写入方依赖 mgmt 那份）
③ 单库/回落部署（GRAPE_DDL_OWNER_GUARD=0）不适用本审计，勿在那边开清理开关
```