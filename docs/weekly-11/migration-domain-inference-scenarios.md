# 迁移域判定：显式 env → 库名推断 → 角色映射（S1–S7）

> 2026-11-21 / 分支 `feat/clone-endpoint-single-source`
> 关联：`docs/weekly-11/arch11-four-process-env-ops.md` 契约 6

## 1. 事故（实测，不是推断）

四进程 env 只有 `GRAPE_DOMAIN_LIB=1`、没有 `GRAPE_MIGRATION_OWNER`：

```
<db_mgmt>.agent_runtimes      desired_plugins=有   行数=0     账本=有   202611200006 盖章 18:14:27
<db_runtime>.agent_runtimes   desired_plugins=缺   行数=19    账本=无   未盖章
```

后果：

```
mgmt 进程   current_migration_domain() 返回 "" = 不筛域
            → init_database() 以「全量迁移」形态跑，runtime 域的 DDL 落进 mgmt 库
            → 迁移 202611200006 把 desired_plugins 建在 0 行的副本上
event/insight  run_domain_migrations() 看到 owner 为空 → 直接 return
            → 本域库永远不迁移（连 grape_t_schema_migrations 都没建）
引擎注册    POST /api/runtime/register → 500
            (1054, "Unknown column 'desired_plugins' in 'field list'")
```

最阴的地方是**台账看起来是已盖章的**（mgmt 账本 18:14:27 有记录），但列建在了错误的库上。

## 2. 设计：三级判定

`src/api/db_setup.py::current_migration_domain()`

```
① GRAPE_MIGRATION_OWNER 显式声明且合法 → 直接返回（部署脚本是权威）
② 缺省 / 脏值 → 由本进程库名推断（db_registry.current_domain()：DEVOPS_DB_NAME → 域）
③ 仍判不出 → 由进程角色映射（server/main → mgmt；event → runtime；insight → insight）
④ 单库模式（GRAPE_DOMAIN_LIB 非 1）→ 仍返回 ""（跑全量，历史行为不变）
```

`run_domain_migrations()` 的两处变化：

```
· 先拦角色：GRAPE_PROCESS_ROLE == worker → 直接 return
  （worker 与 event 共用 runtime 库；库名推断会把 worker 也判成 runtime，
    不拦就会与 event 并发跑同一批 DDL）
· domain 判定改用 current_migration_domain()（推断版），不再是裸读 env
```

## 3. 场景表

| 编号 | 场景 | 期望 | 用例 |
|---|---|---|---|
| S1 | 显式 `GRAPE_MIGRATION_OWNER=insight`（同时库名是 runtime、角色是 worker） | `insight`（env 权威） | `test_s1_explicit_owner_wins` |
| S2 | env 缺失 + `DEVOPS_DB_NAME=<db_runtime>` | `runtime` | `test_s2_infer_runtime_from_db_name` |
| S3 | env 缺失 + `DEVOPS_DB_NAME=<db_mgmt>` | `mgmt` | `test_s3_infer_mgmt_from_db_name` |
| S4 | env 是脏值 `runtme` | 回落推断 → `runtime`（不静默变「不筛域」） | `test_s4_invalid_owner_falls_back_to_inference` |
| S5 | 库名判不出域 + 角色 event/insight/server | `runtime` / `insight` / `mgmt` | `test_s5_role_mapping_when_db_name_unknown` |
| S6 | 单库模式（无 `GRAPE_DOMAIN_LIB=1`） | `""`（跑全量，历史行为） | `test_s6_single_db_mode_stays_empty` |
| S7 | 角色 worker（库名是 runtime） | 域迁移入口跳过，台账/DDL 都不碰；但域判定仍是 runtime | `test_s7_worker_skips_domain_migrations` |

用例文件：`tests/api/test_migration_domain_inference.py`（7 passed，单文件单跑）。

## 4. 真库验证

```
部署侧（已完成）
  arch11-3host.sh 加第 9 参数 mowner，server=mgmt / event=runtime / worker=空 / insight=insight
  重启后 runtime/insight 自动建账本：<db_runtime> 42 条，<db_insight> 2 条
  /api/runtime/register：500 unknown column → 401 invalid runtime credential（缺 token，正确）
  [migration-guard] 的 ERROR 告警消失

代码侧（本次）
  去掉 env 后 current_migration_domain() 仍应给出正确域 —— 见下节自愈验证
```

自愈验证（把 `GRAPE_MIGRATION_OWNER` 从脚本里拿掉再重启）：

```
期望：server → mgmt（否则会再跑全量）、event → runtime（否则本域库不迁移）、
      worker → runtime 但被角色拦截不跑、insight → insight
判据：启动日志里不应再出现
      「[migration-guard] ... 将以「不筛域」形态执行全量迁移」，
      且 event/insight 的台账条数不变（42 / 2），说明没有重复执行也没跑全量
```

## 5. 边界

```
· 推断只看「库名 + 角色」，不看 DSN 内容；库名乱命名的部署必须显式给 env（这是正确的取舍：
  猜错库名的代价比漏配 env 更大，日志会记录推断结果）
· 单库/回落部署不受影响（④ 分支原样返回 ""）
· worker 的域判定仍返回 runtime（供日志与其他入口），只是域迁移入口跳过
```