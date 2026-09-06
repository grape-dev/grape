# 问题定位：Issue 级会话续接指针写回错位（onboarding 双 Issue 表象 × 普通任务通用缺陷）

> 定位日期：2026-09-06 | 类型：通用缺陷（非 onboarding 专属）
> 关联：Issue #46 onboarding 重构（hello-calculator-12）验证时发现
> 状态：**定位完成，待修复评估**
> 文档：本文件仅做问题定位与影响分析，不含修复实施。

## 一、问题现象

在 `Jason7602/hello-calculator-12` 上，onboarding 一次性创建 #1/#2 两个引导 Issue，
VOD 流水线表现为**串行**：`#2` 先被 worker 认领并跑完（17:35-17:38），`#1` 等待约 2.5 分钟
后才开始（17:38）。且两者日志中 runtime session 同名，前端「实时日志」在等待期为空。

疑惑点：按设计「每 Issue 独立 session、跨 Issue 并行」（multi-issue-isolation-design.md §3.3），
#1/#2 不该共用同一个 session、不该串行。

## 二、设计意图（为什么本应每 Issue 独立 session）

multi-issue-isolation-design.md §1/§3 明确定义：

| 层 | 维度 | 说明 |
|----|------|------|
| 配置层 | `devops_agent_session`，键 `(repo_key, agent_type)` | "这台 repo 的这个 Agent 配了什么" |
| 执行层 | `issue_agent_session`，键 `(repo_key, agent_type, issue_id)` | "这个 Issue 的这一段处理上下文"；**纯粹 per-Issue** |

> 核心原则：**配置与执行分离**。`devops_agent_session` 保留管理端 repo 级配置；
> 执行按 Issue 隔离，`runtime_session_id`（opencode `--session` 续接指针）按
> `(repo, agent, issue)` 存储，**同 Issue 串行、跨 Issue 并行**。

写回模型（§3.3 步骤 5）：
> 完成时：`update_issue_session(issue_session_id, new_runtime_session_id)` ← **续接指针只写回 Issue 级**

## 三、数据表结构（配置层 / 执行层）

### 3.1 `devops_agent_session`（配置层，repo+agent 级）

来自 `src/api/db_setup.py:45-61` DDL + 历次 `_ensure_column`/`ALTER` 追加列（skill 名称→skill_id、backend→runtime 术语重构等），**最终完整列**：

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | TEXT PRIMARY KEY | 配置会话 id（uuid），`agent_tasks.agent_session_id` 外键 |
| `repo_key` | TEXT NOT NULL | 仓库（带平台前缀，如 `gitcode:owner/repo`） |
| `agent_type` | TEXT NOT NULL | vod / issue / coding / qa / sre / chat |
| `runtime_id` | INTEGER DEFAULT 0 | 执行引擎（`agent_runtimes.id`；由旧 `backend_id` 迁移改名，见 db_setup.py:1966-1968） |
| `skill` | TEXT DEFAULT '' | 技能名（旧列，已迁移到 `skill_id`） |
| `skill_id` | INTEGER DEFAULT 0 | 技能 id |
| `runtime_session_id` | TEXT DEFAULT '' | **重要**：opencode `--session` 续接指针（repo+agent 级）。**当前无真实消费场景（push 忽略、无 Issue 任务不执行），建议废弃**——停写/停读、保留空列（见 §七 方案 A；由旧 `backend_session_id` 迁移改名） |
| `repo_gitcode_token` | TEXT DEFAULT '' | 仓库级 bot token |
| `status` | TEXT DEFAULT 'idle' | idle / running 等 |
| `enable` | INTEGER DEFAULT 1 | 是否启用（`_ensure_column` 追加，db_setup.py:1989） |
| `subscribe_events` | TEXT DEFAULT '[]' | 事件订阅 JSON 列表（`_ensure_column` 追加，db_setup.py:1998） |
| `im_provider` / `im_app_id` / `im_app_secret` | TEXT DEFAULT '' | 飞书 IM 配置 |
| `created_at` / `last_active_at` | TEXT DEFAULT datetime | 时间戳 |
| `UNIQUE(repo_key, agent_type)` | — | 一个 repo 的每个 agent 类型唯一一条配置 |

### 3.2 `issue_agent_session`（执行层，repo+agent+issue 级）

来自 `src/api/db_setup.py:385-397`，**最终完整列**：

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | TEXT PRIMARY KEY | 执行会话 id（uuid），`agent_tasks.issue_session_id` 外键 |
| `repo_key` | TEXT NOT NULL | 仓库 |
| `agent_type` | TEXT NOT NULL | vod / issue / coding / qa / sre |
| `issue_id` | TEXT NOT NULL | 关联 Issue（如 `#5`，对应 event.json 的 issue_num） |
| `runtime_session_id` | TEXT DEFAULT '' | **重要**：opencode `--session` 续接指针（Issue 级）；首次创建恒为空（`_seed=""`，不继承 repo 级），执行完成后回写 |
| `status` | TEXT DEFAULT 'idle' | idle / running / failed |
| `last_active_at` | TEXT DEFAULT datetime | 最近活跃 |
| `created_at` | TEXT DEFAULT datetime | 创建时间 |
| `UNIQUE(repo_key, agent_type, issue_id)` | — | 一个 Issue 在同一个 agent 类型下唯一一条执行会话 |

