# 方案：Issue 事件重复入库（双 issue_opened / vod 双评论）修复

> 状态：待实施（2026-10-11）｜目标分支：weekly-06 / 集群（weekly-04 语义同步）
> 事件：`grape-dev/grape#296`（新建需求）同一秒落库两条 `issue_opened`（events `#10112`/`#10113`）→ vod 跑两次 → 同 issue 两条评论。
> 本文档对比两事件 payload 实证，定位根因（webhook 真实事件 + `create_task` 同步补发合成事件 + 跨进程去重失效），给出修复方案。

---

## 一、问题现象与证据

`#10112` / `#10113`（grape-dev/grape, Issue #296, agent=vod, 同一秒 15:47:36Z）：

| 维度 | #10112 | #10113 |
|------|--------|--------|
| 来源 | **GitCode 原生 project webhook** | **create_task 同步补发合成事件** |
| payload 特征 | `project`/`object_attributes`/`hook_id=75407`/`uuid`/`produce_random_id` | 仅 `action=opened` / `issue{number,title,user}` / `repository{full_name}` / `sender{login}`（GitHub 格式 4 键） |
| 解析 | GitCode 后端归一化 | 同一 GitCode 后端归一化（gitcode.py `_extract_*` 兼容读 `repository/sender/issue`） |

> 用户侧确认：GitHub 仓库**并未**触发任何 webhook——#10113 **不是**外部 GitHub 事件，而是系统自身构造的合成 payload（见 §二）。

---

## 二、根因分析

### 2.1 双源头：webhook 真实事件 + create_task 同步补发

`create_task_via_issue`（`POST /api/issues/{owner}/{repo}`，issues_center.py:1405-1429，Issue #89「同步触发 pipeline，不依赖 webhook（webhook 异步可能丢事件）」）：

```python
if created:
    set_issue_stage(repo_key, f"#{issue_num}", "vod", "vod", assignee=...)
    _pipe_payload = {"action": "opened",
                     "issue": {"number": int(issue_num), "title": ...,
                               "user": {"login": assignee or user["username"]}},
                     "repository": {"full_name": f"{owner}/{rname}"},
                     "sender": {"login": assignee or user["username"]}}
    _pipe_headers = {"X-Gitcode-Event": "issue_opened"}      # GitCode 也发 GitHub 格式 payload
    run_pipeline(_pipe_payload, _pipe_headers, account=repo_key, repo_key_override=repo_key)
```

链：Task Center 建 #296 → ① GitCode API 建 Issue → GitCode 原生 webhook 回调 → **#10112**；② 同一次请求内 `run_pipeline` 又被同步调用 → **#10113**。两条都路由 issue_opened→vod → vod 双跑 → 双评论。

> 注意：双源是**潜在**的——通常由「先到先得」去重收敛为 1（实证 #303 仅 1 条，幸存者为同步补发的合成事件）；**仅当两条并发命中去重竞态窗口才会双跑**（#296 即此）。

### 2.2 去重为何没拦住第二条（跨进程竞态）

事件去重（event_pipeline.py:1702-1720）在**进程内 `db_lock`** 临界区检查「同 `(repo_key, issue_id, agent_type)` 已有 `running/dispatched` 事件则跳过」。两条事件分别产生于**不同执行上下文**（create 请求线程 vs webhook 后台线程/另一进程），各自持独立锁：

- 并发 SELECT 都查不到 running（都未插入）； 
- 即使同进程，webhook 处理在 `ensure_future(to_thread(...))` 后台线程（webhook.py:121-127），与 create 请求线程并行；
- 集群还可能多进程/多模块，进程内锁完全不可互相。

→ 双双通过 dedup → 重复入库。

---

## 三、修复方案

### 方案 A（治本，推荐）：DB 级活跃事件唯一守卫（跨进程原子）

以「活跃 = 存在未终态事件」为核心，把检查-插入收敛为**唯一索引原子抢占**，替换依赖进程内锁的 SELECT-then-INSERT：

