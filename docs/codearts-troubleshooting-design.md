# CodeArts 引擎问题排查与优化设计

> 创建: 2026-09-01
> 更新: 2026-09-01 23:35 (方案 G 全部实施完成, 部署验证通过, --variant 不适用, webhook 修复)
> 背景: CodeArts 引擎在 Grape pipeline 中效果明显差于 OpenCode 和 Hermes，需系统性定位根因并记录修复方案。

---

## 1. 引擎架构关系

```
OpenCode (upstream v1.18.25)
  └─ CodeArts (华为云 fork v26.8.8)
       ├─ 共享: NDJSON 输出协议、AgentRuntime 基类、parse_output 解析
       ├─ 新增: 网络沙箱层、华为云 xCenter 模型列表、AK/SK 认证
       ├─ 新增: 内置 System Skills (bug-fix 流水线, 10 个)
       ├─ 新增: --sandbox / --auto flag (Grape 未使用)
       └─ 差异: permission config（非 --dangerously-skip-permissions）

Hermes ACP (独立协议)
  ├─ JSON-RPC over stdin/stdout
  ├─ per-task HERMES_HOME overlay
  └─ 外部新进程重试循环
```

CodeArts 虽然基于 OpenCode fork，但在华为云定制过程中引入了多层额外机制，每一层都可能成为 headless 模式下的故障点。

## 2. 三引擎核心差异对比（全维度）

### 2.1 进程与通信层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| 调用命令 | `opencode run --format json --dangerously-skip-permissions <prompt>` | `codearts run --format json <prompt>` | `hermes acp` (JSON-RPC) |
| 通信协议 | NDJSON stdout 逐行 | NDJSON stdout 逐行 (同 opencode) | JSON-RPC over stdin/stdout |
| prompt 传递 | 命令行参数 | 命令行参数 (stdin 触发沙箱封锁) | JSON-RPC `session/prompt` |
| 进程模型 | 单进程, 内部 agent loop | 单进程, 内部 agent loop | 单进程 + 外部重试循环 |
| 输出解析 | `parse_output` (基类共用) | `parse_output` (基类共用) | `_on_message` 回调 (专用) |

### 2.2 权限与安全层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| 权限批准方式 | `--dangerously-skip-permissions` flag | permission config JSON 文件 | `HERMES_YOLO_MODE=1` env |
| 自动批准范围 | 全部 (bash/read/write/edit/...) | 部分: bash/read/allow, **write/edit=ask** | 全部 |
| headless 可用 | 是 | **否 (write/edit 阻塞)** | 是 |
| 等价 --auto flag | 有 (`--auto`) | **有 (`--auto`) 但 Grape 未传** | N/A |
| 沙箱 | 无 | 有 (`--sandbox`, 默认关闭但拦截器存在) | 无 |
| 网络策略 | 无限制 | `deny_all` (默认) → 需手动 `allow_all` | 无限制 |

### 2.3 Session 与上下文层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| session 续接 | `--session <sid>` 支持 | **忽略** (build_args 丢弃 session_id) | `session/resume` 支持 |
| 会话失效重试 | 有 (检测 "session not found" → 重跑) | 无 (session 不传, 无此场景) | 有 (resume 失败 → session/new) |
| 上下文压缩 | 服务端 context_compact.py (200条阈值) | 同 opencode (共用) | state.db 50MB 阈值 + 检测 |
| state.db | 进程内 SQLite | 进程内 SQLite (KERNEL_DATA_DIR 隔离) | per-task overlay (symlink 镜像) |

### 2.4 模型与推理层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| 模型配置文件 | `opencode.json` (1个) | `codearts_cli.json` + `models-cache.json` (2个) | `config.yaml` (1个) |
| 模型列表来源 | opencode.json | xCenter API (503) → models-cache.json 兜底 | config.yaml |
| --variant (推理强度) | 有 (`--variant high/max/minimal`) | 有 (继承) 但 Grape 未传 | N/A (ACP 协议无此概念) |
| --thinking (推理展示) | 有 | 有 但 Grape 未传 | ACP `agent_thought_chunk` 自动收 |
| --agent (agent 选择) | 有 (build/plan) | 有 (继承 + codearts 角色) 但 Grape 未传 | N/A |
| 当前模型 | opengw/glm-5.2 | opengw/glm-5.2 (同配置) | 由 config.yaml 决定 |

### 2.5 技能与工具层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| Grape 技能注入 | `DEVOPS_SKILLS_DIR` env + `{{SKILL_DIR}}` 占位符 | 同 opencode (共用逻辑) | 同 (env + 占位符) |
| 技能物化路径 | cwd/.skills/{skill_name}/ | 同 opencode | 同 opencode |
| 内置 System Skills | 无 | **有 (10个: bug-fix 流水线)** | 无 |
| System Skills 内容 | N/A | issue-analysis, patch-generation, root-cause-localization 等 | N/A |
| System Skills 与 Grape 技能关系 | N/A | **冲突: 两套技能体系并存, LLM 可能混淆** | N/A |
| 工具集 | bash/read/write/edit/search... | 同 opencode (但 write/edit 受权限限制) | terminal/file/web/execute_code... |

### 2.6 执行驱动与完成判定层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| 多步驱动 | 进程内部循环: LLM end_turn → 进程检查是否完成 → 未完成继续驱动 | 同 opencode | 外部新进程重试: end_turn → 检测结构化输出 → 未完成开新进程 |
| 最大步数 | 进程内部决定 (无硬上限) | 同 opencode | MAX_ATTEMPTS=50 |
| 完成判定 | 进程退出 (exit_code) | 同 opencode | 四层兜底: JSON-RPC response / 结构化输出检测 / idle 超时 / reader EOF |
| 结构化输出检测 | 不需要 (进程退出即完成) | 同 opencode | `_detect_structured_output`: 检查 actions + conclusion + issue_summary |
| premature end_turn | 无此问题 | 无此问题 | 有: LLM 说完话不调工具就结束 |
| 空步检测 | 无此机制 | 无此机制 | 连续 2 步无 tool_use → 放弃 |

### 2.7 超时与资源管理层

| 维度 | OpenCode | CodeArts | Hermes ACP |
|------|----------|----------|------------|
| max_exec_seconds (静默超时) | 600s (默认) | 600s (同) | 600s (同) |
| 静默延长上限 | AGENT_SILENT_LIMIT (默认=max_exec_seconds) | 同 opencode | max_exec_seconds × 2 |
| task_timeout (外层硬超时) | RUNTIME_TASK_TIMEOUT=1800s | 1800s (同) | 1800s (同) |
| 全局超时 | N/A (进程内部管理) | N/A | max_exec_seconds × 2 |
| HERMES_HOME 隔离 | N/A | N/A | per-task tempfile overlay |
| KERNEL_DATA_DIR 隔离 | N/A | `~/.codeartsdoer/cli-data` (与 opencode 隔离) | N/A |
| JIT 缓存管理 | `BUN_RUNTIME_TRANSPILER_CACHE_PATH` + `JSC_diskCachePath` | N/A (codearts 自管理) | N/A |

## 3. CodeArts 效果差的根因分析（深层）

### 3.1 【P0 — 头号根因】write/edit 权限在 headless 模式下被硬性拒绝

**【2026-09-01 逆向分析更新 — 根因已确认】**

之前文档认为 --auto flag 或 permission config 改 allow 能修复 write/edit，实测推翻：两者都无效。

**根因（二进制源码级确认）**:

CodeArts CLI 的 `--auto` flag 和 OpenCode 的 `--dangerously-skip-permissions` 行为完全不同：

| flag | OpenCode | CodeArts |
|------|----------|----------|
| `--auto` | auto-approve ALL permissions (bash/write/edit/...) | **仅设置 bashMode=always_allow** (只影响 bash) |
| `--dangerously-skip-permissions` | hidden flag, 等价于 --auto + --yolo | **不存在** (传入后打印 help 退出) |
| `--yolo` | hidden flag, 等价于 --auto | **不存在** |

**源码证据 (从 274MB Bun 编译二进制中提取)**:

1. CodeArts `--auto` handler 只改 bash mode:
```javascript
// codearts binary - run command handler
handler: async (args3) => {
    if (args3.sandbox) {
      await PermissionService.setBashModeWithoutPersist("sandbox");
    } else if (args3.auto) {
      await PermissionService.setBashModeWithoutPersist("always_allow");  // ← 只改 bash!
    }
```

2. CodeArts 在 `--format json` 模式下，permission.asked 事件无条件 auto-reject:
```javascript
// codearts binary - run command event loop
if (event.type === "permission.asked") {
    const permission = event.properties;
    UI2.println(`! permission requested: ${permission.permission} (${permission.patterns.join(", ")}); auto-rejecting`);
    await sdk2.permission.reply({
      requestID: permission.id,
      reply: "reject"  // ← 无条件拒绝！不检查 --auto flag！
    });
}
```

3. OpenCode 的同名代码会检查 --auto/--yolo/--dangerously-skip-permissions:
```javascript
// opencode binary - run command event loop
if (event.type === "permission.asked") {
    let J = B.properties;
    if (Hj)  // Hj = args.auto || args.yolo || args["dangerously-skip-permissions"]
      await N.permission.reply({requestID: J.id, reply: "once"});  // ← auto-approve!
    else
      z.println(`! permission requested: ...; auto-rejecting`);
      await N.permission.reply({requestID: J.id, reply: "reject"});
}
```