### 3.3 两表关系（写回路径）

```
devops_agent_session (repo+agent，配置 + repo 级续接指针)
        ▲ ② 执行完成：UPDATE devops_agent_session SET runtime_session_id=...  ← 缺陷（多余写）
        │
   import / 入队取配置指针（仅无 Issue 任务）
        │
issue_agent_session (repo+agent+issue，Issue 级续接指针)  ● ① 执行完成：update_issue_session(...)（设计正确）
```

## 四、代码复核：两条路径如何取值/写回

### 4.1 入队取会话（`_enqueue_pipeline_task`，event_pipeline.py:1291-1312）

```python
oc_session_id = session.get("runtime_session_id") or ""   # 先取 repo 级
if _issue_num_from(issue_id):
    _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
    issue_session_id = _isess.get("id") or ""
    oc_session_id = _isess.get("runtime_session_id") or ""   # 有 Issue 就改用 Issue 级
```

- **对所有 issue_opened 任务一视同仁，无 onboarding 特判**。
- 首次执行时 `issue_agent_session.runtime_session_id` 恒为空（session_store.py:499-500：
  新 Issue `_seed=""`，显式"不继承 repo 级 session 指针"）。

### 4.2 worker 串行粒度（agent.py:1333-1337）

```python
sid = task.get("issue_session_id") or task.get("agent_session_id") or ""
# 并发槽 key = issue_session_id（跨 Issue 并行）；空值（#0/旧任务）回退 agent_session_id
```

- worker 端本就按 `issue_session_id`（两个 Issue 两个不同 id）判断并发槽，机制正确。

### 4.3 执行收尾写回（`_execute_agent` 完成处，event_pipeline.py:1910-1924）—— **缺陷点**

```python
if new_oc_session_id and new_oc_session_id != oc_session_id:
    if issue_session_id:
        from src.api.session_store import update_issue_session
        update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id)   # ① Issue 级 = 设计正确
    from src.session_manager import db, db_lock
    with db_lock:
        c.execute("UPDATE devops_agent_session SET runtime_session_id=? ...",   # ② 无条件写 repo 级 ← 缺陷
                  (new_oc_session_id, repo_name, agent_type))
        db.commit()
```

## 五、根因

**实现与设计意图背离在 `②`**：`_execute_agent` 对有 `issue_session_id` 的任务，
写完 Issue 级①后**仍会无条件写 repo 级 `devops_agent_session.runtime_session_id`（②）**。

按设计，`devops_agent_session.runtime_session_id` 只应服务于**无关联 Issue**（push/#0）任务
的会话续接（event_pipeline.py:1694-1695 注释：repo 级 session 仅无关联 Issue 用）。
但现在任何真实 Issue 任务完成后都会覆盖它，产生连锁后果：

1. **repo 级指针被反复污染**：后完成的 Issue 将其覆盖为自己的 session id，repo 级
   指针永远等于"最后一个跑完的 Issue 的 session"，不再是"无 Issue 场景的干净指针"。
2. **表象 →「同级会话串行」**：onboarding 双 Issue 并发时，#1 与 #2 的 `issue_session_id`
   各自独立（无并发冲突），但**二者的 `new_oc_session_id` 都源自同一 repo 级会话
   初始化链路**（首跑 `_seed=""`，但 tmp 续接/引擎连接复用可能让二者拿到同源 session），
   `UPDATE devops_agent_session`（②）与 `update_issue_session`（①）共写同一地址的来源
   session → 数据上出现两个 issue_session 指向相同 runtime session 的观感；
   worker 侧虽按 issue_session_id 并行判断，executor 内同源 engine 连接被前一个占用
   → 表现为实际串行。
3. **普通任务同样受影响**：任何两个真实 Issue（不同仓库/不同用户/不同时刻）
   并发推进到 `_execute_agent` 收尾时，②都会让 repo 级指针被后完成者覆盖；
   #1 的时序（先读空指针 → 迟迟不回写）与 #2 覆盖 repo 级后的混乱，是通用代码路径
   自然产生的结果，**onboarding 只是制造了"同仓库、同秒、双 Issue"的最优复现环境**。

## 六、影响范围

