# new-version2 部署操作清单（Production Runbook）

> 版本：v1.0
> 日期：2026-08-08
> 目标服务器：`203.0.113.2`（项目 `/opt/grape`，systemd `grape.service`，nginx 反代 443→3000，webhook 8080 直连）
> 配套脚本：同目录 `deploy.sh`（服务端执行）/ `verify.sh`（部署后校验）/ `rollback.sh`（回滚）
> 前置：`implementation-plan.md` §五（升级顺序与开关）/ `concurrency-design.md`（并发）/ `skill-delivery-remote.md`（技能交付）

---

## 〇、升级总原则（数据安全优先）

1. **先备份、再迁移**：任何结构变更前 `data/sessions.db` 必须留档；部署工具已内置「迁移前自动备份」（`data/backups/*.bak`），仍建议手动再留一份。
2. **开关灰度、可逆回滚**：`execution_mode` 默认 `auto`（本机引擎→direct、远端→入队），M0–M4 全部代码可在 `auto` 下发布；确认无回归后再切 `queue_remote`（远端 worker）。`queue_local` 已废除。
3. **绝不跨分支丢提交**：部署分支必须是已合入的 `main` 或明确的发布分支，`git pull` 前确认无冲突。
4. **禁用 PowerShell 改含中文文件**（见 `DEPLOYMENT-REMOTE.md` §6.1 教训）——本清单所有命令均为 Bash/Python。

---

## 一、发布前置检查（部署机上）

```bash
cd /opt/grape

# 1. 确认当前分支与提交
git status                       # 应为干净工作区（configs/*.json 等 gitignore 文件不受影响）
git log --oneline -1             # 记录当前提交，用于回滚

# 2. 确认磁盘/内存足够
df -h /root | tail -1
free -h

# 3. 记录当前端口占用（部署后 3000/8080 应被新进程接管）
ss -tlnp | grep -E ':(3000|8080)'
```

---

## 二、备份（必须）

```bash
# 手动备份数据库（部署脚本会自动再备一次，双保险）
mkdir -p /root/backups
cp -a /opt/grape/data/sessions.db /root/backups/sessions-$(date +%F_%H%M%S).db

# 记录迁移前表结构（用于对比迁移结果）
sqlite3 /opt/grape/data/sessions.db \
  "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" \
  > /root/backups/tables-before-$(date +%F_%H%M%S).txt
```

---

## 三、拉取代码并发布（`deploy.sh` 已封装，见同目录）

```bash
cd /opt/grape
# 合入 main 后（或发布分支）：
git pull origin main

# 启动（systemd 自动执行 python3 -m src.api.run，双进程 3000+8080）
systemctl daemon-reload
systemctl restart grape

# 确认状态
systemctl is-active grape        # active
journalctl -u grape --since "1 minute ago" | tail -30
```

> 迁移在服务启动时自动执行（`init_database()`）：
> - `backend_agents` → `agent_runtimes`（数据搬迁，保留 id/version/healthy）
> - 引用列改名 `backend_id→runtime_id` / `backend_session_id→runtime_session_id` / `default_backend_id→default_runtime_id` / `events.backend→runtime` 等
> - 新建 `agent_tasks` / `runtime_credentials` 表
> - SQLite 开启 WAL + busy_timeout（跨进程写安全）

---

## 四、部署后校验（`verify.sh` 已封装）

### 4.1 服务健康
```bash
curl -s http://127.0.0.1:3000/health      # {"status":"ok","version":"2.0.0"}
curl -s http://127.0.0.1:8080/health      # {"status":"ok",...,"service":"webhook"}
```

### 4.2 迁移结果
```bash
sqlite3 /opt/grape/data/sessions.db "PRAGMA journal_mode;"          # wal
sqlite3 /opt/grape/data/sessions.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%runtime%' OR name='agent_tasks';"
# 预期：agent_runtimes / agent_tasks / runtime_credentials
sqlite3 /opt/grape/data/sessions.db \
  "SELECT COUNT(*), MIN(name), MAX(name) FROM agent_runtimes;"       # 数据保全（≥1，含 opencode）
sqlite3 /opt/grape/data/sessions.db \
  "PRAGMA table_info(devops_agent_session);"                          # 应含 runtime_id/runtime_session_id，无 backend_id
```

### 4.3 业务冒烟（direct 模式，行为应与旧版一致）
```bash
# 后端列表（新路径）
curl -s -H "Authorization: Bearer <user_token>" \
  http://127.0.0.1:3000/api/settings/agent-runtimes
# 兼容旧路径
curl -s -H "Authorization: Bearer <user_token>" \
  http://127.0.0.1:3000/api/settings/backend-agents
```

### 4.4 前端
- 访问 https://grape.example.com → 侧栏应为 **Agent Runtimes**（原 Backend Runtimes）
- 强制刷新（Ctrl+Shift+R）确保 `?v=` 新版本生效

---

## 五、灰度切换执行模式（核心，可回退）

