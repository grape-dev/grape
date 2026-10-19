# Spec: hwcloud 引擎接入（ACP 协议后端 src/agent/hwcloud_acp.py）

> 状态：**已实施并上线**（阶段 0-2 完成，真机 e2e 全链验证，2026-10-12）
> 关联：docs/agent-workflow-and-prompt-design.md（引擎架构总览）、src/agent/hermes_acp.py（参照实现）

## Objective

将华为云 hwcloud CLI（实现仓 https://github.com/yusheng-g/openagent-go，Go 实现的 Agent Runtime Kernel）
作为一种新的 agent 执行引擎接入 grape，后端文件命名为 `src/agent/hwcloud_acp.py`。

接入方式复用 hermes 已验证的 ACP JSON-RPC over stdio 模式（hermes_acp.py），
不走 CLI 文本解析路径（build_args/parse_output）。

## Background

### hwcloud CLI 是什么

- openagent-go 的二进制名由构建时 `OPENAGENT_BINARY_NAME=hwcloud ./build.sh` 注入（version.Name），
  产物即 `hwcloud` 命令；华为云 AI 开发者空间内置的 HWCloud CLI 即此物。
- 核心能力：事件驱动的 agent kernel（8 节点循环）、内置工具（shell/file/grep/web/acp_*）、
  skills、MCP 客户端/服务端、wasm 插件、会话持久化（session/sqlite）。
- 对 grape 有价值的两个服务面：
  - **ACP over stdio**（本设计采用）：`hwcloud serve --acp`，每行一个 JSON-RPC（NDJSON）
  - REST + SSE：`hwcloud serve`（POST /sessions、POST /sessions/{id}/chat SSE）——备选，不采用（见 §选型）

### ACP 协议对比（hwcloud vs hermes）

协议主线完全同构（initialize → session/new → session/prompt → session/update 通知 →
PromptResponse(stopReason) → session/request_permission），grape 侧 hermes_acp.py 的
`_ACPSimpleClient` 事件解析逻辑可直接复用。hwcloud 的增强点：

| 能力 | hermes acp | hwcloud serve --acp |
|------|-----------|---------------------|
| session/resume（不带历史回放续接） | 有 | 有（ResumeSessionRequest，含 cwd + sessionId） |
| session/load（带历史回放） | 无 | 有（LoadSessionRequest，回放 user_message_chunk 通知） |
| ListSessions（按 cwd 过滤） | 无 | 有（服务端 session/sqlite 持久化） |
| ListMessages（不加载会话分页读消息） | 无 | 有（Limit/Before 分页，最多 200 条） |
| 会话配置项 | 无 | session/new 响应带 configOptions：mode（auto/semi-auto/manual/plan）、thought_level、model |
| 审批语义 | request_permission + allow_once | 同左，且 mode=auto 时全免审（含破坏性操作） |
| 计划更新 | 无 | plan_update（SessionUpdate） |

事件类型映射（session/update 的 update.sessionUpdate 判别字段）与 hermes 一致：
agent_message_chunk / agent_thought_chunk / tool_call / tool_call_update（含 pending/completed/failed 状态
与 rawOutput）/ user_message_chunk（仅 load 回放时出现，忽略）。
`plan_update` 为新增类型，映射为 text 事件透传（`[plan] ...`），暂不单独建事件类型。

## 选型：为什么是 ACP-stdio 而不是 REST/SSE 或 CLI 文本

1. **ACP-stdio（采用）**：grape 已有 hermes_acp.py 全套基础设施（reader 线程、事件队列、
   分级超时、chat 稳定检测、沙箱 bwrap 包装），复刻成本最低；结构化事件无正则解析歧义；
   session/resume 原生支持多轮任务上下文续接。
2. **REST/SSE（不采用）**：需要常驻服务进程 + 端口管理（codearts 方案 E2 的 serve/pre_exec 复杂度），
   而 grape 的 per-task 子进程模型（起→跑→收）与 stdio 天然匹配。
3. **CLI 文本解析（不采用）**：hwcloud 无 `run --format json` 之类的稳定 NDJSON 输出模式可依赖
   （TUI 交互为主），文本解析不可靠。

## Scope

### In Scope

