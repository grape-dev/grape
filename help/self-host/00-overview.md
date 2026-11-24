# 1. 安装部署总览

> 面向要在自己机器 / 自己云上运行 Grape 的运维与开发者：**装哪条、装之前要什么、怎么确认装好了**。
> 装好之后的配置（GitCode/GitHub 集成、IM、对象存储、数据库、安全）见 [平台配置总览](../developer/00-overview.md)。
> 只想直接用现成服务？见官网的托管版说明，不需要看本文。

Grape 是 MIT 许可的开源项目，托管（官方运行）与自托管（你运行）是两条独立路径，前置条件和责任人都不同：

| | 托管（Hosted） | 自托管（Self-hosted） |
|---|---|---|
| 谁运维 | Grape 官方 | 你自己 |
| 数据位置 | 平台数据库 | 你的机器 / 你的 MySQL |
| 开始方式 | 注册后连仓库 | 本目录的两条安装路径 |
| 前提 | 无 | Docker，或 Python 3.10+（Agent CLI 另装） |
| 边界 | 不能改代码、不能离线 | 可改代码、可离线、升级自理 |
## 接入你的 Agent：装 grape 插件

无论走哪条路径，**平台跑起来之后，让你的 Agent 连上 Grape 靠的是官方插件**：

| 插件 | 宿主 | 形态 |
|---|---|---|
| grape-kit（`@grape-dev/grape-kit`） | OpenCode / Codex / CodeArts / WorkBuddy / openclaw | MCP 协议 · 36 工具 |
| grape-dsh（`@grape-dev/grape-dsh`） | DeepSeek Harness (dsh) | 专用插件 · 33 工具 + 侧边栏面板 |

一条口令即可让 Agent 自动安装：**根据 https://www.grapedev.cn/skills.md 为我安装 Grape 平台的插件（按我的宿主选 grape-kit 或 grape-dsh）与全部官方技能。**

安装、宿主差异与自检详见使用指南的 [grape 插件](../usage/grape-kit.md) 一节。

## 两条安装路径怎么选

| 路径 | 适合 | 前置 | 产物 |
|---|---|---|---|
| [Docker 部署](./01-docker.md)（推荐） | 想尽快跑起来、不碰 Python 环境 | Docker Engine + Compose 的 `docker compose` 子命令，能访问 pip 源 | 仓库自带的四容器 + nginx 布局（`deploy/docker-compose.yml`），nginx 对外 `80` |
| [源码部署](./02-source.md) | 要改代码、要调试、要接自定义依赖 | Python 3.10+、git | 单机最小形态：主服务 `3000`；生产模式再加 webhook `8080`，反代需自备 |

> 两条路径的**拓扑不一样**，别混用命令：Docker 路径的进程是 `src.api.grape_server` 等四个，监听 `38080`-`38083`，由 nginx 容器按路径分流；源码路径的入口是 `src.api.main:app`，监听 `3000`。
> 拓扑细节以仓库内 `deploy/docker-compose.yml`、`src/api/run.py` 为准。

## 装之前需要知道的三件事

1. **数据库默认是 SQLite**：`.env.example` 里 `DEVOPS_DB_BACKEND=sqlite`，不需要 MySQL。
   要换 MySQL 时取消 `#DEVOPS_DB_BACKEND=mysql` 的注释并补连接串，见 [数据库后端与官方同步](../developer/05-database-and-sync.md)。
2. **`.env` 放在仓库根**：两条路径都从 `.env.example` 复制到仓库根目录。Docker 路径里 `JWT_SECRET` 是**硬性必填**，
   缺失时 compose 直接报错退出（`deploy/docker-compose.yml` 写的是 `${JWT_SECRET:?...}`），且 `.env` 要通过
   `--env-file ../.env` 传给 compose（原因见 [Docker 部署](./01-docker.md) 2.2 节）。
3. **Agent CLI 至少装一个**：`npm install -g opencode`，或 `pip install codearts-cli`。
   没有 Agent CLI，平台能启动，但流水线跑不起来。

## 装好之后

服务起来只是第一步，能登录还需要先配好登录渠道与凭据：

1. **配登录渠道**：`configs/oauth-providers.json` 已随仓库发布，是登录渠道的真源，**不需要复制模板文件**。
   - 渠道定义（OAuth 端点、scope、字段映射、品牌色）直接改这个文件；
   - `client_id` / `client_secret` **不要写进文件**：唯一落点是 DB meta，请在管理面「登录渠道」里填（文件里出现会被加载器剥离并告警）；
   - `redirect_uri` 一般留空，由 `server_base_url`（`configs/server-config.json` 或管理面 meta）派生为
     `https://<域名>/api/auth/callback/<provider>`；显式填写时必须与第三方平台登记的回调地址逐字节一致。
   - `configs/server-config.json` **不入库**（`.gitignore` 里），首次部署从模板复制：
     `cp configs/server-config.example.json configs/server-config.json`，再改其中的 `server_base_url`。
2. **配 Bot Token 与 webhook**：见 [GitCode 集成](../developer/01-gitcode-integration.md)、[GitHub 集成](../developer/02-github-integration.md)。
3. **登录并接仓库**：打开控制台登录 → 建第一个场景 → 接入仓库，用法见 [Grape 使用指导](../usage/overview.md)。

> `server_base_url` 必须是**浏览器可达**的地址；`localhost` 与 `127.0.0.1` 在第三方平台眼里是两个不同的回调地址，不要混用。
> 自托管给团队用时，`server_base_url` 一般要填公网域名，见 [升级、备份与回滚](./03-upgrade-backup.md) 的生产要求。

生产环境还需在反向代理上终止 TLS、限制管理端口暴露，并配置备份，见 [升级、备份与回滚](./03-upgrade-backup.md)。