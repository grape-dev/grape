# Coding Agent git Worktree 与技能落地目录冲突根因分析（coding-worktree-skill-collision.md）

> 版本：v1.0
> 日期：2026-08-16
> 状态：待评审
> 触发背景：#711（hello-html2，coding，#2）failed，`exit_code=-15`（SIGTERM）。
> 表象是 opencode 卡在 `external_directory` 权限询问，根因是 **`ensure_issue_worktree` 因目标目录
> 已被技能物化占住而失败 → coding agent 拿不到 git 基线 → 自行在 cwd 外新建 worktree → 权限卡死**。

---

## 〇、现象摘要

| 项 | 值 |
|---|---|
| 事件 | #711 `a2a_event`，agent=coding，issue=#2（hello-html2） |
| task | `t_233417eedaf8`，status=failed，exit_code=-15（SIGTERM） |
| cwd | `{WORKSPACE_DIR}/Jason7602/hello-html2/issues/2/coding/` |
| 时间 | started 07:35:01，completed 07:36:55（约 114s） |
| 直接原因 | opencode 卡 `external_directory` 权限询问（`issues/2/work/*`），无人应答 → SIGTERM → failed |

---

## 一、证据链（逐步定位）

### 1.1 task 终态：SIGTERM（-15）

```
opencode done: exit=-15 text_len=0 stderr=''
```
- `-15` = SIGTERM；`text_len=0` 说明卡死期间无输出；
- worker 侧**无看门狗超时日志**（默认 600s，实际 114s）→ 非 executor 静默看门狗；
- 无 systemd 重启、无 OOM、无 `killed opencode` 记录 → 非 worker 主动 kill；
- 结论：opencode 进程被外部 SIGTERM，且卡死在权限询问。

### 1.2 opencode 权限询问卡死（直接失败点）

```
07:36:19 evaluated permission=external_directory pattern=.../issues/2/work/* action=ask
07:36:19 asking id=per_007c89053001Ugow4r652uwp0t permission=external_directory patterns=[.../issues/2/work/*]
07:36:26 再次 asking id=per_007c8aa36001qXtN8t1HeMVNyz ...（重复询问）
```
- coding agent 在 Step 15 `git worktree add .../issues/2/work feat/issue-2` 创建新 worktree；
- **`issues/2/work` 位于 opencode 项目根（`issues/2/coding`）之外** → 判外部目录 → 权限询问 → 挂起。

### 1.3 为什么 coding 要在 cwd 外建 worktree：`issues/2/coding` 不是 git 仓库

```
issues/2/coding/ 只含 .skills（技能物化产物），无 .git
codebase (bare)
issues/2/work [feat/issue-2]        ← coding 自己建的，不是 issues/2/coding
```
- SKILL.md:43-44 告知 agent"已在当前工作目录准备好 git worktree，可直接执行 git 命令；
  若目录非 git 仓库（降级）改用 GitCode contents API"；
- **实际 `issues/2/coding` 非 git 仓库** → agent 未走降级路径，而是自行 `git worktree add` 到 `issues/2/work`。

### 1.4 根因：`ensure_issue_worktree` 因"目录已存在"失败

worker 日志（07:35:01）：
```
repogit worktree add failed gitcode:Jason7602/hello-html2:
  fatal: '/opt/grape-workspaces/Jason7602/hello-html2/issues/2/coding' already exists
```

`repogit.ensure_issue_worktree`（src/runtime/repogit.py:130）：
1. `issue_dir = issue_dir_for(repo, "coding", 2)` = `issues/2/coding`；
2. 检查 `issue_dir/.git` 存在性（:139-142）——**目录存在但无 .git**，判定"未就绪"；
3. 执行 `git worktree add .../issues/2/coding -b feat/issue-2` → **git 报 "already exists"**
   （目录非空已存在，git worktree 不覆盖非空目录）→ 返回 None；
4. **worktree 创建失败**，`GIT_ASKPASS` 未设置。

---

## 二、根因链

