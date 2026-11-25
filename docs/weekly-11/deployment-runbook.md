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
[3db] ERROR: worker pid=1234 启动于 <t-1m30s>，早于本次部署起点 <t0> —— 进程没被重启，仍在跑旧代码
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

### 3.1 worker 托管路径（内存上限与预 bind，Issue #4203）

生产 worker 的启动链路有 **两条**，两条都必须带同一套内存/重启参数，否则同一单在不同
机器上表现不同（有/无 MemoryMax、有/无预 bind）：

| 路径 | ExecStart | 内存/重启参数来源 |
|------|-----------|------------------|
| `grape@worker.service`（`scripts/install-grape-systemd.sh` 托管） | `grape-systemd-run.sh worker` → `scripts/start-worker.py`（预 bind） | drop-in `/etc/systemd/system/grape@worker.service.d/30-worker-oom.conf`（仓库源文件 `deploy/systemd/grape-worker-oom.conf`，安装脚本自动装） |
| `grape-worker-server.service`（静态单元） | `scripts/start-worker.py`（预 bind） | 单元内联 `MemoryAccounting/MemoryHigh=6G/MemoryMax=8G`、`RestartSec=1/RestartSteps=5/RestartMaxDelaySec=30` |

确认本机走哪条、参数是否生效：

```bash
systemctl cat grape@worker.service | head -20   # ExecStart 是否 grape-systemd-run.sh
systemctl show grape@worker -p MemoryAccounting,MemoryHigh,MemoryMax,RestartUSec,RestartSteps,RestartMaxDelaySec
journalctl -u grape@worker -n 50 | grep '\[memory-limit\]'   # 启动自检打印的生效限额
# 期望 MemoryMax=8589934592（8G）+ "[memory-limit] process=worker cgroup limit=..."
# 出现 "cgroup limit=unbounded" = drop-in 没装上，或限额挂在 v1 控制器
# process 标签与端口必须等于部署角色事实源（脚本按角色导出 GRAPE_PROCESS_ROLE /
# GRAPE_WORKER_PORT；出现在 .env 里的旧端口会让脚本 exit 6，而不是监听 A 注册 B）
tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value grape@worker)/environ \
  | grep -E '^GRAPE_PROCESS_ROLE=|^GRAPE_WORKER_PORT=|^GRAPE_UVICORN_WORKERS='
# 期望 worker / 38083 / 1
```

- 只跑 `systemctl show grape-worker-server` 不够：该静态单元可能根本没在运行。
- `MemoryMax` 是 16G 主机的保守默认；SRE 拿到 004 的 `dmesg` victim 行（total-vm/rss）
  后按宿主机覆盖 drop-in 或单元，并把实测值回写本节。
- 两条路径的 ExecStart 都是「脚本路径」启动 `scripts/start-worker.py`（非 `python -m`），
  此时 `-m` 把 cwd 加进 sys.path 的隐式行为不存在——app（`src.api.*`）可导入靠
  launcher 顶层的仓库根 sys.path 自举（Issue #4203 SRE 复核 Critical：缺自举时
  uvicorn config.load 阶段 ModuleNotFoundError，worker 反复 status=1/FAILURE）。
  因此 **worker 启动不依赖 PYTHONPATH**：现场若有以 `Environment=PYTHONPATH=`
  临时救急的 drop-in（如 `/etc/systemd/system/grape@worker.service.d/40-pythonpath.conf`），
  在含 sys.path 自举的版本部署后**必须删除并 `systemctl daemon-reload`**——留着它会把
  「自举被改坏」的回归掩盖成生产能跑、CI/新机器必炸。核查命令：

  ```bash
  systemctl cat grape@worker | grep -i PYTHONPATH   # 期望：无输出（SRE drop-in 已移除）
  systemctl restart grape@worker && sleep 3 \
    && curl -sf http://127.0.0.1:38083/api/ping     # 期望 {"ok":true,...}（无 PYTHONPATH 也能起）
  ```

## 4. 部署后自检

```bash
# 1) worker 连的是不是 stage 三库（#4203 起 worker 由 start-worker.py 预 bind 启动，
#    uvicorn 不再是命令行可见的进程名——先取 systemd MainPID，再兜底 pgrep）
PID=$(systemctl show -p MainPID --value grape@worker 2>/dev/null)
[ -n "$PID" ] && [ "$PID" != "0" ] || PID=$(pgrep -f "start-worker.py|uvicorn src.api.grape_worker_server" | head -1)
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
| `grape git push` 报 `[branch-guard] WARNING … src.kernel.lock … fail-open` 或 ImportError | worker 包缺 lock 模块（Issue #2706，第 8 次同类复发）。修复：重装/升级 worker 包（`scripts/install-worker.sh`，内置 import 冒烟会拦截坏包）。**`grape git push --no-verify` 仅作故障期临时规避**：会同时绕过分支租约与漂移校验，不可日常化；推送成功后必须尽快重装/升级 worker 包恢复守卫 |

## 6. 相关文件

```
deploy/deploy3db.sh                                   部署入口（两种形态）
deploy/systemd/grape-service-3db-env.conf             grape@* 的三库入口 drop-in
deploy/systemd/grape-tenant-worker-ratelimit.conf    非默认租户 worker 重启熔断
scripts/grape-systemd-run.sh                          按角色拆 DSN → DEVOPS_DB_*
scripts/upgrade_worker_agent.sh                       把 agent 执行进程切到当前部署目录
```