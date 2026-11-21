# 3 库 4 进程部署的环境契约（arch11 现场教训）

本文记录 2026-11-21 在 `grape-arch11`（本机 `<server_ip>`，公网 `<public_ip>`）
做会话凭据验证时踩到的 4 个部署侧坑。它们都不在代码里，但每个都能让流水线在
"代码没问题"的情况下整段失效，因此固化成契约。

---

## 一、四进程拓扑（规范基线）

```
进程        端口    角色     库                          账号
mgmt       38080   server   <mgmt_db_host> / grape_lfr_mgmt      liufei_dev_mgmt
event      38081   event    <runtime_db_host> / grape_lfr_runtime  liufei_dev_runtime
worker     38082   worker   <runtime_db_host> / grape_lfr_runtime  liufei_dev_runtime
insight    38083   insight  <insight_db_host> / grape_lfr_insight  liufei_dev_insight
```

三库三机、`GRAPE_DOMAIN_LIB=1`，跨域读走 lib 模式；nginx 按路径分流到四进程
（`/api/runtime/` → worker 域，`/api/` → mgmt 域）。

---

## 二、契约 1：RUNTIME_SERVER_URL 必须带本进程端口

```
正确  RUNTIME_SERVER_URL=http://127.0.0.1:<本进程端口>
错误  RUNTIME_SERVER_URL=http://127.0.0.1        ← 不带端口
```

**为什么会错**：进程用该值参与 GSB 注册的 `base_url` 计算。写成不带端口的形式，
注册表里会落到**相邻进程的端口**上，实测出现过：

```
mgmt    注册成 http://127.0.0.1:38081   （= event 的端口）
event   注册成 http://127.0.0.1:38082   （= worker 的端口）
insight 注册成 http://127.0.0.1:38084   （= 一个不存在的端口）
```

后果是跨进程服务发现打到错误端口，webhook 侧表现为：

```
POST /webhook/gitee → 404 {"code":404,"message":"repository not managed"}
日志：webhook expected-token lookup via GSB failed (repo=...)
      generic webhook rejected: unmanaged repo=...
```

排查特征：端口全 200、DSN 正常、`/internal/gsb/services` 返回 500，只有 webhook 404。

验证命令：

```bash
for p in 38080 38081 38082 38083; do
  pid=$(ss -ltnp | grep ":$p " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | head -1)
  tr '\0' '\n' < /proc/$pid/environ | grep '^RUNTIME_SERVER_URL='
done
grep -h 'GSB registered' data/logs/{server,event,worker,insight}.log | tail -4
# base_url 的端口必须与角色端口一致
```

## 三、契约 2：正式启动路径是 arch11-3host.sh，不是旁路脚本

```
真路径   /home/liufei/bin/arch11-3host.sh start
         ← 由 liufei 的 crontab 每分钟经 guard-grape-four.sh 调用

旁路     data/start_rest.sh、data/restart_svc.py、data/restart_mgmt_launcher.py
         ← 早期手工脚本，env 来自 data/env_svc_<port>.json / data/mgmt_env.json
```

**行为差异（这次踩的坑）**：

1. 改旁路脚本的 env 会在**下一分钟被 guard 用 3host 的硬编码 env 覆盖回去**
   —— 表现为"改完生效、几分钟后又变回去"，很难自证。
2. `guard-grape-four.sh` 用 `ss -tlnpH` 找监听者的 pid。以 root 跑起来的进程，
   liufei 身份的 guard **看不到 pid**，会判成"没监听"→ 再拉一轮 → root/liufei
   两份进程抢同一端口，端口看起来 200，实际是错的那份在服务。
3. `arch11-3host.sh` 的凭据来自 `$HOME/.grape-3host-credentials`。用
   `sudo -u liufei` 但没带 `-H` 时 `HOME` 仍是 `/root`，凭据读不到 →
   `GRAPE_DSN_*` 密码为空 → 跨域查询 `(1045, "Access denied ... (using password: NO)")`。

**正确用法**：

```bash
sudo -u liufei -H bash /home/liufei/bin/arch11-3host.sh start
# 或彻底干净：
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
      HOME=/home/liufei bash /home/liufei/bin/arch11-3host.sh start
```

启动后必须验三件事，缺一不可：

```bash
# ① 四端口 200
for p in 38080 38081 38082 38083; do curl -s -o /dev/null -w "$p=%{http_code} " http://127.0.0.1:$p/health; done
# ② 进程归属统一（不能混 root）
ps -eo pid,user,cmd | grep 'uvicorn src.api.grape' | grep -v grep
# ③ DSN 密码非空
tr '\0' '\n' < /proc/<pid>/environ | grep '^GRAPE_DSN_MGMT=' | grep -q ':@' && echo "密码为空!"
```

## 四、契约 3：revoke 后 env_extra 仍留快照，属正常

任务结束时 `revoke_session_token(task_id)` 会删除 `session_api_tokens` 的行，但
`agent_tasks.env_extra` 里的 `GRAPE_SESSION_TOKEN` 快照仍保留。所以事后审计看到：

