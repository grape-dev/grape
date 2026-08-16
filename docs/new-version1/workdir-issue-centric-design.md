# 工作目录按 Issue 统一隔离改造方案（workdir-issue-centric-design.md）

> 版本：v0.2（设计稿，未实施）
> 日期：2026-08-15
> 状态：待评审
> 前置：`multi-issue-isolation-design.md`（现结构来源）/ `event_archive` 去前缀改造（`5ee7716`）
> 变更：v0.2 补充 onboarding 归并（废弃 `onboarding-agent`，落入 vod 的 `issues/#1/#2/vod/`），
>       新增权限根因回归测试（§五），修正无关联 Issue 表述（§二，`#0`=无关联，不含 onboarding）
> 结论摘要：将任务工作目录从 `{owner}/{repo}/{agent_type}/issues/{n}/` 统一迁移到
> `{owner}/{repo}/issues/{n}/{agent_type}/`，与事件归档 / issue 知识目录的 issue 优先
> 结构完全对齐，消除三套路径并存；onboarding 归并后同时根治 #701/#702 权限卡死根因。

---

## 一、背景与动机

### 1.1 现状：三套路径并存

当前同一 repo 下存在三种工作区路径结构：

| 用途 | 结构 | 位置 |
|---|---|---|
| 任务工作目录（cwd / git worktree） | `{owner}/{repo}/{agent_type}/issues/{n}/` | `event_pipeline.py:554`、`repogit.py:50` |
| issue 级知识文件 | `{owner}/{repo}/issues/{n}/knowledge.md` | `knowledge.py:39` |
| 事件归档 event.json | `{owner}/{repo}/issues/{n}/event.json` | `event_archive.py:58` |
| issue_agent 级知识文件 | `{owner}/{repo}/{agent_type}/issues/{n}/knowledge.md` | `knowledge.py:41` |

`{agent_type}/issues/{n}`（agent 优先）与 `issues/{n}/event.json`、`issues/{n}/knowledge.md`（issue 优先）
并存，且 `{agent_type}/issues/{n}` 这类 agent 名与 `issues` 子目录同名时（如 `issue/issues/2`）
产生明显误导。

### 1.2 现结构的由来

`multi-issue-isolation-design.md §3.4`（2026-08-09 拍板）规定：

```
{WORKSPACE_DIR}/{owner}/{repo}/
├── project-info/          # repo 全局（服务端权威）
├── issues/{n}/event.json  # Issue 档案
├── codebase/              # 共享 bare 只读基线
└── {agent_type}/
    └── issues/{n}/        # Issue 级工作目录（git worktree）
```

`{agent_type}/issues/{n}` 是「按 agent 分组」的早期布局惯性，配套 `issue_agent_session`
执行会话粒度（repo+agent+issue）实施。**git worktree 对路径无硬约束**——`git worktree add`
可指向任意目录，`{repo}/issues/{n}/{agent_type}/` 同样成立。

### 1.3 改造动机

1. **语义统一**：一个 Issue 的全部产出（event.json 档案 + 各 agent 工作目录 + 知识文件）
   聚合在 `issues/{n}/` 下，生命周期视角一致（Issue 关闭 → 清理整个 `issues/{n}/` 子树）。
2. **消除路径歧义**：`issue/issues/2` 这类 agent 名与 `issues` 同名误读不再存在。
3. **与既有 issue 优先结构对齐**：event_archive / knowledge 已按 `issues/{n}/` 布局，改造后
   cwd / worktree / knowledge / archive 四者统一。

---

## 二、目标结构

```
{WORKSPACE_DIR}/{owner}/{repo}/
├── project-info/               # repo 全局（不变）
├── codebase/                   # 共享 bare 只读基线（不变）
└── issues/
    └── {issue_n}/
        ├── event.json          # Issue 档案（不变）
        ├── knowledge.md        # issue 级知识（不变）
        └── {agent_type}/       # Issue 级工作目录（迁移至此）
            ├── .skills/        # 技能包落地
            ├── .project-info/  # project-info 落地
            └── .git            # git worktree（coding/qa/sre）
```

**无关联 Issue（push/#0）** 保持现有兼容路径不变：
`{owner}/{repo}/{agent_type}-agent`（现状）。

