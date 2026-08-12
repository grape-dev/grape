# repo-onboarding 落库接入事件体系设计方案

> 版本：v3.0
> 日期：2026-08-12
> 状态：待评审
> 需求：repo-onboarding 技能（模板一键创建后的首次托管引导）目前是"黑盒"——既不落 `events` 表、也不建 `issue_state`。要求：① 引导过程与结果落入 events 表（用户在 Event 页可见）；② 引导创建的 Issue 在 pipeline 中处于 **vod 阶段**（`issue_state.stage=vod`，由 VOD 先收敛用户需求）；③ **引导由 VOD Agent 负责执行**（而非独立 Agent），引擎可配置（本地无 opencode 时可用远端 worker / 其他引擎）；④ **SKILL 按 prompt 指引创建对应类型的单个 Issue**——`experience` 与 `explore` 各执行一次，不再一次创建两个 Issue。
> 前置：`skills/repo-onboarding/SKILL.md`、`src/api/onboarding.py`、`src/api/event_pipeline.py::_execute_agent`、`src/api/event_store.py`、`src/api/session_store.py`（issue_state 状态机）、`vod-skill-upgrade-plan.md §3.0.2/3.0.3`（阶段矩阵）。

---

## 一、现状与问题

### 1.1 触发链路（现状）

```
模板一键创建成功（templates.create_from_template）
  → trigger_onboarding(repo_key, owner)        src/api/onboarding.py:100
    → _scene_onboarding_skill(scene_id)        读 agent-skill-mapping.json 的 onboarding 技能
    → _is_onboarded 幂等门禁                   meta.onboarded:{repo_key}
    → asyncio.create_task(_run_onboarding)     src/api/onboarding.py:50
      → opencode 执行 repo-onboarding 技能（Onboarding Agent）
        → 分析项目 → 建 2 个 Issue（体验 + 探索）→ assignee=owner → POST /api/tasks/notify
```

### 1.2 缺口（黑盒表现）

| # | 现状 | 用户视角 |
|---|------|---------|
| 1 | `_run_onboarding` 全程**不调用 `append_event`**，不写 events 表 | Event 历史/事件页看不到引导过程 |
| 2 | 不建 `issue_state`，不调用 `set_issue_stage` | Pipeline 视图/阶段矩阵中无引导产生的 Issue |
| 3 | 技能直接调 GitCode API 建 Issue，**Issue 号不回传系统** | 无法把新 Issue 纳入阶段管理 |
| 4 | **硬编码 opencode 后端**（onboarding.py:68 `backend_name="opencode"`），不查 `devops_agent_session`、不走 runtime 队列 | **本地未安装 opencode 时引导直接失败** |
| 5 | **SKILL 一次性创建两个 Issue**（固定"体验 + 探索"），不感知 prompt 指引 | 两个 Issue 的产出意图无差异，无法分别收敛；无法按类型单独触发 |

### 1.3 对比正常事件链路

正常 webhook 事件（`event_pipeline.run_pipeline`）：`append_event`(running) → `set_issue_stage`(建/推进阶段) → agent 执行 → `update_event_complete`(completed/failed)。
onboarding 缺落库两步，且执行端游离于标准 Agent 体系之外，产出方式也与阶段模型不匹配。

---

## 二、设计目标与原则

1. **由 VOD Agent 执行**：引导作为 VOD Agent 的处理环节（`agent_type=vod`），pipeline 呈现「模板创建 → VOD 引导 → 建 Issue → vod 阶段收敛」完整工作流。
2. **引擎可配置**：复用 VOD 会话（`devops_agent_session`）的 backend/runtime_id 配置，走现有 direct / queue 分派——**本地无 opencode 不影响**（远端 worker / codearts 兜底）。
3. **一次引导 = 一个 Issue**：`experience`（快速部署体验）与 `explore`（探索方向引导）各触发一次引导执行，每次 prompt 指明目标类型，SKILL 据此创建**对应类型的单个 Issue**——不再一次创建两个。
4. **skill 按 prompt 指引产出**：SKILL 通过 prompt 注入的 `ONBOARDING_TARGET`（`experience` / `explore`）与对应创建模板，决定创建哪种诉求，保持单次执行单产出。
5. **与事件体系一致**：每次引导以 event 落库（`issue_id=#0` 主事件 + 完成后 Issue 独立事件），复用 Event 页 / 对话 / 归档能力。
6. **issue 阶段归入 vod**：引导建出的 Issue 建立 `issue_state`，`stage=vod`、`owner=vod`——VOD 先收敛需求，符合阶段矩阵 D8；收敛后再 A2A 推进至 issue 阶段。
7. **技能结构化产出回传**：每次引导把创建的 Issue 号写入固定结果文件（方案 A），系统读取后落库。
8. **幂等/防重复**：沿用 `meta.onboarded:{repo_key}` 门禁；`experience`/`explore` 各只执行一次。
9. **默认零回归**：手动托管（`/repos/host`）仍不触发引导；未变更现有 webhook/A2A 逻辑。