**核心差异**: OpenCode 的 `--auto` 设置了 `Hj` 变量，在 permission.asked 事件中检查 `if (Hj)` → reply "once" (批准)。CodeArts 的 `--auto` 只调 `setBashModeWithoutPersist("always_allow")`，没有设 Hj 变量，permission.asked 事件里无条件 reject。

4. write/edit 工具内部调用 `ctx.ask({permission: "edit"})` → 触发 permission.asked 事件 → 被 json 模式的 auto-reject 拦截

5. permission config 中 write/edit=allow 无效：permission config 控制的是规则匹配层，但 permission.asked 事件的 auto-reject 发生在更上层，在检查 config 之前就已经 reject 了

**结论**: CodeArts CLI v26.8.8 在 `--format json` 非交互模式下，write/edit **永远被拒**，没有任何 flag、环境变量或配置文件能绕过。这是 CodeArts 华为云 fork 的硬性限制——fork 时修改了 `--auto` 的语义（从"全部自动批准"降级为"仅 bash 自动批准"），同时移除了 `--dangerously-skip-permissions` 和 `--yolo` flag。

**症状**: agent 能读文件、能跑 bash（如果 bash=allow）、能搜索，但到了真正要写代码的环节就被拒。表现为"分析了一大堆但没产出"。

**可行的解决方案（详见第 5 节）**:
- 方案 A: Binary patch — 修改二进制中 permission.asked 的 auto-reject 逻辑 (高难度, 升级覆盖)
- 方案 B: 降级使用 — codearts 只做只读任务 (VOD/Issue 分析)，coding/qa 不绑 codearts
- 方案 C: Proxy 层拦截 — Grape worker 层代理 codearts 的 permission.reply，在 permission.asked 时自动回复 "once"
- 方案 D: 使用 opencode 替代 — codearts 和 opencode 共享 NDJSON 协议，直接用 opencode

### 3.2 【P1】--sandbox flag 默认行为不明确

**问题**: CodeArts 有 `--sandbox` flag (`run commands in sandbox mode`, 默认 `false`)。虽然默认关闭，但 CodeArts 的网络拦截器 (`OPENCODE_MODE=tui` 时激活) 仍然存在，可能间歇性阻断 API 调用。

**对比**: OpenCode 没有沙箱层, 没有网络拦截器。

**当前缓解**: `get_env()` 设 `OPENCODE_MODE=tui` + sandbox config `network_policy=allow_all`。

**残留风险**: 拦截器代码路径仍在, `allow_all` 只是策略放行, 不是移除拦截器。极端情况下仍可能拦截。

### 3.3 【P1】内置 System Skills 与 Grape 技能体系冲突

**问题**: CodeArts 内置了 10 个 System Skills (bug-fix 流水线):
```
bug-fix-verification, codebase-structure, dynamic-root-cause-localization,
fix-build-command, issue-analysis, issue-reproduction, patch-generation,
regression-validation, static-root-cause-localization, verification-scenario-recognition
```

这些技能定义了完整的 bug-fix 工作流 (issue-analysis → root-cause → patch-generation → verification)，与 Grape 的 VOD→Issue→Coding→QA→SRE 流水线 + 62 个 SKILL.md 是两套独立体系。

**冲突点**:
1. CodeArts 的 System Skills 期望按 bug-fix 流水线执行 (写 `.bugfix/` 目录, 生成 `analysis_results.json`)
2. Grape 的 prompt 要求 agent 读 `{{SKILL_DIR}}/<skill>/SKILL.md` 并遵循 Grape 流水线
3. LLM 可能优先使用 CodeArts 内置技能 (因为是 "System" 级别), 忽略 Grape 下发的技能
4. 两套技能的输出格式不同: CodeArts 写 `.bugfix/` 文件, Grape 期望 JSON 记录块 (actions/conclusion/issue_summary)

**对比**: OpenCode 没有内置 System Skills, 完全使用 Grape 下发的技能。

**修复方向**: 
- 短期: 在 prompt 中明确指示 "忽略 codearts 内置技能, 只使用 {{SKILL_DIR}} 下的技能"
- 长期: 评估是否利用 CodeArts 内置技能增强 bug-fix 场景 (需要适配输出格式)

### 3.4 【P1】无 session 续接 → 上下文丢失

**问题**: CodeArts `build_args()` 里 session_id 被显式忽略。但 `codearts run --help` 显示支持 `-s/--session` 和 `-c/--continue`。

**当前代码**:
```python
# session_id 被忽略——codearts 有独立 KERNEL_DATA_DIR，其他引擎
# 创建的 session 不可见；且 VOD/Issue 任务每次应从头开始，续会话无意义。
```

**分析**: 
- "其他引擎创建的 session 不可见"是对的 (独立 KERNEL_DATA_DIR)
- "每次应从头开始"不完全对: coding/qa 等多轮任务需要上下文续接
- codearts 26.8.8 确实支持 `--session`, 可以启用 codearts 自己的 session

**对比**: OpenCode 支持 `--session`, Hermes 支持 `session/resume`。

**修复**: 在 `build_args()` 中启用 session:
```python
if session_id:
    args.extend(["--session", session_id])
```
前提: 确保 codearts 的 KERNEL_DATA_DIR 跨任务持久 (不随 task 清理)。

### 3.5 【P2】模型列表双重配置

CodeArts 需要两个文件模型列表一致:

1. `~/.codeartsdoer/codearts_cli.json` — 声明 provider + models (当前 6 个模型)
2. `~/.cache/codeartsdoer/models-cache.json` — 本地缓存 (当前已同步 6 个模型)

两者不一致 → "模型列表获取失败" → agent 无法启动。

**对比**: OpenCode 只需 opencode.json 一个文件。

### 3.6 【P2】--variant / --thinking / --agent flag 未使用

**问题**: CodeArts 继承了 OpenCode 的 `--variant` (推理强度)、`--thinking` (展示推理过程)、`--agent` (选择 agent 类型: build/plan) 等 flag，但 Grape 的 `build_args()` 全部未传。

**影响**:
- `--variant high/max` 可以提升推理深度, 对复杂 issue 分析有利
- `--thinking` 可以让前端展示 agent 推理过程 (opencode 也不传, 但 opencode 靠 NDJSON 里的 reasoning 事件天然支持)
- `--agent plan` 可以先用规划 agent 分解任务再执行

**对比**: OpenCode 也不传这些 flag, 但 OpenCode 的默认行为已经够用 (内部 agent loop 自动处理)。CodeArts 因权限等问题本就脆弱, 缺少这些 flag 进一步降低了执行质量。

### 3.7 【P2】环境脆弱性 — 15+ 环境变量

CodeArts `get_env()` 需要以下环境变量全部正确配置:

| 环境变量 | 作用 | 缺失后果 |
|---------|------|---------|
| `CODEARTS_CLI_AK` | 华为云认证 (dummy 绕过) | "认证失败" |
| `CODEARTS_CLI_SK` | 华为云认证 (dummy 绕过) | "认证失败" |
| `SCENARIO` | codeartsdoer 场景标识 | 默认值可能不对 |
| `KERNEL_DATA_DIR` | 数据目录隔离 | 与 opencode SQLite 冲突 |
| `KERNEL_CONFIG_DIR` | 配置目录 | 找不到 codearts_cli.json |
| `OPENCODE_CONFIG` | provider 配置文件路径 | 用错配置 |
| `OPENCODE_CONFIG_FILE` | 配置文件名 | 找不到配置 |
| `OPENCODE_MODE` | tui 模式 (影响网络拦截器策略) | 网络策略异常 |
| `PLUGIN_ENV` | hc 插件环境 | 插件不加载 |
| `OPENCODE_CHANNEL` | 更新渠道 | 自动更新 |
| `OPENCODE_DISABLE_MODELS_FETCH` | 禁止拉 xCenter | 503 错误 |
| `OPENCODE_DISABLE_AUTOUPDATE` | 禁止自动更新 | 升级中断 |
| `NODE_TLS_REJECT_UNAUTHORIZED` | 信任自签名证书 | TLS 错误 |

任何一个缺失或错误 → 静默失败，agent 看起来"启动了但什么都没做"。

**对比**: OpenCode 只需 3 个环境变量 (DEVOPS_SKILLS_DIR, BUN_CACHE, JSC_CACHE)。

### 3.8 【P3】codearts-cli.json 中模型前缀问题

