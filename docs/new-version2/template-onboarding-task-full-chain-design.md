# 模板创建 → 托管引导 → 任务处理 全链路设计方案

> 版本：v1.0
> 日期：2026-08-12
> 状态：待评审
> 需求：将「模板一键创建与托管引导」（`repo-onboarding`）与「任务对话处理」（`chat` 技能）两条链路整合为完整工作流方案。前者目前是"黑盒"（不落 events、不建 issue_state、硬编码 opencode、SKILL 一次建两个 Issue）；后者把多个场景流程混在单一 SKILL.md、任务对话无独立 session/prompt。要求：模板创建 → VOD 引导建 Issue → Issue 分配任务 → 用户"去对话"澄清需求，全链路在 pipeline 中可追踪、可收敛、可处理。
> 合并自：`repo-onboarding-event-pipeline-design.md`（v3.0）+ `chat-skill-scene-split-and-task-prompt-design.md`（v1.0）。
> 前置：`skills/repo-onboarding/SKILL.md`、`skills/chat/SKILL.md`、`src/api/onboarding.py`、`src/api/event_pipeline.py::_execute_agent`、`src/api/routes/tasks.py`、`src/api/routes/chat.py`、`src/prompt_builder.py`、`src/api/event_store.py`、`src/api/session_store.py`（issue_state）、`src/session_manager.py`（chat_conversations）、`vod-skill-upgrade-plan.md §3.0.2/3.0.3`（阶段矩阵）。

---

## 〇、全链路总览

```
① 模板一键创建并托管            templates.create_from_template
      │
      ▼
② VOD 引导建 Issue              trigger_onboarding（两次：experience / explore）
      │  ├─ 落 events 主事件（onboarding_exp / onboarding_explore，issue_id=#0）
      │  ├─ VOD Agent（复用会话引擎配置，direct/queue）执行 repo-onboarding 技能
      │  │    └─ 按 $ONBOARDING_TARGET 创建 1 个对应类型 Issue（体验 #01 / 探索 #02）
      │  └─ _finalize_onboarding：issue_state=vod + onboarding_issue 事件
      ▼
③ Issue 收敛后分配任务          VOD 收敛需求 → A2A 推进 issue 阶段 → issue agent 分诊
      │  └─ POST /api/tasks/notify → 建任务 + 任务会话 + 通知责任人
      ▼
④ 用户"去对话"澄清需求          chat 任务会话（conv_type=task）→ 独立 prompt（场景: task）
      │  └─ SKILL 场景识别 → references/task-handling.md → 澄清 → 回帖到 Issue
      ▼
   后续由 Issue/Coding/QA/SRE Agent 接管（pipeline 完整）
```

---

## 一、现状与问题

### 1.1 阶段 ②：repo-onboarding 现状（黑盒）

```
模板一键创建成功 → trigger_onboarding
  → asyncio.create_task(_run_onboarding)
    → opencode 执行 repo-onboarding（Onboarding Agent）
      → 分析项目 → 建 2 个 Issue（体验 + 探索）→ assignee=owner → POST /api/tasks/notify
```

| # | 问题 | 用户视角 |
|---|------|---------|
| 1 | `_run_onboarding` 不写 events 表 | Event 页看不到引导过程 |
| 2 | 不建 `issue_state` | Pipeline 无引导产生的 Issue |
| 3 | Issue 号不回传系统 | 无法把新 Issue 纳入阶段管理 |
| 4 | 硬编码 opencode，不查 VOD 会话、不走 queue | 本地无 opencode 引导直接失败 |
| 5 | SKILL 一次创建两个 Issue，不感知 prompt 指引 | 两个 Issue 产出意图无差异 |

### 1.2 阶段 ④：chat 技能现状（场景混杂）

#### 1.2.1 任务对话的现状差异：session 已独立、prompt 不独立

| 维度 | 现状 | 是否独立 |
|---|---|---|
| **conversation（session）** | `notify_task`（tasks.py:244-266）创建/复用 `chat_conversations`，title=「Issue #N」，独立 `conv_id`（uuid）+ 独立工作目录 `data/workspaces/chat/{user_id}/{conv_id}`（tasks.py:169），Web conversation（无 `im_channel_id`），Chat Agent 页可见 | ✅ **已是独立对话** |
| **后台 notify prompt** | `_build_task_prompt`（tasks.py:138）注入 Issue 上下文（仓库/Issue 号/标题/摘要/任务描述），但**复用 `build_chat_prompt` 壳**（prompt_builder.py:448），只是把 Issue 上下文作为 `user_text` 传入——无显式 `场景: task` 标识 | ⚠️ 半独立 |
| **用户"去对话" prompt** | 任务中心点「去对话」→ `chat.py send_message`（chat.py:117）→ `_build_chat_prompt`（通用）——**不复用 `_build_task_prompt`**、不读 `tasks` 表的 `issue_number`/`task_description`，唯一线索是 history 里那条「📋 任务分配 · Issue #N」user 消息（tasks.py:262），靠 Agent 嗅探 | ❌ **不独立** |