---

## 三、整体流程（改造后）

```
模板一键创建成功
  → trigger_onboarding(repo_key, owner)
    ├─ 幂等门禁（onboarded 标记）
    ├─ 查 VOD 会话：backend / runtime_id / skill（devops_agent_session WHERE agent_type='vod'）
    ├─ 落库 2 个主事件（各为一次引导的 running 事件，agent_type=vod, issue_id=#0）：
    │    seq_exp = append_event(event_type=onboarding_exp,  title="引导·快速部署体验", status=running)
    │    seq_exl = append_event(event_type=onboarding_explore, title="引导·探索方向", status=running)
    └─ 两次引导执行（可并行，各自 queue/direct 分派，各自 _execute_agent）：
        ├─ 引导 A（target=experience）
        │    prompt 注入：ONBOARDING_TARGET=experience + SKILL 中「体验引导」指引
        │    └─ VOD Agent 执行 repo-onboarding 技能
        │         ├─ 分析项目（codegraph → 知识基线）
        │         ├─ 创建 1 个「快速部署体验」Issue，assignee=owner（新仓库编号 #01）
        │         ├─ 【新增】写 onboarding-result-exp.json：{"number": 1, "title": "...", "target": "experience"}
        │         └─ _execute_agent 收尾主事件 → seq_exp completed
        └─ 引导 B（target=explore）
             prompt 注入：ONBOARDING_TARGET=explore + SKILL 中「探索引导」指引
             └─ VOD Agent 执行 repo-onboarding 技能
                  ├─ 分析项目（复用 A 的知识基线）
                  ├─ 创建 1 个「探索方向」Issue，assignee=owner（新仓库编号 #02）
                  ├─ 【新增】写 onboarding-result-explore.json：{"number": 2, "title": "...", "target": "explore"}
                  └─ _execute_agent 收尾主事件 → seq_exl completed

  └─ 各自引导完成后（_finalize_onboarding 线程末尾触发，各一次）：
       ├─ 读对应 result 文件 → 该次引导创建的 1 个新 Issue（#01 / #02）：
       │    set_issue_stage(repo, #N, "vod", "vod", assignee=owner)
       │      → 新 Issue 处 vod 阶段：VOD Agent 先收敛用户需求（阶段矩阵 D8：issue_opened 恒给 vod）
       │    append_event(issue_id=#N, event_type=onboarding_issue, agent_type=vod, status=completed)
       │      → 每个引导 Issue 一条独立事件 → pipeline 按 Issue 聚类呈现两个 Issue（#01/#02）
       └─ （主事件已由 _execute_agent 收尾，_finalize 仅补充 Issue 阶段与各 Issue 事件）
```

