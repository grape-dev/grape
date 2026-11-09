# aishell-dsh 引擎接入实施计划（一期上线 / 二期完善）

> 📅 2026-11-06 ｜ 🌿 基于 weekly-10 @ 40be0820a（个人工作分支，含 aishell-dsh 系列提交）｜ 📄 分期实施计划（设计结论详见 2026-11-05 设计 spec，本文自包含）
> ⚠️ 行号体系：§2/§4 行号锚定 40be0820a（实施前）；§3.1 增补（2026-11-07）引用的是**实施后**当前分支（含 aishell_acp.py 等）的行号——两套不可互相对照。

## 0. 背景约束与已裁定决策（承接设计 spec，实施必须遵守）

### ⚠️ 第一约束：同名不同物

grape 已有的「AI Shell」引擎 = **hwcloud**（openagent-go 构建，部署绑定云开发环境）。与本次接入的本地 aishell（华为 @huaweidevtools 基于 DeepSeek dsh 框架发行，CLI 名 `aishell`，`aishell acp` 提供 ACP-over-stdio）是**两个完全不同的产品**，同名共存是本方案第一约束。内部名与展示名统一 `aishell-dsh`。

| # | 决策 | 内容 |
|---|---|---|
| D1 | 命名 | 旧 hwcloud 内部名/路由键/binary/展示名 AI Shell **全保持不动**；新引擎内部名=展示名=`aishell-dsh`。旧名占用点（instances.py:31/51、settings.py:985-986 出口转换、AIShell.svg）全部不动；存量瑕疵 agent.py:1063 capitalize 与 instances.py upper 大小写不一致——不修 |
| D2 | 接入模式 | 本地部署：自备机器/已有 worker 装 aishell，**不走云环境镜像预装、不建云环境** |
| D3/D4 | 模型凭据与分工 | 设备预配置（acp profile cordis.patch.yml 用户自写，**grape 不提供模板**；key 预置 .credentials.yaml/env，**grape 不持模型 key、不下发、不透传**。注意：不透传指模型 key；平台自身的 VCS 收口凭据 GRAPE_SESSION_TOKEN 按任务注入并经 GRAPE_AUTH_FILE 穿透进 agent bash，见 §3.1 ⑧）。VCS 收口 grape CLI + GRAPE_SESSION_TOKEN，不在 aishell 侧装 MCP/插件 |
| D5-D7 | usage/技能/遗留 | usage 解析轨、技能投送（物化 dest=`<cwd>/.dsh/skills` 已实证）、resume 等二期方案已定，详见 §5；#14 守卫不独立立项随查表顺带改 |

**边界与不做的事**（沿用设计 spec §9 否决结论）：REST/SSE 接入模式（常驻服务+端口管理与 per-task 子进程模型冲突）；改动旧 hwcloud 任何命名/展示/路由；平台下发模型 key；aishell web/acp 之外的 profile 形态（tui 等）；plan_update 等增强事件结构化消费（映射 text 透传）。集群语义：不引入新状态存储/新标识符，实例仍走 agent_runtimes 行 + credential_id 锚，多实例语义不变。

## 1. 分期总览

| 期 | 目标 | 内容 | 出口标准 |
|---|---|---|---|
| **一期（本次）** | 引擎可上线被真实使用 | ① Linux 本机识别 aishell 并对接，worker/引擎页面可见（**两种部署形态都要求识别**：本机 worker 与远程 ECS worker，同一发现链各验一次）<br>② 项目场景可配置新引擎，调度正确不串引擎<br>③ 引擎正常工作：实时事件流、结果按要求格式化、A2A 正确触发下一环节 | 三阶段验收全过（§4，V1 两形态各验一次），真实 issue 任务端到端出 PR |
| **二期** | 细节完善 | usage token 解析轨、技能投送、resume chat、Windows 路径、上下文占用消费 | 逐项另立 issue 验收 |

**一期 usage 口径**：一期不做 token 解析，用量页对 aishell-dsh 显式标注「token 账本二期接入」（fail-open，不假装通过）。协议面已实测确认 `usage_update` 仅上下文占用、无计费字段，token 账本在 `session.v4.jsonl.zstd`，二期解析方案已定（见 §5）。

## 2. 一期范围与改动清单

### 2.1 标准①：Linux 识别 + 页面可见