> **结论**：任务对话的 **session 已是独立会话**（数据层满足），方案的核心增量是 **prompt 独立**——新增 `build_task_chat_prompt`（注入 Issue 上下文 + `场景: task`）替代通用 `_build_chat_prompt`，并用 `conv_type` 显式标记会话类型。

#### 1.2.2 场景混杂问题

| # | 问题 | 说明 |
|---|------|------|
| 1 | 三个流程（任务处理/意见反馈/实施启动）混在单一 SKILL.md（294 行） | Agent 易混淆该走哪条流程 |
| 2 | 任务"去对话"复用通用 chat prompt（见 §1.2.1） | `_build_task_prompt`（带 Issue 上下文）仅用于后台 notify，用户去对话时不用 |
| 3 | `chat_conversations` 无会话类型字段 | 无法区分任务会话/一般会话 |

### 1.3 对比正常事件链路

正常 webhook：`append_event`(running) → `set_issue_stage` → agent 执行 → `update_event_complete`。
onboarding 缺落库两步且执行端游离；chat 任务对话缺显式场景与独立上下文。

---

## 二、设计目标与原则

1. **全链路 pipeline 可追踪**：模板创建 → VOD 引导（events 落库）→ Issue（issue_state 建立）→ 任务分配 → 对话澄清，每步有事件与阶段轨迹。
2. **VOD 负责引导执行**：`agent_type=vod`，复用 VOD 会话引擎配置（direct/queue），本地无 opencode 可跑。
3. **一次引导 = 一个 Issue**：`experience`/`explore` 各执行一次，SKILL 按 `$ONBOARDING_TARGET` 创建对应类型单个 Issue。
4. **新 Issue 处于 vod 阶段**：VOD 先收敛用户需求（阶段矩阵 D8），收敛后 A2A 推进。
5. **chat 场景隔离**：任务处理 / 意见反馈 / 实施启动 各自独立 `references/` 文件，SKILL.md 作调度入口。
6. **任务对话独立 session + 独立 prompt**：任务会话的 **session 已独立**（独立 `conv_id`/cwd，现状满足）；本方案补齐 **prompt 独立**——`conv_type=task` 会话使用 `build_task_chat_prompt`（注入 Issue 上下文 + `场景: task`），不再复用通用 chat prompt。
7. **幂等/防重复**：`meta.onboarded:{repo_key}` 门禁；事件/阶段仅首次创建。
8. **默认零回归**：手动托管不触发引导；一般问询行为不变；历史会话兼容。

---

## 三、阶段 ② 改造：VOD 引导建 Issue（repo-onboarding）

### 3.1 整体流程

```
模板一键创建成功
  → trigger_onboarding(repo_key, owner)
    ├─ 幂等门禁（onboarded 标记）
    ├─ 查 VOD 会话：backend / runtime_id / skill（devops_agent_session WHERE agent_type='vod'）
    ├─ 落库 2 个主事件（agent_type=vod, issue_id=#0, status=running）：
    │    seq_exp = append_event(event_type=onboarding_exp,  title="引导·快速部署体验")
    │    seq_exl = append_event(event_type=onboarding_explore, title="引导·探索方向")
    └─ 两次引导执行（可并行，各自 _execute_agent）：
        │ 执行模式跟随 VOD Agent 会话（与 vod_handler_async.py:209 一致）：
        │   kind = runtime_execution_kind(vod 会话的 runtime_id)
        │   kind == EXEC_MODE_DIRECT → 就地执行（direct）
        │   kind != EXEC_MODE_DIRECT → enqueue_task 入队（queue，本地/远端 worker 执行）
        ├─ 引导 A（target=experience）
        │    prompt 注入：ONBOARDING_TARGET=experience
        │    └─ VOD Agent 执行 repo-onboarding 技能
        │         ├─ 分析项目（codegraph → 知识基线）
        │         ├─ 创建 1 个「快速部署体验」Issue，assignee=owner（新仓库编号 #01）
        │         ├─ 写 onboarding-result-exp.json：{"number": 1, "title": "...", "target": "experience"}
        │         └─ _execute_agent 收尾主事件 → seq_exp completed
        └─ 引导 B（target=explore）
             prompt 注入：ONBOARDING_TARGET=explore
             └─ VOD Agent 执行 repo-onboarding 技能
                  ├─ 分析项目（复用 A 的知识基线）
                  ├─ 创建 1 个「探索方向」Issue，assignee=owner（新仓库编号 #02）
                  ├─ 写 onboarding-result-explore.json：{"number": 2, "title": "...", "target": "explore"}
                  └─ _execute_agent 收尾主事件 → seq_exl completed

  └─ 各自引导完成后（_finalize_onboarding 线程末尾触发，各一次）：
       ├─ 读对应 result 文件 → 该次引导创建的 1 个新 Issue（#01 / #02）：
       │    set_issue_stage(repo, #N, "vod", "vod", assignee=owner)
       │      → 新 Issue 处 vod 阶段：VOD Agent 先收敛用户需求（阶段矩阵 D8）
       │    append_event(issue_id=#N, event_type=onboarding_issue, agent_type=vod, status=completed)
       │      → 每个引导 Issue 一条独立事件 → pipeline 按 Issue 聚类呈现两个 Issue
       └─ （主事件已由 _execute_agent 收尾，_finalize 仅补充 Issue 阶段与各 Issue 事件）
```