- `src/agent/hwcloud_acp.py`（新建）：HwcloudACPBackend，stream_execute 协议实现
- `src/agent/executor.py`：注册 "hwcloud" 后端 + `_stream_hwcloud_acp` 转发 + stream_agent 专用分支
- `src/agent/executor.py` discover_backends 的 known 表加 hwcloud 条目
- `src/runtime/` 侧同名薄转发（对齐 hermes.py 的兼容转发层模式）
- 配置/环境注入方案（settings.json、模型 provider、skills、权限模式）
- 构建与分发方案（Go 版本要求）

### Out of Scope

- REST/SSE 接入模式
- plan_update 的结构化消费（前端展示通道复用 text 即可）
- hwcloud 的 wasm 插件、IM channel（feishu/wechat）、iac-server MCP 能力
- engine-selection.json 调度策略变更（hwcloud 注册后自动进入引擎池，由现有健康检查/选择逻辑管理）

## Technical Approach

### 1. 后端实现 src/agent/hwcloud_acp.py

结构复刻 hermes_acp.py（同文件内的以下组件直接搬改，不抽公共基类——避免动 hermes 稳定路径）：

- `HwcloudACPBackend`：`name = "hwcloud"`、`binary = "hwcloud"`、`prompt_via_stdin = False`
- `stream_execute(prompt, cwd, session_id, env, max_exec_seconds, agent_label)` 生成器，
  yield 事件元组与 hermes 完全兼容：
  `("step_start"|"text"|"reasoning"|"tool_use"|"tool_finish"|"tool_result"|"step_finish"|"done", data)`
- `_ACPSimpleClient`：从 hermes_acp.py 复制，但**修复了 hermes 的别名快照潜伏 bug**——
  hermes 的 `self._on_prompt_done = self.on_prompt_done` 在 __init__ 时快照成 no-op，
  构造后再绑定的回调永远不触发（生产靠 60s text 静默超时兜底硬撑）；hwcloud 侧改为
  调用点实时读公开属性 `self.on_prompt_done`（单测 test_late_bound_callback_is_honored
  回归守护）。request_permission options 为 allow_once/allow_always/reject_once
  （server.go:3200 实测固定三选），自动应答逻辑不变（优先 allow_once）。
- reader 线程（Linux select 非阻塞读 / Windows readline）、事件队列、chunk 缓冲 flush
  （_BUF_MAX=200）、chat 稳定检测（_CHAT_STABLE/_CHAT_IDLE）、分级超时
  （tool 后 900s 可配 / text 后 60s / 全局 RUNTIME_TASK_TIMEOUT 对齐 #282）。

与 hermes 的差异点（实现时注意）：

| 差异 | 处理 |
|------|------|
| 启动命令 | `hwcloud serve --acp`（hermes 是 `hwcloud acp` 子命令，hwcloud 是 serve 的 flag） |
| 免审方式 | hermes 用 env HERMES_YOLO_MODE=1；hwcloud 在 settings.json 配 `"default_mode": "auto"`（免审含破坏性操作，等效 yolo）。request_permission 自动应答保留作为兜底（mode 非 auto 或会话级覆盖时触发） |
| 会话存储 | hermes 有 state.db 大小检查（check_state_db_size 阈值 50MB 跳过 resume）；hwcloud 会话在服务端 session/sqlite，暂不做大小检查，仅在 resume 失败时降级 session/new（与 hermes 同兜底）。上线后若上下文膨胀再补对齐 |
| session/load | 不使用。resume 失败→new 的降级链与 hermes 一致 |
| available_commands_update / user_message_chunk 通知 | _handle_notification 中忽略（hermes_acp 已按未知类型跳过，无需改） |
| plan_update | 映射 text：`[plan] {entries 标题串}` |

### 1b. 工具静默超时（实施中修正，949f84527）

原设计照搬 hermes 的 tool 后 300s 静默超时。**真机实测暴露不足**：hwcloud 的
shell 是长阻塞单工具（apt/pip 装 playwright+chromium 数百 MB 全程无 ACP 事件），
12:00:33 发起安装 → 12:05:33 恰 300s 整触发 stopReason=timeout，任务 failed。

实现修正：
- `_SILENT_TIMEOUT` 默认 **900s**，`RUNTIME_TOOL_SILENT_TIMEOUT` 环境变量可调
  （优先 task env dict，回退 os.environ——与 HWCLOUD_PROVIDER_* 同注入模式）
- 全局长度不受影响：`_GLOBAL_TIMEOUT`（RUNTIME_TASK_TIMEOUT 默认 3600s）仍兜底，
  静默超时职责仅是防 agent 挂死，不放大总时长
- `_TEXT_SILENT_TIMEOUT=60` 语义不变（text 后无响应视为完成，响应丢失兜底，非超时杀）