| # | 位置（@40be0820a 实测行号） | 改动 |
|---|---|---|
| 1 | src/agent/aishell_acp.py（新建） | AishellDshACPBackend：initialize → session/new → set_config_option(id=model，**字段名是 id 非 configId**，value 从 options 原样取) → session/prompt |
| 2 | executor.py:30 _BACKENDS / :192 known 表 | 注册转发壳；`aishell-dsh: {cli: aishell}` CLI 发现 |
| 3 | executor.py:613 附近 dispatch（复核定版行号） | stream_agent 分支 + _stream_aishell_dsh_acp（env/脱敏/占位符复用，参照 _stream_hermes_acp:363） |
| 3b | 发现链（grape_worker_server.py:86-94，现有代码） | **零改动**——worker 启动 discover_runtimes()→probe→register 扫本机 PATH，本机与远程 ECS 两种部署形态天然同构；一期验收要求两形态各识别验证一次（V1），不新增分叉代码 |
| 4 | runtime/agent.py:1099 _backend_for_engine | **aishell-dsh 分支必须先于 :1108 的 startswith("hwcloud")/("aishell")**——本期实现为 canonical_engine() 查表（见 2.2） |
| 5 | runtime/agent.py:581 | _probe_aishell_dsh_acp 四步探活（参照 _probe_hwcloud_acp:414；**不做 --version 假探活**） |
| 6 | engine_select.py:36 | **不加** _DEFAULT_ENGINE_ROLE_TAGS 条目（#658 11-03 裁定：默认不限角色，仅显式声明轨） |
| 7 | settings.py:1152 _engine_type_of、runtimes.js:260/524/537 | includes('aishell') 误命中修复：aishell-dsh 分支前置；_ENGINE_DOT 加色点 |
| 8 | usage_collect.py:147/204 | aishell-dsh 分支：**一期返回空 + 显式缺口标注**（不加会静默落空） |
| 9 | os_adapter.py find_cli / engine_search_paths | Linux 通用发现兜底：PATH 找不到时追加候选 `~/.local/bin/\<cli\>`（pip/pipx --user 安装目录；systemd 服务 PATH 常缺该目录，E2E 实证 aishell 装在这里导致升级重装后识别丢失）。对称 Windows 侧已有的 _ENGINE_WINDOWS_PATHS 机制（hermes 条目即 %USERPROFILE%\.local\bin 先例），非 aishell 特例 |
| 10 | scripts/install-worker.sh systemd unit 模板 | unit 模板 `Environment=PATH` 追加 `$HOME/.local/bin`（安装时展开为安装用户 home，与服务用户一致）——与 #9 双保险：即使未来发现兜底有遗漏，unit PATH 也不丢该目录。E2E 实证 upgrade 重装会重置 unit PATH 导致识别丢失 |

### 2.2 标准②：调度正确（首期红线）

**instances.py 新增** `ENGINE_ALIASES` + `canonical_engine()`（前缀最长匹配，表内解决顺序、不靠 if 顺序），_backend_for_engine / _engine_type_of / runtimes.js 判定三处改走查表。单测锁死：`aishell-dsh-xxx → aishell-dsh`、`aishell-worker-xxx/hwcloud-xxx → hwcloud`（旧轨逐字不变）、`dsh-xxx → dsh`、未知 → opencode；表序无关。这是全方案唯一兼容性雷（startswith 吞并），**不可遗留、不可简化**。

### 2.3 标准③：正常工作 + A2A

| 点 | 实现依据（2026-11-06 阶段 0 实测，aishell 0.0.2 + 自建 provider 路由真机验证） |
|---|---|
| 实时事件流 | session/update 流式，实测仅 5 类：agent_thought_chunk / agent_message_chunk / usage_update{used,size} / tool_call / tool_call_update。前两类+tool_call/update 映射统一事件契约 text/reasoning/tool_use/tool_result；**usage_update 一期忽略**（{used,size} 是上下文占用非计费，消费属二期 §5，一期不产生 usage 事件），无额外噪音事件 |
| 结果格式化 | PromptResponse stopReason=end_turn 收口；done 携带 {exit_code, text, oc_session_id, errors, stderr}；结论评论 [$AGENT_NAME] 前缀与 PR 回写走通用链路 |
| A2A 衔接 | 已核查：takeover/handoff 全链走通用 engine_name 通道，**无引擎名硬编码分支**——统一事件流 + oc_session_id 即自动衔接，无新增改动 |
| 免审直执行 | shell 工具未触发 requestPermission（tool_call in_progress→completed），Backend 按「不预期 incoming request」实现，收到时记日志降级 |

**一期不做**（二期清单见 §5）：usage token 解析、技能投送、resume chat、Windows 路径、usage_update 上下文占用消费。**#14 守卫不在此列**：它随 S1 查表收敛在一期内顺带改（非独立工作量、非遗留）。

### 2.4 实施参照资产（设计 spec §3.3，走读先例）

| 资产 | 可借鉴点 |
|---|---|
| hwcloud_acp.py（1027 行，最近接） | configOptions 模型选择、噪音事件忽略清单、per-task 配置隔离、免审模式 |
| hermes_acp.py（1694 行） | ACP 事件队列、分级超时（silent/global/init）、initialize 重试、stderr 尾采、HERMES_HOME per-cwd overlay |
| runtime/acp_bootstrap.py | 失败分类结构（aishell 自包含无需补装逻辑） |
| 探活先例 | 三步握手 + 二进制管道（text=True 吞字节 #441 教训）+ 临时 cwd 不污染 |

