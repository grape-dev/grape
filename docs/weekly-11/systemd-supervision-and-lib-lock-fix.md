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

## 遗留（本轮未动，需运营侧确认）

- `grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db` 与 `grape_ljt_db` 仍在同一 MySQL 上，
  是三库演练/迁移残留，容易误判主库；确认后归档。
- `server_base_url` 在两节点都指向公网入口节点：这是既有设计（单入口），
  但入口节点是登录/webhook 的单点，需要 SLB/keepalived，而不是改配置。