### 2. 执行器接入 src/agent/executor.py

- `_BACKENDS` 增 `"hwcloud": HwcloudCompatBackend()`（从 src/runtime/hwcloud.py 转发，对齐
  `src/agent/hermes.py` → `src/runtime/hermes.py` 的兼容转发层模式：runtime 侧持有实现，agent 侧转发）
- 新增 `_stream_hwcloud_acp(...)`（复刻 `_stream_hermes_acp` 的 env 组装：get_env →
  DEVOPS_EVENT_TYPE → _assemble_env token 互斥注入 → env_extra 合并 → AGENT_NAME）
- `stream_agent()` 中加专用分支：

```python
if backend_name == "hwcloud":
    yield from _stream_hwcloud_acp(...)
    return
```

### 3. 引擎发现

`discover_backends()` 的 known 表追加：

```python
"hwcloud": {"cli": "hwcloud", "pip": ""},
```

pip 字段留空（非 pip 分发，走二进制；engine_select 的 only_installed 过滤依据
cli_path 是否存在，不受影响）。

### 3b. worker 端三件套（阶段 2 实施补齐，原设计遗漏）

引擎执行不只 executor 侧——`src/runtime/agent.py`（worker 进程代码）需要三处适配，
阶段 1 设计时遗漏，全部在真机验证中发现并补齐：

1. **`_probe_hwcloud_acp`（f333ed5f4）**：周期探活（600s 一轮），与 hermes 探活同构
   但三段式：initialize → session/new（mcpServers=[]）→ session/prompt "hi"，端到端
   覆盖 provider/模型推理。挂在 `_probe_engine_worker` 的 `engine_name == "hwcloud"`
   分支（hwcloud 无 `run` 式一次性 CLI 命令，不走 `_PROBE_COMMANDS`）。
   **踩坑**：读 stdout 用 select+readline（同 hermes）；不能设 O_NONBLOCK——
   `text=True` 下与 Python 文本缓冲层冲突，select 永不 ready（真机实测）。
2. **`_backend_for_engine` 补 hwcloud 分支（2ce0d9ddf）**：引擎实例名
   `hwcloud-worker-*` → `"hwcloud"`。**内网 worker A2A 事故**：此映射漏了会落到默认
   `return "opencode"`——DB 记录 hwcloud 引擎领取任务、日志 label 标 hwcloud，
   实际执行进程却是 `/usr/local/bin/opencode run`（工作目录 /tmp/opencode 暴露）。
3. **kill patterns（f333ed5f4）**：超时终止匹配表加 `"hwcloud": ["hwcloud serve --acp"]`。

另有一处 worker 通用逻辑修正（a9cf2c62b，非 hwcloud 专属但 hwcloud 任务暴露）：
**`_worker_server_url_reachable` server_url 可达性实测**——原逻辑只认 localhost/127.*
才覆盖任务 server_url，内网 worker（内网地址直连主服务）不命中 → prompt 里
SERVER_URL 用 meta.server_base_url 公网地址（1.92.108.106）→ A2A 交办反复 5s 超时。
修正：worker 对自己的 `RUNTIME_SERVER_URL` 发真实探测（curl /api/health，
connect-timeout 1s），通 → 覆盖为该地址，不通保持原值（宁可走已知公网不猜内网）；
结果按 (rts, task_server_url) 缓存 60s 含失败，避免不可达 worker 每任务空等。
判定条件明确为**实测连通性**，不按网段推断（用户要求，VPC 隔离场景网段判断不可靠）。

### 4. 配置与环境注入（get_env）

hwcloud 的持久化根目录由构建时 version.Name 决定（默认 `~/.hwcloud/`，含 settings.json、
profile/、plugins/、会话数据）。`OPENAGENT_CLI_CONFIG` 指向自定义 settings.json 路径时，
**整个工作集**（profile、plugins）随之迁到该文件所在目录——这是 per-task 隔离的天然钩子。

get_env 策略（对齐 codearts 方案 G 的隔离思路，但更简单）：

```
env = os.environ.copy()
env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR          # 现有约定
env["DEVOPS_EVENT_TYPE"] = ""
# per-task 数据目录（env_extra 可覆盖）：~/.cache/hwcloud-acp-homes/<cwd-hash>/
#   目录内预写 settings.json：
#     provider.<name>.base_url / api_key / models   ← 模型接入（opengw/自定义 provider）
#     default_mode: "auto"                          ← 免审（等效 yolo）
#   OPENAGENT_CLI_CONFIG 指向该 settings.json → profile/plugins/会话全部隔离在该目录
env.setdefault("OPENAGENT_CLI_CONFIG", <iso_home>/settings.json)
```