### 2.5 用户/部署侧一次性操作（每台 worker，上线前置）

**发现形态说明**：发现链对两种部署形态同构——本机 worker 与远程 ECS 上的 worker 走同一条 `discover_runtimes() → probe → register/heartbeat` 链（grape_worker_server.py:86-94，worker 启动扫本机 PATH），差异只是 aishell 装在哪台机器。以下步骤**对每台需要跑 aishell-dsh 任务的机器（本机或任一 ECS）各执行一遍**；验收 V1 对两种形态各验一次即可（用例同一条）。

```text
1. 装 aishell 本体（PATH 可见）：.aishell-app + ~/.aishell     # 本地部署，不需要云环境
2. 配模型路由：acp profile cordis.patch.yml（用户自行编写）
     - 自建 provider（baseURL / apiKeyEnv / models）
     - agent-default-model（provider + model）
3. 预置 key：~/.aishell/.credentials.yaml 写 apiKey（或 export 到 worker 环境）
4. grape Web 端无额外操作 —— 引擎发现自动（find_cli 扫 PATH）
```

## 3. 已踩坑（实施必读，全部真机实证）

① acp profile 默认 provider=aishell/trial，无凭据报 MISSING_CREDENTIAL「not authenticated with Huawei Cloud IAM」——须配自建 provider（自建 provider）+ agent-default-model；② model select 的 value 不透明，**必须从 options 原样取**（自拼报 -32602）；③ session/set_config_option 的 RPC 参数键是 **configId** 非 id（2026-11-07 Windows aishell 0.0.2 真机复测：传 `id` 报 -32602 `Invalid input: expected string, received undefined`，传 `configId` 通过）；**「id」指的是 configOptions 列表里模型选项自身的标识字段**（据此过滤 `id=="model"`），不是 RPC 参数键——勿按旧表述改反代码；④ prompt 前必须 session/new；⑤ atomic-write 锁被 kill 后可能留 0 字节僵尸锁，删锁即恢复；⑥ 同进程对活跃会话 resume 报 -32602（一期每任务 session/new 不涉及）；⑦ **systemd 服务 PATH 不含 ~/.local/bin**（E2E 实证）：pip --user 安装的 CLI（aishell 即如此）人登录可见、服务进程不可见，且 install-worker.sh 重装会重置 unit PATH——由 §2.1 #9 的 Linux 发现兜底根治，不依赖运维改 PATH。

### 3.1 已踩坑增补（2026-11-07 A2A 全链实测，独立于分期计划，接入任何引擎前必读）

⑧ **任务级 GRAPE_SESSION_TOKEN 被 dsh 框架洗掉**：aishell 与 dsh 同框架，bash 工具 spawn 时 scrub 父进程 env 中变量名匹配 /KEY|PASSWORD|SECRET|TOKEN/i 的变量（先例 src/runtime/dsh.py:222）→ grape CLI 全命令 Missing token。解法：token 落 0600 文件 + GRAPE_AUTH_FILE 传递（变量名不命中 scrub；grape CLI 先读该文件再 fallback env）。实现 src/agent/aishell_acp.py prepare_env；⚠️ 调用必须在 env_extra 合并之后（executor.py:535，token 任务级）。

⑨ **GRAPE_AUTH_FILE 不能写 /tmp**——两层隔离依次实证：(a) bwrap 沙箱把 /tmp 挂私有 tmpfs（src/agent/sandbox/__init__.py:88 --tmpfs /tmp），coding/worktree 场景沙箱外写的文件沙箱内不可见（issue/vod/chat 不走沙箱所以通过——**同代码不同场景执行环境不同**）；(b) prepare_env 先于 acp 服务建工位目录执行，isdir(cwd) 为假静默 fallback 回 /tmp。解法：文件落任务 cwd（rw-bind 必可见）+ makedirs(cwd, exist_ok=True) + .git/info/exclude 防污染 + executor finally 清理（仅 cwd 下）。

⑩ **凭据 scope=user 与系统派单身份不匹配**：_check_pool_engines_reachable（service.py:37）对 user scope 引擎要求 task.user_id==owner_id；A2A 交办为系统身份 → 判「范围不匹配」不可达，同凭据手动重派（用户身份）却可达。E2E 测试凭据应为 global。**新引擎接入必须逐格验证 scope×派单方式矩阵**（global/user/repo × 手动/A2A/定时）。

⑪ **探活三副本必须同步登记**：agent.py:55（worker 心跳）/ engine_probe.py:20（主控每 10min，dashboard 本机行读内存不读 DB）/ local_probe.py:48（注册链）。漏任一份 → 页面「跳过 unknown engine」且可达性判死（skip 不在 ok/pending 白名单）。临时解法：主控单源 import agent._probe_aishell_dsh_acp；二期单源化+键集合护栏单测（todo: engine-probe-triple-duplication）。

