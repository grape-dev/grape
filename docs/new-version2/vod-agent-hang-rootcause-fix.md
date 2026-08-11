# VOD/Agent 任务挂起超时根因分析与修复方案

> 版本：v1.0
> 日期：2026-08-11
> 状态：待评审
> 触发背景：gitcode-pr-assistant Issue #6 的 VOD/Issue/Coding/QA 链 475/476/477/478/479 全部超时被杀（exit=-9），但评论均正常发出；Issue #5 曾正常完成。

---

## 〇、现象回顾

| seq | agent | events.status | task 状态 | 现象 |
|---|---|---|---|---|
| 475 | vod (issue_opened #6) | failed | exit=-9, 600s | 评论正常发出（08:55:30），task 600s 超时被杀 |
| 476 | vod (issue_comment #6) | failed | exit=-9, 600s | 同上 |
| 477 | issue | failed | exit=-9, 600s | Spec 评论发出，仍超时 |
| 478 | coding | failed | exit=-9, 600s | 同上 |
| 479 | qa | running→failed | exit=-9, 600s | 同上 |
| 459/467（#5，历史成功） | vod | completed exit=0 | 10s~2min 正常 | 正常 |

**共性**：agent 干活了（GitCode 评论发出），但 **opencode 子进程 stdout 全程 0 行输出（`total_lines=0`）且不退出** → executor 600s 无输出强杀 → done(exit=-9) → worker 报 complete(-9)。

---

## 一、根因（三层，全部实证）

### 根因 1（主因）：多 Issue 复用了同一个 opencode session，导致 cwd/上下文错位

**证据（`issue_agent_session` 表）**：

| Issue 会话 | agent | runtime_session_id |
|---|---|---|
| #5 vod（00d09d21） | vod | **ses_014583f35ffeuOGkxfj2yZn6Rd** |
| #6 vod（070114d8） | vod | **ses_014583f35ffeuOGkxfj2yZn6Rd** ← 相同！ |
| #5/#6 issue | issue | 均 **ses_013d747f2ffe...** |
| #5/#6 coding | coding | 均 **ses_013d600f8ffe...** |
| #5/#6 qa | qa | 均 **ses_013cdb6c4ffe...** |

**机制**：`get_or_create_issue_session`（`src/api/session_store.py:282-298`）创建新 Issue 会话时，用 `_seed = devops_agent_session.runtime_session_id`（**repo 级会话指针**）作为初始 `runtime_session_id`。

- #5 首次创建 vod Issue 会话时，repo 级 `devops_agent_session.runtime_session_id` 指向 `ses_014583f...`（该 session 实际是 #5 创建的）；
- #6 创建 vod Issue 会话时，**仍取同一个 repo 级指针 `ses_014583f...`** → #6 拿到 #5 的 session；
- #6 任务 `opencode --session ses_014583f...` 续跑 → opencode 恢复 #5 的上下文与 **cwd（tracking 指向 `.../vod/issues/5`）**；
- 但 prompt/cwd 参数是 #6（`cwd=.../vod/issues/6`）→ **opencode 内部 cwd 与任务 cwd 不一致** → 上下文错乱。

**worker opencode 日志实证**（475）：
```
00:53:09 "creating instance" directory=.../vod/issues/6   ← 任务 cwd
00:53:09 "creating instance" directory=.../vod/issues/5   ← 续跑恢复 #5
00:53:09 tracking cwd=.../vod/issues/5                    ← opencode 实际在 #5 目录
00:53:18 curl issues/6 ...                                ← agent 却读 #6 API（prompt 是 #6）
00:55:30 curl POST issues/6/comments ...                  ← 发评论成功
00:55:31 loop step=4                                      ← 评论后继续循环不退出
```

### 根因 2：agent 做完业务动作（发评论）后不退出

- opencode `run` 模式下，如果 agent 不显式结束会话/退出，进程会继续 loop；
- 评论发出 ≠ 任务完成：475 发评论后 `loop step=4+`，一直挂到 600s 超时；
- **缺「完成业务即结束任务」的行为约束**。

### 根因 3：opencode stdout 无 NDJSON 输出（续跑模式）

- 续接旧 session 时，opencode 可能走交互/TUI 输出路径，`--format json` 的 NDJSON **未进入 stdout 管道**；
- executor `_stdout_reader` 收不到 → `total_lines=0` → `_last_output` 不更新 → 600s 误判超时。

---

## 二、为什么 #5 正常、#6 失败

- #5 的 vod Issue 会话首次创建时，`_seed` 从 repo 级拿到的 `ses_014583f...` **恰好是 #5 自己** → 上下文一致 → 正常；
- #6 复用同一 `_seed` → 上下文/cwd 指向 #5 → 错乱。
- **问题本质是 repo 级 session 指针被多个 Issue 共享**，第一个 Issue 正常、后续 Issue 全部复用错乱。

---

## 三、修复方案

### 方案 A（核心，必做）：每个 Issue 拥有完全独立的 opencode session

**根本原则**：一个 Issue 一个独立 session，Issue 之间零共享；session 只允许「同一个 Issue 内」的多次事件续接。

**问题**：`get_or_create_issue_session` 用 repo 级 `runtime_session_id` 作为新 Issue 的种子 → 不同 Issue 拿到同一 session，cwd/上下文错位。

**修改**（`src/api/session_store.py:282-298`）：
- **新 Issue 首次创建时 `runtime_session_id` 必须为空**（不继承 repo 级、也不继承任何其他 Issue 的 session 指针），`_seed` 置空；
- 第一次执行由 opencode **生成全新 session**（无 `--session` 新跑），`complete_task`/`_execute_agent` 已会把新 session 回写到该 Issue 自己的 `issue_agent_session.runtime_session_id`；
- **同一 Issue 的后续事件**继续复用该 Issue 自己的 session（`get_or_create_issue_session` 按 `(repo_key, agent_type, issue_id)` 命中既有行），实现「同 Issue 串行续接、跨 Issue 完全隔离」；
- repo 级 `devops_agent_session.runtime_session_id` 仅用于**无关联 Issue 的兜底**（push/#0），**绝不作**任何 Issue 会话的种子。

> 校验点：确认 `_execute_agent`（event_pipeline.py:557-575）与 `_enqueue_pipeline_task`（event_pipeline.py:480-490）里 `get_or_create_issue_session` 的 `oc_session_id` 取值，确保新 Issue 会话首次为空时能正确走"无 session 新跑"，且执行完成后的新 session 正确回写到**该 Issue 自己的行**。

### 方案 B（必要）：agent「完成即结束」行为约束（skill 层）

**问题**：agent 发完评论后 opencode 继续 loop 不退出。

**修改**（skills/vod/SKILL.md + 通用 prompt_builder 的 A2A/收尾指令）：
- 在 `prompt_builder.py` 全局注入段（处理记录输出要求之后）增加：
  ```
  ## 任务结束
  完成所有业务动作（发评论/打标签/交办等）后，输出最终 JSON 块并结束任务，
  不要继续思考或等待用户输入；任务以你的最终输出为终点。
  ```
- vod/issue/coding/qa 各 SKILL.md 末尾补一句同样的「完成后立即结束」。

### 方案 C（兜底防御）：executor 对「无 done」的收尾兜底

**问题**：即使 agent 该退出，opencode 仍可能不产出 done，需要 executor 侧兜底。但兜底的**完成判定必须可靠**，不能误判。

**⚠️ 原判据不准确（已否决）**：~~「已收集到 tool_use/text 但之后长时间无输出 → 主动产出 done(exit=0)」~~
- `tool_use`（如 curl 大文件 / git clone / 编译）可能自身耗时长，期间无 text 输出是**正常执行中**，不等同于完成；
- `tool_use` 可能是中间步骤，后续还有多轮处理，静默不代表收尾；
- 主动标 `exit=0` 会**掩盖真实卡死**，让下游（A2A 接力、pipeline）误以为成功，风险大于收益。

**修正方案**（`src/agent/executor.py`）：

1. **保留「进程真正退出（EOF + returncode）」作为唯一完成依据**：opencode 正常完成时 `run` 会退出并产 done；不修改此判定，避免启发式误判。
2. **超时收尾改为「先礼后兵」**：达到 `max_exec_seconds` 静默阈值时，**先 SIGTERM（优雅终止）**并给短暂收尾窗口（如 10s），期间若 agent 输出最终 JSON/退出则正常完成；仍无输出再 SIGKILL（现状 `_terminate_proc` 已有 SIGTERM→SIGKILL 链，确认超时分支是否充分用了 grace 窗口）。
3. **无 done 时标 failed，不标 completed**：即便进程被回收，只要没收到 done，任务状态就是 failed（现状 `_run_task` 已如此），避免「已产出但未收尾」被误记成功。
4. **可选增强：静默期主动提示**（需确认 opencode `run` 是否支持向运行中会话注入消息）：检测到「已有 tool_use 产出、但静默超过较短阈值（如 120s）」时，向 opencode 发送「请立即输出最终结论并结束」的提示，给 agent 一个显式收尾机会，而非直接杀。
5. **防御日志**：任何超时回收都输出 agent_label + 已产出事件数（tool_use/text 计数），便于区分「真卡死」与「长任务被误杀」。

> 核心原则：**执行器只能以「进程退出 + done」判定成功，不以静默时长判定成功**；兜底仅保证进程不无限挂起，状态如实标 failed。

### 方案 D（可选）：cwd 一致性防御

- `_execute_agent`/`_enqueue_pipeline_task` 入队时，若 task 的 `runtime_session_id` 属于其他 Issue，日志告警（防回归）；
- opencode 启动后校验实际 cwd 与任务 cwd 一致（worker 侧）。

---

## 四、改动点清单

| # | 文件 | 改动 | 方案 |
|---|---|---|---|
| 1 | `src/api/session_store.py:282-298` | `get_or_create_issue_session` 的 `_seed` 置空（不继承 repo 级 session） | A |
| 2 | `src/prompt_builder.py`（全局收尾段后） | 增加「任务结束」指令 | B |
| 3 | `skills/vod/SKILL.md` + 各 skill | 补「完成后立即结束」 | B |
| 4 | `src/agent/executor.py`（超时/终止逻辑） | 超时收尾走 SIGTERM→短窗口→SIGKILL 链；无 done 一律 failed；可选静默期主动提示 | C |
| 5 | `src/api/event_pipeline.py`（入队/执行侧） | cwd/session 归属校验日志 | D |

---

## 五、验证方案

1. **复现验证**：重建 #6 类似场景（两个 Issue 相继触发 vod）：
   - 修复前：Issue #2 复用 #1 的 session → opencode tracking 到 #1 目录 → 超时；
   - 修复后：Issue #2 独立新 session → cwd 正确 → 正常完成 exit=0。
2. **回归**：既有 runtime/pipeline/executor 测试全量通过；
3. **数据检查**：`issue_agent_session` 中 #5/#6 的 `runtime_session_id` 不再相同；每个 Issue 会话独立。
4. **方案 C 防误判验证**：构造「tool_use 后长任务静默」用例（如 git clone 大仓库），确认不会在任务完成前被误判 done/failed；构造「真卡死」用例，确认最终标 failed 而非 completed。

---

## 五·1 测试用例设计

> 新增测试文件：`tests/test_issue_session_isolation.py`（方案 A，session 隔离）与 `tests/test_executor_hang_fallback.py`（方案 C，超时兜底防误判）。
> 隔离写法：`_clean()` 清 `agent_tasks/events/issue_agent_session/devops_agent_session/issue_state`，参照 `test_m2_execution_mode.py` 的 `DEVOPS_EXECUTION_MODE` 切换模式。

### 5.1.1 方案 A：Issue 会话隔离（`tests/test_issue_session_isolation.py`）

| # | 用例 | 步骤 | 断言 |
|---|---|---|---|
| A1 | **新 Issue 不继承 repo 级 session** | 预置 repo 级 `devops_agent_session.runtime_session_id='ses_repo_xxx'` → 调用 `get_or_create_issue_session(repo, 'vod', '#2')` | 返回的 `runtime_session_id` 为空（或 ≠ `ses_repo_xxx`）；表中该 Issue 行 `runtime_session_id` 为空 |
| A2 | **两个 Issue 会话指针相互独立** | 依次 `get_or_create_issue_session(repo,'vod','#1')` 与 `#2` → 各调 `update_issue_session` 写入不同 session id | 两行 `runtime_session_id` 不同，互不影响 |
| A3 | **已有 Issue 会话复用（不重复建）** | 同 `(repo,agent,issue_id)` 二次调用 | 返回同一 id，`new=False` |
| A4 | **首次无 session 可正常新跑** | 构造空 `runtime_session_id` 的任务 → `_execute_agent` 路径 | opencode 以无 session 启动（`stream_agent(session_id='')`），不报错 |
| A5 | **repo 级兜底仅用于无关联 Issue** | 无 issue_id（push/#0）事件 → 走 repo 级 session | 行为不变，仍用 `devops_agent_session.runtime_session_id` |

### 5.1.2 方案 C：executor 超时兜底防误判（`tests/test_executor_hang_fallback.py`）

| # | 用例 | 步骤 | 断言 |
|---|---|---|---|
| C1 | **长任务静默不误判完成** | mock backend：先 yield `tool_use`，再 sleep 120s 无输出（模拟 git clone），最后正常 `done(exit=0)` | 全程不提前产出 done；进程不被提前终止；最终 done 正常，`total_lines>0` 且已产出事件数保留 |
| C2 | **真卡死最终标 failed** | mock backend：yield 少量 `text` 后永久静默（不退出） | 超时后 SIGTERM→窗口→SIGKILL；最终 done 的 `exit_code=-9`，`errors` 含"no output ... killed" |
| C3 | **无 done 一律 failed** | 进程被回收但从未 yield done | 上层 `_run_task` 走 `fail_task("no done event")`，`agent_tasks`/`events` 状态为 failed（非 completed） |
| C4 | **SIGTERM 优先于 SIGKILL** | 进程不响应时验证终止链 | 先发 SIGTERM，等待 grace 窗口；仍存活才 SIGKILL（mock `proc.poll` 断言） |
| C5 | **超时防御日志含产出统计** | 超时回收时 | 日志包含 agent_label + tool_use/text 事件计数，可区分卡死 vs 长任务 |

### 5.1.3 方案 B：prompt「任务结束」指令（可并入 `test_prompt_builder.py`）

| # | 用例 | 步骤 | 断言 |
|---|---|---|---|
| B1 | **prompt 含结束指令** | `build_prompt(...)` 输出 | 末尾含「任务结束」/「完成所有业务动作...结束任务」段落 |
| B2 | **不破坏既有 JSON 收尾契约** | 各 skill prompt | 既有「处理记录输出要求」JSON 块仍存在，顺序在结束指令之前 |

### 5.1.4 回归范围

- 既有 `tests/test_m2_execution_mode.py`、`test_runtime.py`、`test_runtime_protocol.py`、`test_agent_executor.py`、`test_pipeline.py`、`test_prompt_builder.py` 全量通过；
- A1/A5 确认方案 A 不影响「无关联 Issue 走 repo 级 session」的既有语义；
- C1/C3 确认方案 C 不改变「进程退出 + done」的成功判定语义。

---

## 六、风险

| 项 | 说明 |
|---|---|
| Issue 会话失去历史上下文 | 方案 A 使新 Issue 不继承 repo 级 session——符合预期（Issue 上下文本就该独立），且完成时新 session 会回写 |
| 首次执行无 session | opencode 支持无 session 新跑（executor.py:116-119 已有"会话失效自动重建"逻辑），无风险 |
| prompt 加「结束」指令影响 | 各 skill 已输出 JSON 块收尾，新增指令强化该行为，不改变既有输出契约 |
| 方案 C 兜底误判 | 已否决「有输出即成功」启发式；完成判定仅以「进程退出 + done」为准，无 done 一律 failed，杜绝把卡死记成成功 |

---

## 七、结论

- **主因**：每个 Issue 应拥有完全独立的 session，但当前新 Issue 会话错误继承了 repo 级 session 指针，导致多 Issue 共用同一 opencode session，cwd/上下文错位；
- **次因**：agent 完成业务后不退出 + opencode 续跑模式 stdout 无 NDJSON；
- **修复优先级**：方案 A（每 Issue 独立 session，根除主因）→ 方案 B（agent 结束约束）→ 方案 C（executor 兜底）→ 方案 D（防御校验）。

> ⚠️ **补充（2026-08-11）**：本方案（commit 3c86294）修复的是「session 复用 → cwd 错位」。后续发现的「多任务并发时新任务卡 `dispatched`、worker 日志/心跳停更」是**另一独立缺陷**——worker 侧 `_execute_task` 用同步生成器迭代阻塞 asyncio 事件循环（`stream_runtime` 同步 for + 仅 batch≥50 才 await），导致 claim/heartbeat 无法运行。详见 `worker-event-loop-block-rootcause.md`。两者根因独立，勿混淆。