> **编号 #01/#02**：新模板仓库为空仓库，GitCode Issue 从 1 递增。引导 A（体验）先创建得 #1，引导 B（探索）后创建得 #2。以技能回传实际编号为准。

### 3.2 代码改动（`src/api/onboarding.py`）

#### 3.2.1 `trigger_onboarding`：落库两个主事件 + 查 VOD 会话 + 两次引导分派

```python
ONBOARDING_TARGETS = ("experience", "explore")

def trigger_onboarding(repo_key: str, owner: str):
    ...
    if _is_onboarded(repo_key):
        return
    _mark_onboarding_started(repo_key)  # 防并发重复

    # 1. 查 VOD 会话配置（backend/runtime_id/skill_path，走 VOD Agent）
    backend, runtime_id, skill_path = "opencode", 0, ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'", (repo_key,))
            row = c.fetchone()
        if row:
            runtime_id = row[0] or 0
            if row[2]:
                from src.user.db import get_skill_by_id
                _sk = get_skill_by_id(row[2])
                if _sk:
                    skill_path = _sk.get("skill_path") or ""
        if runtime_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                _r = c.fetchone()
            if _r and _r[0]:
                backend = _r[0]
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] vod session lookup failed: {e}")

    # 2. 落库两个主事件 + 两次引导分派（各 target 一次）
    #    执行模式跟随 VOD 会话（kind = runtime_execution_kind(vod runtime_id)，与 vod_handler_async 一致）
    from src.api.event_store import append_event
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    kind = runtime_execution_kind(runtime_id or 0)
    for target in ONBOARDING_TARGETS:
        _title = "引导·快速部署体验" if target == "experience" else "引导·探索方向"
        _event_type = "onboarding_exp" if target == "experience" else "onboarding_explore"
        seq = append_event(
            repo_key=repo_key, event_type=_event_type, title=_title,
            issue_id="#0", agent_type="vod", source="template",
            actor=owner or "", summary=f"{_title}（VOD Agent）：创建对应类型的引导 Issue",
            status="running",
        )
        evt = {"type": _event_type, "repository": from_repo_key(repo_key), "source": "template",
               "actor": owner or "", "account": from_repo_key(repo_key), "issue_id": "#0",
               "payload": {"repository": from_repo_key(repo_key), "owner": owner, "onboarding_target": target}}
        if kind != EXEC_MODE_DIRECT:
            _enqueue_onboarding_task(repo_key, seq, backend, runtime_id, owner, skill, target)
        else:
            evt["_skill_path"] = skill_path
            import threading
            threading.Thread(target=_execute_agent_wrapper, args=(
                repo_key, seq, owner, skill, backend, runtime_id, evt, skill_path, target,
            ), daemon=True, name=f"onboarding-{target}-{seq}").start()
        log.info(f"[Onboarding|repo:{repo_key}] 触发引导 target={target}, event seq={seq}, backend={backend}")
```

#### 3.2.2 新增 `_execute_agent_wrapper`（direct 模式）

```python
def _execute_agent_wrapper(repo_key, seq, owner, skill, backend, runtime_id, evt, skill_path, target):
    from src.api.event_pipeline import _execute_agent
    from src.repo_key import from_repo_key
    from src.config import WORKSPACE_DIR
    import os
    _ws = os.path.join(WORKSPACE_DIR, from_repo_key(repo_key), "onboarding-agent")
    os.makedirs(_ws, exist_ok=True)
    _result_path = os.path.join(_ws, f"onboarding-result-{target}.json")
    evt.setdefault("_env_extra", {})["ONBOARDING_RESULT"] = _result_path
    evt.setdefault("_env_extra", {})["ONBOARDING_TARGET"] = target
    _execute_agent(repo_key, seq, "vod", backend, skill, evt, runtime_id)
    _finalize_onboarding(repo_key, seq, owner, _result_path)
```

#### 3.2.3 新增 `_finalize_onboarding`

```python
def _finalize_onboarding(repo_key, seq, owner, result_path=""):
    """单次引导完成补库：读技能产出 → 该次引导创建的 1 个 Issue 建 vod 阶段 + 独立事件。"""
    from src.api.event_store import append_event
    from src.api.session_store import set_issue_stage
    if not (result_path and os.path.exists(result_path)):
        log.warning(f"[Onboarding|repo:{repo_key}] result not found: {result_path}")
        return
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] read result failed: {e}")
        return

    num = str(data.get("number", "")).strip()
    title = data.get("title", "") or f"Issue #{num}"
    if not num or num in ("0",):
        log.warning(f"[Onboarding|repo:{repo_key}] invalid issue number in result: {data}")
        return
    # 新 Issue 处于 vod 阶段：VOD 需先收敛用户需求（阶段矩阵 D8），收敛后 A2A 再推进
    set_issue_stage(repo_key, f"#{num}", "vod", "vod",
                    assignee=(data.get("assignee") or owner or ""))
    append_event(
        repo_key=repo_key, event_type="onboarding_issue",
        title=title, issue_id=f"#{num}", agent_type="vod",
        source="template", actor=owner or "",
        summary=f"VOD 引导创建的 Issue #{num}：{title}（待 VOD 收敛需求）",
        status="completed",
    )
    log.info(f"[Onboarding|repo:{repo_key}] issue_state=vod for #{num} ({title})")
```