> **`#0` = 无关联 Issue**：`_issue_num_from()`（event_pipeline.py:538）对 `"#0"`/空返回 `""`，
> 走 `{agent_type}-agent` 兼容分支。该分支仅承载 push/#0 等无关联事件，**不含 onboarding**。

### 2.1 onboarding 归并：废弃 `onboarding-agent`，落入 vod 的 issue 目录

**现状问题**：onboarding 本质是 **vod agent 的一次特殊执行**（`trigger_onboarding`
onboarding.py:225-227 挂 `agent_type="vod"`；`_execute_agent_wrapper` :129 复用
`_execute_agent(..., "vod", ...)`；queue 模式 :163 `agent_type="vod"` 且建
`issue_agent_session("vod", #1/#2)`）。但 `_onboarding_workdir`（onboarding.py:47）
**硬编码独立目录** `{repo}/onboarding-agent`，刻意不走
`get_agent_workdir(repo_key, "vod", issue_id)` → 与真实 issue 工作目录错位。

**后果**（#701/#702 权限卡死根因）：onboarding 在 `onboarding-agent` 运行，其 opencode
session 项目根 = `onboarding-agent`；真实 issue #1/#2 任务复用同一
`issue_agent_session` 的 `runtime_session_id`，但 cwd = `vod/issues/{n}` →
**session 项目根 ≠ cwd** → `.skills` 判外部目录 → `external_directory` 权限询问卡死。

**改造**：
1. `_onboarding_workdir`（onboarding.py:44）废弃；direct/queue 两模式 cwd 统一走
   `get_agent_workdir(repo_key, "vod", "#{n}")`（n = ONBOARDING_TARGETS.index(target)+1）：
   - experience（#1）→ `{repo}/issues/1/vod/`
   - explore（#2）→ `{repo}/issues/2/vod/`
2. 结果文件 `onboarding-result-{target}.json` 随 cwd 落 `{repo}/issues/{n}/vod/`；
   读取方 `_finalize_onboarding` 经 `ONBOARDING_RESULT` env 跟随，无需硬编码改。
3. 历史遗留目录 `{repo}/onboarding-agent/`（生产已有 `.skills` + 结果文件）：
   过渡期保留、不再写；下个清理周期随 sweeper 收敛（或手工删除）。
4. **根治效果**：onboarding 与真实 issue #1/#2 共用 `issues/{n}/vod/` + 同一
   `issue_agent_session`，cwd 与 session 项目根**永远同目录**，权限错位结构性消除。

> 剩余时序场景：onboarding 先跑、真实 issue 后到并复用其 `runtime_session_id` 时，
> 两者 cwd 仍同为 `issues/1/vod/`（同目录）→ 不触发权限错位。配合 §4.3 清空存量指针
> 一并根治存量问题。

---

## 三、改动清单

### 3.1 任务工作目录（服务端 + worker）

| 文件 | 现状 | 改为 |
|---|---|---|
| `src/api/event_pipeline.py:554` `get_agent_workdir` | `{repo}/{agent}/issues/{n}` | `{repo}/issues/{n}/{agent}` |
| `src/api/event_pipeline.py:663` cwd/session 校验 `_expected_ws` | 同上 | 同上 |
| `src/runtime/repogit.py:50` `issue_dir_for` | 同上 | 同上 |

> `repogit.py:7` 模块 docstring 的 worktree 路径描述同步更新。
> `event_pipeline.py:547-548` 函数 docstring 同步更新。

### 3.2 知识文件（knowledge.py）

| 文件 | scope | 现状 | 改为 |
|---|---|---|---|
| `src/api/knowledge.py:41` | `issue_agent` | `{repo}/{agent}/issues/{n}/knowledge.md` | `{repo}/issues/{n}/{agent}/knowledge.md` |
| `src/api/knowledge.py:39` | `issue` | `{repo}/issues/{n}/knowledge.md` | **不变** |

> `issue_agent` 知识文件随 cwd 下移到 agent 工作目录内，读路径跟随新 cwd。

### 3.3 清理逻辑（cleanup.py）

| 文件 | 现状 | 改为 |
|---|---|---|
| `src/runtime/cleanup.py:59` `_zombie_issue_dirs` | 遍历 `{repo}/{agent}/issues/{n}` | 遍历 `{repo}/issues/{n}/{agent}`（`_GIT_AGENTS` 过滤不变） |