> **一次引导 = 一个 Issue**：`experience` 与 `explore` 各触发一次引导执行（各自独立事件 seq），SKILL 根据 prompt 的 `ONBOARDING_TARGET` 决定创建哪种诉求——不再一次创建两个 Issue。这使两个 Issue 的 prompt 有差异、产出意图清晰，且每个 Issue 有独立的引导事件轨迹。
>
> **编号 #01/#02**：从项目模板一键创建的新仓库为空仓库，GitCode Issue 编号从 1 递增。引导 A（体验）先创建得 #1，引导 B（探索）后创建得 #2。系统以技能回传的 result 文件实际编号为准（避免编号猜测）。
>
> **阶段归属 vod**：引导创建的新 Issue 状态设在 **vod 阶段**（`stage=vod, owner=vod`）——VOD 建出 Issue 后需先由 VOD 收敛用户需求（体验/探索方向是否清晰、是否需补充），符合阶段矩阵 D8（`issue_opened` 恒给 vod）；需求收敛后再由 VOD A2A 转交给 Issue Agent 进入 issue 阶段。**不直接置 issue 阶段**，否则跳过需求收敛环节。
>
> 关键复用：`_execute_agent`（event_pipeline.py:667）已封装 构建 prompt → 取会话/工作目录 → stream_agent → 更新 event（config/complete/agent_logs）。onboarding 传 `skill_name="repo-onboarding"` 即可覆盖默认 vod 技能，无需自建执行循环。

---

## 四、代码改动清单

### 4.1 `src/api/onboarding.py`

#### 4.1.1 `trigger_onboarding`：落库两个主事件 + 查 VOD 会话 + 两次引导分派

```python
# 引导目标类型：experience（快速部署体验）/ explore（探索方向引导），各执行一次
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

> 说明：`issue_id=#0` 表示该引导主事件是仓库级动作（执行时尚未创建 Issue）；Issue 创建后的独立事件由 `_finalize_onboarding` 补齐。

#### 4.1.2 新增 `_execute_agent_wrapper`（direct 模式）

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
    # 执行完成 → 读结果补 Issue 阶段 + 子事件
    _finalize_onboarding(repo_key, seq, owner, _result_path)
```

> 说明：`_execute_agent` 目前不消费 `_env_extra`。需要在其内部 `env_extra` 合并 `evt.get("_env_extra", {})`（一行改动，见 4.3）。

#### 4.1.3 新增 `_finalize_onboarding`

```python
def _finalize_onboarding(repo_key, seq, owner, result_path=""):
    """单次引导完成补库：读技能产出 → 该次引导创建的 1 个 Issue 建 vod 阶段 + 独立事件。主事件已由 _execute_agent 收尾。"""
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

#### 4.1.4 新增 `_enqueue_onboarding_task`（queue 模式）

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

> worker 端执行完成后回写 event；`_finalize_onboarding` 需在任务终态后触发（入队时由 worker 完成回调，或复用 `wait_task_terminal`——实施期按现有 worker 回写机制对接）。

### 4.2 `skills/repo-onboarding/SKILL.md`：按 prompt 指引创建对应类型 Issue + 结构化产出契约

**核心改动**：SKILL 不再固定创建两个 Issue，而是读取 prompt 注入的 `$ONBOARDING_TARGET`，按目标类型创建**一个**对应诉求的 Issue。

frontmatter description 同步改为单次单产出语义：

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

正文执行步骤改为按目标分支：

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

1. **建立项目知识基线**（参考 issue-advance）
   - 用 codegraph 分析项目结构，生成 `project-graph.md` + `project-capabilities.md`
   - 提炼：技术栈、核心模块、功能特性、启动/部署方式

2. **判定目标类型**（读 `$ONBOARDING_TARGET`，见上）

3A. **快速部署体验（experience）**
   - 创建 **1 个** Issue，标题形如「【体验】快速部署并体验本项目的核心功能」
   - 内容：部署步骤摘要（启动命令/环境要求）、体验路径（如何跑通核心流程）、预期效果
   - assignee = 仓库 owner

3B. **探索方向引导（explore）**
   - 基于知识基线，提炼 2-4 个「有意思、易理解、有真实价值」的探索方向
   - 每个方向：一句话价值 + 期望能力 + 影响范围
   - 创建 **1 个** 汇总 Issue，标题形如「【探索方向】值得探索的 N 个方向」
   - assignee = 仓库 owner

4. **生成任务**
   - 调用 `POST /api/tasks/notify`，`assignee=owner`，`task_description` 简要描述
   ```bash
   curl -X POST {{SERVER_URL}}/api/tasks/notify \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "assignee": "<owner 的 GitCode 用户名>",
       "repo_key": "'"$REPO_FULL_NAME"'",
       "issue_number": "<Issue 号>",
       "task_description": "<该 Issue 的简要任务描述>"
     }'
   ```
   - 若返回 `{ok:false}`，在 Issue 评论说明