#### 3.2.4 新增 `_enqueue_onboarding_task`（queue 模式）

```python
def _enqueue_onboarding_task(repo_key, seq, backend, runtime_id, owner, skill, target):
    """queue 模式：入队由本地/远端 worker 执行（本地无 opencode 也可运行）。"""
    from src.runtime.service import enqueue_task
    from src.repo_key import from_repo_key
    from src.config import WORKSPACE_DIR, SKILLS_DIR
    import os
    repository = from_repo_key(repo_key)
    ws = os.path.join(WORKSPACE_DIR, repository, "onboarding-agent")
    os.makedirs(ws, exist_ok=True)
    result_path = os.path.join(ws, f"onboarding-result-{target}.json")
    from src.prompt_builder import build_prompt
    evt = {"type": "onboarding", "repository": repository, "source": "template",
           "actor": owner or "", "account": repository, "issue_id": "#0",
           "payload": {"repository": repository, "owner": owner, "onboarding_target": target}}
    try:
        prompt = build_prompt(evt, [], "", [skill], skill_path=os.path.join(SKILLS_DIR, skill, "SKILL.md"),
                              runtime_id=runtime_id)
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] prompt build failed: {e}")
        return
    tid = enqueue_task(
        agent_session_id=f"vod:{repo_key}", runtime_id=runtime_id,
        event_seq=seq, issue_id="#0", repo_key=repo_key, agent_type="vod",
        skill=skill, prompt=prompt, cwd=ws,
        runtime_session_id="", event_type="onboarding",
        account=repository, repo_gitcode_token="",
        skill_path=os.path.join(SKILLS_DIR, skill, "SKILL.md"),
        env_extra={"REPO_FULL_NAME": repository, "REPO_OWNER": owner,
                   "ONBOARDING_RESULT": result_path, "ONBOARDING_TARGET": target},
    )
    log.info(f"[Onboarding|repo:{repo_key}] enqueued task={tid} seq={seq} target={target}")
```

> worker 端执行完成后回写 event；`_finalize_onboarding` 需在任务终态后触发（实施期按现有 worker 回写机制对接）。

### 3.3 `skills/repo-onboarding/SKILL.md`：按 prompt 指引创建对应类型 Issue + 结构化产出契约

**核心改动**：SKILL 不再固定创建两个 Issue，读取 `$ONBOARDING_TARGET` 按目标类型创建**一个**对应诉求的 Issue。

frontmatter：

```yaml
---
name: repo-onboarding
version: 1.1.0
description: |
  项目首次托管引导（通用）：分析项目功能特性、使用方式、技术栈。
  根据 $ONBOARDING_TARGET 指引创建对应类型的单个引导 Issue——
  experience=「快速部署体验」需求；explore=「探索方向引导」诉求。
  仅在模板一键创建并托管后异步触发（experience / explore 各执行一次）。
---
```

正文关键步骤：

```markdown
# 项目首次托管引导（repo-onboarding）

## 目标
根据 `$ONBOARDING_TARGET` 创建对应类型的引导 Issue，帮助开发者快速上手模板项目。

## 目标类型判定（必须最先）
- 读环境变量 `$ONBOARDING_TARGET`：
  - `experience` → 执行「快速部署体验」分支（步骤 3A）
  - `explore` → 执行「探索方向引导」分支（步骤 3B）
  - 其他/缺失 → 默认按 `experience` 处理并日志说明

## 执行步骤（必须按序）
1. **建立项目知识基线**（参考 issue-advance）：codegraph 分析 → project-graph.md + project-capabilities.md
2. **判定目标类型**（读 `$ONBOARDING_TARGET`）
3A. **快速部署体验（experience）**：创建 **1 个** Issue，标题「【体验】快速部署并体验本项目的核心功能」；内容含部署步骤/体验路径/预期效果；assignee=owner
3B. **探索方向引导（explore）**：基于知识基线提炼 2-4 个探索方向，创建 **1 个** 汇总 Issue「【探索方向】值得探索的 N 个方向」；assignee=owner
4. **生成任务**：POST /api/tasks/notify（assignee=owner，含 issue_number）
5. **写入引导结果**：创建成功后写 `$ONBOARDING_RESULT`：
   ```json
   {"number": 1, "title": "...", "assignee": "<owner>", "target": "experience"}
   ```
   number 填 GitCode 实际返回的 Issue 号；创建失败则不写文件。
6. **输出引导摘要**：README 顶部追加「🤖 本仓库已托管...」或在该 Issue 中写明

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`；不读 Grape 内部数据（data/workspaces 项目知识文档除外）
- 不修改代码、不建 PR；仅建 1 个 Issue + 分配 + 通知
- assignee 必须是"人"（owner），不是 bot 账号
```

### 3.4 `src/api/event_pipeline.py`：`_execute_agent` 支持 `_env_extra`

```python
env_extra = {
    "REPO_FULL_NAME": repository,
    "ACCOUNT": exec_account,
}
# 事件级扩展环境变量（如 onboarding 的 ONBOARDING_RESULT / ONBOARDING_TARGET）
_ext = evt.get("_env_extra") or {}
if isinstance(_ext, dict):
    env_extra.update(_ext)
