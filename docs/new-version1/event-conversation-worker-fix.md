# Event 对话在远端 Worker 执行失败的修复方案

> 版本：v1.0
> 日期：2026-08-12
> 状态：待评审
> 前置：`event-conversation-design.md`（Event 对话设计）、`skill-delivery-remote.md`（技能随任务下发）、`multi-issue-isolation-design.md`（Issue 会话）
> 触发背景：生产（203.0.113.2）上对 `Jason7602/gitcode-pr-assistant` 的 VOD agent 发起 Event 对话（anchor 事件 seq=499 / issue #8），任务入队到远端 worker（`worker-123249` @ 123.249.12.67）后 **30 分钟毫无回应**，线程只剩用户消息、无任何 agent 回复或失败提示。

---

## 〇、现场证据与根因

### 0.1 现场证据（生产 + worker 机）

| 证据 | 内容 |
|---|---|
| 任务入队 | `Task enqueued: t_7bee703ea9f1 ... runtime=1(opencode-worker-123249) skill=`——**skill 为空** |
| 会话复用 | opencode 同时加载了 `.../vod/issues/8` 目录（该 issue 会话项目目录）——会话复用生效 |
| 技能缺失 | worker 的 `/opt/grape/skills/event-dialogue/` **不存在**（worker git 在 `codeowner` 无提交分支，非 new-version2） |
| opencode 行为 | `opencode.log`:09:06:04 发起 LLM(deepseek-v4-pro)；09:06:07 `read skills/event-dialogue/SKILL.md`（文件不存在）；step1/step2 → 09:06:15 **"exiting loop"** → 此后进程挂起 30+ 分钟（State=S、ep_poll、无网络 socket、无子进程、无输出） |
| worker 看门狗 | `opencode silent 600s but process alive, extending window (total_extend=600/1200/1800s)`——硬上限 `3×max_exec_seconds=1800s`，到 ~30 分钟才终止 |
| 服务端超时 | `wait_task_terminal` 默认 `timeout=1800s`（30 分钟）→ 超时抛 `TimeoutError` → **SSE 静默断开，`_run_conversation` 中断，未写失败、未取消任务** |

### 0.2 根因（4 项核心缺陷）

1. **对话任务未下发技能**：`_run_conversation` 队列分支 `enqueue_task(..., skill="")` → `_pack_skill_dir` 未触发 → `skill_files` 为空 → worker 的 `DEVOPS_SKILLS_DIR` 无 event-dialogue；
2. **prompt 硬编码服务端技能路径**：`build_conversation_prompt`（及 `build_prompt`/`build_chat_prompt`）输出 `Skill 文件：{SKILLS_DIR}/{skill}/SKILL.md`（服务端绝对路径），**未用 `{{SKILL_DIR}}` 占位符**；而 worker 只替换 `{{SKILL_DIR}}`（`agent.py:75` / `service.py:515`）→ 即使技能随任务下发，agent 也读不到物化目录，落到 worker 本地仓库路径 → `event-dialogue` 在 worker 上不存在 → read 失败 → opencode 循环后挂起；
3. **服务端超时静默无回显**：`wait_task_terminal` 30 分钟超时后抛 `TimeoutError`，SSE 连接直接断开、线程不写失败记录、任务不取消 → 用户 30 分钟无反馈，且最终线程只有用户消息；
4. **worker 看门狗终止太晚**：静默硬上限 30 分钟（`AGENT_SILENT_EXTEND_CAP` 默认 `3×max_exec_seconds`），比服务端 30 分钟超时还晚 → 任务悬挂、无人消费（服务端早已放弃）。

> 附带（次要）：对话队列分支未传 `issue_session_id` → D9 串行槽未与本 issue 会话绑定。

---

## 一、目标

- 尊重 agent 模式（对话随绑定 runtime 路由，复用 issue 会话）**不变**；
- 让对话任务在远端 worker 上能真正读到 event-dialogue 技能并正常执行；
- 超时/失败必须有**明确回显**（线程 + 前端），不再 30 分钟静默；
- 缩短悬挂任务清理时间，避免永久悬挂。

---

## 二、修复方案

### Fix A：对话任务下发 event-dialogue 技能

`_run_conversation`（`src/api/routes/events.py`）队列分支，`enqueue_task` 补：