| 场景 | 是否受影响 | 说明 |
|------|-----------|------|
| Onboarding 双引导 Issue（#1/#2 同时） | ✅ 必然 | 一次性并发，复现率最高 |
| 普通同 repo 多 Issue 并发处理 | ✅ | 各 Issue 完成后②互相覆盖 repo 级指针 |
| 不同 repo 同时处理 | ✅ | ①按 repo 写，repo 级指针仍被本 repo Issue 覆盖污染 |
| 无关联 Issue 任务（push/#0） | ✅ 间接 | 读到的 repo 级指针可能指向某个 Issue 的残留 session，cwd/上下文错位 |
| 同 Issue 多轮续接 | ⚠️ | ①写在 Issue 级，本身可用；②多余写带上副作用 |

## 七、修复方向（评估中，不实施）

### 方案 A（推荐，根治）：废弃 repo 级 `devops_agent_session.runtime_session_id`

**依据**：repo 级续接指针的"唯一设计用途"（无关联 Issue 任务续接）在当前路由下**不存在**——
- `routing.py:18` `push 显式忽略（开发由 issue 驱动，不触发 agent）`；
- 无 Issue 的非 Issue 事件（release/tag/build/deploy）归一化 `#0`，仅事件快照不执行 VOD 链路；
- 所有真实执行均为 Issue 驱动 → 全部走 issue 级 session。

废弃后，数据侧读写如下（**连"②写回"根因一起消失**）：

1. **停写**：删除两处 `UPDATE devops_agent_session SET runtime_session_id=...`
   - `event_pipeline.py:1921`（`_execute_agent` 收尾）
   - `runtime/service.py:1374`（`complete_task` 收尾）
2. **停读**：`_enqueue_pipeline_task`（L1307-1312）与 `_execute_agent`（L1707-1712）中
   "无 Issue 时读 repo 级"回退逻辑删除——`oc_session_id` 仅来自 issue 级；
   `#0`/无 Issue 不执行，无需续接。
3. **列保留、值恒空**：不做 ALTER 删列（省迁移成本）；字段在
   `list_agent_sessions`/`agents.py` 展示为空即可。
4. **`clean_agent_session`（agents.py:289）** 语义保留（幂等置空，本就不再写）。
5. 影响面核对：Chat Agent（聊天）续接走独立 `chat_agent_session` 表，不受影响。

**优点**：无判空分支、无"回退到 repo 级"的残留语义，从根上消除本定位的污染路径，
且对未来"无 Issue 执行"也不构成安全网（当前本就不支持）。改动量小（删 2 处 UPDATE + 2 处 SELECT 回退）。

### 方案 B（备选，最小改动但留隐患）：`issue_session_id` 非空时跳过②

```python
if issue_session_id:
    update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id)
else:
    # 仅无关联 Issue（push/#0）才写 repo 级
    UPDATE devops_agent_session SET runtime_session_id=...
```

与设计 §3.3「续接指针只写回 Issue 级」对齐、影响面小；但保留"repo 级指针"字段语义，
`#0`/无 Issue 场景未来若放开执行仍可能踩同一污染问题。

### 备选 C（不推荐单独采用）：onboarding 首跑强制 `oc_session_id=""`

缓解 onboarding 表象，未触及通用根因，不建议。

### 回归验证（A/B 通用）

- `tests/test_issue_session.py`：续接指针隔离 / 空指针重建 / 跨 Issue 并行 / **完成时 repo 级不被写**
- 新增：两处 UPDATE 删除后断言 `devops_agent_session.runtime_session_id` 保持空
- 增量验证：Jonboarding 双 Issue 并发不串行、普通多 Issue 并发互不影响

## 八、关键代码位置

- 入队取会话：`src/api/event_pipeline.py` `_enqueue_pipeline_task` L1291-1312
- 执行收尾写回（缺陷点）：`src/api/event_pipeline.py` `_execute_agent` L1910-1924
- worker 串行粒度：`src/runtime/agent.py` L1333-1337
- Issue 级会话获取：`src/api/session_store.py` `get_or_create_issue_session` L495-534
- Issue 级指针写回：`src/api/session_store.py` `update_issue_session` L537-560
- 设计依据：`docs/new-version1/multi-issue-isolation-design.md` §1.1 / §3.3（R2、写回模型）
- 相关历史：`docs/new-version1/vod-agent-hang-rootcause-fix.md`（方案 A：Issue 首跑不继承 repo 级指针）

## 九、结论

- **这不是 onboarding 专属问题**：`_execute_agent` 收尾对"自带 issue_session_id 的任务"
  保留了对 `devops_agent_session.runtime_session_id` 的无条件写回（②），与设计
  「配置/执行分离、续接指针仅写回 Issue 级」矛盾。
- onboarding 双 Issue 并发只是**最易复现**的场景；普通多 Issue 并发、甚至单 Issue
  任务完成，都会触发 repo 级指针被覆盖的副作用。
- **彻底解法（推荐方案 A）**：repo 级 `runtime_session_id` 当前无真实消费场景
  （push 忽略、无 Issue 不执行），直接**废弃（停写/停读，保留空列）**——
  连 ② 写回根因一并消除，比方案 B 加 `issue_session_id` 判空更彻底。
- 待拍板后实施（倾向方案 A），并补回归测试。