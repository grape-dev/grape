# 部署手册（三库 + 四进程 + 两层 nginx）

适用：本机 `<lan-ip-a>` 与兄弟机 `<lan-ip-b>`（`<sibling-host>`）。

## 1. 一条命令部署

```bash
# 本机（systemd 托管，需要 root）
sudo bash ~/deploy3db.sh

# 兄弟机
bash <deploy-root>/deploy/deploy3db.sh
```

脚本自动识别运行形态，不需要手动判断：

- **systemd 模式**（`grape@*.service` 在运行）→ 内部执行
  `systemctl restart grape@{mgmt,event,worker,insight}`
- **nohup 模式**（没有单元在跑）→ 走原来的 pid/端口/模块名三重 stop + nohup start
- 两种情况都会继续做同一套验收：健康等待 → 每模块恰好 1 个进程 → **启动时间校验**

> 历史坑：脚本早期「检测到 systemd 就 exit 3」，导致兄弟机上的部署长期是空跑，
> 旧进程继续应答，四个 200 看起来像成功，实际代码一行没更新。

## 2. 验收判据（脚本已内置）

```
[3db] all 4 processes healthy
[3db] status:
  127.0.0.1:38081/api/health -> 200
  127.0.0.1:38082/health      -> 200
  127.0.0.1:38083/health      -> 200
  127.0.0.1:38084/api/health  -> 200
```

- 端口 200 **只说明有进程在应答**，不说明是新代码。
- 真正的判据是启动时间：脚本用 `/proc/<pid>/stat` 的 `starttime` 加上 `/proc/stat` 的 `btime`
  算出进程启动时刻，早于本次部署起点（容差 5s）就报错并 `exit 1`：

```
[3db] ERROR: worker pid=1234 启动于 21:07:37，早于本次部署起点 21:09:02 —— 进程没被重启，仍在跑旧代码
[3db] FAIL: 有进程未随本次部署重启（见上），代码未生效
```

- `WARN: agent pid=… 工作目录不在部署目录` —— 判据是 `/proc/<pid>/cwd`。
  只看解释器路径会误报：兄弟机的另一租户复用旧 venv 跑新 checkout，是正常的。

## 3. 三库入口（最容易漏的一环）

`grape@*.service` 通过 `grape-systemd-run.sh` 启动，它需要知道三库 DSN：

```
unit 侧  Environment=GRAPE_3DB_ENV=/etc/grape/grape-3db.env
文件内容  GRAPE_DOMAIN_LIB=1
          GRAPE_DSN_MGMT=mysql://…
          GRAPE_DSN_RUNTIME=mysql://…
          GRAPE_DSN_INSIGHT=mysql://…
文件权限  640 root:grape        ← 必须让服务用户可读
```

drop-in 见 `deploy/systemd/grape-service-3db-env.conf`。

**缺这一环的后果**（2026-11-15 实际发生，两台各踩一次）：

```
脚本走单库分支 → worker 连回 <legacy-db-host>/grape_runtime_db（旧库）
→ runtime_credentials 里查不到 stage 凭据
→ 所有 runtime 请求 2ms 返回 401
→ 四个凭据引擎全部被判 offline，前端显示「经常失联」
```

**权限不对的后果**：服务用户读不到 → 脚本 `exit 4/NOPERMISSION` → 四个 role 全部
`activating → failed`，四个端口 000。

另一租户侧同理，但它不在 `grape` 组，所以要单独一份：

```
<tenant-env-file>     600 <tenant-user>:<tenant-group>
+ drop-in Environment=GRAPE_3DB_ENV=<tenant-env-file>
```

## 4. 部署后自检

```bash
# 1) worker 连的是不是 stage 三库
PID=$(pgrep -f "uvicorn src.api.grape_worker_server" | head -1)
tr '\0' '\n' < /proc/$PID/environ | grep -E "^DEVOPS_DB_HOST=|^DEVOPS_DB_PORT=|^DEVOPS_DB_NAME=|^GRAPE_DOMAIN_LIB="
# 期望：<lan-ip-b> / 3307 / <runtime-db> / 1

# 2) 引擎在线且心跳在 15s 节奏内
mysql -h <lan-ip-b> -P 3307 <runtime-db> -e "
  SELECT ar.id, ar.name, ar.status, ar.healthy, ar.last_seen_at, rc.name
  FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id
  ORDER BY ar.id;"

# 3) 门禁
make lint                      # import / undefined-name / async-blocking 棘轮
pytest tests/arch tests/contracts tests/scripts -q
```

## 5. 常见故障对照

| 现象 | 先查什么 |
| --- | --- |
| 四个端口 000 | `systemctl status grape@worker`，看是否 `exit 4`（3db env 不可读） |
| 引擎全 offline、请求 401 在 2ms 返回 | worker 的 `DEVOPS_DB_NAME` 是否 `<runtime-db>`（连错库） |
| 引擎 online 但心跳 60-300s | 服务端是否跑了旧代码（启动时间校验）／是否跨机 DB 拥塞 |
| `WARN: agent 工作目录不在部署目录` | 该 agent 的 cwd 是否指向旧 checkout（`<legacy-checkout>`、`<tenant-checkout>`） |
| 非默认租户 worker 反复重启（NRestarts 飙升） | unit 是否还指向旧代码目录；熔断见 `deploy/systemd/grape-tenant-worker-ratelimit.conf` |

## 6. 相关文件

```
deploy/deploy3db.sh                                   部署入口（两种形态）
deploy/systemd/grape-service-3db-env.conf             grape@* 的三库入口 drop-in
deploy/systemd/grape-tenant-worker-ratelimit.conf    非默认租户 worker 重启熔断
scripts/grape-systemd-run.sh                          按角色拆 DSN → DEVOPS_DB_*
scripts/upgrade_worker_agent.sh                       把 agent 执行进程切到当前部署目录
```