1. 新增守卫表（SQLite/MySQL 通用）：
   ```sql
   CREATE TABLE IF NOT EXISTS event_active_guard (
     repo_key    VARCHAR(255) NOT NULL,
     issue_id    VARCHAR(64)  NOT NULL,
     agent_type  VARCHAR(32)  NOT NULL,
     event_type  VARCHAR(64)  NOT NULL,
     seq         INTEGER NOT NULL,          -- 对应 events.seq
     created_at  TEXT NOT NULL DEFAULT (datetime('now')),
     PRIMARY KEY (repo_key, issue_id, agent_type, event_type)
   );
   ```
2. 插入事件前先 `INSERT IGNORE`（MySQL `INSERT IGNORE` / SQLite `INSERT OR IGNORE`）守卫行：`rowcount=0` → 已存在活跃事件 → **跳过**（记 skipped，日志 `dedup:active_...`）；`rowcount=1` → 抢占成功，继续 INSERT events + 派发。
3. 事件进入终态（completed/failed/cancelled）删除守卫行；另加**过期兜底清理**（守卫行存活超 TTL，如 30 分钟，由既有 purge 任务一并清理，防进程崩溃遗留成永久跳档）。
4. 兼容：保留原 `db_lock` 内逻辑作为单进程快路径，但以守卫表原子抢占为准。
5. MySQL 唯一索引跨进程天然原子 → 无论 create 请求、webhook 后台线程、多 worker、多实例，**同一 (repo, issue, agent, event) 同时只能有一条活跃事件**。

> 语义验证：#296 的 #10112/#10113 都命中 `(gitcode:grape-dev/grape, #296, vod, issue_opened)` → 先到者抢注守卫、后到者跳过，vod 只跑一次。

### 方案 C（辅助）：同 (issue, agent) 近期同类事件快速去重

在方案 A 基础上，把「已终态但近期（如 60s 内）存在同类事件」也视为重复（处理 GitCode 偶发重发/终态后再投的窗口）：守卫表改为按 `(repo_key, issue_id, agent_type, event_type)` 且 `created_at > now-60s` 判定，超窗自然失效（由占位过期处理），无需终态删除即自愈。与 A 合并实现。

### 方案 D（一致性清理，**强建议**）：create_task 同步补发按平台发原生 payload

`create_task_via_issue` 对 GitCode 目前发 **GitHub 格式合成 payload**（仅换 header），虽 GitCode 后端能解析，但：
- 事件 payload 与 GitCode 真实 webhook 形态不一致，排查时极易被误判为「GitHub 触发的 webhook」（本次 #10113 即如此）；
- 合成 payload 与真实 webhook payload 算不出同一指纹，任何基于 payload 的细粒度去重都无法覆盖双通道（只能靠语义 key 收敛）。

改为：GitCode 平台构造 GitCode 原生 payload（`object_attributes.iid/action/title + project.path_with_namespace + user.username`），GitHub 平台维持 GitHub 格式。**可读性/可排查性 + 去重指纹一致性，建议与方案 A 一并落地。**

### 方案 B（源头权衡，结论=保留并明确职责边界）：是否保留 create_task 同步补发

**结论：保留**。补发不是「多余的重复发送」，而是独立于 webhook 的「主动入流」通道，职责 = webhook 兜底。

**什么时候会补发**：仅 `create_task_via_issue`（Grape Task Center 统一建需求入口，`POST /api/issues/{owner}/{repo}`）**新建成功**（created=True）时同步触发；复用已有 Issue 不补发；GitCode/GitHub WebUI 直接开的 Issue **不经过此入口**，只有 webhook，无补发。**潜在双源**只存在于「Grape 自己建」这一条路径。

**为什么大多数需求只被处理一次（#303 实证，2026-10-12）**：Grape 建的需求确实每次都有两个潜在源（同步补发 + GitCode webhook），但通常只有**一个能落库**——事件去重按「先到先得」序列化：

- `create_task` 在 GitCode API 返回后**同一请求内同步**跑 `run_pipeline`（合成事件，先落 `running`）；
- GitCode 原生 webhook 需要一次独立的 HTTP 往返（RTT + 集群处理），**通常晚几十~数百 ms 才到达**；
- 后到者 SELECT 到先到者的 `running` 事件 → 被 dedup 跳过。

实证：`grape-dev/grape#303`（Task Center 创建）events 只有 **1 条** `issue_opened`（seq 10228，**合成 payload**），说明同步补发先到、原生 webhook 被去重拦下。**这证明双源 ≠ 双跑**。