> 逻辑等价：仍只认 `{owner}/{repo}/issues/{n}/{agent}/` 结构，超 TTL 未活跃则清理。

### 3.4 事件归档 / issue 知识（不改）

- `src/api/event_archive.py:58` `{repo}/issues/{n}/event.json` — **保持**。
- `src/api/knowledge.py:39` `{repo}/issues/{n}/knowledge.md` — **保持**。

### 3.5 worker 侧一致性

worker 使用 `repogit.py`（`issue_dir_for` / `ensure_issue_worktree`）与 cwd 字段，
**随服务端同版本同步**即可，无独立适配代码。检查项：
- worker 端 `src/runtime/agent.py` 是否硬编码旧路径（当前未见，`cwd` 为服务端下发字符串）。
- `service._run_task`（服务端本地 worker）与 `agent.py::_execute_task`（远端）均走
  `ensure_repo_base + ensure_issue_worktree`，随 `repogit.py` 一并生效。

### 3.6 onboarding 归并（onboarding.py）

| 文件 | 现状 | 改为 |
|---|---|---|
| `src/api/onboarding.py:44` `_onboarding_workdir` | `{repo}/onboarding-agent` | **废弃**；direct/queue cwd 改走 `get_agent_workdir(repo_key, "vod", "#{n}")` |
| `src/api/onboarding.py:125` `_execute_agent_wrapper` | `_onboarding_workdir(repo_key)` | `get_agent_workdir(repo_key, "vod", f"#{target_index+1}")` |
| `src/api/onboarding.py:143` `_enqueue_onboarding_task` | 同上 | 同上 |
| `src/api/onboarding.py:150` `get_or_create_issue_session` | `("vod", "#1"/"#2")`（不变） | **不变**（已正确挂在 vod issue 会话） |

> `target_index` 由 `ONBOARDING_TARGETS.index(target)` 推导（experience→0→#1，explore→1→#2）。
> 结果文件路径（onboarding.py:126,144）跟随 `get_agent_workdir` 返回值，一并落新目录。

---

## 四、历史目录迁移策略

### 4.1 方案选择：不迁移存量，新旧共存 + sweeper 自然收敛（推荐）

现有 `{repo}/{agent_type}/issues/{n}/` 目录**不主动搬迁**：

1. **新任务**（改造后触发）走新路径 `{repo}/issues/{n}/{agent}/`。
2. **存量 issue 目录**由 sweeper（`cleanup.py`）在 `stage=done` 后按 TTL 清理——但需注意：
   改造后 sweeper 只扫描**新结构**，旧结构目录不会被扫到。需在过渡期保留旧结构扫描兜底
   （见 4.2），或一次性手工 `git worktree` 迁移。
3. **旧 issue 二次处理**（未 done 的存量 issue 又收到新事件）：会以新 cwd 重新建目录，
   旧目录残留。此时旧目录可能仍被旧 `runtime_session_id` 引用——需在改造发布时**清空存量
   `issue_agent_session.runtime_session_id`**（见 4.3），强制续新会话、走新路径。

### 4.2 过渡期 sweeper 双结构兼容（改造发布后保留 1 个清理周期）

`_zombie_issue_dirs` 同时扫描新旧两套结构，仅删新结构 + 旧结构中**无活跃任务**的目录：
- 新结构：`{repo}/issues/{n}/{agent}/`
- 旧结构：`{repo}/{agent}/issues/{n}/`（仅过渡期，若 `git worktree remove` 正常则直接删）

> 判断活跃：沿用现有 `_active_issue_keys()`（从 `issue_agent_session` 读 dispatched/running）。
> 过渡期结束后移除旧结构扫描分支。

### 4.3 存量会话指针处理

改造发布时执行一次性 DB 维护：
```sql
UPDATE issue_agent_session SET runtime_session_id='' WHERE runtime_session_id <> '';
UPDATE devops_agent_session   SET runtime_session_id='' WHERE runtime_session_id <> '';
```
- 清空后，存量 issue 下次触发任务时 `get_or_create_issue_session` 续新会话，
  opencode 以新 cwd（`issues/{n}/{agent}/`）创建全新项目根，**根治** #701 那类
  "旧 session 项目根（onboarding-agent）≠ 新 cwd" 导致的 `external_directory` 权限卡死。