```

---

## 四、阶段 ③ 衔接：Issue 收敛 → 任务分配

- 引导创建的 Issue 处于 **vod 阶段**，由 VOD 收敛需求（`_execute_vod_agent` / issue 事件驱动）。
- VOD 收敛后经 A2A 推进至 **issue 阶段**（`set_issue_stage`），issue agent 分诊、分配责任人。
- issue agent 分配/换人后调用 `POST /api/tasks/notify`（tasks.py:211）→ 建任务 + 任务会话 + 后台 chat-agent 通知 + 飞书通知。
- 本阶段复用现有逻辑，无新增改动；与阶段 ④ 的衔接点是「任务会话」的创建与标记。

---

## 五、阶段 ④ 改造：任务对话独立 Session + 独立 Prompt（chat 技能）

### 5.1 Skill 结构调整

```
skills/chat/
├── SKILL.md                      # 入口：场景识别 + 通用约束 + 调度
└── references/
    ├── gitcode-api-reference.md  # 已有：GitCode API 参考
    ├── task-handling.md          # 【新增】任务场景：澄清被分配 Issue → 回帖
    ├── inquiry-handling.md       # 【新增】一般问询：需求/建议引导 → 创建 Issue
    └── implementation-start.md   # 【新增】实施启动确认：审批 → PATCH 标签触发 implementation
```

### 5.2 `SKILL.md`（重构为调度器）

保留通用部分（安全硬约束、能力边界、链接格式、@用户、输出格式、参考），替换为场景识别 + 调度：

```markdown
---
name: chat
version: 2.0.0
description: |
  飞书等 IM 对话处理指南。当用户在飞书/Slack/其他 IM 中与 Grape 对话时使用此技能。
  入口职责：识别当前对话属于哪一场景，并按对应流程文件处理。
  场景：task（任务处理）、inquiry（意见反馈/需求引导）、implementation-start（实施启动）。
  不适用于：GitCode 网页上的 Issue/PR 评论直接触发（用 issue skill）。
---

# IM 对话处理

## 安全硬约束（必须遵守）
（保留现有 §14-19 原样）

## 场景识别（必须先判定）

读取 prompt 上下文，按下述优先级判定当前场景：

| 场景 | 判定信号 | 流程文件 |
|------|---------|---------|
| **task 任务处理** | prompt 含「📋 任务分配 · Issue #N」或 `场景: task`；或用户明确「帮我看看/处理 Issue #N」 | `references/task-handling.md` |
| **implementation-start 实施启动** | 用户回复「同意启动/启动/确认/开始/批准」且是针对机器人发出的实施启动批准请求；或 prompt 含 `场景: implementation-start` | `references/implementation-start.md` |
| **inquiry 意见反馈** | 用户提出建议/新需求/问题反馈；或 prompt 含 `场景: inquiry`；或以上均不命中且为 DevOps 相关话题 | `references/inquiry-handling.md` |

**判定顺序**：先查 prompt 显式 `场景:` 字段 → 再按上下文信号识别 → 兜底 inquiry。
**无关话题**：非 DevOps 话题按「能力边界」礼貌说明（不读任何流程文件）。

## 能力边界（保留现有 §26-42）
## 调度执行
- 判定场景后，**先用 read tool 读取对应 references 流程文件**，严格执行其中步骤。
- 流程文件内的 GitCode 调用使用 `$USER_GITCODE_TOKEN`，服务端地址用 `{{SERVER_URL}}`。
## 响应规则 / 操作边界 / 输出格式 / @用户 / 链接格式（保留现有 §223-294 原样）
```

### 5.3 三个流程文件（源自现有 SKILL.md 章节）

| 文件 | 源自 | 内容 |
|---|---|---|
| `references/task-handling.md` | 现 §93-160 | 判断进入澄清 → 读取 Issue 背景 → 分析不清晰点 → 精准引导补充 → 回帖到 Issue → 回复用户 |
| `references/inquiry-handling.md` | 现 §162-221 | 判断建议归属 → 引导补充（功能/问题/优化模板）→ 创建 Issue（不带标签）→ 回复用户 |
| `references/implementation-start.md` | 现 §44-91 | 0 检查自动实现开关 → 1 查询待启动 Feature → 2 筛选 → 3 PATCH 标签启动 → 4 回复 → 5 兜底 |

### 5.4 数据模型：`chat_conversations` 增加 `conv_type`

```sql
ALTER TABLE chat_conversations ADD COLUMN conv_type TEXT DEFAULT 'general';  -- general | task
ALTER TABLE chat_conversations ADD COLUMN issue_number INTEGER DEFAULT 0;
```

### 5.5 DB 迁移（`db_setup.py` `_migrate_tables`）

```python
try:
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "chat_conversations", "conv_type", "TEXT DEFAULT 'general'")
        _ensure_column(c, "chat_conversations", "issue_number", "INTEGER DEFAULT 0")
        db.commit()
        log.info("Added conv_type / issue_number to chat_conversations")