```
[worker] _execute_task
  ├─ ① os.makedirs(cwd) + 落地技能到 cwd/.skills     ← 提前占住 cwd 目录（含 .skills）
  ├─ ② ensure_issue_worktree(coding, #2)
  │     ├─ issue_dir = issues/2/coding（已存在，无 .git）
  │     ├─ git worktree add → fatal: already exists   ← 失败
  │     └─ return None（worktree 未建，GIT_ASKPASS 未设）
  └─ ③ opencode 在 cwd（无 git 基线的空目录）执行
        └─ coding 自行 git worktree add issues/2/work   ← 建在 cwd 外
              └─ external_directory 权限询问 → 卡死 → SIGTERM → failed
```

**核心缺陷**：`_execute_task` 的**执行顺序**——先物化技能占住 cwd，再尝试 git worktree，导致 worktree 无法建在 cwd 根。

---

## 三、与 #701/#704 的关系

| 事件 | 根因 | 是否同一问题 |
|---|---|---|
| #701 | onboarding 旧 session 项目根 ≠ cwd → external_directory | session/权限，已根治 |
| #704 | worker 事件循环被同步生成器阻塞 → claim 停摆 | 执行模型，已修复 |
| #711 | worktree 建在 cwd 外 → external_directory | **新的独立问题**（worktree 路径越界） |

三者都表现为 `external_directory` 权限询问，但触发源不同：#711 是 **coding 自行在项目根外建 worktree**，属 git worktree 交付链路缺陷。

---

## 四、修复方向（待实施）

### 4.0 触发范围：仅 git agent（coding/qa/sre），非固定动作

`ensure_issue_worktree` **不是每个 agent 的固定动作**，仅满足全部条件才触发：

```python
# src/runtime/agent.py:215（worker 端 _execute_task）
if repogit.is_git_agent(task.get("agent_type") or "") and _num and task.get("issue_session_id"):
    _wt = repogit.ensure_issue_worktree(...)
```

三个条件缺一不可：
1. `is_git_agent(agent_type)` —— 仅 `coding/qa/sre`（`_GIT_AGENTS`，repogit.py:36-38）；
2. `_num` —— 有关联 issue（`issue_num_from` 非空）；
3. `issue_session_id` —— 走 Issue 级执行会话。

| agent | is_git_agent | ensure_issue_worktree | 是否受影响 |
|---|---|---|---|
| coding | ✅ | ✅ 触发 | 是（worktree 冲突）|
| qa | ✅ | ✅ 触发 | 是 |
| sre | ✅ | ✅ 触发 | 是 |
| vod | ❌ | ❌ 不触发 | 否（cwd 普通目录）|
| issue | ❌ | ❌ 不触发 | 否 |

> `ensure_issue_worktree` 内部还有一道闸（repogit.py:132 `if not is_git_agent: return None`）防御误传。
> **修复方案 A 只影响 coding/qa/sre**；vod/issue 不触发 worktree，技能落地顺序不变，完全不受影响。

### 4.1 方案 A（推荐）：调整 `_execute_task` 顺序——先建 worktree，再落地技能

```
_execute_task:
  1. ensure_issue_worktree(coding, #n)   ← 先建 worktree（cwd 根成为 git worktree）
  2. 再落地 .skills / .project-info / .knowledge 到 cwd
```
- worktree 建好后再物化技能，cwd 根已有 `.git`，`git worktree add` 不复现 "already exists"；
- 技能落在 worktree 工作树内（`.skills` 子目录），不影响 git 操作。

### 4.2 方案 B：`ensure_issue_worktree` 容忍"目录存在但非 worktree"

```
if os.path.isdir(issue_dir) and 无 .git:
    # 目录被 .skills 占住 → 用 --force 强制建 worktree，或先清空再建
    git worktree add --force issue_dir -b feat/issue-{n} <base>
```
- `--force` 允许在已存在（非空）目录上建 worktree；
- 但需评估 `.skills` 等物化产物是否会被 worktree 检出覆盖（应避免覆盖技能）。