**那 #296 为什么双跑了（竞态窗口）**：`#296` 的 #10112（原生 webhook）与 #10113（合成补发）落在**同一秒**，两个执行上下文（create 请求线程 vs webhook 后台线程/进程）并发进入「SELECT 检查 → INSERT」临界区，**彼此 SELECT 都发生在对方 INSERT 之前** → 双双通过 → 双事件。跨进程下 `db_lock` 互不互斥，这个亚毫秒竞态窗口正是去重失败的唯一通道（也是方案 A 的 DB 级唯一守卫要关掉的窗口）。

**为什么 webhook 正常还要补发**：`create_task` 在创建那一刻**无法预知**仓库 webhook 是否配置、平台是否会投递、是否中途丢失。webhook 对「API 创建」本就不保证可靠送达：
- 仓库托管（managed_repos）≠ GitCode 项目 hook 一定配置/未失效（hook 被删 / token 变更 / 从未注册）→ 事件根本不来；
- API/bot 创建的行为，平台可能按「免回声/机器人行为」不播 webhook，或投递超时重试耗尽被丢弃；
- 集群中 webhook 通道（8080/域名）与主 API 分离、nginx/LB 任一环节抖动都会丢。

**历史一贯性**（查证）：初版（2026-09-11 前）`create_task` 就是直接 `append_event(issue_opened, vod, source="task_center")` 主动入流；2026-09-11 commit 208913561 改为「同步调 run_pipeline」（此时对 GitCode 也喂 GitHub 格式 payload，引入本次隐患）。「创建后主动入流、不赌 webhook 必达」是与平台可靠性博弈后的既定设计，去掉即回归「静默丢需求」。

**因此**：补发保留，其重复收敛完全交给方案 A（DB 级守卫）——webhook 缺失时补发兜底、webhook 正常时 A 将第二份收敛为 1，二者互为补充而非矛盾。