except Exception as e:
    log.warning(f"Add chat conversation type failed: {e}")
```

### 5.6 `notify_task`（tasks.py:244-266）：创建任务会话时标记类型

```python
if not conv_id:
    conv_id = create_conversation(user_id, body.repo_key,
                                  title=f"Issue #{body.issue_number}",
                                  conv_type="task", issue_number=body.issue_number)
else:
    update_conversation(conv_id, conv_type="task", issue_number=body.issue_number)
```

- `session_manager.create_conversation` 增加可选参 `conv_type` / `issue_number`（INSERT 加两列）。
- 已有「Issue #N」title 的会话复用并补标记，兼容存量。

### 5.7 `chat.py send_message`：按会话类型选择 prompt 构建

```python
conv_type = (conv or {}).get("conv_type", "general")

if conv_type == "task":
    prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"])
else:
    prompt = _build_chat_prompt(body.message, repo_key, history, skill_name, skill_id)
```

#### 5.7.1 新增 `build_task_chat_prompt`（prompt_builder.py）

```python
def build_task_chat_prompt(user_text, repo_key, history, conv, account="", runtime_id=0):
    issue_number = conv.get("issue_number") or 0
    parts = [f"## 场景: task", f"## 任务 Issue", f"仓库: {repo_key}", f"Issue: #{issue_number}"]
    if conv.get("title"):
        parts.append(f"标题: {conv['title']}")
    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(f"\n{issue_summary}")
    except Exception:
        pass
    if history:
        parts.append("\n## 对话历史")
        for m in history[-10:]:
            parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")
    parts.append(f"\n## 用户消息\n{user_text}")
    parts.append(f"Skills: chat")
    _sp = f"{{{{SKILL_DIR}}}}/chat/SKILL.md"
    parts.append(f"Skill 文件：{_sp}\n**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令。")
    parts.append(build_chat_role_definition())
    return "\n".join(parts)
```

> `场景: task` 显式注入 → SKILL.md 场景识别直接命中任务场景，Agent 走 `references/task-handling.md`。

### 5.8 前端「去对话」定位（chat.js `loadConversations`）

现有逻辑（chat.js:88-120）：`_openConvId` 定位任务指定会话，`repoKey` 传空跨仓库定位。保持不变即可。

### 5.9 IM 渠道（`im/handler_async.py`）兼容

- IM 群聊/私聊创建会话默认 `conv_type=general`。
- 若 IM 消息上下文含「任务分配 · Issue #N」，SKILL 场景识别按上下文信号走任务流程。

---

## 六、数据模型汇总

| 表 | 写入 | 说明 |
|---|---|---|
| `events` | `onboarding_exp` / `onboarding_explore`（`issue_id=#0`，`agent_type=vod`） | 两次引导的运行总览事件 |
| `events` | `onboarding_issue`（每个引导 Issue 各一条，`issue_id=#01/#02`，`agent_type=vod`） | 引导 Issue 独立记录 → pipeline 聚类呈现 |
| `issue_state` | `stage=vod, owner=vod, assignee=owner`（每个引导 Issue） | 新 Issue 处 vod 阶段，VOD 收敛需求 |
| `chat_conversations` | 新增 `conv_type`（general/task）+ `issue_number` | 区分任务会话与一般会话 |

> 说明：`onboarding_exp` / `onboarding_explore` / `onboarding_issue` 不在 `ISSUE_EVENT_TYPES` / `PR_EVENT_TYPES` / `NON_ISSUE_EVENT_ROUTE` 中，不会被 pipeline 重新路由（完成态记录）。

---

## 七、用户可见效果

1. **Event 历史**：模板创建后出现两条引导 running 事件（「引导·快速部署体验」「引导·探索方向」，归属 VOD Agent）；各自 completed，结论含 Issue 号。
2. **Pipeline 视图**：完整工作流——VOD 处理两次引导 → 各建 1 个 Issue（#01/#02）→ vod 阶段收敛 → 收敛后 A2A 推进 → 分配任务 → 用户"去对话"澄清需求 → 回帖到 Issue → 后续 Agent 接管。
3. **任务中心"去对话"**：进入即携带 Issue 上下文 + 任务场景标记，Agent 直接澄清需求并回帖，不再嗅探场景。
4. **一般问询 / 实施启动**：走独立流程，互不混淆。
5. **引擎无关**：引导复用 VOD 会话引擎（含远端 worker），本地无 opencode 不影响。

---

## 八、边界与取舍