```
env_extra 里有 GRAPE_SESSION_TOKEN（46 字符，tk_ 前缀）
grape_lfr_mgmt.session_api_tokens 里查不到该 session_key 的行
```

**这是正常终态，不是"未落库 token"**。判定方法看任务状态：

```
completed / failed / cancelled 的任务  → 行被 revoke 是预期的
running / queued 的任务               → 行必须存在；不存在才是签发失败
```

`session_api_tokens` 里长期存在的往往是**没跑完的任务**留下的行（被手工 cancelled
的、进程崩溃的），不是泄漏。

## 五、契约 4：probe_status=fail 的 runtime 仍可能被 claim

实测：`agent_runtimes` 里 `990070 dsh` 的 `healthy=1、probe_status=fail`
（`api.deepseek.com/models` 返回 401）时，claim **仍然把 vod 任务派给了它**，
任务以 `Authentication Fails, Your api key: ****xxxx is invalid` 失败，然后自动
重试换到健康引擎才成功。

```
受影响   任务多花一次失败重试；E2E 成功率被拖低
根治方向 claim 时把 probe_status 纳入过滤（当前只看 healthy）
临时规避 把该 runtime 的 healthy 置 0，或删掉对应引擎的无效凭据让探活持续 fail
```

同理，给引擎补 LLM key 时要**先验 key 有效**再落进 service env，否则只是把
"没有 key"换成"有无效 key"，探活依然 fail，任务依然白跑一次。

---

## 六、契约 5：库布局与真源判定（别靠记库名）

四进程 → 三库的映射，两套环境不同，务必先确认在跑的是哪套：

```
本机 arch11（/home/liufei/grape-arch11，38080-38083）
  mgmt    38080  grape_lfr_mgmt     <mgmt_db_host>
  event   38081  grape_lfr_runtime  <runtime_db_host>
  worker  38082  grape_lfr_runtime  <runtime_db_host>
  insight 38083  grape_lfr_insight  <insight_db_host>

227 grape-arch4（8081-8084）
  mgmt 8081 / event 8082 / worker 8083 / insight 8084
  三库同实例 <mgmt_db_host>：grape_dev_mgmt / grape_dev_runtime / grape_dev_insight
```

**归属以 `configs/table_ownership.json` 为准**（149 张基表 + 131 个视图，随仓维护；
`src/kernel/table_ownership.py` 只做判定、未知表放行）。它不是"每张表只在一个库"——
很多表另有视图副本供跨域读。

速查 SQL（任何表都能用）：

```sql
SELECT TABLE_SCHEMA, TABLE_TYPE
FROM information_schema.TABLES
WHERE TABLE_NAME = 'grape_t_repo_credentials' AND TABLE_SCHEMA LIKE 'grape_%';

-- 命中 VIEW 就顺着定义找基表
SELECT TABLE_SCHEMA, LEFT(VIEW_DEFINITION, 200)
FROM information_schema.VIEWS
WHERE TABLE_NAME = 'grape_t_repo_credentials' AND TABLE_SCHEMA LIKE 'grape_%';
```

### D0：证据来源自证（任何「库里 0 行」的结论之前）

**契约层是单点的，DDL 层带历史副本** —— 代码走契约没错，错的是举证走裸 SQL：

```
契约链（永远读 owner 库）
  mgmt  get_repo_credential_meta(rk,'bot')       src/domains/mgmt/user/db.py
    → _insight_public.repo_credential_meta(...)  # 跨域只调 insight 域 public
    → insight read_one_any('repo_cred_value_id') # 连 insight 自己的 DSN
    → _decrypt_repo_secret()
写入：runtime.public.upsert_repo_credential → insight.public.upsert_repo_credential_value

裸 SQL
  grape_lfr_mgmt.grape_t_repo_credentials     BASE TABLE，0 行   ← 空副本，不报错
  grape_lfr_insight.grape_t_repo_credentials  2 行              ← 数据在这
```

副本怎么来的（结构性，不是偶发）：

```
① 单库时代每个进程都建全量表 → 每库一份同名副本
② 拆库后 _migration_conn(domain, default) 在目标域 DSN 不可用时回落进程库
   → 「给 insight 建表」的迁移建在了 mgmt 里
③ _owned_skip 只拦「新建」，对已存在的副本无效；
   CREATE TABLE IF NOT EXISTS 也不会删或重建
```

所以验证脚本第 0 行必须自证来源：

```python
# 契约路径（推荐）：不碰裸表
from src.domains.mgmt.user.db import get_repo_credential_meta
meta = get_repo_credential_meta(repo_key, "bot") or {}   # 读的一定是 owner 库

# 裸 SQL 时必须自证
cur.execute("SELECT DATABASE(), @@hostname")
print("connected:", cur.fetchone())        # 打印在输出里，别只在心里确认
```

**不要指望启动期告警**（2026-11-21 撤销 `db_setup._foreign_copy_warning`）：
建表时逐表查 `information_schema` 属运维诊断，不该塞进启动路径。
排查时自己查一次，把结果打印出来：