- cwd-hash 稳定路径（非随机临时目录）：会话 sqlite 持久化，同仓库下次任务 session/resume
  可续接——与 hermes_acp 的 HERMES_HOME overlay 同一决策（chat-ctx-fix §方向一）。
- 凭据注入：AK/SK 类云凭据与 GitCode token 继续走现有 `_assemble_env` / 用户云凭据
  （user_id 按键注入）体系，进 env_extra；模型 provider 的 api_key 写入 per-task
  settings.json（task 结束随目录保留，含密文注意：该目录权限 0700）。
- AGENTS.md / SOUL.md：可选在 per-task profile/ 放 AGENTS.md 注入 grape 的任务级指令
  （prompt 体系已含任务描述，一期不启用，留作扩展点）。

### 5. 沙箱

`build_bwrap_cmd(cwd, backend_name="hwcloud")` 套 bwrap 白名单（hermes_acp 同款 try/except
降级路径）。hwcloud 自身也有 sandbox 能力（--sandbox flag，bwrap/seatbelt），一期不启用
——避免双层沙箱的路径/网络叠加问题，隔离统一由 grape 侧负责。

### 6. 构建与分发（硬前置）

- openagent-go 要求 **Go 1.26.4+**；本机 go1.17.3（arm64）不满足。需要：
  - 方案 a：升级本机/构建机 Go 到 1.26+（推荐，后续跟进上游还要重建）
  - 方案 b：在云端 master 机（1.92.126.251，EulerOS aarch64）构建
- 构建命令：`OPENAGENT_BINARY_NAME=hwcloud ./build.sh`
- 分发：产物放 grape worker 可达的内网源（对齐 codearts CLI 的下发方式），
  或先手工部署到 worker 机器 PATH 下（一期验证用）
- **验证顺序**：先手工构建 + `hwcloud serve --acp` 用原始 JSON-RPC 行跑通
  initialize/session/new/session/prompt 一轮（echo 管道即可），确认协议行为与
  acp/sdk 源码一致后，再写 hwcloud_acp.py——不基于源码推断直接写实现。

## 文件清单

| 文件 | 动作 | 内容 |
|------|------|------|
| `src/agent/hwcloud_acp.py` | 新建 ✅ | HwcloudACPBackend + _ACPSimpleClient（复制自 hermes_acp 改差异点 + 修别名快照 bug） |
| `src/runtime/hwcloud.py` | 新建 ✅ | HwcloudRuntime 兼容转发层（name/binary/get_env 转发，build_args 保留契约占位） |
| `src/agent/hwcloud.py` | 新建 ✅ | HwcloudBackend 转发（对齐 src/agent/hermes.py 模式，阶段 1 文档遗漏此文件） |
| `src/agent/executor.py` | 修改 ✅ | _BACKENDS 注册 + _stream_hwcloud_acp + stream_agent 分支 + discover_backends known 表 |
| `src/runtime/agent.py` | 修改 ✅ | _probe_hwcloud_acp + _backend_for_engine 分支 + kill patterns + _worker_server_url_reachable |
| `src/config.py` | 修改 ✅ | BWRAP_RO_ENGINE/BWRAP_RW_ENGINE 加 hwcloud 白名单条目（阶段 1 文档遗漏） |
| `src/agent/sandbox/`（stub 目录） | 删除 ✅ | 遮蔽 sandbox.py 真实现（build_bwrap_cmd）的负资产，零引用，删除后三处 import 恢复可达 |
| `docs/hwcloud-runtime-acp-design.md` | 本文档 | — |

注：原计划同步修改 `docs/agent-workflow-and-prompt-design.md`（引擎表加 hwcloud 行），
实际未做（该文档 0 处 hwcloud 引用）——引擎总览文档的更新留待统一整理，避免本 MR 扩面。

测试（新增，归入现有功能域不新建域）：

| 文件 | 内容 | 状态 |
|------|------|------|
| `tests/test_hwcloud_acp.py` | 后端单测 37 项：事件解析（fixture JSON-RPC 行回放，不依赖真二进制）、噪音过滤、迟到绑定回调回归守护、pending 注册、per-task settings 权限 0600 | ✅ 全过 |
| e2e | 真二进制端到端 | ✅ 真机全链：vod→issue→coding→sre 全链 hwcloud（含 A2A 交办、PR 合并后部署阶段） |

