# 三库模式的两处运行期缺陷：systemd 托管 + lib 模式分布式锁

本轮修的是三库独立模式（`GRAPE_DOMAIN_LIB=1` + `GRAPE_DSN_*`）上线后暴露的两处运行期缺陷。
它们都不会让 `/health` 变红，靠健康检查发现不了，只能从日志和进程树的形状上看出来。

## 1. 进程不受 systemd 托管（崩溃/重启后不自愈）

现象：

- `systemctl is-active grape@mgmt …` 四个角色全是 `inactive`，但端口 38081-38084 都在监听；
- 四个 uvicorn 进程 `PPID=1`，由 `deploy/deploy3db.sh` 用 `nohup` 拉起（pid 落在 `/home/grape/3db-<role>.pid`）；
- 机器重启、OOM、进程崩溃都不再自动拉起，必须有人手工再跑一次脚本。

修法（`scripts/install-grape-systemd.sh`，root 执行，幂等）：

1. 把三库 DSN 文件落到 `/etc/grape/grape-3db.env`（`root:grape 0640`，DSN 不写进 `.env`）；
2. 为四个角色写 drop-in `/etc/systemd/system/grape@<role>.service.d/20-3db.conf`：
   `GRAPE_3DB_ENV=<env 文件>` + 按角色的 `GRAPE_GSB_BASE_URL=http://<节点私网 IP>:<端口>`；
3. 停掉 nohup 遗留进程（pid 文件 + 端口兜底，跳过已归 systemd 的 MainPID）；
4. `daemon-reload` + `enable --now grape@{mgmt,event,worker,insight}`；
5. 等 4/4 健康并打印 `MainPID` 与 `/health` 里 `pid` 的对照表——两者相等才算真托管。

配套改动：

- `scripts/grape-systemd-run.sh` 增加三库分支：`GRAPE_3DB_ENV` 存在即 `source` 它、
  `GRAPE_DOMAIN_LIB=1`、把本角色 owner 域的 DSN 用 `urlparse` 拆成 `DEVOPS_DB_*`
  （`deploy3db.sh` 里的 bash 切分在密码含 `@ : /` 时会把 host 拆坏）；
- 同一处补 GSB 注册地址默认值：`GRAPE_GSB_BASE_URL` 留空时取本机首个私网地址。
  留空会让客户端回落 `127.0.0.1`，注册表 `base_url=http://127.0.0.1:<port>`，
  跨节点 round_robin 全打回本机；
- `deploy/deploy3db.sh` 增加互斥守卫：检测到 `grape@*` 已 enable/active 就拒绝执行
  （否则 nohup 与 `Restart=always` 抢同一端口，只有一个能绑上，另一个静默空转）。

验证（自愈）：

```bash
sudo kill -9 "$(systemctl show -p MainPID --value grape@worker)"
sleep 8 && systemctl is-active grape@worker && curl -s 127.0.0.1:38083/health
```

## 2. lib 模式分布式锁崩了，带死四个后台循环

现象（mgmt 日志每轮一条 ERROR）：

```
skill sync run failed
feedback reconcile run failed
feedback status sync run failed
table maintenance (messages) run failed
```

根因不在缺表，而在异常尾巴：

```
File "src/domains/runtime/public/__init__.py", line 5546, in acquire_distributed_lock
    row = c.execute(_any_sql("lock_row"), (lock_key,)).fetchone()
AttributeError: 'int' object has no attribute 'fetchone'
```

`db_registry._CompatCursor.execute()`（自有 DSN 域连接用的兼容游标）把 pymysql 的返回值
（受影响行数 `int`）原样透传，而调用点普遍写 `c.execute(sql).fetchone()`（sqlite3 语义）。
单库拓扑走的是 `mysql_compat.MySQLCompatCursor`（返回 `self`），所以只有分库部署会炸；
炸的时机也不是第一次抢锁，而是「锁已存在」的冲突分支（第 2 轮及以后、或并发的另一个进程）。

修法：`_CompatCursor.execute()/executemany()` 返回游标自身（与 `MySQLCompatCursor` 对齐），
行数改从 `c.rowcount` 读。回归测试：`tests/kernel/test_db_registry_compat_cursor.py`。