```python
from src.config import SKILLS_DIR
tid = enqueue_task(
    agent_session_id=agent_sid, runtime_id=runtime_id, prompt=prompt, cwd=ws,
    runtime_session_id=oc_session_id,
    issue_session_id=issue_sess_id,          # Fix E
    event_type="event_conversation",
    repo_key=repo_key, agent_type=agent_type,
    skill="event-dialogue",                  # Fix A：触发 _pack_skill_dir 打包
    skill_path=os.path.join(SKILLS_DIR, "event-dialogue", "SKILL.md"),
    account=repository, repo_gitcode_token=bot_token,
    issue_id=issue_id or "#0",
)
```

- `enqueue_task` 内 `_pack_skill_dir("event-dialogue", skill_path)` 会把 `skills/event-dialogue/` 打包进 `skill_files` → worker 物化到 `{cwd}/skills/event-dialogue/` 并设 `DEVOPS_SKILLS_DIR`。
- （可选）把 `event-dialogue` 注册进 `skill` 表（官方技能，`agent_type=''` 通用），便于 `resolve_skill_id` 与技能库展示。

### Fix B：prompt 技能路径改用 `{{SKILL_DIR}}` 占位符

`src/prompt_builder.py` 三处技能路径改为占位符（worker 已支持替换 `{{SKILL_DIR}}` → 物化目录）：

- `build_conversation_prompt`（现 `skill_md = os.path.join(SKILLS_DIR, "event-dialogue", "SKILL.md")`）→
  `Skill 文件：{{SKILL_DIR}}/event-dialogue/SKILL.md`；
- `build_prompt`（`Skill 文件：{_sp}`，`_sp = skill_path or {SKILLS_DIR}/{skills[0]}/SKILL.md`）→ 当 `skill_path` 缺省时输出 `{{SKILL_DIR}}/{skills[0]}/SKILL.md`（显式传 skill_path 的场景保留原值，但 pipeline 入队侧同样改为占位符）；
- `build_chat_prompt`（`build_chat_role_definition` 附近）同改。

> 收益：技能**内容随任务下发即生效**，agent 从 `DEVOPS_SKILLS_DIR` 读取，与 worker 仓库版本解耦——普通 pipeline 任务也受益。

### Fix C：对话等待超时/失败要有回显与清理（服务端）

`_run_conversation` 队列分支包 `wait_task_terminal`：

```python
from src.runtime.service import enqueue_task, wait_task_terminal, cancel_task
try:
    task = await wait_task_terminal(tid, timeout=...)          # 可缩短对话专用超时
    final_text = task.get("result_text") or ""
    # usage.session_id 回写…
except TimeoutError:
    cancel_task(tid)                                            # 清理悬挂任务
    final_text = "（对话执行超时，请稍后重试）"
except Exception as e:
    cancel_task(tid)
    final_text = f"（对话执行失败：{e}）"
```

- 超时/异常必走到末尾 `_add_thread_message(..., "agent", final_text)` + `yield ("done", {reply, messages})` → 前端结束"思考中"、线程留痕。

### Fix D：worker 静默看门狗尽早失败（缩短 + 可配）

`src/agent/executor.py` 静默逻辑（`_silent_extend_cap = AGENT_SILENT_EXTEND_CAP or max_exec_seconds*3`）：

- 改为可配且更短的硬上限：新增 `AGENT_SILENT_LIMIT`（默认如 `max_exec_seconds` 或 `15min`），`_silent_extend_cap = float(os.environ.get("AGENT_SILENT_LIMIT", str(max_exec_seconds)))`；
- 保留"进程存活时按 `max_exec_seconds` 续期"的长命令保护，但**累计上限显著缩短**，超限即 `_terminate_proc` + 让 worker `fail(tid)` 上报；
- 目的：技能读不到导致的挂起在 **10–15 分钟内**失败，服务端能及时拿到终态并回显（Fix C 是首要保障，Fix D 兜底清理）。

### Fix E：对话队列分支补 `issue_session_id`（含在 Fix A 代码中）

`enqueue_task(..., issue_session_id=issue_sess_id, ...)`，使 D9 串行槽按本 issue 会话绑定，与会话复用语义一致。

### Fix F（部署/运维）：worker 需同步代码

