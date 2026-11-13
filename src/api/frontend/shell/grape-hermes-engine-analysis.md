# Grape 系统支持 Hermes 引擎改造分析方案

> 版本：2026-08-14（已实施落地，基于 dev 分支 6fd274a0；实施提交见 dev 工作区）
> 状态：P0（HermesRuntime + 注册 + worker 引擎路由 + 杀进程通用化）已实施并通过
> 真实 hermes CLI 端到端验证；P1 安装脚本引擎提示已补；P2 测试已新增。
> 目标：让 Grape worker（远端执行引擎）支持 **Hermes Agent**（Nous Research 的 CLI Agent）
> 作为执行后端，与现有 opencode 后端并列可选。

---

## 1. 现状：Grape 引擎抽象架构

Grape 的执行引擎已做了一层后端抽象（`src/runtime/base.py`）：

```
AgentRuntime（抽象基类）
├── name / binary / prompt_via_stdin     # 类属性
├── build_args(prompt, session_id)       # 组装 CLI 参数
├── parse_output(line)                   # NDJSON 逐行 → 统一事件 dict
└── get_env(account, user_id)            # 子进程环境变量

src/runtime/opencode.py  OpencodeRuntime  → `opencode run --format json [--session X] <prompt>`
src/runtime/codearts.py  CodeArtsRuntime   → codearts CLI
```

后端注册表（`src/agent/executor.py`）：

| 位置 | 现状 | hermes 相关 |
|---|---|---|
| `_BACKENDS` 注册表 | `opencode`、`codearts` | **无 hermes**（`get_backend("hermes")` 会 fallback 到 opencode） |
| `register_backend(name, backend)` | 插件接口，约 20 行接新引擎 | ✓ 复用 |
| `discover_backends()` | 探测本机已装引擎 | **已含 hermes 探测**（cli: hermes / pip: hermes），但探测到也不会被正确使用 |

调用链中的硬编码点：

| 位置 | 硬编码 | 影响 |
|---|---|---|
| `src/runtime/agent.py:_execute_task` L204 | `backend_name="opencode"` | worker 执行任务固定用 opencode 后端 |
| `src/runtime/agent.py:_kill_stale_opencode` | `pgrep -f "opencode run"` | 超时杀进程只匹配 opencode |
| `src/agent/executor.py:stream_agent` | 默认 `backend_name="opencode"` | 事件对话/任务流默认走 opencode |
| `scripts/install-worker.sh` 引擎提示 | `npm install -g opencode-ai` | 一键安装只提示 opencode |

---

## 2. Hermes CLI 能力探测（本机实测）

```
hermes -z "<prompt>" --yolo --cli [--continue [SESSION]] [--skills SKILLS] [--pass-session-id]
```

| 能力 | 实测结果 | 对应 opencode 能力 |
|---|---|---|
| 单次执行 | `-z <prompt>` 一次性提问，退出码 0 | `opencode run <prompt>` |
| 权限自动批准 | `--yolo`（跳过确认） | `--dangerously-skip-permissions` |
| 输出模式 | `--cli` **纯文本**（只打印最终回复，如 "OK"） | `--format json` NDJSON 事件流 |
| 会话继续 | `--continue`（继续最近会话）/ `--resume <session>` | `--session <id>` |
| 会话管理 | `hermes sessions list/export/delete/...`（SQLite 存储） | `opencode sessions` |
| 技能 | `--skills <列表>` 指定技能包；内置 skill_view 系统 | `DEVOPS_SKILLS_DIR` 环境变量 |
| 模型 | `-m <model>` / `--provider`（默认走 hermes 配置） | opencode 自身模型配置 |
| 工具集 | `-t <toolsets>` 限制工具（terminal/file/web/browser…） | opencode 工具集 |

**关键差异：输出协议不同。**
- opencode：NDJSON 事件流（`step_start` / `text` / `reasoning` / `tool_use` / `step_finish` / `error`），
  Grape 的 `AgentRuntime.parse_output` 基类直接解析；
- hermes：`--cli` 输出**纯文本最终回复**（无逐步事件流），
  需要新增**文本 → 事件**的转换层（见 §4）。

---

## 3. 改造清单（按优先级）

### P0 —— 核心可用（不做则 hermes 无法作为执行引擎）