同批修的「非 owner 进程访问 runtime 表」：

| 位置 | 问题 | 修法 |
|------|------|------|
| `domains/runtime/storage/skill_sync.py` | 函数直接吃 `session_manager.db`；mgmt 进程的定时器也会调用 → `1146 skill` | `owner_db.db_pair("runtime")` |
| `domains/runtime/engine/table_maintenance.py` | 同上（`agent_tasks` / `task_messages` 都归 runtime）；mgmt 侧 `§6.2 A 部分` 清 `task_messages` 必 1146 | 三个清理函数统一 `db_pair("runtime")` |

`db_pair()` 的解析规则与 `db_registry.domain_db()` 一致：有自有 DSN → 该域连接；
否则回落进程库，单库/测试拓扑行为不变。

验证（无副作用，只读 + 锁协议）：

```bash
# 1) 锁协议（自有 DSN 路径）
python /tmp/probe_lock.py runtime     # acquire=True / other=False / renew=True / release=1
# 2) 非 owner 进程读 runtime 表
python /tmp/probe_owner.py            # skill / task_messages / agent_tasks / event_thread 计数正常
```

## 3. 真实拓扑复核后追加的两处修正（三库 = 三主机）

`/home/grape/.grape-rds-stage.env`（root 600）里的 DSN 并不是同一个 MySQL：
mgmt 在兄弟节点 `:3311`、insight 在兄弟节点 `:3312`、runtime 在本机 `:3307`
（`/opt/rds/bin/mysqld8 --no-defaults` 起的裸实例）。按这个真实拓扑复核后，还有两个坑：

### 3.1 连接打满 → 跨域读「假性缺表」

本机 runtime MySQL 用 `--no-defaults` 启动，`max_connections` 是默认 **151**；
四个进程默认连接池（32）+ 每线程跨域连接很快打到 `Max_used_connections=152`，
出现 `1040 Too many connections`。那时 `db_registry.domain_db()` 会把**一次**连接失败
记进 `_forbidden`，于是整个进程把 runtime 域拉黑，后续 `meta` / `scene_category` / `skill`
的跨域读全部静默回落到进程库 → 满屏 `1146 Table 'grape_stage_mgmt.xxx' doesn't exist`。
**1146 是症状，1040 才是病因。**

修法：

- `db_registry`：只有鉴权/库不存在/域名错这类「重试也不会好」的错误才永久停用，
  其余按 5s 退避重试（`tests/kernel/test_db_registry_retry_policy.py`）；
- `scripts/grape-systemd-run.sh`：按角色限制连接池（worker 48/24、mgmt 16/8、event 12/6、insight 12/6）；
- `scripts/p4_p5_loop.sh`：启动本机/远端 mysqld 时带 `--max-connections=${RDS_MAX_CONNECTIONS:-500}`。
  已存在的实例需要重启 mysqld 才生效（`SET GLOBAL` 需要 SUPER，部署账号没有）。

### 3.2 `scene_sync` 也直连进程库

`skill sync` 的 scene 步骤只读 runtime 域的表（`scene_category`、`skill`），却用
`session_manager.db`；mgmt 进程跑它时必然 `1146 scene_category`。改为
`owner_db.db_pair("runtime")`（单库拓扑回落进程库）。

### 3.3 重启期 `address already in use` 导致 crash loop

`KillSignal=SIGINT` + `TimeoutStopSec=25`，而关机路径（IM outbox `stop()` 等）有时要
25s 才被 SIGKILL；`RestartSec=5` 让新实例抢在旧进程释放端口之前启动 → `[Errno 98]` → 退出码 3
→ systemd 再重启，形成连环。切换/重启用**干净序列**：`systemctl stop` → 等端口释放 →
`pkill -f "[u]vicorn src.api.grape_"` → `systemctl reset-failed` → `systemctl start`。

## 验收（2026-11-15 19:45 CST 实测）