| 项 | 决策 | 理由 |
|---|---|---|
| 引导执行方 | VOD Agent（`agent_type=vod`） | pipeline 完整；复用 VOD 会话引擎配置 |
| 引导粒度 | `experience`/`explore` 各执行一次 | 两个 Issue 的 prompt 有差异、产出意图清晰 |
| 引导 Issue 阶段 | `stage=vod, owner=vod` | 新 Issue 需 VOD 先收敛需求（阶段矩阵 D8） |
| 主事件 issue_id | `#0`（仓库级） | 执行时尚未创建 Issue；与 push 一致 |
| 技能产出 | 方案 A（`ONBOARDING_RESULT` 文件，按 target 分文件） | 无需轮询 GitCode API；单次引导单产出 |
| 会话类型识别 | `conv_type` 列 + `issue_number` | 显式标记优于 title 嗅探；存量 Issue #N 会话迁移补标记 |
| 场景判定优先级 | 显式 `场景:` 字段 → 上下文信号 → 兜底 inquiry | 显式最可靠；上下文覆盖 IM；兜底不遗漏 |
| 任务 prompt | 新增 `build_task_chat_prompt`，与 `_build_task_prompt` 共用上下文逻辑 | 后台 notify 与用户对话分离，各司其职 |
| 幂等 | `meta.onboarded:{repo_key}` 门禁（现状保留） | 换场景不重跑；事件/阶段仅首次创建 |
| 手动托管 | 不触发引导（现状保留） | 仅模板一键创建链路引导 |
| 失败处理 | 单次引导失败不写 result，主事件 completed 但无 stage 写入 | 一次引导失败不影响另一次 |

---

## 九、测试计划

| 用例 | 断言 |
|---|---|
| `trigger_onboarding` 落库两个主事件 | `onboarding_exp` + `onboarding_explore` 各一条，status=running，issue_id=#0，agent_type=vod |
| 两次引导分派 | `experience`/`explore` 各触发一次执行（direct 线程 / queue 入队） |
| `_execute_agent` 合并 `_env_extra` | ONBOARDING_RESULT / ONBOARDING_TARGET 注入子进程环境 |
| SKILL 按 target 分支 | target=experience 走体验分支；target=explore 走探索分支 |
| `_finalize_onboarding` 建阶段 | `issue_state.stage=='vod'` 且 `owner_agent=='vod'`、assignee 正确 |
| `_finalize_onboarding` 建事件 | 每条 `onboarding_issue` 事件，issue_id=#1/#2，agent_type=vod，completed |
| `notify_task` 任务会话 | 新会话 `conv_type=task`、`issue_number` 正确；存量 Issue #N 会话补标记 |
| `send_message` 任务会话 | 走 `build_task_chat_prompt`，prompt 含 `场景: task` + Issue 上下文 |
| `send_message` 一般会话 | 走 `build_chat_prompt`，prompt 无 `场景: task` |
| SKILL 场景识别 | 任务分配信号 → task；审批信号 → implementation-start；其余 → inquiry |
| references 文件存在性 | 三个流程文件存在且被 SKILL 正确引用 |
| 幂等 / 无产出回退 | onboarded 不重跑；result 缺失主事件 completed 无 stage 写入 |
| 现有回归 | 手动托管不触发引导；IM 一般对话、Chat 页新建对话行为不变 |

---

## 十、实施计划（详细任务拆分）

> 约定：每个任务含「改动文件 / 具体变更 / 验证方式」。阶段 ② 与阶段 ④ 相互独立，可并行开发；联调在两者完成后进行。所有 Python 改动提交前跑 `py_compile` + 相关单测；前端 JS 改动跑 `node --check`。

### 阶段 ②：repo-onboarding 落库（任务 A1-A4）

#### A1. `_execute_agent` 支持事件级环境变量
- **文件**：`src/api/event_pipeline.py`
- **变更**：`_execute_agent` 的 `env_extra` 构建处（约 :761）合并 `evt.get("_env_extra", {})`：
  ```python
  env_extra = {"REPO_FULL_NAME": repository, "ACCOUNT": exec_account}
  _ext = evt.get("_env_extra") or {}
  if isinstance(_ext, dict):
      env_extra.update(_ext)
  ```
- **验证**：单测 mock `stream_agent` 断言收到 `ONBOARDING_TARGET`；`py_compile`

#### A2. repo-onboarding SKILL 改版（v1.1.0）
- **文件**：`skills/repo-onboarding/SKILL.md`
- **变更**：
  - frontmatter description 改单次单产出语义
  - 新增「目标类型判定」：读 `$ONBOARDING_TARGET`（experience/explore/兜底 experience）
  - 步骤 3A/3B 按目标分支创建**1 个**对应 Issue
  - 步骤 5 写入 `$ONBOARDING_RESULT` 结构化产出（number/title/assignee/target）
  - 硬约束更新：「仅建 1 个 Issue」
- **验证**：读文档检查步骤完整、变量名与代码一致；不涉及代码