> 默认 `auto`（本机引擎→direct，远端 worker→queue_remote）。`queue_local` 已废除（见 `engine-default-selection-design.md` §9）。**确认线上稳定后再切换。**

### 5.1 切 `queue_remote`（远端 RuntimeAgent）

1. 生成凭据：Admin UI（Settings → Agent Runtimes → ＋ 生成凭据）→ 得到 `rta_...`
2. worker 机（或本机）装同仓库 + `pip install -r requirements.txt`
3. 启动边端：
   ```bash
   RUNTIME_TOKEN=rta_... \
   RUNTIME_SERVER_URL=https://grape.example.com \
   RUNTIME_POLL_INTERVAL=3 \
   RUNTIME_HEARTBEAT_INTERVAL=15 \
   python3 -m runtime.agent   # 注意：模块在 src/runtime，需在项目根目录执行
   ```
4. 切模式（本机引擎保持 `auto`/`direct`，无需为远端任务改动全局开关）：
   ```bash
   # 方式 A：环境变量（重启生效）
   systemctl edit grape    # 追加 Environment=DEVOPS_EXECUTION_MODE=queue_remote
   systemctl restart grape

   # 方式 B：API（写 meta 表，仅生效于下次 get_execution_mode 调用）
   curl -s -X PUT -H "Authorization: Bearer <admin_token>" \
     -H "Content-Type: application/json" \
     -d '{"mode":"queue_remote"}' http://127.0.0.1:3000/api/settings/execution-mode
   ```
5. 验证：注入一条真实 webhook → `events` 有记录 + `agent_tasks` 出现 `queued` → 远端 worker claim 执行 → `completed` → `data/workspaces/<repo>/issues/<n>/event.json` 生成；Runtime 管理页设备在线（绿点）。

### 5.2 回退

```bash
# 切回 direct（或 auto）
systemctl edit grape    # Environment=DEVOPS_EXECUTION_MODE=direct
systemctl restart grape
# 滞留 queued 任务：可取消（API）或重入队
```

---

## 六、远端 RuntimeAgent 进程托管（可选，M3）

> worker 机建议用 systemd 托管边端，随开机自启：

```ini
# /etc/systemd/system/grape-runtime.service
[Unit]
Description=Grape RuntimeAgent (edge)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/grape
ExecStart=/usr/bin/env RUNTIME_TOKEN=rta_xxx RUNTIME_SERVER_URL=https://grape.example.com python3 -m runtime.agent
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now grape-runtime
journalctl -u grape-runtime -f
```

---

## 七、回滚（`rollback.sh` 已封装）

### 7.1 代码回滚
```bash
cd /opt/grape
git checkout <上版本提交>       # 或 git checkout main~N
systemctl restart grape
```

### 7.2 数据库回滚（迁移失败/数据异常）
```bash
# 停服 → 还原备份 → 起服（注意备份含迁移前结构，还原后旧代码可正常跑）
systemctl stop grape
cp -a /root/backups/sessions-<时间戳>.db /opt/grape/data/sessions.db
# 删除 WAL/shm 残留
rm -f /opt/grape/data/sessions.db-wal /opt/grape/data/sessions.db-shm
systemctl start grape
```

> ⚠️ 若已切 queue_remote 且远端 worker 正在跑，回滚代码前先停 worker，避免新旧协议混跑。

---

## 八、验收检查单（部署完成标记）

| # | 检查项 | 命令/方式 | 通过条件 |
|---|---|---|---|
| 1 | 服务在线 | `systemctl is-active grape` | active |
| 2 | 主 API 健康 | `curl :3000/health` | `{"status":"ok"}` |
| 3 | webhook 健康 | `curl :8080/health` | ok |
| 4 | WAL 生效 | `PRAGMA journal_mode` | wal |
| 5 | 表迁移 | `sqlite3 ... tables` | agent_runtimes/agent_tasks/runtime_credentials 存在，backend_agents 移除 |
| 6 | 数据保全 | `SELECT COUNT(*) FROM agent_runtimes` | ≥1 且含 opencode |
| 7 | 自动备份 | `ls data/backups/` | 有迁移前 `.bak` |
| 8 | direct 回归 | webhook 触发事件 | 行为与旧版一致 |
| 9 | queue_remote | 注入事件 | 入队→claim→completed→event.json |
| 10 | 前端 | 侧栏 Agent Runtimes | 显示正确、无乱码 |

---

## 九、常见问题（FAQ）

| 现象 | 排查 |
|---|---|
| 启动后日志 `Rebuild backend_agents failed: no such table` | 全新库/已迁移库的无害 warning（历史迁移块在 M0 改名后跳过），可忽略 |
| `database is locked` | 应已由 WAL+busy_timeout 兜底；若仍出现检查是否多进程异常（如重复起 uvicorn） |
| 前端乱码 | 检查是否用 PowerShell 改过 JS/HTML；从干净提交还原 |
| 任务一直 queued | 绑定 runtime 离线（health 页可见）；检查 `execution_mode` 与 worker 是否在跑 |
| `rta_` token 失效 | 被吊销或未激活；重新生成 |