⑫ **凡涉及 worker 包内文件的修复，验收必须含远端版本确认**（四步）：①判定文件属 worker 包（worker 加载路径（如远端 Worker 的 /opt/grape/））；②找本次修复的新符号；③worker 上 `grep -c "<新符号>" <文件>` 必须命中，0=旧代码先同步再测；④`systemctl restart runtime-agent` 触发 auto-upgrade，日志见 `Auto-upgrade to h-xxxx` 再复测。**没这步会把「代码没同步」误判成「修复无效」**（2026-11-06 实证白跑一轮）。

⑬ **实体 configs 复制自 example 后必须查占位值**：email.example.json 的示例 SMTP 段（smtp.qq.com/your@qq.com）残留 → is_email_configured() 判 True → R7 注册降级被挡死（页面要求验证码但永远收不到）。部署后必查：`grep -E 'your@|your_smtp|example' configs/*.json | grep -v example`；send-code 应返回 EMAIL_NOT_CONFIGURED；注册页应注入 __EMAIL_CONFIGURED__=false。

### 3.2 Windows 适配踩坑增补（2026-11-07 Windows 真机实证，aishell 0.0.2 + grape win 适配）

⑭ **aishell CLI 在 Windows 无 PATH 无 exe**：唯一入口是桌面安装内 `resources\runtime\cli\bin\aishell.cmd`（ELECTRON_RUN_AS_NODE=1 启动器，DSH_HOME 钉 `%USERPROFILE%\.aishell`，对齐 Linux 启动器）。发现链兜底两层：`_ENGINE_WINDOWS_PATHS["aishell"]` 默认目录 + **Uninstall 注册表 `InstallLocation` 动态候选**（HKCU/HKLM 按 DisplayName 前缀 "AI Shell" 定位，装在非默认盘/目录仍可发现）——未启动过桌面的机器用户 PATH 无该目录，不可依赖。`.cmd` 形态**不命中 issue #351 多行截断雷**：ACP 模式 argv 只有 `["acp"]`，prompt 走 stdin 不经 cmd 壳（find_cli 实测命中 + `--version` 0.0.2 通过）。

⑮ **探活/会话 cwd 不得写死 `/tmp`**：Windows 无此目录，且报错错位——session/new 正常返回，到 prompt 才报 `working-directory: directory does not exist: "/tmp"`，易误判成模型/凭据问题。解法 `tempfile.gettempdir()`（跨平台）。同修：探活延迟单位统一为秒（曾以 ms 混进 `{latency}s` 日志打出 `29994s`）。

⑯ **subprocess 捕获必须固定 `encoding="utf-8", errors="replace"`**：Windows 控制台/子进程可能输出 GBK（0xC0 起），strict utf-8 reader 线程抛 UnicodeDecodeError，且调用方 `stderr` 读成 None——报错只剩 `exit=127\nNone`，极难排查（templates 自动同步实证；与 f4dc10289 的 pip GBK 崩溃同族）。

⑰ **模型凭据可全自包含，无需平台注入 env**：apiKeyEnv 的解析链为 credentials store → 进程 env → `<cwd>/.env` → **`$DSH_HOME/.env`**；Windows DSH_HOME 钉 `%USERPROFILE%\.aishell`，key 写 `~/.aishell/.env`（`<REF_NAME>=<key>`）即自包含——grape 服务不加载 .env 也能跑通探活与任务（无注入实测 probe ok）。自建 provider 配置行 id 为 `llm-pi-ai`（`providers: {<name>: {apiKeyEnv, api, baseURL, models}}`，api 取 openai-completions/openai-responses/anthropic-messages）；默认路由须同时改 `acp` 与 `agent-default-model` 两行，漏一行则 session/options 与实际路由不一致。

⑱ **Windows `select()` 不能轮询管道（仅 socket）**：aishell_acp 的 select 读线程在 Windows 上首个循环即抛 `[WinError 10038] 在一个非套接字上尝试了一个操作`，reader 一起跑即死——**真任务 stdout 事件流全丢**（探活无感：agent.py 探活是独立 readline 读线程，导致「探活 ok、任务黑屏」的割裂假象）。解法：Windows 分支改专用阻塞 readline 线程（行即时投递、EOF 返回 b""），切行逻辑抽 `_feed()` 共用，POSIX select 路径不动（2026-11-07 真任务实证 + 修复后 E2E done exit=0 text=ok）。⚠️ `hwcloud_acp.py:410` 与 `hermes_acp.py:407` 存在同样的 select 轮询模式——这两个引擎在 Windows 上跑 ACP 任务会踩同一雷（未改，hwcloud/hermes 非 Windows 适配范围）。

## 4. 一期验收（三阶段，页面端到端）

前置：测试项目仓库，worker 上旧 AI Shell(hwcloud) 与新 aishell 并存——全程「新旧对照」，旧行为逐字不变才算过。

