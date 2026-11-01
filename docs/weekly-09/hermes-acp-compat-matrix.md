# hermes ACP 健康探测与 launcher/ACP 兼容矩阵（Issue #613）

> 来源：用户反馈单 #196（hermes 引擎注册后 healthy=False，探测恒失败）。
> 修复：`src/runtime/acp_bootstrap.py` 三断点（探测方式 / venv 解析 / 补装版本）。

## 1. 健康探测方式（跨版本稳定）

worker 注册 hermes 引擎时的 ACP 健康门由 `hermes acp --check` 退出码
（hermes v0.9.0 起不存在该子命令，恒 EXIT 2 → 误报 unhealthy）改为
**真实 ACP 握手**：

- 对 `hermes acp` 子进程 stdin 发送 `initialize` + `session/new` 两条
  JSON-RPC 请求，stdout 收到任意 JSON-RPC 响应即视为 ACP 依赖可用
  （`_acp_probe`，超时 `ACP_CHECK_TIMEOUT=15s` 兜底）；
- 失败原因分类：
  - **未知子命令**（stderr 含 unrecognized arguments / unknown command 等）
    → hermes 版本过旧，**不是依赖缺失**，不触发自动补装；
  - **依赖缺失**（stderr 含 ModuleNotFoundError / agent-client-protocol 等）
    → 走自动补装链路；
  - 其他（超时/崩溃/协议异常）→ healthy=False，按日志指引人工排查。

## 2. hermes launcher 形态兼容矩阵（venv 解析）

| launcher 形态 | 解析方式 | 支持 |
|---|---|---|
| bash shim（`exec ".../venv/bin/hermes"`） | exec 行正则提取（原有逻辑，保留兜底） | ✅ |
| 软链 → venv/bin/hermes console-script | `os.path.realpath` 跟随软链 + 目标文件 shebang（`#!/…/venv/bin/python`）反推 venv/bin | ✅（本期新增） |
| console-script 直连（文件即 venv/bin/hermes） | shebang 解释器路径 dirname 反推 | ✅（本期新增） |
| `#!/usr/bin/env python` 形态 shebang | 无法定位 venv → 空串，healthy=False 走人工指引 | ❌ |
| Windows .exe / .cmd / .bat | 不支持，healthy=False（维持现状） | ❌ |

## 3. ACP 依赖版本策略（不再硬钉）

- 旧实现硬钉 `agent-client-protocol==0.9.0`：ACP 0.12.x 与 hermes v0.9.0
  适配器（`acp.schema` API）不兼容，被误报「依赖未装」并盲目降级安装。
- 新策略：
  1. 握手成功 → 直接 healthy，不做任何安装；
  2. 握手失败且依赖缺失 → 查询 venv 已装 agent-client-protocol 版本
     （`importlib.metadata`）：已装但握手失败 → 判定为 hermes 适配器与该
     版本**不兼容**，不盲目补装，日志输出人工对齐版本指引；未装 →
     `pip install agent-client-protocol`（无版本约束，按 hermes 侧
     pyproject 的 acp extra 解析）后复查握手。

## 4. hermes 最低版本要求

| hermes 版本 | `hermes acp` 子命令 | worker 健康门 | 说明 |
|---|---|---|---|
| < v0.9.0（无 acp 子命令） | ❌ | healthy=False | 日志提示升级 hermes，不误报依赖缺失 |
| v0.9.0 | ✅（无 `--check`） | 真实 ACP 握手 | 本期验收基准 |
| > v0.9.0 | ✅ | 真实 ACP 握手 | 同上 |

- 验收环境：hermes v0.9.0 + 软链/console-script launcher + ACP 0.12.x
  → 注册后 healthy=True；
- hermes 侧适配器与 ACP 版本的兼容性以 hermes 仓库 pyproject 的
  `acp` extra 约束为准（worker 不再硬钉具体版本）。

## 5. 人工修复指引（探测失败时日志会给出对应命令）

```bash
# 依赖未装（venv 解析失败时手工指定 hermes 所属环境）
/opt/path/to/hermes/venv/bin/pip install agent-client-protocol

# 适配器不兼容（ACP 已装但握手失败）：对齐版本
#   方式一：升级 hermes 至与其 acp extra 匹配的版本
#   方式二：按 hermes pyproject 的 acp extra 约束安装对应 ACP 版本
```

相关模块：`src/runtime/acp_bootstrap.py`（探测/解析/补装）、
`src/runtime/local_probe.py`（本机直连探测口径对齐）、
`src/runtime/agent.py`（worker 注册健康门）。