- 代价：存量 issue 丢失历史对话续接（可接受，事件上下文本就由 `build_issue_context`
  从 event.json 内联，不依赖 opencode 会话记忆）。

---

## 五、测试方案

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/test_event_conversation.py`（扩展） | 触发 issue #5 | `agent_tasks.cwd = {WORKSPACE_DIR}/{o}/{r}/issues/5/{agent}/` |
| `tests/test_repogit.py`（扩展） | `issue_dir_for` | 返回 `{repo}/issues/{n}/{agent}`；worktree add 到新路径 |
| `tests/test_cleanup.py`（扩展） | `_zombie_issue_dirs` | 新结构目录可被扫描；旧结构目录过渡期可被扫描 |
| `tests/test_knowledge.py`（如存在） | `issue_agent` scope | `knowledge_path` 返回 `{repo}/issues/{n}/{agent}/knowledge.md` |
| 新增兼容用例 | 双结构共存 | 新目录正常读写，旧目录不被误删（有活跃任务时） |
| `tests/test_onboarding.py`（新增） | onboarding cwd | `_onboarding_workdir` 废弃；direct/queue 触发后 `agent_tasks.cwd = {repo}/issues/1/vod/`、`{repo}/issues/2/vod/` |
| `tests/test_onboarding.py`（新增） | 结果文件落位 | `onboarding-result-{target}.json` 生成于 `{repo}/issues/{n}/vod/` |
| `tests/test_onboarding.py`（新增） | **权限根因回归** | 模拟 onboarding（#1/#2, cwd=`issues/{n}/vod/`）+ 真实 issue 复用同一 `issue_agent_session`：断言真实任务 `runtime_session_id` 为空（或 cwd 与 session 项目根同目录），**不触发 external_directory 权限错位** |

> 权限回归用例核心断言：**cwd 与 `runtime_session_id` 对应 session 的项目根同目录**。
> onboarding 不再产生独立 `onboarding-agent` 目录 → 旧 session 项目根 ≠ 新 cwd 的场景不存在。

> 沿用现有 `patch("src.config.WORKSPACE_DIR", tmp_path)` 隔离模式（见 `test_event_archive.py`）。

---

## 六、实施顺序（评审通过后执行）

1. **代码改动**：`event_pipeline.py`（2 处）、`repogit.py`（1 处 + docstring）、
   `knowledge.py`（1 处）、`cleanup.py`（1 处 + 过渡期兼容）、`onboarding.py`（废弃
   `_onboarding_workdir`，direct/queue cwd 改走 `get_agent_workdir`）。
2. **测试**：按 §五 扩展/新增。
3. **一次性 DB 维护**：清空存量 `runtime_session_id`（§4.3）。
4. **部署**：生产 + worker 同版本 `git pull` + 重启（服务端改动需重启 grape.service；
   worker 需同步 `repogit.py` 并重启 runtime-agent）。
5. **观察**：一个新 issue 事件走新路径；sweeper 过渡期扫描旧目录；下个清理周期确认
   旧目录收敛后移除兼容分支。

---

## 七、风险与回退

| 风险 | 影响 | 缓解 |
|---|---|---|
| 存量 worktree 路径变更 | 旧 worktree 目录残留 | 过渡期双结构扫描 + TTL 清理；不主动迁移 |
| 会话清空后上下文丢失 | 存量 issue 无对话续接 | 事件上下文靠 `build_issue_context`（event.json 内联），不依赖会话记忆 |
| 服务端/worker 版本不同步 | worker 用旧 repogit 写旧路径 | 发布时生产与 worker 同时 `git pull` 重启 |
| 改造引入回归 | cwd 校验 / 清理误删 | 全量 pytest + 双结构兼容用例 |
| onboarding 结果文件迁移 | 历史 `{repo}/onboarding-agent/` 结果文件读不到 | 过渡期保留旧目录；`_finalize_onboarding` 经 `ONBOARDING_RESULT` env 跟随新路径 |
| onboarding 旧会话残留 | 存量 onboarding 会话指向旧目录 | §4.3 清空 `runtime_session_id` 强制续新会话 |

**回退**：代码 `git revert` 改动 commit + 重新部署；DB 已清空指针不可逆回退，但无害
（清空后只是续新会话，功能正常）。`issues/{n}/` 下新结构目录可手工删除，回落到旧结构。