| 阶段 | 验收内容 | ✅ 正常 | ⛔ 异常（判失败） |
|---|---|---|---|
| V1 识别可见（标准①） | 重启 worker → 设置页引擎列表 + 卡片视图 | aishell-dsh-worker-xxx 原样、类型=aishell-dsh、probe 健康、独立色点无 AIShell 水印；旧行/旧卡片逐字不变；chat 下拉双实例并存 | 类型列显示 aishell、卡片画 AIShell 水印、旧行被动到 |
| V2 调度正确（标准②） | 同一轻量 issue（README 追加一行提 PR）双引擎各派一单；worker pgrep 快照 | 绑旧出 `hwcloud serve --acp`、绑新出 `aishell acp`（initialize/sessionId/done exit=0 齐） | **新单出现 hwcloud serve --acp = 吞并，直接判失败** |
| V3 工作正常（标准③） | 任务详情事件流、GitCode 实际 PR、结论评论；卸载 aishell CLI 注入失败一单再恢复 | 事件流统一字段无原生泄漏（无 sessionUpdate/stopReason 字样）、PR 实际存在与 result 一致、结论评论 [$AGENT_NAME] 前缀；失败注入时**明确报错指名 aishell-dsh** 不静默回退，恢复后自愈 | 原生 ACP 字段泄漏；completed 但 PR 不存在（假成功）；CLI 缺失却被旧引擎兜底执行成功 |

**失败注入注意**：卸载 aishell 的窗口内该 worker 的 aishell-dsh 派单都会失败——选验收专用时段执行，确认窗口内无他人向该 worker 派新引擎任务。

对照详细用例：见下 §4.1（场景用例）与 §4.2（Q 端到端三阶段详情）。

### 4.1 一期场景用例（自设计 spec §8 搬移，已按一期口径调整）

#### A 引擎注册与展示（→V1）

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| A1 worker 重启触发发现 | worker 日志：aishell-dsh installed=True、cli_path 正确 | |
| A2 探活 | probe ok（initialize+session/new+set模型+prompt 四步全通），延迟记录 | |
| A3 注册 API 出口 | instance_name=aishell-dsh-xxx 原样，不被 to_display 转成 AIShell（复核结论：to_display 为 dn=="aishell" 精确匹配 + get 回退原值，**无需改码天然成立**，本用例作回归看护） | |
| A4 前端卡片 | aishell-dsh 独立色点、无 AIShell 水印（三处修复生效） | |

#### B 任务执行与实施日志（→V2/V3）

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| B1 派发真实 issue 任务 | worker 日志：`aishell acp` 启动/initialize/session_id/事件计数/done exit=0 全链行 | |
| B2 grape 任务详情 | reasoning/text/tool_use/tool_result 按序呈现；oc_session_id 落库 | |
| B3 会话落盘 | ~/.aishell/sessions/\<任务cwd\>/\<sessionId\>/session.v4.jsonl.zstd 生成（二期解析轨的数据源，一期即验证落盘） | |

#### C 输出格式验证（→V3，C4 已按一期缺口口径调整）

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| C1 PR 场景 | 分支/commit/PR 链接回写 GitCode，与 opencode 同格式 | |
| C2 结论评论 | [$AGENT_NAME] 前缀、markdown 结构完整 | |
| C3 事件流契约 | 统一字段（type/text/tool/input）；无 sessionUpdate/stopReason 原生字段泄漏 | |
| C4 用量上报（一期口径） | 用量页对 aishell-dsh **显式标注「token 账本二期接入」**（缺口可见、不假装通过、不误记旧引擎账）；二期换解析轨后改断言 usage 非空 | |

#### D 结果对账（防假成功，→V3）

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| D1 终态一致性 | agent_tasks completed 且 result 与 PR/评论实际内容一致 | |
| D2 失败注入对照 | 配错 apiKeyEnv → MISSING_CREDENTIAL fail-fast 事件可见、归因正确 | |

#### E 新旧同名共存派发对照（命名冲突回归，→V2 核心）

构造：同 worker 并存 hwcloud 引擎（aishell-worker-xxx）+ aishell-dsh 引擎（aishell-dsh-worker-xxx）。

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| E1 双绑定 | 用户 Chat 配置分别绑定两实例（resolve_instance_id 精确解析） | |
| E2 派发路由 | 绑旧名 → `hwcloud serve --acp`；绑新名 → `aishell acp`；查表前置不吞并 | |
| E3 列表并存 | settings 页两行：类型 aishell / aishell-dsh，display AIShell / aishell-dsh | |
| E4 反向冒烟 | 卸载 aishell CLI（**验收专用时段**，窗口内该 worker 新引擎派单都会失败）→ 绑新名任务明确报引擎不可用，不静默回退 | |
| E5 用量不串账（一期口径） | 新引擎任务**不进旧 hwcloud/aishell 别名账**（缺口标注可验）；旧任务归因不变（token 正数入库二期验收） | |

