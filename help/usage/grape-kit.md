# 14. grape 插件：把 grape 装进你自己的 Agent

grape 官方插件把 Grape 平台能力带进你的 Agent。两种形态按你使用的 Agent 二选一：

| 插件 | 适配对象 |
|---|---|
| **grape-kit**（`@grape-dev/grape-kit`） | 主流 Agent：OpenCode / CodeArts / WorkBuddy / openclaw |
| **grape-dsh**（`@grape-dev/grape-dsh`） | dsh（DeepSeek Harness）的专门适配 |

> ⚠️ **凭据安全**：两种插件的 Token 都只在环境变量（`GRAPE_API_TOKEN` / `GRAPE_BASE_URL`）或 `~/.grape/config.json`（权限 600）中提供，不写入任何 Agent 配置文件明文区。

## grape-kit

面向 OpenCode / CodeArts / WorkBuddy / openclaw 等主流 Agent。装上后你的 Agent 获得 36 个工具：触发/等待流水线（A2A 唯一入口）、issue 全生命周期、事件回环、知识库与看板，并支持**端云协同**——通过接管会话把云端 coding 节点原子让渡到你的 Agent 继续实施，完成后携带结构化产物交回平台，流水线链路保持完整。

**口令安装**——把这句粘贴给你的 Agent，它按 README 步骤完成安装与配置：

> 请阅读https://gitcode.com/grape-dev/grape-kit/blob/dev/README.md，按照步骤为我安装并配置grape-kit

**npm 安装**（Node ≥ 20）：

```bash
npm i @grape-dev/grape-kit
```

安装后按 README 配置 `GRAPE_BASE_URL` / `GRAPE_API_TOKEN`（API Key 在 [WebUI 账户页](https://www.grapedev.cn/app/#account/profile) 生成）并选择目标 Agent `--target`。

### 各 Agent 安装差异

| Agent | 安装方式 |
|---|---|
| OpenCode | `--target opencode`（写 mcp.grape 条目 + 技能拷贝，幂等带备份） |
| CodeArts Agent | `--target codearts`（写 `codearts_cli.jsonc`；凭据只走环境变量） |
| WorkBuddy | `--target workbuddy`（写 `~/.workbuddy/mcp.json`；**凭据严禁写入 mcp.json**，只走环境变量） |
| openclaw | `openclaw plugins install clawhub:grape-kit@latest`（ClawHub 市场） |

### 验证与排障

- `npx -y @grape-dev/grape-kit doctor --probe`：node 版本、服务端连通、36 工具逐项自检
- 安装后重启 Agent 会话生效；opencode 需重启当前会话
- 401 时先检查 Token 是否为**用户级 API Key**（`grape_` 前缀，WebUI 账户页生成）

详细工具面与版本历史见[源码仓库](https://gitcode.com/grape-dev/grape-kit)。

## grape-dsh

grape 插件对 dsh（DeepSeek Harness）的专门适配。随 dsh 获得 33 个 grape 工具——触发/等待流水线、issue 管理、端云协同接管——并可与 dsh 面板协作。

**口令安装**：

> 请阅读https://gitcode.com/grape-dev/grape-dsh/blob/main/README.md，按照步骤为我安装并配置grape-dsh

**npm 安装**：

```bash
npm i @grape-dev/grape-dsh
```

安装与配置步骤见[源码仓库](https://gitcode.com/grape-dev/grape-dsh)。