#### 3.1 新增 `src/runtime/hermes.py`（HermesRuntime，约 40 行）
```
class HermesRuntime(AgentRuntime):
    name = "hermes"
    binary = "hermes"
    def build_args(prompt, session_id=""):
        args = ["-z", prompt, "--yolo", "--cli"]
        if session_id:
            args += ["--continue"]        # hermes 无指定 id 继续，用"最近会话"
        return args
    def parse_output(line):               # 覆盖基类：文本 → text 事件
        line = line.rstrip("\n")
        if not line.strip(): return None
        return {"type": "text", "text": line, "sessionID": ""}
    def get_env(account, user_id):
        env = os.environ.copy()
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR   # 与 opencode 一致
        return env
```
要点：
- `parse_output` 逐行把 stdout 转 `text` 事件（Grape 前端/事件记录只消费 text，足够）；
- 会话继续用 `--continue`（hermes 支持继续最近会话；跨任务精确会话恢复见 §5）；
- `--cli` 确保非交互（避免 TUI 阻塞子进程）。

#### 3.2 注册后端（`src/agent/executor.py`）
```
from .hermes import HermesRuntime   # 或 from src.runtime.hermes import HermesRuntime
_BACKENDS["hermes"] = HermesRuntime()
```
（`discover_backends()` 已含 hermes，探测自动生效。）

#### 3.3 worker 任务执行按引擎路由（`src/runtime/agent.py`）
- `_execute_task` L204 的 `backend_name="opencode"` 改为：根据任务/引擎映射决定
  （runtime 注册时引擎实例名 `hermes-<device>` → 后端 `hermes`；`opencode-<device>` → `opencode`）；
- 推荐：任务下发时携带 `engine_backend` 字段（`agent_tasks.pool_agent_type`/`affinity_runtime_id`
  已能定位 runtime，由 runtime 的 engine 名推导后端，无需改表结构）。

#### 3.4 超时杀进程通用化（`src/runtime/agent.py:_kill_stale_opencode`）
- `pgrep -f "opencode run"` → `pgrep -f "<binary> (-z|run)"`，binary 按后端取值；
- 函数改名 `_kill_stale_engine(task, backend)`，保持按 cwd 匹配防误杀。

### P1 —— 体验完整（建议随 P0 一起）

#### 3.5 事件对话指定引擎（`src/api/routes/events.py` 对话接口）
- 对话入队路径支持 `engine` 参数（默认 opencode，可选 hermes）；
- SSE 流式对话（`/api/events/{seq}/chat/stream`）对 hermes 文本输出天然兼容
  （text 事件逐行 yield）。

#### 3.6 技能/工作区传递
- opencode 用 `DEVOPS_SKILLS_DIR` env；hermes 用 `--skills` 参数或 prompt 内联；
- 改造：HermesRuntime.get_env 设置 `DEVOPS_SKILLS_DIR`（hermes 的 shell 工具可读），
  同时任务 prompt 中引用技能说明（grape 的 skill-description.json 已下发）；
- 验证项：worker 在 cwd 下能读取仓库与技能文件（terminal/file 工具天然支持）。

#### 3.7 worker 一键安装脚本（`scripts/install-worker.sh`）
- 引擎提示段增加 hermes：`pip install hermes`（或检测 `hermes` 命令存在即跳过）；
- 引擎探测逻辑已在 `discover_backends`，安装脚本只需提示文案扩展。

### P2 —— 完善（后续迭代）

#### 3.8 测试（`tests/test_agent_executor.py`）
- mock `hermes -z ... --cli` 进程，验证文本 → text 事件流；
- 验证 `--continue` 会话参数、`get_env` 环境变量、超时杀进程通用化；
- 新增 `tests/test_runtime_hermes.py`（build_args/parse_output 单测）。

#### 3.9 文档
- `docs/new-version2/agent-cluster-design.md` 引擎章节补 hermes；
- worker 包 `discover_backends` 返回 hermes 后，控制台 Workers 页引擎下拉自动可见。

---

## 4. 输出协议适配设计（文本 → 事件）

hermes `--cli` 只输出最终回复，Grape 的事件模型需要以下映射：

| Grape 事件 | opencode（现状） | hermes（适配后） |
|---|---|---|
| `step_start` | NDJSON step_start | 忽略（无逐步语义） |
| `text`（回复正文） | NDJSON text | **stdout 每行 → text 事件**（逐行 yield，前端可流式） |
| `reasoning` | NDJSON reasoning | 忽略（--cli 不输出推理过程；如需可加 `--dev` 探测） |
| `tool_use` | NDJSON tool_use | 忽略（无流式工具事件） |
| `step_finish` | NDJSON step_finish + tokens | 进程退出（exit 0）→ step_finish |
| `error` | NDJSON error | 非零退出码 + stderr → error 事件 |

实现位置：`HermesRuntime.parse_output` + `stream_agent` 进程收尾逻辑
（`_terminate_proc`/收尾窗口已通用，无需改）。