#### F 同 worker 并发任务隔离

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| F1 双进程并发 | 两 `aishell acp` 子进程独立、session 互不可见、不串流（已实证：双进程暗号互不串） | |
| F2 会话文件独立 | storages/sessions 按 cwd 分目录 + 会话文件独立 | |

#### L token 脱敏凭据不泄漏

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| L1 诱导 echo env | key 被 [REDACTED]（executor 既有脱敏链复用） | |
| L2 凭据文件 | .credentials.yaml 内容不出现在任何事件 | |

#### Q 函数面单测（canonical_engine 锁死，CI 回归）

| 步骤 | 断言 | 执行记录 |
|---|---|---|
| Q-单测 | 四向映射（aishell-dsh-xxx→aishell-dsh；aishell-worker-xxx/hwcloud-xxx→hwcloud 旧轨不动；dsh-xxx→dsh；未知→opencode）+ 表序无关 + _backend_for_engine/_engine_type_of/runtimes.js 三判定点与表同源（#14 两处守卫同函数域抽查） | |

二期用例（G 多 profile 干扰 / H 冷启动自愈细化 / I prompt 中途 kill / J stdin EOF / K 大输出截断 / M 畸形消息 / N role_tags 显式声明过滤 / O 技能投送端到端 / P 探活不污染计费）留在设计 spec §8，随二期 issue 搬移。一期用例结果总表见 §4 V1/V2/V3 阶段表。

### 4.2 Q 端到端三阶段验收详情（配置→触发→观察，含判定点↔页面症状对照）

场景：真实仓库（建议测试项目），同 worker 上**旧 AI Shell(hwcloud) 与新 aishell 并存**，两个引擎各完整跑通一轮真实 issue 任务。查表错位的页面症状：归错类型(步骤2)、画错水印(步骤2)、派错后端(步骤5)、记错账(步骤8)、静默回退(步骤9)。

**查表判定点 ↔ 页面肉眼呈现对照**（每个代码判定点都有页面可见症状，验收时对号入座）：

| 查表判定点（代码面） | 页面上肉眼可查的呈现 | 看哪里 | 对应步骤/断言 |
|---|---|---|---|
| _engine_type_of（settings.py:1152，实例归类） | 设置页「类型」列：新行显示 aishell 而非 aishell-dsh，或旧行被改 | 设置 → 引擎列表「类型」列 | 步骤 2 / A1、A2 |
| runtimes.js:260 水印底图 | 新引擎卡片右下角浮出 **AIShell 水印**（错误归属的视觉铁证） | 引擎卡片视图右下角 | 步骤 2 / A3 |
| runtimes.js:524 类型归并 | 两行实例被归并成同一类（下拉重名/列表合项） | 引擎下拉与列表分组 | 步骤 3 / A4 |
| _backend_for_engine（agent.py:1099，路由吞并） | 任务事件流启动行是 `hwcloud serve --acp` 而非 `aishell acp`；或 CLI 缺失却任务成功（被旧引擎兜底） | 任务详情事件流首行 + worker pgrep 快照 | 步骤 5、9 / B2、E4（§4.2 的失败注入即 §4.1 用例 E4） |
| usage_collect.py:147/204 归因分支 | 用量页：新任务记到旧「AI Shell」名下，或两边都不见（一期为缺口标注形态） | 用量/台账页按实例过滤 | 步骤 8 / E5 |
| #14 两处重试守卫（agent.py:2707 / master_usage.py:93） | 一期表现为缺口标注正常——此守卫随查表收敛顺带改，若收敛时漏改则二期接入解析轨后出现**间歇性**缺账，与稳定错账可区分 | 用量页多轮任务对比 | 步骤 8 旁证 |

#### 阶段一：配置（一次性，页面 + worker 各一半）

| # | 怎么做 | 执行记录/截图 | 标记 |
|---|---|---|---|
| 1 | worker 上装 aishell（PATH 可见，见 §2.5）+ 配 acp profile + 预置 key，然后**重启 worker 进程**触发引擎发现 | | |
| 2 | grape Web → 设置 → 引擎(运行时)：确认「aishell-dsh-worker-xxx」新行出现且探活健康；**同时确认旧「AI Shell」行原样还在** | | |
| 3 | 项目场景 → Chat/任务配置：引擎下拉分别把「绑旧 AI Shell」「绑新 aishell-dsh」两套配置各存一份（同一项目、同一仓库） | | |

| 断言 | ✅ 正常 | ⛔ 异常（查表错位的症状） | 执行记录 | 标记 |
|---|---|---|---|---|
| A1 引擎发现 | 新行实例名 aishell-dsh-worker-xxx 原样、类型=aishell-dsh、健康/延迟正常 | 类型列显示 aishell/AIShell；或实例名被改写；或不出现 | | |
| A2 旧行不伤 | 旧行 aishell-worker-xxx、类型=aishell、健康——**与改造前逐字一致** | 旧行消失/改名/健康态翻转 | | |
| A3 卡片呈现 | aishell-dsh 卡片独立色点、无 AIShell 水印；旧卡片水印原样 | 新卡片画 AIShell 水印/共用旧色点 | | |
| A4 下拉可选 | Chat 配置引擎下拉同时列出两实例，名字精确可区分 | 重名/合并成一项/缺一个 | | |

