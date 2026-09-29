# Issue #15 流水线执行分析：为什么没有给关联仓库（GTD-Backend）提交 PR

- 仓库：GTDTeam/GTD-Portal（主仓，前端）+ GTDTeam/GTD-Backend（关联仓，后端）
- Issue：#15 开发者代表工作台增加操作日志查询功能
- 分析时间：2026-09-29
- 数据来源：grape_system_db（events / agent_tasks / task_messages / event_skipped / managed_repos）、Grape 服务端源码（src/api/routes/webhook.py、src/api/auth_token.py）、GitCode API、implementation 技能快照（skill_files）

---

## 一、结论（大白话）

**流水线只会给"主仓库"建 PR，不会给"关联仓库"建 PR——因为 coding agent 的实现技能和平台注入的环境里，压根没有定义关联仓库该怎么走分支→提交→PR 这套流程；agent 只能自己即兴发挥，直接在部署机上的后端代码检出里改完就 `git push origin lvjuntao` 直推分支，把 PR 和代码审查整个绕过去了。最后是用户手工补的 PR。**

放大这个问题的还有两个帮凶：
1. QA 审查发现问题后想把修复任务派给后端仓库的 coding，被 A2A 鉴权挡死（会话 token 只对 GTD-Portal 有效，对 GTD-Backend 无效，3 次全部 403），自动修复闭环断链；
2. GTD-Backend 虽然是托管仓库，但没配场景/agent 路由（0 个 issue、308 条事件全部 skipped），agent 直推分支后平台侧完全无感知、无追踪。

---

## 二、事件链时间线（2026-09-28，UTC+8）

| 时间 | 事件 | 内容 |
|------|------|------|
| 21:53 | #15 创建 → vod (8518) | 结构化，A2A 交办 issue agent |
| 21:54–21:58 | issue (8519) | 评级 almost-ready：需确认后端 API 就绪性 |
| 22:47–22:51 | 用户评论确认 → issue (8522) | 评级 ready，发布 Spec（明确"后端需同步开发"），A2A coding |
| 22:50–23:00 | coding 第一轮 (8523) | **22:52:46 后端直推**：在部署机 `/home/gtd/projects/GTD-Backend` 检出上 commit `05d36ba` → `git push origin lvjuntao`（**无 PR**）；22:58:29 前端推 `feat/issue-15` → 22:58:51 创建 **PR #229（GTD-Portal）** → A2A qa |
| 22:59–23:11 | qa (8524) | 审查 PR #229 + 后端 commit 05d36ba；发现 1 个 Required（`resolveUser()` 全表遍历应改 `findByEmail()`）；**A2A → coding（payload.repository=GTDTeam/GTD-Backend）3 次全部 403 invalid token**，修复闭环断链，结论"需人工介入" |
| 23:17:39 | 用户手动合并 PR #229 | pull_request_merged (8525) → qa pass → A2A sre |
| 23:18–23:22 | sre (8526) | 构建部署到 127.0.0.1:8080；验证时把 API 403 误判为"需认证=正常" |
| 23:32 | 用户评论报障：操作日志页面 403 | issue agent (8527) 分析 → A2A coding（repository=GTD-Portal，**这次鉴权通过**） |
| 23:33–23:47 | coding 第二轮 (8528) | 定位 403 根因：`t_gtd_operation_log` 表未创建（Flyway 禁用，V190 迁移没跑）；修复 = 手动在部署库执行 V190 + Controller 加 try-catch；**23:39:51 再次直推 lvjuntao commit `904608c`（无 PR）**；重启后端验证 API 200 |
| 23:42:37 | **用户手工建 PR #230**（GTD-Portal，lvjuntao→dev）并合并 | 触发 pull_request_opened (8529) → coding 确认无需变更 |
| 23:43:38 | **用户手工建 PR !273**（GTD-Backend，lvjuntao→dev）并合并 | 平台事件被跳过（issue_done / no_route） |

**结果：GTD-Backend 上与 #15 相关的 PR 只有用户手工建的 !273，流水线在关联仓库上提交的 PR 数量 = 0。**

---

## 三、根因分析

### 主因：实现技能（implementation）对关联仓库零指引

平台确实把关联仓库信息注入给了 coding agent：

