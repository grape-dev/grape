# AtomCode 安装与使用

AtomCode 是**基于 Rust** 的 Agent CLI。npm 是它的分发安装渠道。

## 安装

在实际执行任务的机器上安装（本机直连装在 grape 服务所在机器；远程执行装在 Worker 上）：

```bash
npm install -g @atomgit.com/atomcode  # 基于 Rust，通过 npm 安装
atomcode --version
```

使用运行 grape/Worker 的同一系统用户启动 `atomcode`，完成模型与认证配置。
配置和凭据由执行机器本地管理。安装后可先验证一次独立对话：

```bash
atomcode -p "Reply OK" --ephemeral --no-tools
```

该命令会调用已配置的模型；ephemeral 模式不写入会话，no-tools 模式不执行工具。
二进制版本检测通过只代表 CLI 可启动，不代表模型认证可用。

Worker 安装脚本支持 `--engine atomcode`，例如在原有安装命令后追加该参数。
安装失败不会阻止 Worker 注册，会在日志中给出手动安装命令。
默认 `--engine auto` 检测到 AtomCode 时会跳过默认引擎安装。

## 在 grape 中使用

1. 确保 grape/Worker 进程的 PATH 中能找到 `atomcode`。交互终端可找到 CLI
   不代表 systemd 等后台服务使用相同 PATH；必要时更新服务环境并重启。
2. 在引擎管理页确认 `atomcode` 已被发现、注册且健康。
3. 在场景节点或会话的引擎选择中绑定该 AtomCode 实例。引擎角色**不按引擎类型
   默认映射**（Issue #658 修订：能力与引擎类型无关，默认不限角色）；需要限定
   角色时在引擎配置中显式声明 role_tags（如 `["coding","issue","qa"]`）。
4. 派发任务，检查回复文本、工具事件、用量及任务完成状态。

grape 的执行适配使用 `--prompt-file <临时文件> --output-format jsonl
--dangerously-skip-permissions`。这是无人值守执行：工具审批被自动放行。
任务在既有 grape 工作目录和可选 sandbox 中运行，提示词文件在调用结束后清理。
多行提示词不会进入命令行参数或参数日志。

用户级技能目录为 `~/.atomcode/skills`；设置 `ATOMCODE_HOME` 时使用
`$ATOMCODE_HOME/skills`。任务下发的技能沿用 grape 既有物化和提示词引用机制。

## 会话续接与版本兼容

本适配要求 CLI 支持 `--prompt-file` 和 `--output-format jsonl`（schema version 1）。
安装后可用 `atomcode --help` 核对；不支持时请更新 CLI。

完整的多轮续接还要求 `run.started` 输出 `session_id`。本次在 AtomCode 源码中
补充了该可选字段，来自原生 CodingRuntime；grape 保存该 ID 并在后续调用传入
`--resume <id>`。此能力要在包含该改动的 AtomCode 构建发布/安装后才可用，
不能仅以 npm 安装成功判断。

旧版 JSONL 没有 session_id 时仍可执行独立任务，但 grape 无法自动获得新会话的
续接 ID。不要使用 `--continue` 代替：同一机器并发执行时“最近会话”不属于确定的任务。
显式恢复失败会报错，不会偷偷新建会话重跑。
成功退出但没有回复文本时同样明确失败并保留会话 ID，不会自动切换到新会话。

CLI 非零退出、失败终态、会话快照写入错误或事件流缺少终态，都会判为失败。
用量按每次 usage 事件累计，turn.completed 的汇总不会重复计费。
AtomCode 的 prompt_tokens 包含缓存输入；适配层会拆为非缓存输入与 cache_read，
避免看板重复统计。取消与超时终止使用 grape 已登记的进程 PID，并精确匹配工作目录；
Windows 使用 taskkill /T /F，无需依赖 CLI 文件名或 WMIC 查询。