#### 阶段二：触发（同一 issue 任务，两个引擎各跑一单对照）

| # | 怎么做 | 执行记录/截图 | 标记 |
|---|---|---|---|
| 4 | 测试项目建轻量 issue（如「README 末尾追加一行引擎自检记录并提 PR」），用**绑旧 AI Shell** 配置派发，等跑完 | | |
| 5 | 同样内容复制为第二个 issue，用**绑新 aishell-dsh** 配置派发，等跑完 | | |
| 6 | SSH worker：`pgrep -af "aishell acp"` 与 `pgrep -af hwcloud` 快照；`ls ~/.aishell/sessions/` 看会话落盘 | | |

| 断言 | ✅ 正常 | ⛔ 异常 | 执行记录 | 标记 |
|---|---|---|---|---|
| B1 旧单路由 | 事件流出现 `hwcloud serve --acp` 启动行，全链旧格式，PR 正常回写 | 旧单行为变化（查表收敛误伤旧轨） | | |
| B2 新单路由（吞并雷主断言） | 事件流出现 `aishell acp` + initialize + sessionId + 事件计数 + done exit=0；worker 快照有 `aishell acp` 进程 | **新单出现 hwcloud serve --acp = 查表被吞并，⛔ 直接判验收失败**；或进程起了但事件流空 | | |
| B3 会话落盘 | `~/.aishell/sessions/\<任务cwd\>/\<sessionId\>/session.v4.jsonl.zstd` 生成 | 无文件（session/new 链路断） | | |

#### 阶段三：观察（页面看点 + 一次失败注入）

| # | 怎么做 | 执行记录/截图 | 标记 |
|---|---|---|---|
| 7 | 任务详情页对照两单：事件流类型序列、结论评论 [$AGENT_NAME] 前缀、PR 链接回写 GitCode | | |
| 8 | 用量/台账页按实例过滤：新单缺口标注形态正确、**不进旧引擎账**（一期口径，详见用例 C4/E5） | | |
| 9 | **⚠️ 验收专用时段操作**：此步期间该 worker 无法跑 aishell 任务，勿在生产 worker 高峰期顺手执行——worker 上 `mv $(which aishell){,.bak}`（或改 PATH），绑新名再派一单；观察后 `mv` 回来再派一单，确认恢复 | | |

| 断言 | ✅ 正常 | ⛔ 异常 | 执行记录 | 标记 |
|---|---|---|---|---|
| C1 事件契约 | 新单与其他引擎同格式（统一事件字段，无 sessionUpdate/stopReason 原生字段泄漏） | 原生 ACP 字段直接漏到事件流 | | |
| C2 用量归因（一期口径） | 新单缺口标注挂在 aishell-dsh 名下、**不进旧 hwcloud/aishell 别名账**；旧单归因不变 | 缺口标注挂错实例，或新任务被记到旧引擎名下 | | |
| C3 结果对账 | agent_tasks completed 且 result 与 GitCode 实际 PR/评论一致（防假成功三对账） | 终态 completed 但 PR 不存在 | | |
| C4 失败注入（反向冒烟） | 任务**明确失败**，失败信息指名 aishell-dsh 不可用；**绝不静默回退到旧 hwcloud 执行成功**（回退成功 = 吞并雷复发 ⛔）；恢复后下一单自愈成功。**注入窗口内须确认无他人向该 worker 派 aishell-dsh 任务** | CLI 缺失却任务成功（被旧引擎兜底执行）；或失败信息不指名引擎 | | |

| 阶段 | 结果 | 执行人/时间 | 备注（含关键日志粘贴：两单事件流首行 + pgrep 快照 + 用量行） |
|---|---|---|---|
| 配置 1-3 | | | |
| 触发 4-6 | | | |
| 观察 7-9 | | | |
| **Q 总判定** | | | 任一 ⛔ 即查表收敛验收失败；全 ✅ 且旧行为逐字不变才算过 |

**与单测的分工**：本节是页面端到端看护（真实任务、真实并存、肉眼可判）；单测（§4.1 Q 函数面：四向映射/表序无关/三判定点同源）在 CI 锁回归——页面验收抓「表和调用点不同源」的集成错，单测抓「表本身映射错」。

## 5. 二期范围（逐项另立 issue）