5. **写入引导结果**（必须，系统据此建立 Issue 阶段）
   - 创建的 Issue 成功后，在 `$ONBOARDING_RESULT` 指向的文件写入：
     ```json
     {"number": 1, "title": "【体验】快速部署并体验本项目的核心功能", "assignee": "<owner>", "target": "experience"}
     ```
   - `number` 填 GitCode 实际返回的 Issue 号（新模板仓库编号从 1 递增：体验 #01、探索 #02）。
   - `$ONBOARDING_RESULT` 由系统注入（=`data/workspaces/{owner}/{repo}/onboarding-agent/onboarding-result-{target}.json`），直接写该文件，不新建路径。
   - 若创建失败：不写文件（主事件仍 completed，由 _finalize 以"无结果"处理）。

6. **输出引导摘要**
   - 在仓库 README 顶部追加一段「🤖 本仓库已托管，可体验以下内容...」或在该 Issue 中写明

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`；不读 Grape 内部数据（`data/workspaces` 项目知识文档除外）
- 不修改代码、不建 PR；仅建 1 个 Issue + 分配 + 通知
- assignee 必须是"人"（owner），不是 bot 账号
```

### 4.3 `src/api/event_pipeline.py`：`_execute_agent` 支持 `_env_extra`

在 `_execute_agent` 的 `env_extra` 构建处（event_pipeline.py:761）合并事件级扩展变量：

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

### 4.4 `src/api/event_store.py` / `src/api/session_store.py`

无改动（复用现有 `append_event` / `update_event_complete` / `set_issue_stage`）。

---

## 五、数据模型

| 表 | 写入 | 说明 |
|---|---|---|
| `events` | `event_type=onboarding_exp` / `onboarding_explore`（各一次，`issue_id=#0`，`agent_type=vod`） | 两次引导各自的运行总览事件 |
| `events` | `event_type=onboarding_issue`（**每个引导 Issue 各一条**，`issue_id=#01/#02...`，`agent_type=vod`） | 引导产出的每个 Issue 独立记录 → pipeline 按 Issue 聚类呈现两个 Issue |
| `issue_state` | `stage=vod, owner=vod, assignee=owner`（每个新 Issue） | 新 Issue 处于 vod 阶段：VOD 先收敛用户需求，收敛后 A2A 推进至 issue 阶段 |

> 说明：`onboarding_exp` / `onboarding_explore` / `onboarding_issue` 均不在 `ISSUE_EVENT_TYPES` / `PR_EVENT_TYPES` / `NON_ISSUE_EVENT_ROUTE` 中，因此不会被 pipeline 重新路由（完成态记录，不触发新 agent），与 `a2a_event` 的记录语义一致。`agent_type=vod` 使事件归入 VOD 名下。

---

## 六、用户可见效果

1. **Event 历史**：模板创建后出现两条引导 running 事件（「引导·快速部署体验」「引导·探索方向」，均归属 VOD Agent）；各自完成 → completed，结论含创建的 Issue 号。
2. **Event 详情**：每个引导主事件可点入查看 VOD Agent 答复结论（result），复用现有详情页（含对话/知识产出）。
3. **Pipeline 视图**：完整工作流——VOD 处理两次引导 → 各建 1 个 Issue（#01 体验、#02 探索）→ 各 Issue 处于 vod 阶段（VOD 收敛需求中）→ 后续 VOD 收敛后 A2A 推进到 Issue Agent。每个引导 Issue 都有独立事件与阶段轨迹。
4. **引擎无关**：VOD 会话配置的引擎（含远端 worker）即可执行，本地无 opencode 不影响。

---

## 七、边界与取舍

