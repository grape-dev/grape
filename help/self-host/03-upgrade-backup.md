# 4. 升级、备份与回滚

> 自托管意味着升级和备份都由你负责。本文给出最小可用的操作顺序，不依赖具体云厂商。

## 4.1 需要备份的东西

| 路径 | 内容 | 丢了会怎样 |
|---|---|---|
| `.env` | `JWT_SECRET`、数据库连接、OBS、SMTP 等 | 会话全部失效，第三方凭据要重配 |
| `data/` | `sessions.db`（SQLite，默认）、日志、备份 | 任务、会话、技能与仓库绑定关系全丢 |
| `configs/` | 登录渠道、场景同步、邮件、安全、引擎调度 | 需要按 [平台配置总览](../developer/00-overview.md) 重配 |
| `.grape/skills/` | 技能资产（compose 把它挂到容器的 `/app/.grape/skills`） | 自定义技能丢失 |
| `examples/` | 示例（可选） | 只是少一份参考，不影响运行 |
| Docker 卷 `grape-data` | 容器内的 `/app/data`（同上） | 同 `data/` |

`data/` 与 `grape-data` 是同一份数据的两种存放位置——Docker 路径看卷，源码路径看目录，备份其中一个即可。

## 4.2 备份

**源码部署**

```bash
tar czf grape-backup-$(date +%F).tar.gz .env data configs .grape/skills examples
```

**Docker 部署**（先停写，再用一次性容器打包卷数据，避免 SQLite 写一半）

```bash
cd deploy
docker compose --env-file ../.env stop

docker compose --env-file ../.env run --rm --no-deps -T grape-server \
  tar czf - -C /app data > ../grape-data-$(date +%F).tar.gz

cd ..
cp .env grape-env-$(date +%F)
tar czf grape-config-$(date +%F).tar.gz configs .grape/skills examples
```

`configs/`、`.grape/skills`、`examples` 是 bind mount 在宿主仓库目录里的，直接从宿主打包即可。

**升级前一定要先备份**，尤其是 `data/`：新版可能自动迁移表结构，降级不一定能还原。

## 4.3 升级

**Docker**

```bash
git pull
cd deploy
docker compose --env-file ../.env up -d --build
docker compose --env-file ../.env ps        # 等到 healthy
```

**源码**

```bash
git pull
source .venv/bin/activate
pip install -r requirements-local.txt    # 依赖有变动时
# 重启进程：systemd 或手动
```

单机 SQLite 模式：迁移在进程启动时执行（`init_database()`），重启即完成。

分域库 / MySQL 多库模式：**顺序不可颠倒——迁移 → 视图修复 → 复核 → 重启**。先跑
`bash scripts/deploy-after-migrate.sh`，它依次执行迁移、`scripts/domain_view_freshness.py --repair`、
复核；脚本非 0（库不可用 / 多份基表未定 owner / 仍有视图漂移）**不得重启进程**。安全按
`deploy/README.md` 的退出码表处理。

用 systemd 托管的话，仓库 `deploy/systemd/` 里除了四个应用单元
（`grape-server`、`grape-event-server`、`grape-worker-server`、`grape-insight-server`），
还有 xwrite 巡检的 drain/reconcile 单元与 4 个 timer，安装脚本见 `scripts/install-grape-systemd.sh`；
重启按你实际启用的单元名执行，例如 `systemctl restart <你的单元名>`（单元清单见 `deploy/systemd/`）。

升级后按 [Docker 部署的验证步骤](./01-docker.md)（2.4 节）或 [源码部署的验证步骤](./02-source.md)（3.5 节）
做同一套检查：容器/进程健康 + `curl -f /api/health` 返回 200 且 `status` 为 `ok`。

## 4.4 回滚

1. 停服务：`cd deploy && docker compose --env-file ../.env down`（源码则停进程）。
2. 代码回到上一个版本：`git checkout <tag 或 commit>`。
3. 还原数据：`data` 与 `configs/` 用 4.2 的备份覆盖回去。
   - Docker：`docker compose --env-file ../.env run --rm --no-deps -T grape-server tar xzf - -C /app < ../grape-data-<日期>.tar.gz`
   - 源码：把 `data/`、`configs/`、`.grape/skills/` 解回原位置。
4. 重新构建启动：`docker compose --env-file ../.env up -d --build`。

**顺序很重要**：先停服务再还原数据，否则 SQLite 会带着旧连接继续写，还原等于无效。

## 4.5 生产环境的最低要求

| 项 | 要求 |
|---|---|
| TLS | 用 nginx / Caddy 等反代终止 HTTPS；Docker 路径由 `grape-nginx` 对外，源码路径不要把 `3000` / `8080` 直接暴露到公网 |
| Cookie | HTTPS 下把 `.env` 的 `COOKIE_SECURE` 设为 `true` |
| 对外基址 | 配好 `server_base_url`（浏览器可访问的地址），否则 OAuth 回调落在 localhost |
| 备份 | 频率按业务可接受的数据丢失量（RPO）决定，仓库不提供默认值；`data/` 与 `.env` 建议异地存一份 |
| 端口 | 只对外开放 80/443；`3000` / `8080`（源码）与 `38080`-`38083`（compose 内部）不对外 |

可观测性资产仓库里已有现成配置：`deploy/grafana/`（仪表盘）、`deploy/alloy-config.alloy`（采集）、
`deploy/alerts.yml`（告警规则），需要时直接接入。

安全相关配置（`JWT_SECRET` 门禁、Cookie、登录白名单）见 [安全与引擎调度](../developer/06-security-and-engine.md)。