**取舍说明**：hermes 文本模式牺牲逐步过程事件（reasoning/tool_use），
但 Grape 的事件记录与前端对话主要消费 `text` 最终回复，功能不受影响；
若要过程可视化，可后续用 hermes 的 `--dev` 模式或 `sessions export` 补充。

---

## 5. 会话模型设计

| 场景 | opencode | hermes |
|---|---|---|
| 事件对话多轮 | `--session <id>` 精确恢复 | `--continue`（继续最近会话） |
| 并发多对话 | 每对话一个 session id | hermes 的 `--continue` 是"最近会话"，并发多对话会互相覆盖 |

风险与对策：
- **单 worker 并发多对话**时 hermes `--continue` 不够精确 → 对策：
  a) 任务/对话串行化（hermes 引擎的 runtime 只跑 1 个并发任务，利用现有
     `max_concurrent`/槽位机制，runtime 建池时 hermes 类引擎限 1）；
  b) 后续 hermes 若支持 `--resume <session_id>` 精确恢复（`hermes sessions list`
     SQLite 可查 id），再升级为精确恢复。
- 建议：**hermes 引擎运行时 max_concurrent=1**（与 D9 串行槽位机制一致），
  避免会话覆盖导致的任务串话。

---

## 6. 技能兼容性分析

| 维度 | opencode | hermes |
|---|---|---|
| 技能来源 | `DEVOPS_SKILLS_DIR` env 指向 grape SKILLS_DIR | `--skills` 参数 / 内置 skills 系统（SKILL.md frontmatter） |
| 技能格式 | SKILL.md + frontmatter（name/description） | SKILL.md + frontmatter（name/description）——**格式同源** |
| 传递方式 | env 变量（opencode 进程读取） | 参数（--skills）或工作目录引用 |

结论：
- grape 的 `SKILLS_DIR`（`docs/` 下 skill-description.json 描述 + 技能目录）与 hermes
  skills 的 SKILL.md 格式兼容（同为 markdown + frontmatter）；
- 适配方式：HermesRuntime.get_env 设置 `DEVOPS_SKILLS_DIR`（hermes 的 terminal/file
  工具可读技能文件），并在 prompt 中引用技能名；不强制 hermes 加载 grape 技能包
  （hermes 内置 terminal/file/browser 工具已覆盖 devops 任务执行能力）。

---

## 7. 风险与权衡

1. **输出协议差异**：hermes 无 NDJSON 逐步事件 → 推理过程/tool_use 不落事件记录，
   仅最终回复。影响：事件详情页看不到 hermes 任务的逐步过程（opencode 有）。
   缓解：text 逐行流式输出保留完整回复；必要时升级 `--dev` 探测。
2. **会话精确恢复**：`--continue` 最近会话语义 vs opencode 精确 session id。
   缓解：hermes 引擎限并发 1。
3. **模型/Provider**：hermes 用自身配置（`hermes model`），grape 不干预；
   与 opencode 引擎并存时各用各的模型配置，互不影响。
4. **进程管理**：超时杀进程需通用化（§3.4），否则 hermes 任务超时杀不掉。
5. **依赖**：worker 机需安装 hermes CLI（pip install hermes 或官方安装脚本）；
   一键安装脚本补提示即可，不强制随包内置。

---

## 8. 实施路线图

| 阶段 | 内容 | 工作量 |
|---|---|---|
| 阶段 1（P0） | HermesRuntime + 注册 + worker 按引擎路由 + 杀进程通用化 | 半天 |
| 阶段 2（P1） | 事件对话引擎参数、技能传递验证、安装脚本提示 | 半天 |
| 阶段 3（P2） | 测试用例（mock hermes 进程）、文档、控制台引擎下拉 | 半天 |

阶段 1 完成后即可：`hermes` 引擎 runtime 注册 → 任务下发 → worker 用
`hermes -z ... --cli` 执行 → 结果回传（text 事件 + 终态）。

---

## 9. 结论

Grape 的引擎抽象（`AgentRuntime` + `register_backend` 插件接口 + `discover_backends`
已含 hermes 探测）对新增引擎支持良好，**核心改造集中在 3 处**：
① 新增 `HermesRuntime`（build_args + 文本→事件 parse_output，约 40 行）；
② `_BACKENDS` 注册 hermes；
③ worker 任务执行按引擎路由（消除 backend_name="opencode" 硬编码）+ 杀进程通用化。

输出协议差异（纯文本 vs NDJSON）通过 parse_output 转换层吸收，不影响 Grape
事件记录与对话前端；会话模型差异通过 hermes 引擎限并发 1 规避。
预估总工作量 1~1.5 人日，可先落地 P0（半天）打通端到端。