**推荐组合：A（主）+ C（并入 A）+ D（强建议）+ B 保留补发**。

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/db_setup.py` | 新增 `event_active_guard` 建表 + 索引；purge 任务纳入守卫行过期清理 |
| `src/api/event_pipeline.py`（run_pipeline 去重段 :1702-1735） | 用守卫表原子抢占替代/叠加进程内锁检查；终态释放守卫；记 `dedup:active_...` |
| `src/api/event_store.py` | 终态更新（completed/failed/cancelled）时释放对应守卫行（按 seq 反查） |
| `src/api/issues_center.py` `create_task_via_issue` | 方案 D：GitCode 分支发 GitCode 原生 payload（保持同步补发本身） |
| `tests/` | 新增：并发双 run_pipeline 同 issue → 仅 1 事件+1 任务、后到被 `dedup` 跳过；终态后守卫释放可再次处理；reopen/comment 多次事件不受影响 |

---

## 五、影响面 / 兼容性 / 安全

| 项 | 说明 |
|----|------|
| 行为变化 | 仅收敛「同一 (repo, issue, agent, event) 并发活跃」为 1 条；正常串行事件流不受影响 |
| 不误伤 | key 含 `agent_type`+`event_type`：同 issue 多个 agent / 多变事件（comment/clarify）可并发，不互斥 |
| 剔除范围 | 只挡「活跃或近 60s」重复；历史已入库重复数据不回溯删除（暴露层不去重），如需清洗另做迁移脚本 |
| 崩溃兜底 | 守卫行 TTL 过期自动失效，进程挂掉不会永久锁死该 issue |
| 多库 | 守卫表同时建 SQLite/MySQL；INSERT OR IGNORE / INSERT IGNORE 两方言均支持 |
| 安全 | 纯收敛类修复，不改变事件 payload/执行权限模型 |

---

## 六、测试用例设计

### 6.1 单元 / 集成用例（tests/，mock 引擎与 DB）

| 编号 | 用例 | 前置 / 步骤 | 断言 | 覆盖 |
|------|------|-------------|------|------|
| UT-01 | 守卫表原子抢占（同 key 第二次 INSERT OR IGNORE） | 先插入 `(repo, #1, vod, issue_opened)` 守卫行，再同 key 尝试抢占 | 第二次 `rowcount=0`，events 未新增 | A |
| UT-02 | 串行双投同 issue_opened（同进程顺序调用 run_pipeline） | 连续两次 `run_pipeline`（GitCode 原生 payload） | 仅 1 条事件；第 2 次返回 `skipped=duplicate_active_task`，`append_skipped_event('dedup:active_...')` | A |
| UT-03 | 并发双投（同 issue 两「进程」同时到达） | 用两个独立线程/两处调用同时 run_pipeline（补发合成 payload + webhook 原生 payload 各一份，语义 key 相同） | **仅 1 条事件、1 次派发（vod 任务仅 1 个）**；后到者日志 `dedup:active_...` | A（跨进程竞态主场景，等价 #10112/#10113） |
| UT-04 | 终态释放后可再次处理 | 事件 completed 后释放守卫；再发同 key 事件 | 可正常建新事件（reopen/新 round 不跳档） | A |
| UT-05 | 守卫 TTL 过期兜底 | 插入守卫行并伪造老 created_at，跑 purge | 守卫行被清理；后续同 key 事件可处理 | A |
| UT-06 | 近窗去重（方案 C） | 事件已 completed 但在 60s 窗内，再投同 key | 视为重复跳过；> 60s 后可处理 | C |
| UT-07 | 多事件类型/多 agent 不误伤 | 同 issue：issue_comment×N、a2a_event、coding+qa 并行事件 | 各自独立、全部落库，互不拦截 | A 不误伤 |
| UT-08 | create_task 补发 = GitCode 原生 payload（方案 D） | `create_task_via_issue`（gitcode 仓库）新建成功，mock run_pipeline 捕获 payload | payload 含 `object_attributes.iid/action/title` 与 `project.path_with_namespace`；**不再含** `action/issue/repository/sender` 的 GitHub 四键 | D |
| UT-09 | create_task 补发 = GitHub 格式（github 仓库兼容） | `create_task_via_issue`（github: 前缀仓库） | payload 为 `action/issue/repository/sender`，header `X-GitHub-Event: issues` | D 不回归 GitHub |
| UT-10 | 补发保留：webhook 缺失场景有兜底（方案 B） | mock 掉 webhook 通道，仅 create_task 补发路径 | 依旧产生 issue_opened 事件并派发 vod（不静默丢需求） | B |

### 6.2 集群实测回归（grape.example.com）

| 编号 | 场景 | 步骤 | 断言 |
|------|------|------|------|
| E2E-01 | #296 同型回归 | Task Center 新建需求（gitcode 仓库） | events 仅 1 条 issue_opened、vod 仅 1 条评论；`event_active_guard` 无残留行 |
| E2E-02 | 双通道真并发 | 同时走 create_task（补发）与 GitCode webhook（用 curl 模拟重放同 hook id） | 守卫收敛为 1 条事件；第 2 条记 `dedup:active_...` 到 skipped |
| E2E-03 | 一般流程不受影响 | 直接 GitCode WebUI 开 issue / 评论 / PR | 事件正常（无守卫误跳档） |
| E2E-04 | 崩溃自愈 | 制造守卫行不清理（kill 进程）→ 等待 TTL | TTL 后同 issue 新事件可正常处理 |

### 6.3 回归基线

- `tests/` 事件/流水线/vod/comment 相关套件全部通过；全量套件 branch-only 失败 = 0（与既有 359 存量失败基线一致）。
- 集群部署后观察 1~2 条新需求：确认事件数量 == 1 才关闭本 issue。

---

## 七、风险提示 / 待办留档

- 部署顺序：先 DB 建守卫表（幂等）→ 再发布事件去重逻辑；集群侧需把本方案同步到部署分支（现为 weekly-04 语义），避免多版本混跑期间仍存在竞态。
- 守卫表 key 长度：repo_key 建议 VARCHAR(255)、issue_id VARCHAR(64)，MySQL 唯一索引超限需缩短（SQLite 无限制）。
- 若先做方案 D 再做 A，互不冲突；建议拆两个提交分别评审。
- 与问题「流水线到 qa 但阶段仍在 issue」独立，本文不覆盖阶段推进问题（另文结论：阶段仅随 A2A 交办推进，订阅旁路不改 stage）。