| 项 | 决策 | 理由 |
|---|---|---|
| 执行方 | VOD Agent（`agent_type=vod`） | pipeline 呈现完整工作流；复用 VOD 会话引擎配置，本地无 opencode 可跑 |
| 引导粒度 | `experience` / `explore` 各执行一次（两次引导） | 两个 Issue 的 prompt 有差异、产出意图清晰；每个 Issue 有独立引导轨迹 |
| 主事件 issue_id | `#0`（仓库级） | 引导执行时尚未创建 Issue；与 push 一致，避免误归入某 Issue 流水线 |
| 每个引导 Issue 独立事件 | 是（`onboarding_issue`，各挂 `#01/#02`） | 用户可按 Issue 追踪，Pipeline 分组聚类呈现两条 Issue 工作流 |
| Issue 编号 | 新模板仓库从 #1 递增：体验 #01、探索 #02 | 空仓库 GitCode 顺序分配；以技能回传实际编号为准 |
| 技能产出方式 | 方案 A（`ONBOARDING_RESULT` 文件，按 target 分文件） | 无需事后轮询 GitCode API；单次引导单产出，文件天然隔离 |
| 阶段归属 | `stage=vod, owner=vod`（**非 issue 阶段**） | 新 Issue 需 VOD 先收敛用户需求（阶段矩阵 D8）；收敛后再 A2A 推进 |
| 引擎/模式 | 复用 VOD 会话 runtime_id，走 queue/direct 分派 | 本地无 opencode 时 queue 模式由 worker 执行 |
| 幂等 | `meta.onboarded:{repo_key}`（现状保留） | 换场景不重跑；`experience`/`explore` 各只执行一次 |
| 手动托管 | 不触发（现状保留） | 仅模板一键创建链路引导 |
| 失败处理 | 单次引导创建失败：不写 result 文件，主事件 completed 但 `_finalize` 无 stage 写入 | 一次引导失败不影响另一次引导 |
| VOD 会话缺失 | 回退 `backend="opencode"`（与现状一致）；引导仍触发 | 模板创建链路 `ensure_default_agent_sessions` 已建 VOD 会话，正常不缺 |

---

## 八、测试计划

| 用例 | 断言 |
|---|---|
| `trigger_onboarding` 落库两个主事件 | events 表新增 `onboarding_exp` + `onboarding_explore` 各一条，status=running，issue_id=#0，agent_type=vod |
| 两次引导分派 | `experience` 与 `explore` 各触发一次执行（direct 线程 / queue 入队各一次） |
| VOD 会话配置读取 | runtime_id/backend 从 devops_agent_session(vod) 正确取到；缺失回退 opencode |
| `_execute_agent` 合并 `_env_extra` | ONBOARDING_RESULT / ONBOARDING_TARGET 注入到子进程环境 |
| SKILL 按 target 分支 | prompt 注入 target=experience 时执行体验分支；target=explore 时执行探索分支 |
| `_finalize_onboarding` 读结果建阶段 | 对 result 文件中 Issue：`issue_state.stage=='vod'` 且 `owner_agent=='vod'`、assignee 正确 |
| `_finalize_onboarding` 建独立事件 | 该 Issue 对应一条 `onboarding_issue` 事件，issue_id=#1（体验）/ #2（探索），agent_type=vod，status=completed |
| 主事件结束 | 各自 `update_event_complete` 后 status=completed、result 含结论 |
| queue 模式 | runtime_id 绑定 worker 时 `enqueue_task` 被调用（两次），任务由 worker 执行 |
| 幂等 | 已 onboarded 仓库再次触发：不重复建事件/阶段 |
| 无产出回退 | result 文件缺失/为空：主事件 completed（无 stage 写入），不报错 |
| 现有回归 | 手动托管不触发；webhook/A2A 链路不受影响（`onboarding_*` 事件类型不参与路由） |

---

## 九、实施顺序（后续排期）

1. `src/api/event_pipeline.py`：`_execute_agent` 合并 `_env_extra`（一行）。
2. `skills/repo-onboarding/SKILL.md`：改版 v1.1.0——按 `$ONBOARDING_TARGET` 分支创建单个对应 Issue + 结构化产出契约。
3. `src/api/onboarding.py`：`trigger_onboarding` 落库两个主事件 + 两次分派；新增 `_execute_agent_wrapper` / `_finalize_onboarding` / `_enqueue_onboarding_task`（各带 target）。
4. queue 模式终态回调（worker 回写后触发 `_finalize_onboarding`）。
5. 测试落地（§八）+ 全量回归（onboarding / event / pipeline / a2a / runtime queue 相关）。