历史问题: 曾写死 `modelarts/deepseek-v4-flash` 导致本机 opengw/… 配置被覆盖、模型前缀不存在报 Model not found (seq#1390)。

当前修复: 不设默认硬编码前缀，仅当显式设置 `CODEARTS_MODEL` 环境变量时才传 `--model` 覆盖。但仍需确保 codearts_cli.json 中的 `model` 字段指向正确 provider/model。

## 4. Hermes ACP 对比参考

Hermes 的问题和 CodeArts 完全不同:

| 维度 | Hermes ACP 问题 | CodeArts 问题 |
|------|----------------|---------------|
| 权限 | 无问题 (YOLO) | **write/edit 阻塞** |
| 沙箱 | 无问题 (无沙箱) | **网络拦截器干扰** |
| 多步驱动 | **premature end_turn** (说完不干) | 无此问题 (进程内循环) |
| 完成检测 | **四层兜底仍偶发漏检** | 无此问题 (进程退出即完成) |
| 上下文 | state.db 50MB 阈值 | **session 不续接** |
| 技能冲突 | 无 (无内置技能) | **System Skills 冲突** |

详见 `docs/new-version3/` 下 hermes-acp 相关设计文档和 `hermes-acp-migration` skill。

## 5. CodeArts 效果提升方案

### 5.0 【决策点】P0 write/edit 问题的方案对比（最终版）

| 方案 | 可行性 | 难度 | 权限粒度 | 影响宿主机 | 内存/task | 启动时间 | 外部依赖 | 风险 |
|------|--------|------|---------|-----------|----------|---------|---------|------|
| A: Binary patch | 中 (274MB ELF) | 高 | per-process | 不影响 | 479MB | 1-2s | 无 | 升级覆盖/破坏二进制 |
| B: 降级只读 | 高 | 低 | N/A | 不影响 | 479MB | 1-2s | 无 | codearts 无法 coding/qa |
| C: 持久 serve + SSE | 已验证 | 中 | per-session | 不影响 | 657MB 常驻 | 6-8s | 无 | SSE 不稳定/单点故障 |
| C2: per-task serve + polling | 已验证 | 中高 | per-session | 不影响 | 657MB | 6-8s | 无 | 完成判定不精确 |
| D: opencode 替代 | 高 | 低 | per-process | 不影响 | 301MB | 1-2s | 无 | 没用到 codearts |
| E2: per-task serve + opencode --attach | 已验证 | 低 | per-process | 不影响 | 958MB | 7-10s | **requests** | serve 启动慢/import requests 缺失致秒崩 |
| F: 改宿主机 global.json | 已验证 | 低 | **per-machine** | **影响全局** | 479MB | 1-2s | 无 | 用户 IDE 安全风险/并发写损坏 |
| **G: per-task 独立数据目录** | **已验证全通过** | **低** | **per-process** | **不影响** | **700MB** | **3-5s** | **无** | **磁盘 67MB/task** |

**推荐: 方案 G — per-task 独立数据目录隔离权限 ★★★**

### 5.0.1 方案 G 原理

codearts 启动时通过 `KERNEL_DATA_DIR` 环境变量决定数据目录路径。当前固定指向 `~/.codeartsdoer/cli-data`，所有 codearts 进程共用同一个 `global.json`。

方案 G 改成每个 task 指向独立临时目录 `/tmp/codearts-task-<taskId>/cli-data`，在该目录预写一份 `global.json`（write/edit/external_directory_write=allow）。task 结束删除临时目录。宿主机的 global.json 保持 ask 不动。

一句话: 每个 task 有自己的 global.json，权限隔离，用完即删。

### 5.0.2 codearts 权限系统逆向分析（二进制源码级）

通过 strings 提取 codearts 二进制（274MB Bun ELF）中的权限相关 JS 代码，确认权限判定链路:

**权限判定入口: `computeAskRuleset()` 函数**

返回一个 ruleset 数组，`evaluate()` 用 `findLast` 取最后一个匹配的 rule:
```
[
    ...getEffectivePermissions(),      // ① global.json 规则（唯一可外部控制）
    ...FIXED_PERMISSIONS,               // ② 硬编码: read/todowrite/skill/task/doom_loop/external_directory=allow
    ...await resolveGlobalWhiteList(),  // ③ MCP tools=allow + read/external_directory_read=allow
    ...customAgentRuleset,              // ④ Config.get().agent[name].permission（Config 不读 codearts_cli.json）
    ...await resolveWhiteList()         // ⑤ skill dirs read/external_directory_read=allow
]
```

**write/edit 权限: 只有来源 ① 含 write/edit 规则，来源 ②-⑤ 都不含。**
- global.json write=ask → evaluate 返回 ask → 触发 permission.asked → codearts run 模式下无人回复 → 失败
- global.json write=allow → evaluate 返回 allow → 不触发 permission.asked → 直接通过

**尝试过的所有突破口（全部不生效）:**

| 方式 | 结果 | 原因 |
|------|------|------|
| `OPENCODE_PERMISSION` env | ✗ 不生效 | codeagent-permission-service 不读 opencode 的 Flag |
| `config.json` (storage/permission/) 顶层 permission | ✗ 不生效 | computeAskRuleset 不读 config.json |
| `opencode.json` (/root/.config/opencode/) permission | ✗ 不生效 | message-bridge 忽略 OPENCODE_CONFIG |
| `codearts_cli.json` 顶层 permission | ✗ 不生效 | 同上 (日志确认 `config.user_config.opencode_config_ignored`) |
| `codearts_cli.json` agent.xxx.permission + `--agent` | ✗ 不生效 | Config.get() 不读 codearts_cli.json |
| 改宿主机 `global.json` | ✓ 生效 | getEffectivePermissions() 直接读此文件 |
| `KERNEL_DATA_DIR` 指向临时目录 + 预写 `global.json` | ✓ 生效 | 每 task 独立 global.json，不影响宿主机 |

**结论: codearts 的权限系统是封闭的，run 模式下唯一能控制 write/edit 权限的就是 global.json 文件。通过 KERNEL_DATA_DIR 隔离数据目录可实现 per-process 权限控制。**

### 5.0.3 方案 G 执行流程

```
task 开始:
  1. 创建 /tmp/codearts-task-<taskId>/cli-data/storage/permission/global.json
     内容: write=allow, edit=allow, external_directory_write=allow
  2. KERNEL_DATA_DIR 指向 /tmp/codearts-task-<taskId>/cli-data
     KERNEL_CONFIG_DIR 保持 /root/.codeartsdoer（模型配置不变）
     OPENCODE_CONFIG 保持 /root/.codeartsdoer/codearts_cli.json（模型/provider 不变）
  3. codearts run --format json --auto <prompt>
     → codearts 读 KERNEL_DATA_DIR → 读到临时目录的 global.json
     → write=allow → 不触发 permission.asked → 直接执行
     → bash: --auto 设 always_allow → 直接执行
     → NDJSON stdout → parse_output → 事件回报
  4. codearts 进程退出 → done

task 结束:
  5. 删除 /tmp/codearts-task-<taskId>/ 临时目录
```

### 5.0.4 方案 G 验证结果 (2026-09-01 本机验证)

```
权限验证:
  write: PASS (tool=write status=completed, 文件创建成功)
  bash:  PASS (tool=bash status=completed, 文件创建成功)
  edit:  PASS (tool=edit status=completed, 文件内容修改成功)

隔离性:
  宿主机 global.json: 未改动 (write=ask, edit=ask, external_directory_write=ask)
  临时目录 global.json: write=allow, edit=allow, external_directory_write=allow

内存:
  方案 G (临时目录): 700 MB
  旧模式 (固定目录): 479 MB
  差异: +221 MB (SQLite DB 创建 + sandbox_runner 解压 + system skills 解压)
  E2 (serve+attach): 958 MB (方案 G 比 E2 省 258 MB)

磁盘:
  临时目录: 67 MB/task
    ├─ sandbox_runner:     66 MB (linux_sandbox ARM64 二进制, codearts 每次从内置包解压)
    ├─ system/skills:     364 KB (10 个内置技能: bug-fix 流水线 SKILL.md + schema + references)
    ├─ opencode.db-wal:   388 KB (SQLite WAL)
    ├─ opencode.db-shm:    32 KB
    ├─ opencode.db:         4 KB
    ├─ log:               156 KB
    ├─ storage:            24 KB (session_diff / userInfo / sandbox config / migration)
    └─ 其他:                8 KB (checkpoint / cron / settings.json)
  4 并发: 268 MB
  task 结束删除: 无残留
  优化方向(P2): sandbox_runner 预放共享目录 + symlink, 临时目录降至 1MB

并发验证 (2026-09-01 本机 2 task 并发):
  Task A (write hello_a): PASS
  Task B (write hello_b): PASS
  两个 task 独立的 global.json / opencode.db / session_diff / sandbox_runner
  宿主机 global.json 未改动
  无文件竞争 / 无 DB 争锁 / 无端口冲突

启动时间:
  方案 G: 3-5s (DB 创建 + sandbox 解压)
  旧模式: 1-2s
  E2: 7-10s

外部依赖: 无
进程数: 1 (和旧模式一样)
```

### 5.0.5 方案 G vs E2 vs F 对比

| 维度 | E2 (serve+attach) | F (改宿主机 global.json) | G (独立数据目录) |
|------|-------------------|------------------------|-----------------|
| 权限粒度 | per-process | **per-machine** | per-process |
| 影响宿主机 | 不影响 | **影响全局** | 不影响 |
| 用户 IDE 安全 | 不影响 | **有风险** | 不影响 |
| 并发安全 | 安全 | **文件写损坏风险** | 安全（独立文件） |
| 内存/task | 958 MB | 479 MB | 700 MB |
| 启动时间 | 7-10s | 1-2s | 3-5s |
| 外部依赖 | requests（venv 缺失致秒崩） | 无 | 无 |
| 进程数 | 2 (serve + opencode) | 1 | 1 |
| kill 歧义 | 有 (opencode run 误杀) | 无 | 无 |
| 单点故障 | serve 崩溃 | 无 | 无 |
| 磁盘 | 无额外 | 无额外 | 67 MB/task |
| 代码改动 | ~150 行 | ~20 行 | ~20 行 |
| parse_output | 共用 | 共用 | 共用 |
| _run_once | 不用（独立路径） | 共用 | 共用 |

### 5.0.6 方案 G 代码实施（已完成, 2026-09-01）

**实际改动 2 个文件（比设计预估更少，executor.py 和 agent.py _kill_stale_engine 已是干净状态）:**

#### 1. src/runtime/codearts.py（改动 ~15 行）

- build_args: `["run", "--format", "json"]` → `["run", "--format", "json", "--auto"]`
- get_env: KERNEL_DATA_DIR 注释更新（setdefault 让 env_extra 能覆盖）
- 新增模块级常量 `_ALLOW_PERMS`（预写 global.json 的内容）
- 文档字符串更新为方案 G 描述

#### 2. src/runtime/agent.py（改动 ~35 行）

- 文件顶部新增 `_CODEARTS_ALLOW_PERMS` 常量（权限规则列表）
- _execute_task 中 knowledge 落地之后、done = None 之前，新增 codearts 临时目录创建逻辑:
  - 判断 engine_name 含 "codearts" 时触发
  - 创建 `/var/cache/codearts-tasks/{tid}/cli-data/`
  - 预写 `storage/permission/global.json`（write/edit/external_directory_write=allow）
  - 预写 `storage/sandbox/config.json`（network_policy=allow_all）
  - `env["KERNEL_DATA_DIR"] = cli_data`（局部变量，并发安全）
  - `_res.track_tmp_pattern(f"/var/cache/codearts-tasks/{tid}")` 注册清理

#### 不需要改的（已确认干净）:
- executor.py: 无 E2 残留（E2 代码已不在任何分支）
- agent.py _kill_stale_engine: 已是 "codearts run"（无 "opencode run"）
- prompt_builder.py / service.py / event_store.py: 不变
- worker 注册/claim/heartbeat: 不变
- _run_once / parse_output / reader 线程 / 静默超时: 不变
- skill / project-info / knowledge 注入: 不变（通过 cwd 路径注入，与 KERNEL_DATA_DIR 无关）

#### 本机验证结果 (2026-09-01):
```
语法检查: codearts.py OK, agent.py OK
build_args: ['run', '--format', 'json', '--auto', 'hello test'] ✓
get_env KERNEL_DATA_DIR: /root/.codeartsdoer/cli-data (fallback) ✓

权限验证 (临时目录 /var/cache/codearts-tasks/test_impl/):
  write: PASS (tool=write status=completed, 文件创建成功)
  bash:  PASS (tool=bash status=completed, 文件创建成功)
  edit:  PASS (tool=edit status=completed, 文件内容修改成功)

隔离性:
  宿主机 global.json: 未改动 (write=ask)
  临时目录 global.json: write=allow
  临时目录 sandbox config: network_policy=allow_all
```

### 5.0.8 方案 G 深度分析：潜在问题与对策

#### 问题 1：GRAPE_TASK_ID 环境变量并发竞态（严重，已修正）

**问题:** _execute_task 是 async，多个 task 在同一事件循环中交错执行。os.environ 是进程级全局变量，两个 task 交错时互相覆盖 GRAPE_TASK_ID。task A 设了 "t_aaa"，task B 设成 "t_bbb"，task A 的 get_env 读到 "t_bbb" → 用错临时目录。

**修正:** 不用 os.environ 传 task_id。改在 _execute_task 的 env dict（局部变量，并发安全）中直接设置 KERNEL_DATA_DIR，通过 env_extra → env.update 覆盖 get_env 的值。get_env 中用 setdefault 而不是直接赋值，让 env_extra 能覆盖。

#### 问题 2：/tmp 是 tmpfs，临时目录占内存（中等）

**问题:** worker 的 /tmp 是 tmpfs（内存文件系统）。临时目录 67MB（主要是 sandbox_runner 66MB 二进制）实际占内存。
- worker-0002: 7.2GB 内存, /tmp 3.7GB tmpfs, 4 核, max_concurrent=4
- 4 并发: 67MB × 4 = 268MB 额外内存
- 加上 codearts 进程 700MB × 4 = 2800MB
- 总计 3068MB，可用 4.6GB 只剩 1.6GB → 高负载时可能 OOM

**对策:** 临时目录改到磁盘路径 `/var/cache/codearts-tasks/{task_id}/`。/var/cache 不占内存。

#### 问题 3：内存从 479MB 涨到 700MB（中等）

**问题:** 方案 G 每个 codearts 进程 700MB，比旧模式 479MB 多 221MB。原因是临时目录从零创建时 codearts 要：
- 创建新的 opencode.db（SQLite）
- 解压 sandbox_runner（66MB 二进制）
- 解压 system skills（10 个内置技能 364KB）
这些在旧模式下已存在于宿主机目录，不需要重新创建。

**对策（P2 优化）:** worker 启动时创建 base 模板目录（含 sandbox_runner + system skills），每个 task 用 `cp -r` 或 symlink 复制，不从零解压。预计可将启动时间从 3-5s 降到 1-2s，内存从 700MB 降到接近 479MB。

#### 问题 4：worker 崩溃后残留临时目录（低）

**问题:** 如果 worker 进程被 OOM kill，TaskResourceStack.dispose() 不会执行，临时目录残留。

**对策:**
- /var/cache 目录: worker 启动时清理残留（`rm -rf /var/cache/codearts-tasks/*`）
- 可在 runtime-agent 启动脚本中加清理逻辑

#### 问题 5：codearts System Skills 每次重新解压（低）

**问题:** 临时目录每次创建，codearts 内置 10 个 System Skills 每次从二进制内置包解压。启动慢 2-3s。且 System Skills 可能和 grape 的技能指令冲突（P1 问题，旧模式也有）。

**对策:** 同问题 3 的 base 模板目录方案。System Skills 冲突问题在 prompt_builder 层处理（P1，与方案 G 无关）。

#### 问题 6：临时目录清理需注册到 TaskResourceStack（低，已含在代码改动中）

**问题:** 临时目录 `/var/cache/codearts-tasks/{task_id}/` 需要注册到 TaskResourceStack，确保 task 正常/异常/超时/终止退出时都清理。

**对策:** _execute_task 中 `_res.track_tmp_pattern(f"/var/cache/codearts-tasks/{tid}")`。TaskResourceStack.dispose() 在 4 处 finally 块中调用，覆盖所有退出路径。

#### 问题 7：session 续接不可用（无，与旧模式一致）

**情况:** 临时目录的 opencode.db 每次 task 结束删除，--session 参数传了也找不到旧 session。

**结论:** 不是问题。旧模式 codearts 也没启用 --session（build_args 明确忽略 session_id）。三个引擎在 pipeline 中的上下文传递靠 prompt_builder 注入前序 agent 结论摘要，不靠引擎内部 session history。

#### 问题 8：sandbox network_policy 默认 deny_all（严重，已修正）

**问题:** 临时目录启动时 codearts 自动生成 `sandbox/config.json`，默认值是 `{"network_policy": "deny_all"}`，而宿主机上之前手动配的是 `{"network_policy": "allow_all"}`。

实测 deny_all 不影响 LLM API 调用（codearts 内部的 LLM 请求不走沙箱），但会影响 agent 通过 bash 执行的网络命令——SRE agent 用 bash 调 hcloud CLI / curl GitCode API 会被沙箱拦截。

**修正:** 预写 `cli-data/storage/sandbox/config.json` = `{"network_policy": "allow_all"}` 到临时目录。已加入预写文件清单。

#### 问题 9：env_extra 中 KERNEL_DATA_DIR 不会被服务端覆盖（已确认无问题）

**担忧:** _execute_task 中 `env["KERNEL_DATA_DIR"] = ...` 设到 env dict 后，通过 env_extra 传给 stream_agent。但 _execute_task 还会调 `client.get_task_env(tid)` 获取服务端最新凭据，会不会覆盖 KERNEL_DATA_DIR？

**确认:** `get_task_env` → `_collect_server_env` 只返回 AK/SK 凭据（HUAWEICLOUD_SDK_AK/SK、CODEARTS_CLI_AK/SK），不包含 KERNEL_DATA_DIR。env_extra 中的 KERNEL_DATA_DIR 不会被覆盖。

#### 问题 10：_run_once session 重试与临时目录（已确认无问题）

**担忧:** _run_once 可能跑两次（session not found 重试），第二次 _run_once 会重新创建临时目录吗？

**确认:** 临时目录在 _execute_task 中创建（一次），不在 _run_once 中。两次 _run_once 用同一个临时目录，第二次重试时 global.json 和 sandbox config 已存在（幂等），不会重复创建。

### 5.0.7 三引擎全链路对比（方案 G 落地后）

| 维度 | opencode | hermes | codearts (G) |
|------|----------|--------|--------------|
| 权限解决 | --dangerously-skip-permissions | HERMES_YOLO_MODE=1 | KERNEL_DATA_DIR 隔离 + global.json allow + --auto |
| 权限粒度 | per-process | per-process | per-process |
| 通信协议 | NDJSON stdout（单向） | JSON-RPC stdin/stdout（双向） | NDJSON stdout（单向） |
| 执行路径 | _run_once 共用 | 独立 _stream_hermes_acp | _run_once 共用 |
| parse_output | base.py 共用 | 不用（结构化 JSON） | base.py 共用 |
| 进程数 | 1 | 1 + 临时目录 | 1 |
| 内存 | 301 MB | 76 MB | 700 MB |
| 启动时间 | 1-2s | 1-2s | 3-5s |
| session 续接 | --session | session/resume | --session |
| 模型来源 | 本机 opencode.json | hermes config.yaml | 本机 codearts_cli.json |
| 华为云特性 | 无 | 无 | System Skills + PLUGIN_ENV=hc |
| 隔离 | 无 | HERMES_HOME 临时目录 + state.db=:memory: | KERNEL_DATA_DIR 临时目录 |
```

### 5.1 【P0】加 --auto flag + --session 支持 (方案 G 已含)

```python
# src/runtime/codearts.py
def build_args(self, prompt: str, session_id: str = "") -> list[str]:
    args = ["run", "--format", "json", "--auto"]  # --auto: 解决 bash 权限
    model = os.environ.get("CODEARTS_MODEL")
    if model:
        args.extend(["--model", model])
    if session_id:
        args.extend(["--session", session_id])  # 启用 session 续接
    if prompt:
        args.append(prompt)
    return args
```

注意: --auto 只能让 bash 在 ask 模式下自动批准。write/edit 仍被拒（见 3.1 根因）。

### 5.2 【P0】permission config bash → allow (配置层, 配合 --auto 双保险)

```json
{"permission": "bash", "pattern": "*", "action": "allow"},
```

write/edit 的 config 改 allow 无效（3.1 已证明），不需要改。

### 5.3 【P1-观察】System Skills 冲突（codearts 特有, 暂不处理）

**现状**: codearts fork opencode 后华为云加了 10 个内置 System Skills（bug-fix-verification、issue-analysis、root-cause-localization 等），打包在二进制里，启动时自动解压到 KERNEL_DATA_DIR 的 system/skills/ 目录并注入 prompt。这些技能描述都写着 "Only the bug-fix sub-agent can use this skill"。

**影响范围**: 仅 codearts 引擎。opencode 和 hermes 没有内置 System Skills，不受影响。这不是系统共性问题，是 codearts 特有的。

**实际影响评估**: 技能描述明确限定 "Only the bug-fix sub-agent can use"，grape 的 VOD/Issue/Coding/QA/SRE agent 看到后大概率会忽略。实际影响可能比想象的小，但未经实测确认。

**不能通过预写 SystemSkillStatus.txt 禁用**: codearts 启动时会先解压 system skills zip 到 system/skills/ 目录，如果目录已有文件会报 ENOTEMPTY 错误。

**决策: 暂不处理, 观察实际影响。** 如果后续发现 agent 被 System Skills 干扰（如 VOD agent 尝试走 bug-fix 流水线），再在 prompt_builder.py 的技能注入段落后加指令:
```python
parts.append(
    "⚠️ 如果运行环境内置了 System Skills（如 bug-fix-verification、issue-analysis 等），"
    "请忽略它们，只执行上述 {{SKILL_DIR}} 指定的技能指令。"
)
```

### 5.4 【P1】启用 session 续接

在 `build_args()` 中加 `--session` 支持 (见 5.1 代码)。
确保 KERNEL_DATA_DIR 跨任务持久, 不随 task 清理。

### 5.5 【P2】考虑传 --variant 提升推理质量

```python
# 可选: 对复杂 agent_type (issue/coding) 传 --variant high
_variant = os.environ.get("CODEARTS_VARIANT", "")
if _variant:
    args.extend(["--variant", _variant])
```

### 5.6 【P1】环境变量 self-check（已实施）

在 `get_env()` 末尾加校验日志，关键变量缺失时打 warning，便于 worker journalctl 定位:

```python
_required = ["CODEARTS_CLI_AK", "CODEARTS_CLI_SK", "KERNEL_DATA_DIR", 
             "OPENCODE_CONFIG", "OPENCODE_MODE", "SCENARIO"]
_missing = [k for k in _required if not env.get(k)]
if _missing:
    log.warning(f"codearts env missing: {_missing}")
```

**注意**: 当前 get_env 中每个变量都用了 `env.setdefault()` 或 `env.get(key, default)`，正常不会缺失。
self-check 主要防两种场景:
1. 有人手动改了 os.environ 把某个变量设成空字符串
2. env_extra 覆盖时误传了空值（如 KERNEL_DATA_DIR 被覆盖为空）

**不解决的问题**: xCenter 503 — codearts 启动时调华为云 `snap-access.cn-north-4.myhuaweicloud.com` 验证模型权限，503 后降级为 free version。这是华为云侧的问题，不是环境变量能解决的。OPENCODE_DISABLE_MODELS_FETCH=1 已设但 codearts 仍会调 xCenter 验证 tenant settings（不同 API）。

### 5.7 【P3】models-cache.json 同步流程化

每次更新 codearts_cli.json 的 models 后, 脚本自动同步 models-cache.json。
可在 codearts-cli skill 的 verify 脚本中加一致性检查。

## 6. 修复优先级与行动项

| 优先级 | 问题 | 修复动作 | 影响范围 | 状态 |
|--------|------|---------|---------|------|
| **P0** | write/edit 被硬性拒绝 (根因确认) | 方案 G: per-task 独立数据目录 + KERNEL_DATA_DIR 隔离 global.json | 代码层 (worker 升级) | **已实施** |
| **P0** | bash 权限阻塞 (worker 默认 ask) | build_args 加 --auto | 代码层 | **已实施** |
| **P0** | --session 未启用 | build_args 加 --session 支持 | 代码层 (worker 升级) | 待修复 |
| **P0** | E2 残留代码回退 | E2 代码已不在任何分支, 无需回退 | — | **无需处理** |
| **P0** | GRAPE_TASK_ID 并发竞态 | 不用 os.environ, 改用 env dict (局部变量) 传 KERNEL_DATA_DIR | 代码层 | **已实施** |
| **P0** | /tmp tmpfs 占内存 | 临时目录改到 /var/cache/codearts-tasks/ (磁盘路径) | 代码层 | **已实施** |
| **P0** | sandbox network_policy 默认 deny_all | 预写 sandbox/config.json = allow_all 到临时目录 | 代码层 | **已实施** |
| **P1** | System Skills 冲突 | codearts 特有问题, opencode/hermes 无此机制; 技能描述限定 "Only bug-fix sub-agent", 实际影响待观察 | 观察 | **暂不处理** |
| **P1** | 沙箱网络封锁 | 已通过预写 sandbox/config.json 解决 | 代码层 | **已实施** |
| **P1** | 环境变量脆弱性 | get_env() 末尾加 self-check, 关键变量缺失时打 warning | 代码层 | **已实施** |
| **P2** | --variant 未使用 | codearts v26.8.8 不支持 --variant 参数, 传入后 codearts 打印 help 退出; 待 codearts 升级支持后再加 | 代码层 | **不适用** |
| **P2** | 模型列表双重配置 | 同步 models-cache.json 流程化 | 运维流程 | 待修复 |
| **P2** | 内存 700MB (比旧模式多 221MB) | symlink sandbox_runner/system/skills 指向宿主机, 避免每次解压 66MB | 代码层 | **已实施** |
| **P2** | 临时目录磁盘开销 (67MB/task) | 同上: symlink 后临时目录降至 1.3MB | 代码层 | **已实施** |
| **P2** | worker 崩溃后残留临时目录 | main() 启动时 rmtree /var/cache/codearts-tasks/* | 代码层 | **已实施** |
| **P3** | 模型前缀写死 | 已修复 (commit ba63d20a, 2026-08-20) | — | 已修复 |
| **P0** | pool_engine_ids 引用失效 runtime id 导致 task 永久卡死 | claim 时检查 pool_engine_ids 中的 id 全部 offline 时自动清空, task 变全量广播 | 代码层 | **已实施** |
| **P0** | direct 模式 agent_tasks 状态不同步 | _update_direct_task 的 SQL 条件 status='queued' 匹配不到 running 状态的行, 改用 update_task_status (和 worker 模式统一) | 代码层 | **已实施** |
| **P0** | A2A 本机端口错误 (3000→8000) | _resolve_server_url 硬编码 127.0.0.1:3000, 但 grape-main 在 8000 端口; 改为 GRAPE_MAIN_PORT 环境变量, 默认 3000 兼容老服务 | 代码层 | **已实施** |
| **P1** | VOD 跳过 A2A 时 stage 不推进 | VOD 检测到已有 PR 跳过 A2A 交办, 但 issue_state.stage 停在 vod 不推进到 issue/done, 前端显示"执行中" | SKILL.md | 待修复 |

## 7. 验证方法

### 7.1 权限修复验证

```bash
# 检查 --auto 是否传入
codearts run --auto --format json "echo test" 2>&1 | head -5
# 应看到 NDJSON 输出, 无 permission 询问

# 检查 permission config
cat ~/.codeartsdoer/cli-data/storage/permission/config.json | python3 -c "
import sys,json
cfg = json.load(sys.stdin)
for item in cfg:
    if item['permission'] in ('write','edit'):
        print(f\"{item['permission']}: {item['action']}\")
# 期望: write: allow / edit: allow
"
```

### 7.2 session 续接验证

```bash
# 第一次执行, 记录 session_id
codearts run --format json "hello" 2>&1 | grep sessionID | head -1
# 第二次执行, 用 --session 续接
codearts run --format json --session ses_xxx "what did I just say?" 2>&1 | head -5
# 期望: agent 能回忆上一次对话
```

### 7.3 任务级验证

在 DB 中查 codearts 任务的 tool_use 事件，修复后应出现 write_file/edit_file:

```sql
-- 查 codearts 任务是否有 write/edit tool_use
SELECT t.id, t.event_seq, t.status, t.created_at
FROM agent_tasks t
JOIN agent_runtimes r ON r.id = t.runtime_id
WHERE r.provider = 'codearts' OR r.name LIKE '%codearts%'
ORDER BY t.created_at DESC LIMIT 10;
```

### 7.4 对比测试

同一 prompt + 同一 repo 分别用 opencode 和 codearts 执行，对比:
1. tool_use 事件数（特别是 write_file/edit_file）
2. 任务完成率 (exit_code=0)
3. 结构化输出 (actions + conclusion + issue_summary) 完整性
4. 总执行时间
5. session 续接: 连续两个相关任务, codearts 是否能利用前序上下文

## 9. 三引擎 worker 执行链路对比（2026-09-01 新增）

### 9.1 公共执行链路（三引擎共享）

所有引擎在 worker 端共享同一条任务执行主干（`src/runtime/agent.py._execute_task`）：

```
主控端下发 task (JSON)
  │
  ▼
worker claim task (HTTP long-poll)
  │
  ▼
_execute_task(task, engine_name)
  │
  ├─ 1. _remap_cwd: 路径映射 (/home/grape → 本机 home)
  ├─ 2. validate_task_dependencies: 依赖检查
  ├─ 3. git worktree 前置 (coding/qa/sre)
  ├─ 4. skill 物化: OBS 直连/代拉 → 解压到 cwd/.skills/
  ├─ 5. project-info / knowledge 物化
  ├─ 6. {{SKILL_DIR}} / {{SERVER_URL}} 占位符替换
  │
  ├─ 7. _backend_for_engine(engine_name) → "opencode" / "codearts" / "hermes"
  │
  ├─ 8. stream_runtime(prompt, cwd, backend_name=...)  ← 这里分叉
  │     │
  │     ▼
  │     _stream_runtime_async → 后台线程迭代生成器
  │     │
  │     ▼
  │     stream_agent(prompt, cwd, backend_name=...)
  │       │
  │       ├─ hermes → _stream_hermes_acp() (专用路径)
  │       └─ opencode/codearts → _run_once() (共享路径)
  │
  ├─ 9. 逐事件消费: yield (evt_type, data)
  │     ├─ step_start → task_start(tid)
  │     ├─ text/reasoning/tool_use → batch.append
  │     └─ 50条/500ms → report_messages(tid, batch)
  │
  └─ 10. done → complete_task(tid, exit_code, text, ...)
```

### 9.2 分叉点：stream_agent 内部三条路径

```
stream_agent(prompt, cwd, backend_name)
  │
  ├─ backend_name == "hermes"
  │   └─ _stream_hermes_acp()  ← 路径 A: ACP JSON-RPC
  │       │
  │       ├─ HermesACPBackend.stream_execute()
  │       ├─ 启动 `hermes acp` 子进程
  │       ├─ JSON-RPC over stdin/stdout (NDJSON)
  │       ├─ initialize → session/new → session/prompt
  │       ├─ 监听 session/update 通知 (agent_message/tool_call/thought)
  │       ├─ session/request_permission → 自动批准 allow_once  ★
  │       └─ PromptResponse.stopReason → done
  │
  ├─ backend_name == "opencode"
  │   └─ _run_once()  ← 路径 B: subprocess + NDJSON
  │       │
  │       ├─ build_args: ["run", "--format", "json", "--dangerously-skip-permissions", prompt]
  │       ├─ subprocess.Popen(opencode, args, stdout=PIPE, stderr=PIPE)
  │       ├─ 独立 reader 线程: readline → _line_queue
  │       ├─ 主循环: pop queue → parse_output(line) → yield event
  │       ├─ 权限: --dangerously-skip-permissions flag 绕过全部 permission.asked  ★
  │       ├─ 静默超时: max_exec_seconds + AGENT_SILENT_LIMIT
  │       └─ 进程退出 → done
  │
  └─ backend_name == "codearts"
      └─ _run_once()  ← 路径 C: subprocess + NDJSON (同 opencode 路径)
          │
          ├─ build_args: ["run", "--format", "json", prompt]
          │     └─ 无 --auto, 无 --dangerously-skip-permissions
          ├─ subprocess.Popen(codearts, args, stdout=PIPE, stderr=PIPE)
          ├─ 独立 reader 线程: readline → _line_queue
          ├─ 主循环: pop queue → parse_output(line) → yield event
          │     └─ permission.asked 事件 → codearts 内部 auto-reject (不可绕过)  ✗
          ├─ 静默超时: 同 opencode
          └─ 进程退出 → done
```

### 9.3 逐维度对比

| 维度 | opencode | codearts (当前) | hermes ACP |
|------|----------|-----------------|------------|
| **调用方式** | `opencode run --format json --dangerously-skip-permissions <prompt>` | `codearts run --format json <prompt>` | `hermes acp` (JSON-RPC) |
| **通信协议** | NDJSON stdout 逐行 | NDJSON stdout 逐行 (同 opencode) | JSON-RPC over stdin/stdout |
| **进程模型** | 单进程, 内部 agent loop | 单进程, 内部 agent loop | 单进程 + 外部驱动循环 |
| **权限处理** | `--dangerously-skip-permissions` 绕过全部 | `--auto` 仅 bash, write/edit 被硬拒 | `session/request_permission` → 自动 allow_once |
| **权限层级** | 1层 (flag 绕过) | 3层 (flag → config → event auto-reject) | 2层 (env YOLO + request_permission 回复) |
| **prompt 传递** | 命令行参数 | 命令行参数 (同 opencode) | JSON-RPC session/prompt |
| **session 续接** | `--session <sid>` | 显式忽略 | `session/resume` |
| **输出解析** | `parse_output` (基类共用, NDJSON) | `parse_output` (基类共用, 同 opencode) | `_on_message` 回调 (专用, 结构化) |
| **事件类型** | step_start/text/tool_use/step_finish/error | 同 opencode | agent_message_chunk/tool_call/tool_call_update/agent_thought_chunk |
| **完成判定** | 进程退出 (exit_code) | 同 opencode | PromptResponse.stopReason (end_turn) |
| **多步驱动** | 进程内部循环 (无外部干预) | 同 opencode | 外部循环: stopReason=end_turn → 判断是否完成 → 不完成发新 prompt |
| **HERMES_HOME** | N/A | N/A | per-task tempfile overlay (state.db=:memory:) |
| **KERNEL_DATA_DIR** | N/A | `~/.codeartsdoer/cli-data` (隔离) | N/A |
| **技能注入** | `DEVOPS_SKILLS_DIR` env + `{{SKILL_DIR}}` 占位符 | 同 opencode | 同 (env + 占位符) |
| **环境变量数** | 3个 (DEVOPS_SKILLS_DIR, BUN_CACHE, JSC_CACHE) | 13+ 个 | 5个 (DEVOPS_SKILLS_DIR, YOLO, NO_COLOR, TERM, TOOL_PROGRESS) |
| **内置技能** | 无 | 有 (10个 System Skills, 冲突) | 无 |
| **代码量** | 31 行 (opencode.py) | 72 行 (codearts.py) | 280 行 (hermes.py) + 1137 行 (hermes_acp.py) |

### 9.4 关键差异分析

**权限处理是三引擎最大的分叉点**:

- **opencode**: 1层绕过。`--dangerously-skip-permissions` flag → 在 run 命令的 event loop 里检查 `if(Hj)` → permission.asked 时回复 "once" (批准)。所有工具(bash/write/edit)全部自动批准。
  - Grape 只需传一个 flag，代码 1 行。

- **hermes ACP**: 2层绕过。`HERMES_YOLO_MODE=1` env 禁止 hermes 发 permission 请求 + `session/request_permission` JSON-RPC 回调兜底（即使发了也自动回复 allow_once）。
  - Grape 在 `_handle_agent_request` 里处理，代码 ~20 行。

- **codearts**: 3层全卡。`--auto` flag 只改 bashMode=always_allow → permission config write/edit=allow 无效 → `--format json` 模式 event loop 里 permission.asked 无条件 auto-reject。
  - 3层都不能绕过 write/edit。

**代码复用关系**:

```
AgentRuntime (base.py, 52行)
  ├─ parse_output() — NDJSON 逐行解析, opencode 和 codearts 共用
  ├─ build_args() — 各引擎实现
  └─ get_env() — 各引擎实现

OpencodeRuntime (31行)     CodeArtsRuntime (72行)
  └─ 共用 parse_output       └─ 共用 parse_output
  └─ 共用 _run_once            └─ 共用 _run_once (executor.py)
  └─ build_args 有 flag         └─ build_args 无 flag

HermesACPBackend (1137行)
  └─ 不走 parse_output / build_args / _run_once
  └─ 独立的 stream_execute + JSON-RPC 协议
  └─ 独立的 _stream_hermes_acp 入口
```

**方案 C2 (per-task serve + polling) 的改造影响范围**:

方案 C2 的进程模型与 hermes ACP 一致（per-task 子进程），将成为第三条独立路径:

```
stream_agent()
  │
  ├─ hermes → _stream_hermes_acp()         ← 独立路径 (已有, stdin/stdout JSON-RPC)
  ├─ opencode → _run_once()                ← 共享路径 (不变, subprocess + NDJSON)
  └─ codearts → _stream_codearts_serve()   ← 新独立路径 (方案 C2, HTTP API + polling)
      │
      ├─ 启动 codearts serve --port 0 (随机端口, per-task 子进程)
      ├─ 等待 server ready (轮询 /session 直到 200)
      ├─ POST /session 创建会话
      ├─ POST /session/{id}/prompt_async 发送 prompt
      ├─ polling 循环:
      │   ├─ GET /permission → 按 sessionID 过滤 → POST reply {"response":"once"}
      │   ├─ GET /session/{id}/message → 提取新 parts → yield 事件
      │   └─ 检查 session idle → done
      └─ 杀掉 serve 进程 → 回收资源
```

与 hermes ACP 的异同:
- 相同: per-task 子进程, 独立 stream_execute, yield 兼容事件格式
- 不同: hermes 用 stdin/stdout 管道 (可靠), C2 用 HTTP polling (无 SSE 依赖)
- 不同: hermes 有 request_permission JSON-RPC 回调, C2 用 GET /permission 主动轮询

需要新增的代码:
- codearts_serve.py: CodeArtsServeBackend (类似 HermesACPBackend, ~300行)
  - start_serve(): 启动 serve 子进程, 提取端口
  - stream_execute(): polling 循环 + yield 事件
  - 端口提取: 从 serve stdout/stderr 日志里 grep "listening on http://127.0.0.1:PORT"
- executor.py: 新增 _stream_codearts_serve() 入口 (类似 _stream_hermes_acp)
- agent.py: _backend_for_engine 不变 (codearts → "codearts")
- _kill_stale_engine: patterns 加 "codearts serve"

不需要改的:
- prompt_builder.py: prompt 文本通过 API 发送, 占位符替换逻辑不变
- service.py / event_store.py: 事件格式兼容, 不需要改
- worker 注册/claim/heartbeat: 不变

## 8. 相关文件

| 文件 | 位置 | 说明 |
|------|------|------|
| CodeArtsRuntime | `src/runtime/codearts.py` | build_args / get_env (核心修改点) |
| OpencodeRuntime | `src/runtime/opencode.py` | 对比参考 (--dangerously-skip-permissions) |
| HermesRuntime | `src/runtime/hermes.py` | 对比参考 (CLI 模式) |
| HermesACPBackend | `src/agent/hermes_acp.py` | 对比参考 (ACP 模式) |
| AgentRuntime 基类 | `src/runtime/base.py` | NDJSON parse_output |
| executor | `src/agent/executor.py` | stream_agent 调度入口 |
| prompt_builder | `src/prompt_builder.py` | 技能指令注入 (System Skills 冲突修复点) |
| context_compaction | `src/runtime/context_compaction.py` | 上下文压缩 |
| permission config | `~/.codeartsdoer/cli-data/storage/permission/config.json` | 权限配置 |
| sandbox config | `~/.codeartsdoer/cli-data/storage/sandbox/config.json` | 网络沙箱 |
| provider config | `~/.codeartsdoer/codearts_cli.json` | 模型/provider 配置 |
| models cache | `~/.cache/codeartsdoer/models-cache.json` | xCenter 503 绕过缓存 |
| System Skills | `~/.codeartsdoer/cli-data/system/skills/` | 内置 10 个 bug-fix 技能 |
| codearts-cli skill | `~/.hermes/skills/codearts-cli/SKILL.md` | 安装配置指南 |

## 10. 部署验证发现的问题（2026-09-01）

### 10.1 pool_engine_ids 引用失效 runtime id 导致 task 永久卡死

**发现场景**: 部署方案 G 后触发 issue #26, VOD A2A 交办 issue agent, 但 issue task 一直 queued 无人 claim。

**根因链路**:
```
1. 用户在控制台配置场景时选了引擎 rt=80 作为 issue agent 的绑定引擎
2. 场景配置存在 DB 中, 记录 engine_runtime_ids: [80]
3. task 创建时从场景配置读出 pool_engine_ids=[80] (event_pipeline.py L1028)
4. worker claim 时检查 pool_engine_ids (service.py L686-720):
   - 当前 runtime id (253-270) 都不在 [80] 里 → skip
   - claim 代码有"跳过不匹配继续找下一个"的逻辑 (2026-08-29 修复)
   - 但只有一个 queued task → 跳过后没有下一个 → 返回 None
5. task 永远 claim 不到
```

**逻辑缺陷**: claim 代码只跳过不匹配的 task 继续找下一个, 但不会清理失效的 pool_engine_ids。
如果场景配置引用的引擎已经 offline/删除, pool_engine_ids 中的 id 永远不会有人 claim, task 就永久卡死。

**修复方案（已实施）**: claim 和 claim_by_tag 中, 发现 pool_engine_ids 不匹配时检查其中所有 id 是否全部 offline (status != 'online' 或不存在)。如果全部 offline, 自动清空 pool_engine_ids='', 让 task 变成全量广播可被任意引擎认领。如果其中有 online 的 (如 [80, 266] 中 266 online), 不清空, 保持限制。

**修改文件**: src/runtime/service.py — claim() L695-710 和 claim_by_tag() L921-936 两处同步修改。

**部署**: 0007 (本机) + 001 主控端, grape-main 重启。worker 不需要更新 (service.py 是主控端代码)。

### 10.2 direct 模式 agent_tasks 状态不同步

**发现场景**: issue #25 的 codearts VOD 任务在 001 本机 direct 模式执行完成, events 表标 completed, 但 agent_tasks 表还停在 running。

**根因**: `_execute_agent_inner` 中两步操作矛盾:
- L1464: `update_task_status(task_id, "running")` — 开始执行时改 running
- L1736/1752: `_update_direct_task(seq, "completed")` — 完成时只更新 `status='queued'` 的行
- running 状态匹配不到 queued 条件 → agent_tasks 永远停在 running

**修复**: direct 模式改用 `update_task_status(task_id, ...)` (和 worker 模式统一), 通过 task_id 直接更新, 终态幂等保护, running 状态能正常更新成 completed/failed。`_update_direct_task` 的 SQL 条件也改成 `status IN ('queued','running','dispatched')` 作为 fallback。

**修改点 (3 处, _execute_agent_inner + _execute_agent)**:
- L1736-1740: _execute_agent_inner 无产出 failed 路径, 有 task_id 时用 update_task_status
- L1756-1760: _execute_agent_inner 有产出 completed/failed 路径, 有 task_id 时用 update_task_status
- L1434-1437: _execute_agent 顶层 except 崩溃处理, 有 task_id 时用 update_task_status (深度检查发现遗漏+双重调用 bug, 已补修为 if/else)

### 10.3 A2A 本机端口错误 (3000 → 8000)

**发现场景**: direct 模式执行的 codearts agent 调 A2A `127.0.0.1:3000/api/a2a` 失败, "A2A 服务不可达"。

**根因**: `_resolve_server_url` 中本机引擎返回 `http://127.0.0.1:3000`, 但 grape-main 实际在 8000 端口。3000 是老 devops-bot 时代的端口, 迁移到 grape 后改了 8000 但没更新这里。

**修复**: 改为 `GRAPE_MAIN_PORT` 环境变量, 默认 3000 (兼容老 devops-bot), grape-main 部署时在 systemd Environment 中设 `GRAPE_MAIN_PORT=8000`。

### 10.4 VOD 跳过 A2A 时 stage 不推进

**发现场景**: issue #25 VOD 完成后, issue_state.stage 停在 vod, 前端显示"执行中"。

**根因**: VOD SKILL.md 中"重复 A2A 检查"逻辑: 检测到已有 PR#21 就跳过 A2A 交办, 只发评论。但跳过 A2A 就没调 `set_issue_stage`, stage 停在 vod 不推进。

**影响**: 前端显示"执行中"误导用户, 实际 VOD 已完成只是没推进 stage。

**修复方向**: VOD 跳过 A2A 时也要推进 stage (到 issue 或 done), 或在 complete_task 中检测 VOD 完成但 stage 仍=vod 时自动推进。

### 10.5 集群场景安全分析（0007 + 001 双主控）

**架构**: 0007 和 001 两个主控进程, 共享 MySQL DB (127.0.0.1), worker 通过域名 example.com 访问 (nginx 负载均衡到任一主控)。

**并发安全分析**:

1. **db_lock 是进程内线程锁** (`threading.RLock`), 不能跨进程互斥。两个主控进程各自有自己的 db_lock。
2. **MySQL autocommit=False 事务模式**, claim 中的 SELECT/UPDATE 在事务中执行。
3. **pool_engine_ids 清空操作是幂等的** — 清空后再清空还是空值, 无副作用。
4. **真正的 claim 互斥靠 MySQL 行级锁**: `UPDATE agent_tasks SET status='dispatched' WHERE id=? AND status='queued'` — 两个进程同时 UPDATE 同一行, MySQL 行锁保证只有一个成功 (rowcount > 0), 另一个匹配不到 status='queued' 返回 rowcount=0 → return None。
5. **清空 pool_engine_ids 和 dispatched 之间的间隔**: 0007 清空并 commit 后, 001 看到已清空的 task 也会走到 dispatched 竞争, MySQL 行锁保证只 claim 一次。

**结论**: 集群场景安全。db_lock 保护进程内线程并发, MySQL 事务+行锁保护跨进程数据一致性。

### 10.6 部署验证矩阵

| 检查项 | 0007 | 001 | worker(0002-0006) | 验证方式 |
|--------|------|-----|-------------------|----------|
| service.py 两处 pool_engine_ids 修复 | L710 L952 | L710 L952 | 不需要 (不 import) | grep "all offline" 确认 |
| prompt_builder.py GRAPE_MAIN_PORT | env=8000 | env=8000 | 不需要 | grep + systemctl Environment |
| event_pipeline.py direct 模式修复 | update_task_status | update_task_status | 不需要 | grep 确认 |
| codearts.py --auto/--variant/self-check | ✅ | ✅ | ✅ 5台已推 | grep CODEARTS_VARIANT |
| agent.py 方案G 临时目录+symlink+清理 | ✅ | ✅ | ✅ 5台已推 | grep codearts-tasks |
| grape-main active + health ok | ✅ pid=2108108 | ✅ pid=80546 | — | curl /health |
| runtime-agent active | — | — | ✅ 5台 active | systemctl is-active |
| worker 不 import service.py/prompt_builder.py | — | — | grep 确认无 import | agent.py/client.py/executor.py |
| 集群 DB 共享 | mysql 127.0.0.1 | mysql 127.0.0.1 | — | db-backend.json 一致 |
| pool_engine_ids 清空幂等 | 是 | 是 | — | 逻辑分析 |

### 10.7 修改文件清单

| 文件 | 改动内容 | 部署位置 |
|------|---------|---------|
| src/runtime/codearts.py | build_args 加 --auto, get_env KERNEL_DATA_DIR setdefault, self-check | 0007 + 001 + 5台 worker |
| src/runtime/agent.py | _CODEARTS_ALLOW_PERMS 常量, 方案G 临时目录创建+预写 global.json+sandbox config+symlink+track_tmp, main() 启动清理 | 0007 + 001 + 5台 worker |
| src/runtime/service.py | claim() 和 claim_by_tag() 中 pool_engine_ids 全部 offline 时自动清空 | 0007 + 001 (主控端) |
| src/prompt_builder.py | _local_port() 环境变量, 默认 3000 兼容老服务 | 0007 + 001 (主控端) |
| src/api/event_pipeline.py | direct 模式改用 update_task_status, _update_direct_task SQL 条件放宽 | 0007 + 001 (主控端) |
| /etc/systemd/system/grape-main.service | 加 Environment=GRAPE_MAIN_PORT=8000 | 0007 + 001 |
| docs/codearts-troubleshooting-design.md | 设计文档全文更新 | 0007 (git) |
| GitCode webhook | url 从 http://1.92.115.229/webhook/gitcode 改为 https://example.com/webhook/gitcode | GitCode API |

### 10.8 历史 codearts 失败根因总结

之前 codearts "容易失败" 的三个叠加原因:

1. **write/edit 权限被拒** (方案 G 已修复)
   - codearts 的 permission.asked 事件被二进制硬性 reject
   - agent 不能写文件, coding agent 需要写代码但写不了, 直接失败
   - 方案 G: per-task 独立 KERNEL_DATA_DIR + 预写 global.json (write/edit=allow) + --auto

2. **E2 代码 import requests 崩溃** (已回退)
   - dev 分支 E2 方案在 codearts.py 里 import requests
   - worker venv (Python 3.14) 没装 requests, 进程启动就崩
   - 所有 codearts task 3 秒内失败, 报 "timed out after 1800s"
   - E2 代码已不在任何分支, version-20260901 是干净代码

3. **xCenter 503** (华为云侧偶发, 无法修复)
   - 华为云模型验证 API 偶发 503, codearts 启动后卡死无输出
   - OPENCODE_DISABLE_MODELS_FETCH=1 已设但 codearts 仍调 xCenter 验证 tenant settings
   - 不是代码能解决的问题, 需等华为云恢复

### 10.9 部署验证结论 (2026-09-01)

**方案 G 验证通过:**
- codearts VOD/issue/coding/qa/sre 全流程执行正常
- bash/read/write/edit 工具全部正常 (方案 G 权限隔离生效)
- 临时目录 /var/cache/codearts-tasks/ 执行完自动清理, 无残留
- symlink sandbox_runner/system/skills 指向宿主机, 临时目录 1.3MB
- 无 codearts run 子进程残留, 无进程卡死
- 内存正常 (5台 worker 可用 3.2~5.9GB)
- 磁盘正常 (/var/cache 4KB, 无堆积)

**codearts vs hermes 执行质量对比 (默认模式, 无 --variant):**

| 维度 | codearts | hermes |
|------|----------|--------|
| VOD 步数 | 8-17 步 | 11-25 步 |
| VOD 工具调用 | 3-12 次 | 11-20 次 |
| VOD 耗时 | 34-127s | 29-172s |
| VOD 消息量 | 20-52 条 | 145-486 条 |
| 结构化输出 | ✅ 完整 JSON (actions+conclusion+artifacts+next_step+issue_summary) | ✅ 有但格式不统一 |
| coding 能力 | ✅ 9文件 30/30自测 68次工具调用 | 无样本 |
| issue 分析 | ✅ 4维度评级表格+危险场景识别 | 无样本 |
| 执行效率 | 步数少 耗时短 消息少 | 消息量大 步数多 |

**codearts 任务统计:** 30 个任务, 1 个 failed (失败率 3.3%, 唯一失败是 --variant high 不支持导致)
**hermes 任务统计:** 17 个任务, 1 个 failed (失败率 5.9%)

**结论:** 方案 G 修复后 codearts 实际成功率正常, 不再"容易失败"。执行效率高于 hermes, 结构化输出更规范。

### 10.10 方案 G 内存实测与 symlink 安全性

**内存实测 (worker-0003, task 执行中):**

| 场景 | 进程 RSS | 临时目录 | 总占用 | 说明 |
|------|---------|---------|--------|------|
| 旧模式 (无方案G) | 479MB | 0 | 479MB | 之前测量值, codearts JS 运行时 |
| 方案G 无symlink | 700MB | 67MB (tmpfs) | 767MB | sandbox_runner 解压到内存+tmpfs |
| 方案G 有symlink (实测) | 622MB | 28KB (磁盘) | 622MB | symlink 省掉解压开销 |

**内存明细 (/proc/PID/status):**
```
VmRSS:      622MB (实际物理内存)
Pss_Anon:   570MB (JS 运行时堆, 不可回收, codearts 本身开销)
Pss_File:   102MB (文件映射: codearts 二进制 + sandbox_runner + system skills)
Private_Clean: 100MB (文件映射干净页, 可回收)
Private_Dirty: 570MB (匿名脏页, JS 运行时)
```

**622MB vs 479MB 多出 143MB 的原因:**
临时目录是空的, codearts 启动时要从零创建 opencode.db (SQLite 初始化) + 加载 system skills 索引到内存。
旧模式下这些已在宿主机目录中现成存在, 不需要重新创建。
symlink 已省掉 sandbox_runner 解压的 78MB (从 700MB 降到 622MB)。

**symlink 并发安全性:**
- sandbox_runner: 66MB ARM64 沙箱二进制, 只读执行, 不写 → 多进程并发读安全
- system/skills: 10 个内置技能 SKILL.md + references, 只读加载, 不写 → 多进程并发读安全
- opencode.db: 每个 task 在各自临时目录中独立创建, 不通过 symlink 共享 → 无并发写问题
- global.json: 每个 task 在各自临时目录中预写, 不通过 symlink 共享 → 无并发写问题
- 操作系统文件系统支持并发读, 无锁竞争

**4 并发内存预估:** 622MB × 4 = 2488MB, worker 7.2GB 内存, 可用 4.7GB, 够用。

### 10.11 --variant 参数不适用

codearts v26.8.8 不支持 `--variant` 参数。传入后 codearts 打印 help 退出, task 2 秒失败。
`codearts run --help` 确认支持的参数: `--auto`, `--format`, `--model`, `--session`, `--continue` 等, 无 `--variant`。
待 codearts 升级支持后再加, 当前使用默认推理质量。

### 10.12 webhook 地址修复

**发现场景:** issue_comment 事件 (用户回复 Issue 评论) 不触发, VOD 澄清后用户回复但流水线不继续。

**根因:** GitCode webhook 地址是 `http://1.92.115.229/webhook/gitcode` (0007 公网 IP + HTTP), nginx 80 端口做 301 跳转到 443, GitCode POST 请求不跟随跳转, 返回 405。

**修复:** webhook 地址改为 `https://example.com/webhook/gitcode` (HTTPS + 域名), nginx 443 端口正确代理 `/webhook/` 到 grape-webhook。