## Acceptance Criteria

- [x] `hwcloud serve --acp` 手工 JSON-RPC 验证记录（initialize→new→prompt 全流程实测，非源码推断）
- [x] stream_agent("hwcloud") 产出事件流与 hermes 后端事件类型完全兼容（report_messages/fanout 无需改动）
- [x] session/resume：同 cwd 第二次执行传入 oc_session_id 能续接上下文（实测多轮对话内容延续：进程 A 存暗号，进程 B resume 答出"葡萄"）
- [x] resume 失败自动降级 session/new，任务不失败（实测调整：resume 不报错不可靠，一期直接 session/new，见实测行为 §5）
- [x] request_permission 自动应答 allow_once（mode=auto 全程免审实测 + 单测覆盖）
- [x] per-task 数据目录隔离：不同 repo 的任务 settings.json/会话数据互不可见（cwd-hash 分片）
- [x] 凭据不入 LLM 上下文（api_key 仅存在于 settings.json（0600）与 env，事件流中无明文）
- [x] discover_backends 报告 hwcloud installed 状态正确（真机 installed=True）
- [x] 现有 4 引擎（opencode/codearts/hermes/codex）回归不受影响（预存失败与本次无关，clean tree 同基线）
- [x] e2e：hwcloud 引擎跑通 issue 任务全流程（vod → issue → coding → sre，A2A 链路 + PR 合并 + 部署阶段）

## 已确认决策（2026-10-12）

1. **模型 provider**：接 opengw（对齐 codearts）——api_key 来源为用户云凭据
   （provider=opengw），写入 per-task settings.json 的 provider 段。
2. **上游 release**：无公开 release 产物，需自构建 `OPENAGENT_BINARY_NAME=hwcloud ./build.sh`。
3. **Go 升级**：由 worker 机器自行完成（worker 侧自装 Go 1.26+ 并构建/获取 hwcloud 二进制，
   grape 不在服务端集中分发构建产物）。
4. **会话清理**：hwcloud 会话目录（~/.cache/hwcloud-acp-homes/<cwd-hash>/）**不纳入**
   现有 6h 清理任务——保留会话 sqlite 供 session/resume 长期续接；磁盘增长靠目录按
   cwd-hash 天然分片，膨胀风险接受。

## Rollout 计划

1. 阶段 0（前置）✅ **已完成（2026-10-12）**：见下方"实测行为"
2. 阶段 1：hwcloud_acp.py + executor 接入 + 单测（fixture 回放）✅ **已完成**（e98b046c5 + b66ee444c，37 项单测全过）
3. 阶段 2：worker 真机注册（engine_select 健康检查）+ e2e ✅ **已完成**（f333ed5f4/2ce0d9ddf/a9cf2c62b/949f84527；真机注册 probe ok，全链 e2e）
4. 阶段 3（可选）：AGENTS.md profile 注入、plan_update 结构化

## 实测行为（阶段 0 回填，2026-10-12）

环境：本机 aarch64 Linux，Go 1.27.2（~/tools/go，注意 GOPROXY 需切 goproxy.cn，
默认 proxy.golang.org 超时），源码 @8b49d913，
`OPENAGENT_BINARY_NAME=hwcloud ./build.sh` 产物 79MB 已装 ~/.local/bin/hwcloud。

实测确认的协议行为（手工 JSON-RPC，探针脚本逐条验证）：

1. **initialize** ✅ 返回 protocolVersion=1 + agentCapabilities：
   `loadSession:true, sessionCapabilities:{close,delete,list,resume}, mcpCapabilities:{http,sse},
   promptCapabilities:{image,embeddedContext}`。
2. **session/new** ✅ 关键差异（与 hermes_acp 现实现不同）：
   - **`mcpServers` 必须传 `[]`（空数组），传 `{}` 报 -32602 unmarshal 错误**
     （McpServers 类型区分 null 与空数组，源码 acp/sdk/types.go McpServers 注释明确）。
     hermes_acp.py 目前发的是 `{}`，复制时必须改为 `[]`。
   - 响应 sessionId 形如 `acp_<ns>_1`；带 modes.currentModeId（实测为 auto，即
     settings.json default_mode 生效——免审确认）。