#### A3. `src/api/onboarding.py` 落库与分派
- **文件**：`src/api/onboarding.py`
- **变更**：
  - `ONBOARDING_TARGETS = ("experience", "explore")`
  - `trigger_onboarding`：查 VOD 会话（backend/runtime_id/skill_path）→ 循环落两个主事件（`onboarding_exp`/`onboarding_explore`，`issue_id=#0`，`agent_type=vod`，running）→ 按 `runtime_execution_kind` 分派 direct 线程 / queue 入队
  - 新增 `_execute_agent_wrapper`（direct）：注入 `ONBOARDING_RESULT`/`ONBOARDING_TARGET` 到 `_env_extra` → 调 `_execute_agent` → `_finalize_onboarding`
  - 新增 `_finalize_onboarding`：读 result 文件 → `set_issue_stage(repo,#N,"vod","vod",assignee=owner)` → `append_event(onboarding_issue)`
  - 新增 `_enqueue_onboarding_task`（queue）：构建 prompt + `enqueue_task`（env_extra 带 ONBOARDING_RESULT/TARGET）
- **验证**：单测（见 §九：主事件落库、两次分派、VOD 会话读取、`_finalize` 建阶段/建事件、幂等、无产出回退）

#### A4. queue 模式终态回调
- **文件**：`src/api/onboarding.py`（或复用现有 worker 回写机制）
- **变更**：入队任务终态后触发 `_finalize_onboarding`（按现有 `wait_task_terminal` 或 worker 完成回调机制对接）
- **验证**：queue 模式下注入任务，断言 worker 执行后 issue_state 建立

### 阶段 ④：chat 场景拆分与任务对话（任务 B1-B5）

#### B1. SKILL.md 重构 + 三个 references 流程文件
- **文件**：`skills/chat/SKILL.md`、`skills/chat/references/task-handling.md`、`inquiry-handling.md`、`implementation-start.md`
- **变更**：
  - SKILL.md：保留通用约束（安全硬约束/能力边界/链接格式/@用户/输出格式/参考），新增「场景识别」+「调度执行」
  - `task-handling.md`：源自现 §93-160（澄清被分配 Issue → 回帖）
  - `inquiry-handling.md`：源自现 §162-221（需求引导 → 创建 Issue）
  - `implementation-start.md`：源自现 §44-91（审批 → PATCH 标签）
- **验证**：`node --check` 不适用（md）；人工检查三文件与 SKILL 引用一致

#### B2. `chat_conversations` 加列
- **文件**：`src/api/db_setup.py`（`_migrate_tables` 幂等区）、`src/session_manager.py`
- **变更**：
  - `_ensure_column(chat_conversations, "conv_type", "TEXT DEFAULT 'general'")`
  - `_ensure_column(chat_conversations, "issue_number", "INTEGER DEFAULT 0")`
  - `create_conversation` 增加可选参 `conv_type`/`issue_number`（INSERT 加两列）
  - `get_conversation`/`get_user_conversations` 返回新列；`update_conversation` allowed 加 `conv_type`/`issue_number`
- **验证**：迁移后新库/旧库均含两列；单测 CRUD 往返

#### B3. `notify_task` 标记任务会话
- **文件**：`src/api/routes/tasks.py`
- **变更**：创建会话时传 `conv_type="task"`、`issue_number`；复用存量「Issue #N」会话时 `update_conversation` 补标记
- **验证**：单测断言任务会话 `conv_type=task`、`issue_number` 正确

#### B4. 新增 `build_task_chat_prompt`
- **文件**：`src/prompt_builder.py`
- **变更**：新增 `build_task_chat_prompt(user_text, repo_key, history, conv, account="", runtime_id=0)`——注入 `场景: task` + Issue 上下文（仓库/号/标题/摘要）+ 历史 + chat skill 指引
- **验证**：单测断言 prompt 含 `场景: task`、Issue 上下文；不含时为通用 prompt

#### B5. `chat.py send_message` 按类型选 prompt
- **文件**：`src/api/routes/chat.py`
- **变更**：`conv_type == "task"` → `build_task_chat_prompt`；否则 `_build_chat_prompt`
- **验证**：单测任务会话走任务 prompt、一般会话走通用 prompt；`node --check`（前端无改动可跳过）

### 联调与发布（任务 C1-C3）

#### C1. 全链路手动冒烟
- **前置**：A1-A4 + B1-B5 完成
- **场景**：模板一键创建 → 观察两个引导事件 running→completed → 确认 2 个 Issue（#01/#02）落 issue_state=vod → 触发任务通知 → 任务中心"去对话" → 确认 prompt 含 `场景: task` + Issue 上下文 → 澄清后回帖到 Issue
- **验证**：每步 events 表/issue_state/chat_conversations 数据符合 §六

#### C2. 自动化测试补齐
- **文件**：`tests/test_onboarding.py`（扩展）、`tests/test_agent_event_subscription.py`（如适用）、新增 chat/task 相关用例
- **验证**：§九 全表用例通过

#### C3. 全量回归 + 发布
- **命令**：`python -m pytest tests/ -q`（剔除已知环境依赖失败的用例）
- **发布**：提交 → push → `temp/deploy_git_pull.py` 部署两台服务器 → 健康检查
- **验证**：`systemctl is-active` active、`:3000/:8080` 健康、无回归