- 环境变量：`GRAPE_LINKED_REPOS=[{"repo_key":"gitcode:GTDTeam/GTD-Backend","branch":"lvjuntao"}]`
- A2A context/Spec 明确写了"本仓库为前端仓 GTD-Portal，后端在 GTD-Backend 仓库，需同步开发"

但 implementation 技能的全部提交/PR 规则只针对主仓：

- `implementation-runbook.md` A4："分支纪律：直接用平台预置分支 feat/issue-{n}"——这个预置 worktree 只存在于 `grape-workspaces/.../GTD-Portal/issues/15/coding`（主仓）
- A6："创建 PR：`grape git-repo pulls create --owner $ACCOUNT --repo $REPO_NAME`"——`$REPO_NAME` 只有主仓
- 对 `GRAPE_LINKED_REPOS`、关联仓库、多仓库协作的搜索结果：**0 命中**（技能快照全文检索确认）

agent 面对后端任务没有 worktree、没有分支纪律、没有 PR 要求，于是即兴发挥：找到部署机上现成的后端检出 `/home/gtd/projects/GTD-Backend`（正好停在 lvjuntao 分支上），改完直接 `git push origin lvjuntao`（task_messages seq 245–247、925–927 铁证）。而 lvjuntao 恰好是部署基线分支——直推它等于改动一步到位进入部署链路，PR、QA 审查、代码追溯全部失效。

### 帮凶一：A2A 鉴权按"目标仓库"作用域，跨仓派发必 403

服务端 `_authorize_a2a`（src/api/routes/webhook.py:188）的逻辑：

```
_auth == 目标仓库 bot token  或  verify_session_token_for_repo(_auth, 目标repo_key)
```

QA 会话拿到的 `GRAPE_SESSION_TOKEN`（tk_…）绑定的是 `gitcode:GTDTeam/GTD-Portal`（`verify_session_token_for_repo` 只校验 repo_key 匹配，src/api/auth_token.py:91-102）。QA 把修复任务 A2A 到 `GTDTeam/GTD-Backend` 时：

- 会话 token 的 repo_key（Portal）≠ 目标 repo_key（Backend）→ 不通过
- CLI 发的是会话 token，不是 bot token → 也不通过

尽管两个仓库的 bot token 完全相同（`3Bk_xovwuv…`，已核对 managed_repos），agent 也拿不到它。于是 QA→coding(Backend) 三次重试全部 403，审查发现的 Required 问题无法自动派发修复——只能等用户在 Issue 里再评论，由 issue agent（在主仓上，token 匹配）转一手才派出去。

### 帮凶二：GTD-Backend 无流水线路由，直推完全无感知

GTD-Backend 是托管仓库（webhook 79141，bot token 已配），但：

- `issue` 表中 GTD-Backend 的 issue 数 = 0
- `event_skipped` 中 GTD-Backend 累计 308 条（no_route=156、issue_done=132、no_commit_on_pr_update=15、self_loop=4、action_disallowed=1），无一条进入流水线
- `devops_agent_session` 里 GTD-Backend 的各 agent 绑定从未实际跑过事件（events 表 GTD-Backend 0 行）

所以 22:52:46 和 23:39:51 两次直推产生的 Push Hook 全部 no_route 跳过；用户手工建 PR !273 的 opened/merged 事件也被 issue_done 跳过（#15 在主仓已判 done）。平台对这个仓库上的任何动作既不记录、也不审查、也不追踪。

### 直推绕过审查的实际代价（已发生）

QA 提的唯一 Required 问题——`OperationLogController.resolveUser()` 应把 `findAllOrderByIdAsc()` 全表遍历改为 `findByEmail()` 索引查询——**至今未修**。dev 分支当前代码（API 实拉验证）仍是 `findAllOrderByIdAsc`。第二轮 coding 只加了 try-catch 吞异常，没有做 QA 要求的修改，也没有任何机制拦住它。另外它还直接在生产部署库上手动执行了 V190 迁移（绕过 Flyway/发布流程）。

---

## 四、关键证据索引