- Fix B/D 的代码在 **worker 进程**运行（`agent.py` / `executor.py` / `prompt_builder.py` 由服务端打包 prompt，但 executor 看门狗在 worker 侧）：worker 的 `/opt/grape` 需 `git checkout new-version2 && git pull` 并重启 `src.runtime.agent`，否则 worker 跑旧逻辑；
- 注意：技能**内容**随任务下发后，worker 不再需要仓库里有 `skills/event-dialogue/`；但**代码**修复需 worker 同步。

---

## 三、改动点清单

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `src/api/routes/events.py` | `_run_conversation` 队列分支：`skill='event-dialogue'` + `skill_path` + `issue_session_id` + `wait_task_terminal` try/except 超时回显/取消 | Fix A/C/E |
| 2 | `src/prompt_builder.py` | `build_conversation_prompt`/`build_prompt`/`build_chat_prompt` 技能路径改 `{{SKILL_DIR}}` | Fix B |
| 3 | `src/agent/executor.py` | 静默硬上限缩短 + 可配（`AGENT_SILENT_LIMIT`） | Fix D |
| 4 | `src/api/db_setup.py`（可选） | event-dialogue 注册进 `skill` 表（官方/通用） | Fix A 配套 |
| 5 | `tests/test_event_conversation.py` | 新增：对话任务含 `skill_files`；prompt 含 `{{SKILL_DIR}}`；超时 → 线程失败消息 + 任务 cancelled | 覆盖 |

---

## 四、验证方案

### 4.1 单元测试

| # | 用例 | 断言 |
|---|---|---|
| T1 | 对话队列任务技能下发 | `enqueue_task(..., skill='event-dialogue')` → 任务 `skill_files` 含 `event-dialogue/SKILL.md` |
| T2 | 对话 prompt 占位符 | `build_conversation_prompt` 输出含 `{{SKILL_DIR}}/event-dialogue/SKILL.md`，不含服务端绝对路径 |
| T3 | 等待超时回显 | mock `wait_task_terminal` 抛 `TimeoutError` → `event_thread` 写 agent 失败消息 + 任务被 `cancel_task` |
| T4 | 普通任务技能仍可读 | `build_prompt` 输出 `{{SKILL_DIR}}/{skill}/SKILL.md`，worker 物化后可读（worker 侧集成） |

### 4.2 端到端（生产）

1. 对绑定远端 worker 的 agent 发起对话 → **应在数秒~数分钟内得到回复或明确失败回显**，不再 30 分钟静默；
2. worker 侧验证 opencode 能 `read {{SKILL_DIR}}/event-dialogue/SKILL.md`（物化目录存在）；
3. 悬挂任务清理：`t_7bee703ea9f1`、`t_59f01bd3cf9c` 手工置 failed/cancelled 后再验证新逻辑。

### 4.3 回归

- `test_event_conversation.py` 全量；`test_runtime*.py`（enqueue/claim 兼容）、`test_tasks.py`、`test_pipeline.py`；
- 普通 pipeline 任务在远端 worker 上仍能读技能（Fix B 影响所有技能路径）。

---

## 五、影响与风险

| 项 | 说明 |
|---|---|
| 行为变化 | 对话在远端 worker 正常执行；超时/失败有回显；悬挂任务更快清理 |
| 普通任务 | Fix B 改所有技能路径为占位符——worker 一律从物化目录读，**不再依赖 worker 仓库技能文件**（正向，需回归确认长路径场景） |
| 长命令保护 | Fix D 缩短静默上限，可能误杀超长 bash 命令（> 上限）——保留可配 `AGENT_SILENT_LIMIT` 与 `max_exec_seconds` 续期，需按线上任务时长权衡 |
| 部署顺序 | 服务端 + worker **需同时升级**（worker 代码不同步则 Fix B/D 不生效） |
| 会话 | 复用 issue 会话不变（Fix E 只是补绑定，不改续接语义） |

---

## 六、附带观察（非本次范围）

- opencode 在"read 不存在的技能文件"后 `exiting loop` 但进程不退出——疑似 opencode 端行为，可在 worker 侧跟进（升级 opencode 1.18.15→1.18.16 或加 `--exit` 语义）；
- 交互式对话的超时阈值（30 分钟对 Chat 太长）可下调（如 5–10 分钟），与 Fix C/D 一并配置。

---

*本文为 Event 对话远端执行修复方案，待评审后实施。*