### 4.3 方案 C：worktree 路径与技能落地分离

- worktree 建在 cwd 根，技能落地在 `cwd/.skills`（子目录），两者天然共存；
- 本质与方案 A 等价，但强调目录布局约定。

### 补充：opencode 权限问题（#701 遗留矛盾）

- worker 用 `--dangerously-skip-permissions` 启动（`opencode.py:12`），但生产仍出现权限询问（#701/#711 均实证）；
- **需核实生产 opencode 实际启动命令与 `opencode.jsonc` permissions 配置**——`skip-permissions` 是否真正生效；
- 若未生效，即使修复 worktree 路径，其他越界访问仍可能卡权限。

---

## 五、验证方案（实施后）

### 5.1 测试用例表

| 用例 | 文件 | 步骤 | 断言 |
|---|---|---|---|
| worktree 就绪 | `tests/test_repogit.py`（扩展） | 触发 coding 新开发任务 | `issues/{n}/coding/` 为有效 git worktree（含 `.git` + 分支 `feat/issue-{n}`），`git status` 可用 |
| 技能不覆盖 | `tests/test_repogit.py`（扩展） | worktree 建好后 `.skills/` 正常物化 | 技能文件可读，不触发 external_directory |
| **先 worktree 后技能（顺序看护）** | `tests/test_agent_executor.py`（扩展） | mock `_execute_task`：cwd 已含 `.skills` 时调用 `ensure_issue_worktree` | **worktree 创建在前**（先 `git worktree add` 成功，再物化技能）；修复前该顺序颠倒导致 "already exists" |
| 无权限卡死 | `tests/test_event_loop_block.py` 或集成 | coding 执行长命令 + 访问 cwd 内路径 | 无 `asking external_directory`，任务正常 completed |
| **触发范围（仅 git agent）** | `tests/test_repogit.py`（新增） | 遍历 agent 类型调用 `ensure_issue_worktree` | 仅 `coding/qa/sre` 返回 worktree 路径；`vod/issue` 返回 None（`is_git_agent` 守卫）|
| **cwd 占用不阻塞** | `tests/test_repogit.py`（新增，回归） | 目标目录已存在且含 `.skills`（无 `.git`）时调用 `ensure_issue_worktree` | worktree 仍成功创建（方案 A 顺序后不触发该路径；方案 B `--force` 兜底）|
| 回归 | `tests/test_runtime_agent_concurrency.py` 等 | 触发 issue/qa/vod 任务 | 不受 worktree 调整影响（非 git agent 无 worktree） |

### 5.2 看护测试要点

- **触发范围守卫**：新增 `tests/test_repogit.py::test_is_git_agent_guard`——断言 `is_git_agent` 仅对 `coding/qa/sre` 返回 True，且 `ensure_issue_worktree` 对 `vod/issue` 直接返回 None（含内部 `if not is_git_agent: return None` 分支），防止未来误扩触发范围；
- **顺序看护**：`tests/test_agent_executor.py` 扩展，mock `_execute_task` 捕获调用顺序——断言 `ensure_issue_worktree` 在技能物化**之前**执行；若回归为先技能后 worktree（#711 场景），测试失败；
- **目录占用回归**：`tests/test_repogit.py` 新增——预置 `issues/{n}/coding/.skills` 占位目录，调用 `ensure_issue_worktree`，断言 worktree 仍成功（修复后不再 "already exists"）。

---

## 六、结论

- **#711 failed = coding agent 在项目根外新建 git worktree（`issues/2/work`）→ external_directory 权限询问卡死 → SIGTERM**；
- **上游根因 = `ensure_issue_worktree` 因 `issues/{n}/coding` 被技能物化占住而失败**（"目录已存在"），coding agent 拿不到 git 基线后自行越界建 worktree；
- **修复优先级**：方案 A（先 worktree 后技能）为最小且最稳改动；同时需核实 opencode `--dangerously-skip-permissions` 为何未生效（#701/#711 共同遗留）。

*本文为根因分析，待评审后实施。*