| 项 | 方案（已定） | 依据 |
|---|---|---|
| usage token 解析 | 解析 `~/.aishell/sessions/\<cwd\>/\<sid\>/session.v4.jsonl.zstd`（含 inputTokens/outputTokens/cacheReadTokens）；每任务独立文件无需 watermark/baseline；fail-open：v4 漂移/zstd 缺失返回 []+告警。zstd 依赖三级：zstandard 包 → zstd CLI → 缺口。**Windows 目录名映射（实测）**：`\<cwd\>` 段是压扁路径——`C:\Users\<user>\AppData\Local\Temp` → `--C-Users-<user>-AppData-Local-Temp--`（`:`→`-`、`\`→`-`、首尾各补 `-`），解析器必须按同规则映射 cwd，映射不到时回退目录扫描（并入 fail-open） | 账本位置已实测；opencode（SQLite）/hwcloud（日志）同为先例 |
| 技能投送 | 物化 dest 按引擎切换：aishell-dsh → `<cwd>/.dsh/skills`（**已实证引擎自动读取**：真实 SKILL.md 投放后会话识别技能名与内容）；无需软链/双写/env（skillsRoot 硬编码不可配已核实）。engine_skill_dirs 挂 ~/.dsh/skills 供卡片扫描 | asar 逆向 + 真机投放验证 |
| resume chat | session/close → 跨进程 session/resume → 续聊上下文保留（已验证），configOptions 保留 | 另立 issue（阶段 4） |
| Windows 路径 | os_adapter _ENGINE_WINDOWS_PATHS 条目（Linux 一期仅 PATH） | — |
| usage_update 消费 | {used,size} 上下文占用的展示消费 | — |
| 引擎设置页补探活状态列 | 设置→引擎页只有在线/健康分，探活失败（如无凭据 MISSING_CREDENTIAL）在页面无线索，需去可观测页看——补四态探活列（✅延迟/❌/⏳）+ 悬停完整 probe_error，复用 observability.js:1214 既有渲染 | 2026-11-06 页面验收发现 |
| 失败事件 Engine 行显示真实绑定引擎 | 事件页 Engine 行取 `data.runtime \|\| data.backend \|\| 'OpenCode'` 兜底，派发失败事件显示成 opencode 误导排障（实例：vod 绑 aishell-dsh 失败事件显示 Engine: opencode）——失败事件改显示绑定引擎名（失败文案里已有），events.js:140 | 2026-11-06 页面验收发现 |
| **TODO（Windows）：hwcloud/hermes 任务链 select 轮询改 readline** | `hwcloud_acp.py:410`、`hermes_acp.py:407` 的 select 读线程在 Windows 首循环即抛 WinError 10038（select 仅支持 socket）→ ACP 任务事件流全丢，复现 aishell ⑱ 的「探活 ok（agent.py:271/469 已有 _win_acp_* 兼容层）、任务黑屏」割裂。修法照抄 aishell_acp 9ce71b005：win32 分支专用阻塞 readline 线程 + 切行抽 `_feed()` 共用，POSIX 路径不动，各 ~20 行 | 2026-11-07 aishell ⑱ 同族排查 |
| **TODO（Windows）：agent shell 中文乱码——PS 5.1 按 GBK 解码子进程 UTF-8 输出** | 真任务实证（真任务实证）：PowerShell 工具跑 `grape issues get \| ConvertFrom-Json` 构建更新载荷时，未带 `[Console]::OutputEncoding=UTF8` 前缀的调用按控制台 CP936 解码 grape 的 UTF-8 输出 → body 变 `鍦╮eadme娣诲姞椤圭洰鍦板潃`（原文「在readme添加项目地址」），乱码载荷险些经 `issues update` 写穿（服务重启中断未执行）。候选修法：① 服务控制台启动即 `chcp 65001`（子 PS 继承，系统级根治，需验证）；② grape-skills 各 runbook 强制每条 grape 命令带 UTF-8 前缀（已有部分命令带，不一致）；③ grape CLI 增加输出编码自描述。防写穿对账：issues update 前检查载荷含 CJK 乱码特征（如 `鍦`/`娣诲姞`） | 2026-11-07 真任务实证；同族根因见 executor.py PATH 注入注释块 |

## 6. 实施顺序（一期）

| 步 | 内容 | 验收 |
|---|---|---|
| S1 | canonical_engine 查表 + 单测（先立地基，后续接入都走它） | 四向映射单测过；现有 6 引擎路由回归不变 |
| S2 | aishell_acp.py Backend + executor 注册/dispatch/known 表 + probe 四步 | fixture JSON-RPC 回放单测过 |
| S3 | usage 缺口分支 + 前端三处判定 + _ENGINE_DOT | 设置页/卡片正确呈现，旧轨不变 |
| S4 | 真机联调：worker 注册 → V1/V2/V3 三阶段验收 → 真实 issue 出 PR | 三阶段全 ✅；防假成功三对账过 |

---

aishell-dsh 实施计划 · 一期/二期 · 2026-11-06 · 设计结论与实测证据详见 2026-11-05 设计 spec（同目录）
