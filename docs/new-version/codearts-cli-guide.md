# CodeArts CLI 使用指南 & opencode 对比

## 一、CodeArts CLI 概述

华为云码道 CodeArts Agent CLI（`codearts`）是基于开源项目 [opencode](https://opencode.ai) 改造的华为云定制版本。核心差异在于：

- **认证方式**：华为云 AK/SK（`CODEARTS_CLI_AK` / `CODEARTS_CLI_SK`）
- **输入方式**：支持通过 stdin 管道传递 prompt（`prompt_via_stdin=True`）
- **无 `--dangerously-skip-permissions`**：不需要也无需传递该参数
- **版本**：当前使用 v26.6.2

## 二、安装与认证

### 2.1 安装

```bash
# Linux 安装脚本（华为云官方渠道）
curl -sSL https://codearts.your-region.com/cli/install | bash
```

### 2.2 认证

配置华为云 AK/SK 环境变量：

```bash
export CODEARTS_CLI_AK="your-access-key"
export CODEARTS_CLI_SK="your-secret-key"
```

AK/SK 可在华为云统一身份认证页面申请。

### 2.3 验证

```bash
codearts --version
# 输出: 26.6.2
```

## 三、命令总览

| 命令 | 描述 |
|------|------|
| `codearts help` | 查看命令帮助 |
| `codearts completion` | 生成 Shell 自动补全脚本 |
| `codearts [project]` | 启动 TUI 交互式开发环境 |
| `codearts run [message]` | 非交互模式运行，直接传入提示词 |
| `codearts agent` | 管理智能体（create / list） |
| `codearts models [provider]` | 列出可用模型 |
| `codearts stats` | 显示 Token 用量和成本统计 |
| `codearts session` | 管理会话（list / delete） |
| `codearts export [sessionID]` | 以 JSON 导出会话数据 |
| `codearts import <file>` | 从 JSON 文件导入会话 |
| `codearts mcp` | 管理 MCP 服务器 |
| `codearts serve` | 启动无界面 HTTP 服务器 |
| `codearts attach <url>` | 连接到远端 codearts 服务 |
| `codearts codebase` | 管理代码库索引 |
| `codearts debug` | 调试与故障排查工具 |
| `codearts upgrade` | 升级到最新版本 |
| `codearts uninstall` | 卸载并删除所有文件 |
| `codearts docs` | 获取帮助文档链接 |
| `codearts privacy` | 查看隐私政策 |

## 四、`codearts run` 详解（核心）

### 4.1 基本用法

```bash
# 直接传 prompt（作为 positional argument）
codearts run 解释这个函数的逻辑

# 通过 stdin 传 prompt（推荐用于自动化）
echo "解释这个函数的逻辑" | codearts run --format json
```

### 4.2 所有参数

| 参数 | 缩写 | 描述 |
|------|------|------|
| `--command` | | 运行命令，支持 SDD 命令（sdd-new 等） |
| `--continue` | `-c` | 继续上一个会话 |
| `--session` | `-s` | 指定要继续的 session ID |
| `--fork` | | 继续时分叉会话 |
| `--model` | `-m` | 指定模型 |
| `--agent` | | 指定智能体 |
| `--file` | `-f` | 附加文件到消息 |
| `--format` | | 输出格式：`default`（格式化）或 `json`（NDJSON） |
| `--title` | | 会话标题 |
| `--thinking` | | 显示思考块 |
| `--attach` | | 连接到运行中的 codearts 服务器 |
| `--print-logs` | | 将日志输出到 stderr |
| `--log-level` | | 日志级别：DEBUG / INFO / WARN / ERROR |
| `--pure` | | 不加载外部插件 |

### 4.3 会话延续

```bash
# 第一次运行（自动生成 session ID）
echo "hello" | codearts run --format json
# 输出中包含 "sessionID":"ses_xxxxx"

# 继续该会话
echo "继续说" | codearts run --format json --session ses_xxxxx
```

### 4.4 NDJSON 输出格式

`--format json` 输出的每一行是一个独立 JSON 事件：

```json
{"type":"step_start","sessionID":"ses_xxx","part":{...}}
{"type":"text","sessionID":"ses_xxx","part":{"type":"text","text":"回答内容"}}
{"type":"tool_use","sessionID":"ses_xxx","part":{"tool":"read","state":{...}}}
{"type":"reasoning","sessionID":"ses_xxx","part":{"text":"思考过程"}}
{"type":"step_finish","sessionID":"ses_xxx","part":{"tokens":{"total":100}}}
{"type":"error","sessionID":"ses_xxx","error":{"data":{"message":"错误信息"}}}
```

| 事件类型 | 描述 |
|----------|------|
| `step_start` | 步骤开始，包含 `sessionID` |
| `text` | 文本回复（`type=text`）或推理过程（`type=reasoning`） |
| `tool_use` | 工具调用，包含 `tool` 名和 `input` |
| `reasoning` | 推理块（已弃用，改用 text type=reasoning） |
| `step_finish` | 步骤结束，包含 token 统计 |
| `error` | 错误事件 |

### 4.5 使用 --command 运行 SDD

```bash
# 初始化项目
codearts run --command init

# 运行测试
codearts run --command test

# 使用 SDD 生成代码
codearts run --command sdd-new "实现用户登录功能"
codearts run --command sdd-design design-output-dir
```

## 五、会话管理

```bash
# 列出最近会话
codearts session list --format json

# 列出最近 10 个
codearts session list --max-count 10

# 删除会话
codearts session delete ses_xxxxx

# 导出会话数据
codearts export ses_xxxxx

# 从文件导入会话
codearts import session.json
```

## 六、TUI 模式

```bash
# 进入 TUI 交互模式
codearts /path/to/project

# 继续上次会话
codearts /path/to/project --continue

# 指定会话继续
codearts /path/to/project --session ses_xxxxx

# 分叉会话
codearts /path/to/project --fork --continue
```

## 七、模型管理

```bash
# 列出所有可用模型
codearts models

# 列出指定供应商的模型
codearts models huaweicloud-maas
```

## 八、服务端模式

```bash
# 启动无界面 HTTP 服务器
export CODEARTS_SERVER_USERNAME="admin"
export CODEARTS_SERVER_PASSWORD="mypassword"
codearts serve --port 4096

# 从另一个终端连接
codearts attach http://localhost:4096 --password mypassword

# 连接后运行
echo "hi" | codearts run --attach http://localhost:4096 --password mypassword
```

## 九、调试工具

```bash
# 查看完整配置
codearts debug config

# 列出所有技能
codearts debug skill

# 查看智能体详情
codearts debug agent <name>

# 查看路径信息
codearts debug paths

# 文件系统调试
codearts debug file read /path/to/file
codearts debug file list /path/to/dir
codearts debug file tree /path/to/dir

# 代码搜索调试
codearts debug rg search "pattern"
codearts debug rg files

# 列出所有项目
codearts debug scrap
```

## 十、Stats 统计

```bash
# 查看全部统计数据
codearts stats

# 近 7 天
codearts stats --days 7

# 按工具维度
codearts stats --tools

# 按模型维度
codearts stats --models

# 按项目
codearts stats --project
```

## 十一、全局标志

| 标志 | 描述 |
|------|------|
| `--version` | 打印版本号 |
| `--print-logs` | 将日志输出到 stderr |
| `--log-level` | 日志级别（DEBUG / INFO / WARN / ERROR） |
| `--pure` | 不加载外部插件 |

---

# opencode vs codearts CLI 差异对比

## 12.1 命令对比

| 命令 | opencode | codearts | 备注 |
|------|----------|----------|------|
| `run` | ✅ | ✅ | 核心非交互模式 |
| `[project]`（TUI） | ✅ | ✅ | 启动交互式开发环境 |
| `serve` | ✅ | ✅ | HTTP 服务端 |
| `attach` | ✅ | ✅ | 连接到远端服务 |
| `session` | ✅ | ✅ | 会话管理 |
| `models` | ✅ | ✅ | 模型列表 |
| `stats` | ✅ | ✅ | 用量统计 |
| `agent` | ✅ | ✅ | 智能体管理 |
| `mcp` | ✅ | ✅ | MCP 服务器管理 |
| `export` | ✅ | ✅ | 会话导出 |
| `import` | ✅ | ✅ | 会话导入 |
| `upgrade` | ✅ | ✅ | 升级 |
| `uninstall` | ✅ | ✅ | 卸载 |
| `debug` | ✅ | ✅ | 调试工具 |
| `completion` | ❌ | ✅ | Shell 补全 |
| `docs` | ❌ | ✅ | 文档链接 |
| `privacy` | ❌ | ✅ | 隐私政策 |
| `codebase` | ❌ | ✅ | 代码索引管理 |
| `auth` | ✅ | ❌ | 凭据管理（opencode 用 API Key） |
| `github` | ✅ | ❌ | GitHub Agent |
| `pr` | ✅ | ❌ | PR 操作 |
| `web` | ✅ | ❌ | Web 界面 |
| `plugin` / `plug` | ✅ | ❌ | 插件管理 |
| `acp` | ✅ | ❌ | ACP 协议服务器 |
| `db` | ✅ | ❌ | 数据库工具 |

## 12.2 `run` 子命令参数对比

| 参数 | opencode | codearts v26.6.2 | 备注 |
|------|----------|-------------------|------|
| `--command` | ✅ | ✅ | 运行命令 / SDD |
| `--continue` / `-c` | ✅ | ✅ | 继续上次会话 |
| `--session` / `-s` | ✅ | ✅ | **相同 flag 名！** 会话 ID 格式 `ses_xxx` 兼容 |
| `--fork` | ✅ | ✅ | 分叉会话 |
| `--model` / `-m` | ✅ | ✅ | 指定模型 |
| `--agent` | ✅ | ✅ | 指定智能体 |
| `--file` / `-f` | ✅ | ✅ | 附加文件 |
| `--format` | ✅ | ✅ | 输出格式（default / json） |
| `--title` | ✅ | ✅ | 会话标题 |
| `--thinking` | ✅ | ✅ | 显示思考块 |
| `--attach` | ✅ | ✅ | 连接到运行中的服务 |
| `--share` | ✅ | ❌ | 分享会话（opencode 特有） |
| `--password` / `-p` | ✅ | ❌ | 在 attach 子命令中支持 |
| `--username` / `-u` | ✅ | ❌ | 在 attach 子命令中支持 |
| `--dir` | ✅ | ❌ | 指定运行目录 |
| `--port` | ✅ | ❌ | 端口（codearts 在 serve 中支持） |
| `--variant` | ✅ | ❌ | 模型变体 |
| `--auto` | ✅ | ❌ | 自动批准权限 |
| `--dangerously-skip-permissions` | ✅ | ❌ | **opencode 独有**，codearts 不需要 |
| `--print-logs` | ✅ | ✅ | 日志输出到 stderr |
| `--log-level` | ✅ | ✅ | 日志级别 |
| `--pure` | ✅ | ✅ | 不加载外部插件 |

## 12.3 关键差异总结

| 差异点 | opencode | codearts |
|--------|----------|----------|
| **认证方式** | API Key / 第三方 provider 登录 | 华为云 AK/SK 环境变量 |
| **prompt 传递** | 仅命令行参数 `run <prompt>` | 支持命令行参数 + **stdin 管道** |
| **权限跳过** | `--dangerously-skip-permissions` | **无需此参数**（默认无权限限制） |
| **会话 flag** | `--session` | `--session`（**兼容**） |
| **会话 ID 格式** | `ses_xxx` | `ses_xxx`（**兼容**） |
| **NDJSON 输出** | 完全一致 | 完全一致 |
| **输出解析** | 通用 `parse_output()` | 复用 `parse_output()`（**兼容**） |
| **源码基础** | 开源项目 | opencode 的华为云定制版 |

## 12.4 后端集成注意事项

在 DevOps Bot 中使用时，两个后端通过统一的 `AgentBackend` 抽象层调用：

```python
# codearts.py — 差异点
class CodeArtsBackend(AgentBackend):
    binary = "codearts"
    prompt_via_stdin = True  # 通过 stdin 传 prompt

    def build_args(self, prompt, session_id=""):
        args = ["run", "--format", "json"]
        if session_id:
            args.extend(["--session", session_id])  # 与 opencode 相同的 flag
        return args  # 不追加 prompt（走 stdin）

    def get_env(self):
        env = os.environ.copy()
        env["CODEARTS_CLI_AK"] = CODEARTS_CLI_AK  # 华为云认证
        env["CODEARTS_CLI_SK"] = CODEARTS_CLI_SK
        return env
```

对比 opencode：

```python
class OpencodeBackend(AgentBackend):
    binary = "opencode"
    prompt_via_stdin = False  # 通过命令行参数传 prompt

    def build_args(self, prompt, session_id=""):
        args = ["run", "--format", "json", "--dangerously-skip-permissions"]
        if session_id:
            args.extend(["--session", session_id])
        args.append(prompt)  # 追加 prompt
        return args
```

---

*文档生成日期: 2026-07-16 · 基于 codearts v26.6.2 / opencode latest*