```sql
SELECT TABLE_SCHEMA, TABLE_TYPE FROM information_schema.TABLES
 WHERE TABLE_NAME='grape_t_repo_credentials';   -- 非 owner 库出现 BASE TABLE 即副本
```

需要批量判断时调原语（只发现不改）：`table_ownership.audit_foreign_copies(conn, "<库名>")`。

### 已核实偏差（2026-11-21）

`grape_t_repo_credentials` 规范归属 = **insight**（清单：tables=insight，views=insight+mgmt）：

```
规范        insight=BASE(真源)    mgmt=VIEW    runtime=无
本机实测    insight=BASE(2 行)✔   mgmt=BASE(0 行)✘ 应是视图   runtime=BASE(0 行)✘
227  实测   insight=VIEW✘         mgmt=VIEW✔                  runtime=BASE✘ 应是 insight
```

后果（只影响排查，不影响运行）：

```
· 本机查 mgmt 得 0 行 → 会误判「该仓没配 bot」（已实际踩过）
· 227 因视图转发，查任何库都读到 runtime 那份 → 看不出落点错误
· 同一张表在两套环境物理位置不同，结论不可跨环境套用
```

同批"双份副本"表（清单已裁决，别再按库名猜）：

```
grape_t_system_credentials      runtime   (view: mgmt)
grape_t_credential_usage_log    runtime   (view: mgmt)
takeover_sessions               runtime   (无 view)
branch_guard_state              runtime   (无 view)
grape_t_repo_credentials        insight   (view: insight, mgmt)
```

待办（需停机窗口，未执行）：227 把 rc 基表迁回 insight；本机 mgmt 那张空基表改成视图。

## 七、契约 6：GRAPE_MIGRATION_OWNER 必须按角色设置

四进程 env 只有 `GRAPE_DOMAIN_LIB=1` 是不够的。缺 `GRAPE_MIGRATION_OWNER` 时
`current_migration_domain()` 返回空串 = **不筛域**，mgmt 进程会以「全量迁移」形态把
别域的 DDL 建进自己的库；event/insight 进程则因为 `run_domain_migrations()` 看到
owner 为空直接 return，**自己的库永远拿不到该有的列和台账表**。

```
正确取值（arch11-3host.sh 第 9 参数）
  server  :38080  mgmt
  event   :38081  runtime
  worker  :38082  （留空 —— 与 event 共用 runtime 库，按设计不跑迁移，
                    设了会与 event 并发跑同一批 DDL）
  insight :38083  insight
```

实测事故（2026-11-21）：

```
迁移 202611200006（agent_runtimes.desired_plugins，domains=("runtime",)）
  盖章在 grape_lfr_mgmt 账本 18:14:27，列建在 mgmt.agent_runtimes（那表 0 行）
  grape_lfr_runtime.agent_runtimes 有 19 行，反而没这一列；连账本表都不存在
  → 引擎注册 /api/runtime/register 连续 500
     (1054, "Unknown column 'desired_plugins' in 'field list'")
```

修法与验证：

```
① 补列（该 MySQL 上 TEXT 不能带字面默认值，会报 1101）：
     ALTER TABLE grape_lfr_runtime.agent_runtimes ADD COLUMN desired_plugins TEXT;
     UPDATE grape_lfr_runtime.agent_runtimes SET desired_plugins='[]' WHERE desired_plugins IS NULL;
   （mgmt 里那条是迁移用表达式默认值建的，列定义 text/NULL/DEFAULT '[]'，效果等价）
② arch11-3host.sh 的 start_one 增加第 9 参数并导出 GRAPE_MIGRATION_OWNER
   （`${mowner:+"GRAPE_MIGRATION_OWNER=$mowner"}`，空则不导出）
③ 重启后：runtime/insight 自动建起 grape_t_schema_migrations 并盖章
     grape_lfr_runtime 42 条 / grape_lfr_insight 2 条
     /api/runtime/register 从 500 unknown column → 401 invalid runtime credential
     （401 是缺 token 的正确行为）
   `[migration-guard]` 的 ERROR 告警随之消失
```

排查判据：`SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='<db>'
AND TABLE_NAME='agent_runtimes' AND COLUMN_NAME='desired_plugins'` 在该域库里应为 1；
若只在别域库有，就是 owner env 缺失。

## 八、回滚清单（本机契约落地后如需退回）

```
启动脚本   /home/liufei/bin/arch11-3host.sh.bak-<ts>        ← 去掉 RUNTIME_SERVER_URL 行
旁路 env   data/env_svc_3808{1,2,3}.json、data/mgmt_env.json  ← 去掉 RUNTIME_SERVER_URL 键
crontab    /home/liufei/crontab.bak-<ts>                     ← guard 行若被改过可整体还原
```

## 九、与其他文档的关系

```
会话凭据本身的行为契约   docs/weekly-11/session-token-idempotent-issue.md
平台卡 / clone 端点派生  docs/weekly-11/clone-endpoint-single-source-design.md §9
本文                     部署侧环境契约（三库四进程 + 启动路径 + 审计口径）
```