3. **session/prompt** ✅ PromptResponse 带 `stopReason:"end_turn"` + `_meta:{mode,turn_count}`；
   流式事件按序：available_commands_update / available_skills_update / mcp_servers_update /
   session_info_update（新会话首轮，噪音事件，解析时忽略）→ agent_thought_chunk →
   agent_message_chunk → **usage_update**（token/cost，hermes 没有，可忽略或映射 text）。
4. **session/resume 跨进程续接** ✅ 进程 A 存暗号 → 进程 B resume 同 sessionId →
   问答延续（"暗号是什么"→"葡萄"）。会话持久化由服务端 sqlite 完成。
5. **resume 不校验存在性** ⚠️ 关键差异：对不存在的 sessionId，session/resume **不报错**，
   直接重建空会话返回 200（OnResumeSession 找不到 session 时按持久化状态重建，找不到
   就静默新建）。后果：**grape 侧 oc_session_id 失效时 resume 会"成功"但上下文为空**，
   任务不报错但丢失前序结论。降级链调整：不能靠 resume 报错触发降级，需在 resume 后
   主动验证（如 session/list 比对，或首轮 prompt 后检查响应是否引用了前文）；
   一期简化：**每次 task 直接 session/new，多轮上下文靠 prompt_builder 注入前序结论**
   （对齐 codearts 现状），resume 能力保留给 chat 场景二期启用。
6. **prompt 到不存在 session** ✅ 报 -32603 "session xxx not found"——grape 侧错误兜底
   依赖此行为。
7. **settings.json provider** ✅ opengw 接入确认：provider.opengw.{base_url,api_key,models}
   实测用 opengw.clouddeveloper.club/v1 + deepseek-v4-flash-0731 真实调通；
   default_mode:"auto" 生效（全程无 request_permission 事件）。
   注：settings.json 含明文 api_key，per-task 目录必须 chmod 600（已列验收标准）。

对 hwcloud_acp.py 实现的直接修正（相对"复刻 hermes_acp"的原始假设）：

| 项 | hermes_acp 现实现 | hwcloud 实测要求 |
|----|------------------|-----------------|
| mcpServers 参数 | `{}` | `[]` |
| resume 失效降级 | 靠 resume 报错降级 new | resume 不报错，需主动校验或不用 resume（一期：不用） |
| 噪音事件 | 无 | available_*/mcp_servers/session_info/usage_update 需忽略 |
| 免审 | env HERMES_YOLO_MODE=1 | settings.json default_mode:"auto"（env 兜底保留 request_permission 自动应答） |
| 回调绑定 | _on_prompt_done 别名快照（潜伏 bug） | 调用点实时读公开属性 |
| 工具静默超时 | 300s | 900s 可配（RUNTIME_TOOL_SILENT_TIMEOUT） |

## 真机部署与运行记录（阶段 2，2026-10-12）

**已部署 worker**：
- 实测 worker 机（内网直连测试主服务）：
  二进制为 openagent-go 自建（~/.local/bin/hwcloud），
  probe ok（2.9-7.4s，偶发 15s 冷启动超时后自愈——`_ACP_PROBE_TIMEOUT=15` 偏紧，
  待放宽），settings.json 预写 opengw provider + default_mode=auto
- devspace 云开发环境设备：probe fail——设备预装的是
  **旧版官方构建 v0.0.1-alpha.20**，卡在 session/new 无响应。**教训：hwcloud 必须
  用 openagent-go 源码自建版本**（协议行为差异大），devspace 镜像的预装版不可用，
  接入新设备需替换二进制 + 预写 ~/.hwcloud/settings.json

**e2e 验证链（issue"改造成 web 项目并部署公网可访问"）**：
vod（需求收敛+A2A 交办）→ issue（spec 推进）→ coding
（pygbag/WASM 构建+PR）→ PR 合并 → sre（devbridge 部署）——全链 hwcloud 引擎执行。

**运行中发现并修复的 4 个问题**（详见 §3b 与 §1b）：
1. _backend_for_engine 漏 hwcloud → 任务被 opencode 冒名执行（2ce0d9ddf）
2. 内网 worker server_url 走公网 → A2A 超时，改实测可达判定（a9cf2c62b）
3. 工具静默超时 300s 杀长安装 → 900s 可配（949f84527）
4. sandbox/ stub 目录遮蔽真实现（顺手修复，b66ee444c）

**部署注意**：worker 自动升级用主服务的 worker 包覆盖整个 src/——所有 worker 端
改动必须提交入库让主服务打包（曾因 worker 机本地补丁未入库被自动升级冲掉），
不能只在 worker 机器上手工改。