| 检查 | 结果 |
|------|------|
| 四角色 `is-active` / `NRestarts` | 4×active / 0（稳定 2 分钟） |
| `MainPID` == `/health` 里的 `pid` | 4/4 相等（真托管） |
| 自愈（kill -9 worker） | 10s 内新 pid，`/health` 200 |
| mgmt 日志 1146 / 1040 / AttributeError / run failed | 0 条（此前每轮各 1 条） |
| skill sync | `remote done` → `obs sync done` 正常 |
| scene sync | `Scene sync done: added=0 updated=0 skipped=14 failed=0` |
| runtime MySQL 连接 | 峰值 91 / 500，`Connection_errors_max_connections=0` |
| GSB 注册 | `mgmt@hd002 base_url=http://<本机私网 IP>:38081` |

## 4. `grape_stage_mgmt` 缺表的处置（2026-11-15 补做）

实测兄弟节点 `:3311` 的 `grape_stage_mgmt`：59 张基表、0 视图；按清单缺 33 张。分类后发现清单
是从旧三域库 `grape_dev_mgmt`（141 张）派生的，里面混着**本仓没有任何代码引用**的名字。

处置（全部用源库 DDL，不手写建表语句）：

- `SHOW CREATE TABLE grape_dev_mgmt.<t>` 取原语句（去 `AUTO_INCREMENT=` 起始值），在
  `grape_stage_mgmt` 里 `CREATE TABLE IF NOT EXISTS`，一次补了 **27 张**：
  `admin_*`（10 张）、`cloud_sandboxes` / `sandbox_*` / `engine_sandbox_*` / `user_vms` /
  `user_vm_repos`、`egress_admin_*`、`task_proxy_tokens`、`repo_platform_tokens`、
  `user_llm_credentials`、`template_usage`、`devops_agent_runtime_binding`。
  结果：59 → **86 张**，三域缺口全部归零（mgmt 81/86、runtime 35/98、insight 12/20 归属表齐）。
- 其余 6 张不建，并从 `configs/table_ownership.json` 移除（迁到 `_not_in_current_schema` 记录）：
  5 张 `backup_*_20260817` 是历史快照、`flyway_schema_history` 是 Java 迁移遗留；
  同时删掉 `credential_reveal_log` 这条旧名重复登记（真名 `grape_t_credential_reveal_log`，
  权威 `docs/weekly-11/table-owner.tsv` 里只有真名）。

清单 135 → 128 张，`tests/api/test_table_ownership.py` 18 例通过；守卫行为复核：
`agent_tasks` 在 mgmt 进程 skip=True、`admin_users` skip=False、未登记表放行。

### 4.1 清掉 mgmt 库里的 3 张 runtime 域残留表

`grape_stage_mgmt` 里还有 `bot_seen_messages` / `chat_agent_session` / `repo_im_group_binding`，
是单库时期留下的（3 张都是**空表**，且 `chat_agent_session` 只有 14 列，runtime 库正本是 17 列 32 行）。
SQL 侧其实都走 owner 域：`chat_agent_session` 的语句集中在 `domains/runtime/public/_ANY_QUERIES`，
经 `_any_cursor()` 连 runtime DSN；`bot_seen_messages` / `repo_im_group_binding` 只在
`_purge_chat_im_binding` 的注释里出现。

处置（可回滚）：

1. `SHOW CREATE TABLE` 导出 DDL → `/home/grape/backup-leftover-tables/leftover-<ts>.sql`；
2. `RENAME TABLE x TO zz_legacy_x`（秒级可回滚），观察 10 分钟；
3. 窗口内 `(1146 ... 这三张表)` 计数为 0、四角色 `/health` 200 → `DROP TABLE zz_legacy_x`。

结果：`grape_stage_mgmt` 83 张表（27 张新建 + 3 张清理），三域缺口仍为 0。

## 遗留（本轮未动，需运营侧确认）
- `grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db` 与 `grape_ljt_db` 仍在
  共用的 RDS 实例（`<legacy-rds-host>`）上，是三库演练/迁移残留，容易误判主库；确认后归档。
- `server_base_url` 在两节点都指向公网入口节点：这是既有设计（单入口），
  但入口节点是登录/webhook 的单点，需要 SLB/keepalived，而不是改配置。