| # | 证据 | 出处 |
|---|------|------|
| 1 | 第一轮 coding 后端直推：`git add …V190…` → `git push origin lvjuntao`，无 pulls create | task_messages t_e629872a9549 seq 245–247 |
| 2 | 第一轮 coding 前端走完整流程：feat/issue-15 → PR #229 | 同上 seq 251–263 |
| 3 | 第二轮 coding 再次直推：patch `/home/gtd/projects/GTD-Backend/...` → `git push origin lvjuntao` | task_messages t_60fad78f1bcf seq 906/925/927 |
| 4 | QA A2A → coding(GTD-Backend) 403 × 3："coding handoff 因 403 invalid token 失败（重试3次）" | task_messages t_dd49e77b7d1d seq 372–388 |
| 5 | A2A 鉴权按目标仓库校验会话 token | src/api/routes/webhook.py:188-213 + src/api/auth_token.py:91-102 |
| 6 | implementation 技能无任何关联仓库指引（GRAPE_LINKED_REPOS/关联仓库 0 命中） | agent_tasks.skill_files 技能快照全文检索 |
| 7 | GTD-Backend：0 issue、308 条 skipped（no_route/issue_done）、events 0 行 | grape_system_db 查询 |
| 8 | GTD-Backend 上 #15 唯一 PR !273 由 songspace 手工创建（23:43:38） | GitCode API `/repos/GTDTeam/GTD-Backend/pulls/273` |
| 9 | lvjuntao 分支顶端 904608c（agent 直推），PR !273 携带 05d36ba/904608c 两个直推提交入库 | GitCode API commits?sha=lvjuntao |
| 10 | QA Required（findByEmail）未修，dev 仍为 findAllOrderByIdAsc | GitCode API raw OperationLogController.java?ref=dev |

---

## 五、解决方案

> 按用户要求仅定位与方案，未做任何代码/配置改动。

### 方案 1（治本）：implementation 技能补"关联仓库工作流"章节

在 SKILL.md 和 implementation-runbook.md 中新增关联仓库规则：

- 读取 `GRAPE_LINKED_REPOS`，对其中每个需要改动的仓库，执行与主仓相同的纪律：
  - 基于该仓库注入的 branch（如 lvjuntao）创建 `feat/issue-{n}` 开发分支，**严禁直接 push 基线分支**（lvjuntao/dev 等部署/合入基线）；
  - 改动完成后 `grape git-repo pulls create`（--repo 指向关联仓，head=feat/issue-{n}，base=该仓工作分支）；
  - Issue 完成评论中列出**所有仓库**的 PR 链接，QA 阶段逐仓审查。
- 若关联仓库未提供 worktree，明确要求先在 `grape-workspaces` 下建立受控工作目录，禁止在部署机生产检出（`/home/gtd/projects/*`）上直接改动。

### 方案 2（打通闭环）：A2A 鉴权放行"同场景关联仓库"

`_authorize_a2a` 增加一条放行规则：会话 token 的 repo_key 与目标 repo_key 若属于同一 Issue 场景的关联仓库集合（查 `GRAPE_LINKED_REPOS` 同源的 scene 关联配置，或两仓 bot token 相同即视为同信任域），则放行。这样 QA→coding(Backend) 的审查-修复闭环才能自动流转，不再依赖用户评论转一手。

风险评估：放行范围限于"同场景已关联的托管仓库"，不扩大到任意仓库；bot token 相同本来就是同一运维主体，语义不变。

### 方案 3（可观测）：给关联仓库配最小路由

- 为 GTD-Backend 配置场景（复用 GTD-Portal 的场景或最小 qa/coding 节点），至少让 push/PR 事件入 events 表而不是全部 no_route 丢弃；
- `issue_done` 跳过判定考虑关联仓库：主仓 issue done 不应吞掉关联仓的正常 PR 审查事件（!273 的 opened/merged 被跳过即此问题）。

### 方案 4（护栏）：平台侧防直推

事件流水线或 worker 侧加一道检查：agent 任务对 GRAPE_LINKED_REPOS 中仓库的 git push，若目标 ref 是基线分支（非 feat/issue-* 或 pr 分支），记 warning 事件并评论提示——不阻断（避免误杀），但留痕可审计。

### 遗留修复项（独立小 PR）

- `OperationLogController.resolveUser()` 改用 `findByEmail()`（QA Required，至今未修）；
- 评估 V190 手动建表与 Flyway 状态的偏差（部署库有表、Flyway 禁用状态未记录，后续开 Flyway 可能冲突）。

---

## 六、一句话复盘

主仓有流程、关联仓没流程：技能没写、token 不通、路由没有——三道关卡全开，agent 直推分支就成了最省事的路；堵上任何一道（尤其是技能里写清楚"关联仓库也要建 PR"），这个坑就不会再踩。
