# Spec: Issue #453 — bot 评论自循环 + 事件身份判定缺失（自循环检测被级别分派旁路）

> 基准分支：`weekly-08`（560d1b6b7）| Spec 源：Issue #453 终版（已封版）| Developability: ready

## Objective

1. 自循环检测不再依赖 token 解析级别（session/repo_bot/global_bot/user）分派：对评论类事件
   （`issue_comment` / `note_hook_pr`），actor 命中 bot 用户名表（`get_all_bot_usernames()`）时在
   流水线入口无条件跳过，阻断 "bot 评论 → 触发任务 → 任务再评论" 的闭环。
2. 指令与状态变更通道加身份前置校验：`/done` `/cancel` `/resume`（及中文等价词）和
   澄清恢复（awaiting_input → processing）**仅人类 actor 可触发**，bot 评论（含 agent
   引用用户原文的回声）不参与指令解析与状态变更。

## 背景与根因（已对 weekly-08 head 实测核验）

1. `read_stored_token()`（issue-372）在保存 agent 配置时把托管人 PAT 抄写进
   `devops_agent_session.repo_gitcode_token`（存量仓重存配置同样被抄写——本机实测某托管仓
   session token == bot_gitcode_token，`resolve_config_level` 返回
   level=session）。
2. 策略链第一级命中 session → 自循环检测走 `SessionTokenHandler.is_self_loop_actor()`，
   该 handler 写死 `return False`。
3. bot 自己发的评论被当作新用户评论受理 → 循环放大（实测两个事故仓：任务堆积、worker
   被拉黑、issue 卡死 processing）。
4. 2.0b 指令检测位于 2.0 自循环检测之前且不校验 actor（问题 22/23）——bot 或第三方发
   含"确认完成"的评论即可关闭他人需求。
5. 硬约束（问题 37）：issue-multitask 技能用 bot token 直连创建「## 父任务」子 Issue，
   功能上依赖 bot issue_opened 豁免放行，S3-1 必须保留。

架构本质：检测正确性取决于"token 存在哪张表"，而受理方关心"actor 是不是自己家的 bot"。
任何 token 写入路径变化都可能再次旁路检测。

## 方案决策（已定）

- **采用：入口统一拦截（方向 2）**——评论族事件按 actor 判定，与 token 级别完全解耦；
  纯"检测 + 跳过 + 留痕"，零破坏半径。
- **采用：指令/状态通道身份前置校验**——2.0b 分支前加 `_is_bot_actor` 守卫，仅人类 actor 生效。
- **采用：redispatch 重派重校验（问题 29）**——重派前排除 reason in (self_loop, self_loop_path_b)
  的被拦事件，堵住绕过入口拦截的通道。
- **不采用：SessionTokenHandler 内加兜底（方向 1）**——维持"按级别分发"架构，只修一例不修一类。
- **不做（收窄）**：不改 `read_stored_token()` 语义；不动 `SessionTokenHandler.is_self_loop_actor`；
  不覆盖 `issue_updated`（非评论族）；受害仓数据清理属人工处置不进代码。

兼容方案对齐（issue 第十七节）：执行序列第 1 步 = 本 spec 三修复点；第 4 步（停止抄写，
后续项）改 agents.py 调用方写入来源，与"不改 read_stored_token()"不冲突；第 2/3/5 步
各自立 issue，本 spec 不做。

## 场景与场景用例

### S1 session 级仓库 bot 评论（本 bug 核心场景）

前置：session 表带 token（新托管或重存配置），`level=session`，bot_username 已登记。

| 用例 | 事件 | actor | 期望 |
|------|------|-------|------|
| S1-1 | issue_comment | grape_bot（在 bot 表） | 跳过，记 `event_skipped(reason=self_loop, actor=grape_bot)`，不派发任务 |
| S1-2 | note_hook_pr（PR 评论） | grape_bot | 同上跳过，不派发任务 |
| S1-3 | issue_comment | 真实用户（不在 bot 表） | 正常受理，派发任务 |

### S2 存量 repo_bot 级仓库（回归保护）

前置：session 表无 token，`level=repo_bot`。

| 用例 | 事件 | actor | 期望 |
|------|------|-------|------|
| S2-1 | issue_comment | bot | 跳过（行为与原 RepoBotTokenHandler 拦截一致） |
| S2-2 | issue_comment | 真实用户 | 正常受理 |

### S2b 存量仓重存配置后的 session 级污染

前置：存量仓重存过 Agent 配置，session token == bot_gitcode_token（本机实测某托管仓即此态）。

| 用例 | 事件 | actor | 期望 |
|------|------|-------|------|
| S2b-1 | issue_comment | bot | 跳过（判定与级别解耦，无需先清理 session 表 token） |
| S2b-2 | issue_comment | 真实用户 | 正常受理 |
| S2b-3 | （数据态）session 表残留 token，未清理 | — | 拦截不依赖清理动作生效（兜底判断对污染数据零前置要求） |

### S3 非评论族事件不受入口拦截影响

| 用例 | 事件 | actor | 期望 |
|------|------|-------|------|
| S3-1 | issue_opened（body 含「## 父任务」） | bot | 放行（bot 子任务豁免保留，多任务模式硬约束） |
| S3-2 | issue_opened（普通 issue） | bot | 由原 handler 链按级别判定（本期不扩） |
| S3-3 | pull_request_opened 等 PR 族 | bot | 走 1.5 PR 订阅预匹配，不受影响 |

### S4 指令与状态变更通道的身份前置校验（问题 22/23）

前置：bot 用户名在 `get_all_bot_usernames()` 表内；issue 存在活跃任务。

| 用例 | 事件 | actor | 评论内容 | 期望 |
|------|------|-------|---------|------|
| S4-1 | issue_comment | bot | 含 "确认完成" | **不执行** `confirm_issue_done`，不关需求；走 S1 入口拦截 |
| S4-2 | issue_comment | bot | 含 "/cancel" | **不执行** `cancel_issue_task` |
| S4-3 | issue_comment | bot | 含 "/resume" | **不执行** `resume_issue_task` |
| S4-4 | issue_comment | bot | 引用用户原文（原文含 "/cancel"）——agent 引用回声 | 不执行指令（bot actor 不参与指令解析） |
| S4-5 | issue_comment | 真实用户 | 含 "确认完成" | 正常执行 `confirm_issue_done`（人类指令语义保留） |
| S4-6 | issue_comment | 真实用户 | 含 "/cancel" | 正常执行 `cancel_issue_task` |
| S4-7 | issue_comment | bot | 澄清类回复，issue 处 awaiting_input | **不**恢复 processing |
| S4-8 | issue_comment | 真实用户 | 澄清回复，issue 处 awaiting_input | 正常恢复 processing（原行为保留） |

### S6 边界与降级

| 用例 | 条件 | 期望 |
|------|------|------|
| S6-1 | actor 为空 | 不进拦截（`if actor and repo_key` 守卫） |
| S6-2 | repo_key 为空 | 同上 |
| S6-3 | `get_all_bot_usernames()` 抛异常 | 空列表 → 不拦截，走原 handler 链（宁漏拦不误杀） |
| S6-4 | bot 用户名与真实用户同名（理论上） | 该用户评论被跳过并留痕，可复核（同名即同一账号语义） |
| S6-5 | actor 解析为 `"unknown"`（问题 6） | 本期维持放行；unknown 拒绝+告警属准入层 |
| S6-6 | 路径 B 仓库：agent 回帖 actor=托管人本人，任务已 completed | **本期不拦**（issue 问题 4/5 窗口）——根治依赖代发记录表，见 S10/S11 |
| S6-7 | 路径 B 仓库：托管人配置了 bot_username | 命中名单则拦截；否则同 S6-6——覆盖度取决于配置形态 |

### S7 redispatch 重派不得复活被拦事件（问题 29）

前置：事件曾被拦截（`reason in (self_loop, self_loop_path_b)`）；操作者持合法登录身份。

| 用例 | 操作 | 目标事件 | 期望 |
|------|------|---------|------|
| S7-1 | POST /events/{seq}/redispatch | 被 self_loop 拦截的 bot 评论事件 | **拒绝重派**（403/409），不重新入队 |
| S7-2 | POST /events/{seq}/redispatch | 被 self_loop_path_b 拦截的事件 | 同上拒绝 |
| S7-3 | POST /events/{seq}/redispatch | 正常受理过的历史事件 | 重派成功（无回归） |
| S7-4 | 操作者显式传 force/override 类参数 | 被拦事件 | 平台无此语义，不引入绕过通道；确需重派走管理台 |

### S8 兼容方案执行序列（判定本 spec 的兼容边界）

| 用例 | 场景 | 期望 |
|------|------|------|
| S8-1 | 兜底判断上线后，session 表仍带污染 token 的仓库 | bot 评论被拦、用户评论正常——脏数据在场不影响判定（存量不清策略可行） |
| S8-2 | 停止抄写（第 4 步，后续项）落地前：新托管仓保存配置 | session 继续被抄写但兜底已覆盖，循环不发生 |
| S8-3 | 第 4 步落地后：新托管仓保存配置 | session 无 token，级别回落 repo_bot/user；兜底判断独立生效（双保险） |
| S8-5 | 存量 271 仓（从未重存配置） | 全程无感知（用户评论/非评论族路径零变化） |

### S9 双实现并存与部署收口（问题 45）

背景：79 主手工部署过 handler 层兜底（方向 1，未入库）；174 主与 origin/main 均为旧版
`return False`。本 spec 入口拦截（方向 2）在 fix/issue-453-self-loop 分支已实现并测试通过。

| 用例 | 场景 | 期望 |
|------|------|------|
| S9-1 | 两实现并存：bot 评论到达 79 主 | 入口拦截先行命中，handler 兜底为冗余保险，不重复拦截（`event_skipped` 单条） |
| S9-2 | 分支合入 weekly-08 并部署双主后 | 双主 md5 一致；手工版被入库版替代，`git checkout/reset` 不再丢失保护 |
| S9-3 | 合入后 handler 维持 `return False` | 入口拦截独立生效，不构成依赖 |
| S9-4 | bot 名单不完整（问题 24/33） | 该 bot 评论不被入口拦截，回落 handler 链——已知窗口期风险，验收以名单覆盖度抽查 |
| S9-5 | 实例运行旧版代码期间收到 bot 评论 webhook | 不拦截——部署后必须逐实例核对版本 |

**实测核验修正（2026-10-23）**：issue 所称"已提交 78ffda608、双主同步 md5 一致"——该
commit 经 GitCode API 与本地全部 ref 核验**不存在**；origin/main 的 SessionTokenHandler
仍为旧版。远端可证实的保护仅在本分支（入口拦截，9 项测试通过）。issue 该段须改写为真实
commit 号或补推；"双主一致"在入库前属不可验证声明，验收保留 S9-2 逐实例核对。
**另：修复点 2（2.0b 指令守卫）当前无对应代码**，S4 用例须在合入前落地。**（已修正：第十三轮 D1 修复落地，2.0b 段 now `and not _is_bot_actor`，S4-1/4/5 三条测试就位——本段为历史轮记录保留）**

### S10 止血 vs 根治边界（防误判"已根治"）

| 用例 | 场景 | 本期行为 | 根治归属 |
|------|------|---------|---------|
| S10-1 | 路径 B 用户身份自回声（S6-6 态）循环发生 | 不拦，循环可复现 | 代发记录表（S11-1） |
| S10-2 | bot/第三方发含指令关键词的评论——修复点 2 落地前 | 指令被执行 | 本 spec 修复点 2 |
| S10-3 | webhook 伪造/重放（问题 27/28：无 HMAC/幂等键） | 无法识别 | 准入层（时间戳+幂等键；HMAC 待平台支持） |
| S10-4 | 单仓循环打满 worker 池（问题 30/31） | 无配额无熔断 | 熔断/配额（后续项） |
| S10-5 | 假双活：webhook 只打一台主（问题 34） | 切流/重启即露 | 入口高可用评估 + 版本核对 runbook |

完成度口径：止血 100%（三修复点落地即达成）；根治约 15%。根治验收标准 = issue 四问
全部查表回答；根治路线独立 issue 排期，本 spec 不承载。

### S11 最小根治版：代发记录表 + 评论 ID 对账（问题 46-48）

纲领：**"问谁发起的，别问用谁的钥匙"**——发起人 = 代发表里记的任务/会话；token 降级为
纯工具。匹配键：**评论 ID 为主**（VCS 签发、不被净化的身份证），内容指纹/时间为辅。
工作量以 S12-5 为准。

实测佐证与修正：发送侧 marker 追加存在而事故 payload 逐字节无——GitCode 内容净化剥
HTML 注释实锤；issue 原文"所有代发汇于单点"**已被 S12 五点普查推翻**，收口以 S12 为准。

| 用例 | 场景 | 期望（代发记录表落地后） |
|------|------|------------------------|
| S11-1 | 路径 B：agent 拿用户 PAT 代发评论（actor=托管人本人），webhook 回流 | 评论 ID 命中代发表 → 拦截——S6-6/S10-1 窗口关闭 |
| S11-2 | bot 代发评论（任意 token 级） | 命中 → 拦截——与入口拦截双网并存（竞态第二层网） |
| S11-3 | 用户本人评论（无代发记录） | 放行——消除 UserTokenHandler 空窗误拦 |
| S11-4 | 毫秒级竞态：发送响应未返回前 webhook 先到 | TTL 集合补拦；实际时序 webhook 跨网络至少几百毫秒，正常必然先有账 |
| S11-5 | provider 不返回评论 ID 的边角 | 内容指纹/时间辅匹配；仍无匹配 → 放行 + 兜底判断兜底 |
| S11-6 | 代发表数据膨胀 | 保留 30 天清理，规模有界 |
| S11-7 | marker 类信物方案终结判定 | 零宽字符/可见后缀均否决；入口拦截与 ID 对账互补而非替代 |

执行序列：快修（兜底入库+同步+停抄写）→ **代发记录表** → 熔断/幂等。S11 用例属代发表
落地后的验收，本期不实现——但不得引入与其冲突的设计（如评论体明文信物）。

### S12 写入点普查与收口

实测：代发路径至少三族五个点（issue 原文"一个写入点"不成立）：

| # | 写入点 | 位置 | 响应可取 ID | 记账改造 |
|---|--------|------|------------|---------|
| 1 | 网关端点 | git_repo_api.py create_issue_comment / create_issue / create_pull / update_pull | ✅ | 端点层或底层统一埋点 |
| 2 | handler 发评论 | token_resolver.py 三个 handler → _post_comment_via_provider | 取决于底层 | 委托点 4，底层埋点后自动覆盖 |
| 3 | 系统补发评论（GitHub） | event_pipeline._post_result_to_issue（_github_post_issue_comment） | ⚠️ 需读响应 | 补读响应体取 comment_id |
| 4 | 系统补发评论（GitCode） | event_pipeline._post_result_to_issue 内裸 urllib POST | ⚠️ 当前未读响应 | 补读响应体取 comment_id |
| 5 | 代编辑 issue 正文 | issue 编辑路径（issue_updated 回声源） | 无 comment_id | 见 S14，按 (issue_id+内容指纹) 记 |

| 用例 | 场景 | 期望 |
|------|------|------|
| S12-1 | 任一写入点（1-5）代发后，代发表均有一条记录 | 最底层公共调用处统一记账或逐一埋点，五点全数覆盖 |
| S12-2 | skill 直连 VCS API（未经网关） | 不入账（旁路）——兜底判断保底，skill 收口渐进消除 |
| S12-3 | 发送响应返回后、INSERT 落盘前 webhook 先到 | 进程内 TTL 集合先行记录，查询点先查集合再查表 |
| S12-4 | INSERT 前进程崩溃（TTL 集合丢失） | 账缺失 → 放行——best-effort：bot 场景入口拦截兜底，路径 B 场景知情接受 |
| S12-5 | 工作量 | **2-3 人天**（原估 1-2 未计入五点收口+响应补读），排期以此为准 |

### S13 检查顺序：ID 对账优先，marker 降级 fallback

| 用例 | 场景 | 期望 |
|------|------|------|
| S13-1 | GitCode 评论事件到达事件入口 | **先查代发表/TTL 集合**——GitCode 必剥 marker，不做前置必 miss 扫描 |
| S13-2 | GitHub 等不剥注释平台的评论事件 | 表未命中再看 marker——marker 降级为非 GitCode 平台 fallback，2.0a 保留 |
| S13-3 | 查不到 ID 的边角 provider | 内容指纹/时间辅匹配；仍无 → 放行 + 兜底 |

### S14 issue_updated 代编辑回声（实测发现的覆盖缺口）

背景：代编辑 issue 正文无 comment_id 可对账；原防线 marker 在 GitCode 已死——该族回声
当前零防线（入口拦截只覆盖评论族）。

| 用例 | 场景 | 期望（代发记录表设计须覆盖） |
|------|------|------------------------|
| S14-1 | agent 代编辑 issue 正文，回声到达 | 按 (repo + issue_id + 编辑指纹) 记账，事件入口对 issue_updated 增加对账分支——**ID 精确命中 → 拦截；指纹不匹配只告警不拦**（指纹可能因平台净化/格式化差异，不得因指纹误差误拦正常事件） |
| S14-2 | 用户本人编辑 issue 正文 | 无记录 → 放行（现状不变） |
| S14-3 | 本期（代发表未落地）issue_updated bot 回声 | 维持放行（无放大回路，风险 Low）——代发表字段设计必须含编辑动作记录，不得只设计 comment_id |

### S15 chat/IM 建 issue 的受理语义（选项 A 已定）

| 用例 | 场景 | 期望 |
|------|------|------|
| S15-1 | chat 现有形态（挂已有 issue 对话 + agent 回复） | 兜底判断保证正常；代发表上线后全转正 |
| S15-2 | 将来 chat/IM 直接建 issue（代发命中代发表） | **拦：不自动进流水线**，由 chat/IM 会话跟进——"系统代发的内容永不自我触发" |
| S15-3 | 用户想让该 issue 进入流水线 | 用户真人评论一次——真实操作驱动 |
| S15-4 | 需要代建 issue 又自动触发的产品形态 | 选项 B（带凭证的代提交豁免）——将来按需，本期不做 |

### S16 最终实现版：台账判定与多任务豁免的顺序（实现定稿，2026-10-23 交实现）

最终设计与 S12 五点的对应关系及两条顺序决策：

| 决策 | 内容 | 依据 |
|------|------|------|
| 写入收口 | 网关三端点（create_issue_comment / create_issue / update_issue）+ event_pipeline 系统发评论各路径（_post_result_to_issue 双分支、_post_subtask_completion_to_parent、_post_all_subtasks_done）逐一埋点；token_resolver handler 链委托 _post_result_to_issue 自动覆盖 | S12-1 五点全数覆盖 |
| 查询位置 | 2.0a 段头部（marker 检测之前）：issue_comment/note_hook_pr 按 ID 查；issue_updated 按 (repo+内容指纹) 查（S14 落地）；issue_opened 按 issue 编号查但**在「## 父任务」豁免之后** | S13 顺序 + 多任务硬约束（S3-1） |
| 查询异常 | 一律放行（不挡业务，兜底判断接手），日志 warning | 零误杀原则 |
| 留痕 | reason=`outbound_ledger_hit`（区别于 self_loop，报表可独立统计） | 验收可观测 |
| TTL 集合 | 进程内 300s，写侧先记集合再 INSERT；查询先集合后表 | S12-3 |
| 编辑回声 | update_issue 端点记 (repo, issue_number, md5(new_body))，issue_updated 查指纹命中 → 拦 | S14-1/2 落地 |

| 用例 | 场景 | 期望 |
|------|------|------|
| S16-1 | GitCode 系统补发评论（_post_result_to_issue 裸 urllib 路径）成功 | 响应体被读取，评论 ID 入台账——该写入点原先连 ID 都丢弃，是本次最大改造点 |
| S16-2 | 子任务完成回帖父 Issue（_post_subtask_completion_to_parent） | 入台账；父 issue 的 issue_comment 事件命中 → 拦（bot 回帖不再依赖名单兜底） |
| S16-3 | issue_opened 且台账命中，但 body 含「## 父任务」 | **放行**（豁免优先级高于台账，多任务行为不变）；不含「## 父任务」→ 拦 |
| S16-4 | issue_updated 回声，description 指纹命中台账 | 拦（reason=outbound_ledger_hit）；用户本人编辑无记录 → 放行 |
| S16-5 | 回滚 | 删查询点一处 + 写入点调用即恢复（表留存无害）；与向前兼容表一致 |

### S17 全链路写点收口清单（第三轮全库复扫，实现完成度对照）

写侧 11 点 + 读侧 6 类事件，当前实现状态：

| # | 写点 | 状态 |
|---|------|------|
| 1-2 | git_repo_api create_issue_comment / create_issue / update_issue | ✅ 已埋 |
| 3 | event_pipeline _post_result_to_issue 双分支 | ✅ 已埋 |
| 4 | subtask×2 + _post_result_to_pr | ✅ 已埋（第十四轮审查 P1-1 修正：原先仅 _post_result_to_issue 接了 ledgered 辅助，subtask×2 与 PR 回帖仍是裸 urllib——虚报已纠正，三处全部入账） |
| 5 | issues.py 澄清评论 | ✅ 已埋（读响应取 ID） |
| 6 | issues_center 双建单入口 | ✅ 已埋 + _grape_internal_trigger 同步触发豁免（第十四轮审查 P0-1 修正：run_pipeline 调用曾误缩进进 GitCode 分支致 GitHub 建单不触发——已移回两分支共用位） |
| 7 | token_resolver _post_comment_via_provider | ✅ 委托 _post_result_to_issue 自动覆盖 |
| 8 | VcsApiClient post_issue_comment / close_issue / create_pr / merge_pr | ✅ 客户端层收口（所有调用方自动覆盖） |
| 9 | worker 侧 create_pr（runtime/agent.py:1639，worker 进程够不着平台 DB） | **豁免声明**：不入账，靠 bot 名单兜（PR 创建者恒为 bot，风险低）；根治待 worker 回转 API |
| 10-11 | close_issue×3 / create_pr_for_repo 平台侧 | ✅ 经 VcsApiClient 层收口 |

tag/release 写点（旧矩阵写点 15/16）：**S24 轮归因修正**——tag_created/release_created 不在 stage matrix（无任务路径），sre 打 tag 走 git 协议（worktree 命令行）不经 VCS API，无写点可埋——旧矩阵的"16 写点"口径已废弃，以本表 11 写点+豁免为准。

读侧 6 类：issue_comment / note_hook_pr（ID）、issue_opened（编号，父任务豁免优先 + 内部触发豁免）、issue_updated（编辑 ID+指纹双命中）、issue_closed（编号）、pull_request 族（PR 编号）——全部 ✅。

已修正的实现缺陷（测试抓出）：issue_edit 不走 TTL 集合且要求指纹双命中——issue 编号非全局唯一键，单 ID 命中会误拦用户编辑同一 issue 的正常事件。

### S18 场景用例 ↔ 测试代码对照（实现完成度验收锚点）

每个场景用例对应的测试函数（tests/test_issue_453_outbound_ledger.py），验收时逐条执行：

| 用例 | 场景 | 测试函数 |
|------|------|---------|
| S11-1/S16-1 | bot/系统代发评论 → 台账命中 → skip（reason=outbound_ledger_hit） | test_ledger_hit_comment_skipped |
| S11-1 核心 | 路径 B 用户身份回声（actor=托管人，不在 bot 名单）仍拦 | test_ledger_hit_pathb_user_identity |
| S11-2 | token 级别无关：任意级别代发同样命中 | test_ledger_hit_comment_skipped（不构造 token 级别，判定不读级别）+ test_vcs_client_close/create_pr 入账 |
| S11-3 | 用户真人评论无记录 → 放行 | test_user_comment_no_record_passes |
| S11-4 | TTL 集合挡 INSERT 未落盘竞态 | test_ttl_set_hits_before_insert |
| S11-5 | provider 不返回 ID → 按无记录放行 | test_no_id_provider_allows_through |
| S11-6 | 30 天清理有界 | test_cleanup_expired_removes_old_rows |
| S13-1/2 | GitCode 先查表后 marker；GitHub marker fallback 保留 | test_marker_fallback_still_works |
| S13/S12-4 | 查表异常 → 放行不崩 | test_ledger_exception_allows_through |
| S14-1 | 编辑回声 ID+指纹双命中 → 拦 | test_issue_edit_echo_blocked |
| S14-1 修正 | 指纹不匹配 → 只告警放行（不打死系统） | test_issue_edit_fingerprint_mismatch_allows |
| S14 修正 | 编辑回声不走 TTL（用户编辑同号 issue 不误拦） | test_issue_edit_ttl_not_used |
| S16-2 | subtask 完成回帖父 issue → 入账 | test_subtask_comment_ledgered |
| S16-3 | issue_opened 父任务豁免优先于台账 | test_issue_opened_parent_task_exempt |
| S16-3 | 无「## 父任务」的 bot 建 issue → 台账命中拦 | test_issue_opened_ledger_hit_blocks |
| S16 建单 | _grape_internal_trigger 同步直调跳过台账 | test_issue_opened_internal_trigger_bypasses |
| S17-8 | VcsApiClient post_issue_comment 入账 | test_vcs_client_post_comment_ledgered |
| S17-10 | VcsApiClient close_issue 入账 | test_vcs_client_close_issue_ledgered |
| S17-11 | VcsApiClient create_pr / merge_pr 入账 | test_vcs_client_pr_ops_ledgered |
| S17 读侧 | issue_closed / PR 事件台账命中拦 | test_issue_closed_and_pr_event_blocked |
| 单元 | 分平台事件 ID 提取（gitcode/github 字段差异） | test_extract_event_content_id_platforms |
| 单元 | 响应 ID 归一（issue 取编号 / 评论取 ID） | test_extract_content_id_issue_vs_comment |
| 单元 | 指纹剥离 marker（GitCode 净化兼容） | test_content_hash_strips_marker |
| S19-1/S19-2 | 元数据回声（assigned/labeled/reopened）命中 issue_meta → 拦 | test_issue_meta_echo_blocked / test_issue_reopened_echo_blocked |
| S19-3 | 用户本人改元数据/重开 → 放行 | test_user_meta_change_allows |
| S19 写侧 | update_issue 双分支记账（issue_meta / issue_edit）+ 类型隔离 + 仓库隔离 | test_update_issue_meta_and_edit_branches |
| S19-4 | GitHub 澄清评论入账 | test_github_clarify_comment_ledgered |
| S19-5 | provider 前缀隔离（同 ID 跨平台不串扰） | test_provider_prefix_no_cross_talk |
| S20 匹配键 | iid == number 假设显式用例：写侧记 number、读侧查 iid（值等则命中） | test_issue_iid_number_value_alignment |
| S20 清理 | 清理挂写入路径（不依赖重启），保留期内留存 | test_cleanup_expired_removes_old_rows |
| S27-1/D1 | bot 评论含指令关键词不执行（指令守卫） | test_bot_command_comment_not_executed |
| S27-1/D1 | bot 引用回声（原文含 /cancel）不执行指令 | test_bot_command_echo_quoted_not_executed |
| S27-1/D1 | 真人指令正常执行（人类语义保留） | test_user_command_comment_executed |
| S27-2/D2 | 真实事件名 pull_request_updated 进台账判定段（假名回归钉子） | test_pr_updated_real_event_name_blocked |
| S27-3/D3 | redispatch 留痕按事件类型匹配（异类型事件不被历史留痕封禁） | test_redispatch_event_type_scoped |
| P0-1 | GitHub 仓建单后同步触发 pipeline（缩进回归钉子） | test_github_create_task_triggers_pipeline |
| P1-1 | subtask 完成回帖父 issue 入账（真实调用链） | test_subtask_completion_real_chain_ledgered |
| P1-1 | 全部子任务汇总回帖入账（真实调用链） | test_all_subtasks_done_real_chain_ledgered |
| P1-1 | qa 审查结论回帖 PR 入账（真实调用链） | test_pr_result_comment_real_chain_ledgered |
| S28-UC1 | 账号名漂移：名单 miss + 台账 hit → 拦 | test_ledger_hit_bot_name_drift |
| S28-UC2 | 账号名漂移：同 actor 台账 miss → 放行（不误拦） | test_ledger_hit_bot_name_drift（反向兜底段） |
| S29-UC1 | 网关 PR 评论端点入账 → note_hook_pr 回流拦 | test_gateway_pr_comment_ledgered |
| S29-UC2 | 网关建 PR 端点入账 → 外部用户 PR 回流拦 | test_gateway_create_pull_ledgered |
| S29-UC3 | 网关建 PR 入账后 bot PR 回流仍豁免给 QA（S24 红线钉子） | test_gateway_create_pull_bot_exempt |
| S29-UC4 | 网关端点响应无 ID → 零变化回落基线行为 | test_gateway_pr_write_idempotent_on_missing_id |
| S29-UC5 | 台账 30 天清理后，ledger_hit 留痕仍拒绝重派 | test_redispatch_ledger_hit_trail_blocks_after_retention |
| S30-UC1 | close 窗口内回声 → 双命中拦 | test_issue_close_recent_hit_blocks |
| S30-UC2 | meta 窗口内回声 → 双命中拦 | test_issue_meta_recent_hit_blocks |
| S30-UC4 | 窗口外旧 close 记录不误拦用户手动关 | test_issue_close_stale_record_allows_user |
| S30-UC5 | 窗口外旧 meta 记录不误拦用户改元数据/reopen | test_issue_meta_stale_record_allows_user |
| S30-UC6 | 窗口边界（29 分钟拦 / 31 分钟放行） | test_issue_close_window_boundary |

### S19 第四轮复扫补漏：元数据变更回声 + GitHub 澄清评论（2026-10-23）

背景：全链路第四轮复扫（写侧全库 URL 模式 + 读侧对齐三平台 normalize 全事件表）发现三处遗漏。

| 用例 | 场景 | 期望 |
|------|------|------|
| S19-1 | 我们代改 issue 元数据：assignee 变更（bot 接管）触发 issue_assigned 回声 | update_issue 写侧对非 body 修改（assignee/labels/state）记 `issue_meta`（编号键）；读侧 issue_assigned/issue_labeled/issue_reopened 命中 → 拦 |
| S19-2 | 我们 reopen issue 触发 issue_reopened 回声 | 同上 issue_meta 键命中 → 拦（两平台都有此事件：GitCode Issue Hook action=reopen / GitHub issues: reopened） |
| S19-3 | 用户本人改标签/换 assignee/重开 | 台账无记录 → 放行（现状不变） |
| S19-4 | GitHub 仓的澄清评论（issues.py `_github_req` POST 分支） | 响应 dict 带 comment id → 入账（原先只埋了 GitCode 分支） |
| S19-5 | GitCode 与 GitHub 同仓双写（迁移中仓库） | provider 前缀区分台账键，互不串扰 |

确认无漏的边界（第四轮核验）：feishu_async collaborators 为读操作；project_info/builder 全 GET；repo_manager 的 POST/PATCH 属仓库管理域（邀请/建仓），不产评论类 webhook 回流，不入账；worker 侧 create_pr 维持 S17-9 豁免；skill 直连 curl 维持 issue 待执行第 3 项渐进收口。

### S20 第五轮（台账自身生命周期）核验收尾（2026-10-23）

第五轮盯台账表自身（存储/清理/匹配键），结论：功能面已穷尽，以下为部署标注与已知假设，无代码改动：

| 项 | 核验结论 |
|----|---------|
| 存储架构 | 台账与 managed_repos/event_skipped 等流水线表同库（session_manager db）。MySQL 后端（本机内网 RDS）双主共享✅；**sqlite 环境（DEVOPS_DB_PATH 本地文件）台账单主**——但兜底判断的 bot 名单表、event_skipped 同样单主，台账不引入新风险，属部署既有约束。sqlite 环境部署时需确认 webhook 主归属 |
| 建 issue 匹配键 | 写侧 number/iid/id 按值取、读侧 iid/number 按值查——依赖平台事实 **GitCode iid == number（同编号双字段名）**。GitHub 只有 number，无此问题。该假设若被平台打破（iid 与 number 分叉），issue_opened 对账失配 → 放行（零误拦，降级为 bot 名单兜底） |
| 清理时机 | `_cleanup_expired` 挂在 `record_outbound` 写入路径（每日一次节流），不依赖进程重启——长期运行双主持续清理，无积灰。GitHub/GitCode 评论 ID 两端同字段同值域已实测（样本 ID 略） |

**完成度终态**：出站 11 写点 + 读侧 9 事件类型（comment/pr评论/opened/updated/closed/reopened/assigned/labeled/pr 族）+ 匹配键/存储/清理/竞态/豁免/索引四元键全过——功能面穷尽，剩余为实现质量项（repo_key 对齐、字段规范化，均已标注）。

### S21 第六轮复扫补漏：多任务模式 patch issue body 不记账（2026-10-23）

第六轮盯重试语义/后台任务/引用回声/GitHub 载体——发现 1 处真遗漏，其余 4 项核验通过：

| 用例 | 场景 | 期望 |
|------|------|------|
| S21-1 | 多任务模式建单后系统 patch issue body 追加 "[multi-task-mode]" 标记（GitCode `_patch_issue_body` / GitHub `_patch_github_issue_body`） | issue_edit 记账（编号+剥离 marker 指纹）——编辑回声命中 → 拦（原先 marker 在 GitCode 必 miss） |
| S21-2 | 核验通过：issues_center 401 刷新重试——重试成功只入账一次，无重复账 | ✅ 无需改动 |
| S21-3 | 核验通过：后台定时任务（skill_sync/scene_sync/upload_cleanup/login_throttle）不直发 VCS 评论 | ✅ 无需改动 |
| S21-4 | 核验通过：agent 引用用户原文的回声（prompt 要求 quote）——评论 ID 对账不受正文内容影响（台账优于 marker 的根本原因：ID 不被引用污染） | ✅ 无需改动 |
| S21-5 | 核验通过：GitHub issue_comment 恒带 comment.id，与 issue number 无关（无 number 的评论形态也能对账） | ✅ 无需改动 |

### S22 第八轮（provider 维度）GitHub 流级核验（2026-10-23）

| 项 | 核验结论 |
|----|---------|
| 读侧 ID 提取 | 已显式分 provider（extract_event_content_id：gitcode→object_attributes.id，github→comment.id），不依赖 or 链 ✅ |
| 正文/指纹提取 | `_note` 仍为 or 链（`_oa.note or comment.body or body`）——GitHub 靠后半截工作。仅影响指纹旁路告警（不参与拦截），漏查后果 = 少一条 warning，零拦截影响。已知代码卫生项，不改 |
| GitHub 定位 | 真实生产流量（25 仓，含高流量样本仓）；marker 在 GitHub 有效（HTML 注释不被剥），台账 = 第三防线纯增强，无碰撞 |
| 建 issue 键 | GitHub 响应/回流都是 number（无 iid），双存规范化天然兼容 |
| 验签 | GitHub 已有 X-Hub-Signature-256 HMAC；遗留只剩 GitCode 侧（问题 27） |
| GitLab | api_client 有 merge_requests 分支但托管数为零，不投入（表 provider 字段已留位） |
| 流级测试缺口（本轮补齐） | GitHub 原生 payload（comment 顶层对象）过完整 pipeline 的命中/放行测试——补 test_github_payload_flow_ledger_hit / test_github_payload_flow_user_allows。**踩坑记录**：repo_key_override 必须带 provider 前缀（github:test/...），裸 owner/repo 会被 to_repo_key 默认 gitcode 前缀导致读侧走错分支 |

### S23 系统与 Issue 交互全入口图（第九轮，2026-10-23）

六个事件入口 + 内部来源，与台账的关系逐一定义（全部实测核验链路）：

| # | 入口 | 链路（实测） | 台账行为 |
|---|------|-------------|---------|
| 1 | GitCode webhook | /webhook/gitcode → run_pipeline，6 类事件 | 主判定 |
| 2 | GitHub webhook | /webhook/github（HMAC 验签）→ run_pipeline，payload 结构不同 | 主判定（分平台提取） |
| 3 | 网页新建需求 | /api/issues/{owner}/{repo} → issues_center 建 issue（入账）+ 同步补发（_grape_internal_trigger 豁免） | 建 issue 记账；webhook 回流拦（去重） |
| 4 | 飞书 VOD 反馈 | 飞书 WS → vod_handler_async → **经 issues_center 网关链/agent 技能**（非直连 VCS）建 issue | 同 3 模式：入账 + 回流拦 |
| 5 | chat agent | /api/chat/send：指令（/done /cancel /resume）→ confirm/cancel/resume_issue_task → VcsApiClient.close_issue（入账）；代发评论走网关（入账）；ensure-issue/feedback 不产 VCS 写 | 指令关 issue 记账；代发评论记账 |
| 6 | A2A 交办 | POST /api/a2a，纯任务派发无 VCS 写 | 无关 |
| 7 | PR 订阅 | PR 事件 → 订阅匹配 → qa → _post_result_to_pr（入账） | 回帖记账，PR 回流拦 |
| 8 | issue 澄清 | issues.py 澄清评论（GitCode/GitHub 双分支均已入账，S19-4） | 评论记账 |
| 9 | 飞书卡片回调 | card.action → confirm_issue_done → _try_close_issue_for_repo → **VcsApiClient.close_issue（客户端层已入账，S17-10）** | 我们关的 issue 回流拦；用户手动关放行 |
| 10 | redispatch 重投 | 1.0a 守卫（S7 修复点 3）：event_skipped 留痕或台账命中 → 409 | 已拦 |

| 用例 | 场景 | 期望 |
|------|------|------|
| S23-1 | 飞书/网页"确认完成"（卡片回调或聊天指令）→ 系统关 issue → webhook 回流 issue_closed | close_issue 入账（issue_close 键）→ 命中拦（系统自己已处理状态，回流是回声） |
| S23-2 | 用户在 GitCode 网页手动关 issue → webhook 回流 | 无台账记录 → 放行（唯一触发源） |
| S23-3 | chat/卡片恢复任务（resume）→ 不关不建 VCS 内容 | 无 VCS 写 → 无关 |
| S23-4 | 飞书 VOD 建 issue（经网关） | 同 S16 建单模式：入账 + 同步触发豁免 + 回流拦 |

### S24 全场景处理矩阵（第十轮，实测 actor 分布佐证，2026-10-23）

判定原则：处理的唯一合法触发 = 真人的真实意图；系统自己产生的动静回流一律不处理；第三方自动化按白名单。矩阵归档于 issue #453（事件 × 触发者 × 处理/不处理 × 判定方式），本节记录矩阵暴露的核验结论与行动项处置：

| 项 | 核验结论 / 处置 |
|----|----------------|
| bot 建的 PR 必须放行（矩阵唯一系统性例外） | **实测发现真缺口并已修**：PR opened + bot actor → 跳过台账判定（S24 豁免，对齐 1.5 预匹配的 Issue #318 语义）。此前无豁免时，1.5 预匹配未命中的仓 bot PR 会被台账拦断 QA 流水线 |
| issue_labeled 的 label 写点（矩阵待查证项） | **查证无需新写点**：打 label 走 update_issue 端点（PATCH issues 带 labels，无 body）→ S19 的 issue_meta 键已覆盖，labeled 回声可拦。LabelCreateBody 是建仓库标签定义非打标签 |
| tag_created / release_created（矩阵"台账命中"） | **矩阵归因有误，已修正**：三类事件（push/tag/release）不在 _STAGE_EVENT_AGENT_MAP——本来就没有任务路径（纯 summary 记录），不是台账拦的。sre 打 tag 走 git 协议 push（worktree 命令行）不经 VCS API，无写点可埋，无需行动项 |
| grape_bot 一周 10297 条 comment | 事故元凶场景，台账上线后全拦（S1/S11） |
| issue_bot/grapedev-bot/github-actions[bot] 在流 | bot 名单兜底已正确处理（第二道网） |
| 用户 token 代发 / chat 代建 / 网页建单回流 | 全部"台账命中拦"行与实现一致（S16/S23） |

| 用例 | 场景 | 期望 |
|------|------|------|
| S24-1 | bot 建 PR（台账必有记录）→ pull_request_opened 回流 | **放行**（S24 豁免）——QA 审查流水线不断；非 bot 的 PR 台账命中（如外部自动化用我们 API 建）仍拦 |
| S24-2 | 用户/外部贡献者 PR → 回流 | 无记录放行 ✅（同矩阵） |
| S24-3 | sre agent 打 tag 回流 | 不派任务（stage matrix 无路径，现状即正确）——无需台账参与 |

### S25 判定用例矩阵（U/S/E/I 系列，第十一轮终版归档，2026-10-23）

判定总原则：**处理的唯一合法触发 = 真人真实意图**；我们自己的动静一律不处理；唯一系统性例外 = coding 建的 PR 必须放行给 qa。

U 系列（用户动作，全部 ✅ 处理）：

| 用例 | 事件 | 判定 |
|------|------|------|
| U1 用户建 issue | issue_opened | 放行→vod |
| U2 用户评论（澄清回复/意见/确认完成） | issue_comment | 放行（最高优先级） |
| U3 用户编辑 issue | issue_updated | 放行 |
| U4 用户手动关 issue | issue_closed | 放行→归档 |
| U5 用户/管理员打标签 | issue_labeled | 放行 |
| U6 用户提 PR | pull_request_opened | 放行→PR 订阅→qa |
| U7 用户 PR 评论 | note_hook_pr | 放行 |
| U8 用户合并 PR | pull_request_merged | 放行→sre |
| U9 外部贡献者 PR | pull_request_opened | 放行 |
| U10 人工打 tag/发 release | tag_created / release_created | 放行→sre |
| U11 用户推代码 | push | 入库不派任务（现状即正确） |

S 系列（系统动作回流，全部 ❌ 不处理）：

| 用例 | 事件 | 台账后判定 |
|------|------|-----------|
| S1 bot 结果/澄清/汇报回帖 | issue_comment | 台账命中（事故元凶：10297 条/周） |
| S2 agent 拿用户 PAT 发的评论 | issue_comment | 台账命中（origin_task） |
| S3 issue_bot/grapedev-bot 等 | issue_comment | 名单兜底 |
| S4 网页建需求回流 | issue_opened | 台账命中（同步触发已处理） |
| S5 飞书 VOD 建的 issue 回流 | issue_opened | 台账命中（IM 侧跟进） |
| S6 chat 代用户建的 issue 回流 | issue_opened | 台账命中（origin_channel 字段实测为死字段，通道归属已从验收口径移除——S29-G4） |
| S7 agent 编辑 issue | issue_updated | repo+指纹命中 |
| S8 卡片/"确认完成"关的 issue | issue_closed | 台账命中（状态已自维护） |
| S9 agent 打的标签 | issue_labeled | 台账命中（issue_meta，S19） |
| S10 github-actions[bot] CI 打标签 | issue_labeled | 名单兜底（实测已正确） |
| S11 qa 的 PR 审查结论回帖 | note_hook_pr | 台账命中 |
| S12 平台自动合并的 PR | pull_request_merged | 台账命中 |
| S13 agent 改 PR（改标题/正文） | pull_request_updated | 台账命中（update_pull 写点，本轮补齐） |
| S14/S15 sre 打 tag / 发 release | tag/release_created | **归因修正**：无任务路径（stage matrix 不含）非台账拦；打 tag 走 git 协议无 API 写点——无需行动 |
| S16 老 bot 评论被 redispatch 重投 | issue_comment | 1.0a 守卫拒绝（修复点 3 已实现） |

E 系列（例外放行红线）：

| 用例 | 事件 | 判定 | 原因 |
|------|------|------|------|
| E1 多任务子 issue（「## 父任务」） | issue_opened | ✅ 放行 | 豁免在台账之前（S16-3） |
| E2 coding agent 建的 PR | pull_request_opened | 视仓库配置：配订阅 → 1.5 派 qa ✅；session/user 级仓 → 台账不拦 ✅；repo_bot/global_bot 级无订阅仓 → 2.0 老墙维持基线（拦），不因本分支改变 | 唯一系统性例外 = 新闸（台账/入口拦截）不拦 bot PR（S24-1 豁免）；基线的墙不在本分支改动范围 |
| E3 issues_center 同步补发 | issue_opened | ✅ 触发器 | _grape_internal_trigger 跳过台账（S16） |

I 系列（内部来源，不走 webhook）：I1 A2A 交办（实测 751+568+546 次/周）✅ 正规通道；I2 飞书消息/卡片 ✅；I3 chat 消息 ✅；I4 build/deploy 事件按 CI 语义无回环面；I5 vod_signal 内部闭环。

V 系列验收用例（V1-V15）与 S18 对照表合并——V5"16 写点各发一条全部入账"本轮补齐 update_pull/merge_pull 后成立（此前 14 写点，S13/V10 抓出的真漏）。

### S26 PR 三来源三分路（V4 红线终版，第十二轮，2026-10-23）

PR 场景的三类来源三种处理——V4 红线"用户 PR 进流水线、agent PR 进 qa 审查，两个都不能断"的实现映射：

| 用例 | 来源 | 处理 | 实现机制 | 测试 |
|------|------|------|---------|------|
| S26-1 | 用户自己提的 PR | ✅ 走流水线（PR 订阅 → qa） | 台账无记录 → is_our_outbound False → 放行；1.5 段非 bot actor deferred to main route | test_user_pr_opened_allows_flow |
| S26-2 | coding agent 建的 PR | 视仓库配置而定（见下） | 三层结构如实描述：①配了 PR 订阅的仓——1.5 预匹配（Issue #318）bot actor 在自循环前派 QA，进审查 ✅；②session/user 级仓（2.0 老墙塌或不拦）——S24 豁免（pull_request_opened + bot actor 跳过台账判定）保证新闸不拦，落到主路由 ✅；③**repo_bot/global_bot 级且无订阅配置的仓——2.0 老墙维持基线行为**（RepoBot/GlobalBot handler 的 is_self_loop_actor 不区分事件类型，bot PR 被拦 self_loop），bot PR 命运与基线相同，**不因本分支改变**。原文"即使无订阅配置也不断"是把"新闸不拦"误写为"整条链不断"，已更正 | test_bot_pr_opened_exempt_from_ledger（验证②的新闸行为；①③分别为既有逻辑与基线行为） |
| S26-3 | qa 回帖/平台回声 | ❌ 拦 | note_hook_pr 评论 ID 台账命中（_post_result_to_pr 写点） | test_issue_closed_and_pr_event_blocked |
| V10 | agent 改 PR → pull_request_updated 回流 | 台账命中 | test_agent_update_pr_echo_blocked（读侧单元级——PR updated 流级有 no_commit/issue_done 等前置防线，台账为防线链一环） |
| S16-2 后半 | subtask 回帖父 issue 后，该评论回流 | 命中拦 | test_subtask_comment_parent_issue_echo_blocked（写侧入账→读侧回流拦截全链） |
| S11-2 | token 级别无关命中 | test_ledger_hit_level_independent |
| S21-1 | 多任务 patch body 入账（GitCode/GitHub 双分支） | test_multitask_patch_body_ledgered_gitcode / test_multitask_patch_body_ledgered_github |
| S7-1 | redispatch 台账命中拒绝重派 | test_redispatch_blocked_for_ledger_hit_event |
| S7-1 | redispatch self_loop 留痕拒绝重派 | test_redispatch_blocked_for_self_loop_skipped_event |
| S7-3 | redispatch 正常事件不拦 | test_redispatch_normal_event_allows |
| S23-1 | 卡片/聊天确认完成 → close 入账 → issue_closed 回流拦 | test_confirm_done_close_echo_blocked |
| S23-2 | 用户手动关 issue → 放行 | test_user_manual_close_allows |
| S24-1 反例 | 非 bot 的 PR 台账命中（外部自动化经 API 建）仍拦 | test_nonbot_pr_ledger_hit_still_blocks |
| S25/V5 | update_pull / merge_pull 网关写点入账（pr / pr_merge 键） | test_update_pull_and_merge_pull_gateway_ledgered |
| E2/S13 细化 | PR update 有新 commit → 放行 qa 复审 | test_pr_update_with_commit_flows_to_qa_review |
| S13 | PR update 纯文本编辑（无 commit）→ 拦 | test_pr_update_text_only_edit_blocked |
| S13 第三格 | 用户手动改自己 PR 标题/描述（纯文本、台账无记录）→ 放行——判定链"台账命中才看 commit，无记录直接放"，文本编辑拦截绝不做成无条件规则 | test_pr_update_user_text_edit_allows |

**E2/S13 落地规格（实现红线）**：pull_request_updated 三格判定——①台账无记录（用户动作）→ 直接放行（无论有无 commit）；②台账命中 + 有新 commit → 放行给 qa 复审（Issue #46 闭环）；③台账命中 + 纯文本编辑 → 拦（回声）。判据 oldrev/commits 是 payload 现成客观字段，与"查事实不反推"哲学一致；与 event_rule 的 update 一体路由兼容（放行照旧走 update 路由）；GitCode push+PR_updated 双发不双派（同路由 + active_guard 去重兜底）。

### S27 深度审查修正（D1/D2/D3，第十三轮，2026-10-23）

外部深度审查报告核验属实的三项缺陷及修正（每项附测试）：

| 项 | 缺陷（审查发现） | 修正 | 测试 |
|----|-----------------|------|------|
| D1（高） | 修复点 2（2.0b 指令守卫）未实现——bot 评论含"确认完成"先执行 confirm_issue_done 再被入口拦截，副作用已发生（问题 22/23 敞开，违反验收红线） | 2.0b 段条件改 `and not _is_bot_actor`——bot actor 不参与指令解析与澄清恢复 | test_bot_command_comment_not_executed / test_bot_command_echo_quoted_not_executed / test_user_command_comment_executed |
| D2（中） | PR 读侧元组用了不存在的假名（pr_merged/pr_opened/pr_updated）——真实事件名 pull_request_updated/merged 不在元组内，S26 三格判定整段死代码；测试假绿（守卫没跑天然通过 + OR 逃逸） | 元组改为真实名 pull_request_opened/updated/merged；text_only 测试收紧断言去 OR 逃逸 | test_pr_updated_real_event_name_blocked + test_pr_update_text_only_edit_blocked（收紧版） |
| D3（中） | redispatch 留痕查询只按 (repo_key, issue_id)——事故仓每 issue 上千条 bot 留痕，该 issue 所有事件重派全 409，S7-3 整体失效 | 查询加 event_type 级匹配（只拦同类型被拦事件的重派） | test_redispatch_event_type_scoped |

审查确认的低危项处置：D4（redispatch issue_edit 无 hash 死代码）——留待后续（该分支本就返回 False 放行，无危害）；D5（网关层入账前不查 error dict）——record_outbound 对空 ID 返回 False 已挡主要风险，error dict 带 id 的边角已知；D6（_ensure_table 每调用跑 DDL）——性能优化待后续；D7（close 键 30 天窗口 reopen 后误拦）——"宁漏拦不误杀"例外，知情接受。

### S28 真实数据模型对照（第十四轮，2026-10-23，dev 库近 30 天实测）

以生产数据反推需求宇宙后对照本 spec，结论：机制设计匹配真实分布，补充一条用例并登记两个遗留风险。

| 项 | 实测数据（dev 库，库实名略） | 处置 |
|----|-----------------------------------|------|
| 拦放规模 | 30 天 bot 评论被 self_loop 拦 434 条 vs 真人评论放行 14 条（拦/放 ≈ 31:1） | "宁漏拦不误杀"取舍与该比例匹配，维持 |
| F1 bot 账号名漂移 | managed_repos/全局名单均不含 grape_bot（某托管仓登记 grape_dev_bot 而代发 actor=grape_bot），全历史 255 条零拦截；台账不认名字认 ID，同形态新事件即拦 | 补用例 S28-UC1/S28-UC2 |
| F2 第三方 bot 双 miss | 名单+台账都 miss 的非平台 bot（外部自动化回帖）当前无系统性防线，仅 dedup:active_vod 类兜底碰巧拦（30 天 29+15+11 条） | 记入遗留清单：密度熔断/租户配额（问题 30/31）排期时以此为实证依据 |
| F4 bot PR 生产形态 | 30 天 events 无 pull_request_opened（全历史仅 1 条真人）；bot 的 PR 事件主要撞 2.0 老墙被拦（30 天 62 条 pr_opened + 55 pr_updated + 88 note_hook_pr）——bot PR 进 QA 依赖订阅配置在场，与 S26-2 修正口径一致 | S24 豁免回归测试维持高优先级（断它 = coding→qa 断）；订阅配置覆盖率另议 |
| F5 bot 元数据动作 | 30 天 issue_labeled/issue_assigned 被 self_loop 拦 57/54 条 | S19 issue_meta 覆盖正确，维持 |

#### S28 场景与场景用例（bot 账号名漂移，F1 落地）

S28a 场景：账号名漂移的 bot 代发评论（本 bug 的生产变体）

前置：托管仓 `managed_repos.bot_username` 与实际代发 actor 名不一致（托管时账号名与当前配置漂移，生产实测某托管仓登记 grape_dev_bot 而历史代发 actor=grape_bot）；该评论 cid 已入台账（我方网关/系统代发）。

| 用例 | 前置/步骤 | 预期 | 测试代码 |
|------|----------|------|---------|
| S28-UC1 | 名单 miss（get_all_bot_usernames 不含 grape_bot）+ cid 台账 hit → webhook 回流 | 拦（reason=outbound_ledger_hit），不派任务——台账不认名字认 ID，是这类事件的唯一系统性防线 | test_ledger_hit_bot_name_drift（正断言段） |
| S28-UC2 | 同 actor、cid 不在台账（历史漏网 >30 天保留期 / 非我方代发） | 放行——名单 miss 不误拦，靠后续防线（marker/dedup）兜底；历史漏网属知情接受 | test_ledger_hit_bot_name_drift（反向兜底段） |

S28b 场景：第三方自动回复 bot（F2 边界登记，本期无系统性防线）

| 用例 | 前置/步骤 | 预期（现状） | 归属 |
|------|----------|------------|------|
| S28-UC3 | 非平台 bot（外部自动化/平台自动回复）评论：名单 miss + 台账无记录 → webhook 回流，VOD 任务不活跃（dedup:active_vod 不命中） | 放行（当前无系统性防线，靠密度熔断/租户配额根治） | 遗留清单（问题 30/31），本期不实现、不引入与 S15 相冲突的设计 |
| S28-UC4 | 同上但 VOD 任务活跃 | dedup:active_vod 兜底拦（30 天实测 29+15+11 条即此路径）——非设计保证，行为依赖任务态 | 同上（实测证据已记录，作为熔断排期依据） |

### S29 对照核验修正轮（第十五轮，2026-10-23，补充迭代四处 G1-G4）

外部对照报告逐条核验属实后的补充迭代：G1/G2 是台账新防线覆盖面补全（基线上该两处为零拦截，修复后行为对基线零变化、纯增强）；G3 封死 redispatch 30 天复活窗口；G4 文档/验收口径修正。

**S29a 场景：网关 PR 端点族代发回流（G1/G2 落地）**

前置：外部 API 调用者（或内部经网关的调用）通过 `POST /pulls/{n}/comments` 发 PR 评论、`POST /pulls` 建 PR——此前系统内部同族路径（update_pull/merge_pull/event_pipeline 裸 urllib）均已入账，唯此二端点漏账，回流无台账防线。

| 用例 | 前置/步骤 | 预期 | 测试代码 |
|------|----------|------|---------|
| S29-UC1 | 经网关端点发 PR 评论（响应含评论 ID）→ note_hook_pr 回流 | 入账（issue_comment 键）→ 台账命中 → 拦 | test_gateway_pr_comment_ledgered |
| S29-UC2 | 经网关端点建 PR（响应含 number）→ pull_request_opened 回流，actor 为外部用户（非 bot） | 入账（pr 键）→ 台账命中 → 拦 | test_gateway_create_pull_ledgered |
| S29-UC3 | 经网关端点建 PR → 回流 actor 命中 bot 名单（我方 coding agent 经网关建 PR 的假设形态） | 入账，但 S24 豁免（pull_request_opened + bot actor）先于台账判定 → 放行给 QA，红线不断 | test_gateway_create_pull_bot_exempt（S24 豁免回归钉子） |
| S29-UC4 | 端点响应不含 ID（provider 边角） | record_outbound 返回 False，端点响应/状态码零变化（best-effort 兜底回落基线行为） | test_gateway_pr_write_idempotent_on_missing_id |

**S29b 场景：redispatch 30 天窗口（G3 落地）**

前置：路径 B 用户身份回声事件曾被台账拦截（reason=outbound_ledger_hit 留痕）；台账 30 天保留期已过，记录被清理。

| 用例 | 前置/步骤 | 预期 | 测试代码 |
|------|----------|------|---------|
| S29-UC5 | 上述事件在留痕存在（永久保留）但台账已清的情况下被人工 redispatch | 409 拒绝（留痕守卫含 outbound_ledger_hit，不再依赖台账窗口） | test_redispatch_ledger_hit_trail_blocks_after_retention |
| S29-UC6 | 同类型正常事件（从未被拦）redispatch | 200 放行（无回归，S7-3 维持） | test_redispatch_normal_event_allows（既有用例回归） |

**S29c 口径修正（G4，纯文档）**
- origin_channel 字段实测为死字段（全库唯一显式赋值硬编码 "webhook"，chat 代建路由经 issues_center 无独立写点传值）——**从验收口径移除**（不接线：接线需先有真实多通道写点，避免"为实现字段而造写点"）；表列保留不删（schema 稳定）。
- issue §五 事件清单按 event_types.py 实测 20 种重写：评论族（issue_comment/note_hook_pr）ID 对账；issue_opened/updated/closed/reopened/assigned/labeled 台账对账；PR 族（opened/updated/merged）台账+S24 豁免+commit 分流；pull_request_closed/pull_request_review 默认规则 allow=0（无派发路径，行为无害）；push/tag_created/release_created/build_*/deploy_* 无任务路径（纯记录/CI 语义）。

### S30 共享键动作的误拦窗口（第十六轮，2026-10-23，G5 落地）

**问题本质**（对照分析定稿）：台账对账键的适用性随动作类型分三档——
- 评论族：评论 ID 全局唯一 → 单键命中即精确，拦得准（核心修复，治本）；
- issue_edit：编号共享但正文可比 → "编号+内容指纹"双确认已解（S17）；
- **issue_close / issue_meta（assignee/labels/reopened）：编号共享且动作内容无唯一可比物——"我们关过一次 → 30 天保留期内谁再关都算我们的"，把用户手动操作误当回声拦掉**。唯一键用在了非唯一的场景，属覆盖不全的粗粒度实现，非方向性改错。

**修复设计（与 S11-4 竞态假设同构）**：webhook 回流必然紧邻发送动作（毫秒到秒级）。close/meta 对账改为**编号命中 + 时效窗口（默认 30 分钟）双确认**：窗口内 = 我们刚做完动作的回声 → 拦；窗口外的旧记录不参与对账 → 用户 30 天内手动关/改 → 放行。取舍方向：**零误杀优先于零漏拦**（最坏情况 = 漏拦一条迟到 >30 分钟的回声，无放大回路；对比原状 = 误拦用户全部手动归档动作）。

**S30a 场景：系统关闭/改元数据后 30 分钟内的回声（正常对账）**

前置：我们经任一写点（卡片确认完成 / VcsApiClient.close_issue / update_issue meta 分支）关闭 issue 或改 assignee/labels，webhook 回流紧邻到达。

| 用例 | 前置/步骤 | 预期 | 测试代码 |
|------|----------|------|---------|
| S30-UC1 | 我们关 issue → issue_closed 回流（窗口内） | 编号+窗口双命中 → 拦（outbound_ledger_hit） | test_issue_close_recent_hit_blocks |
| S30-UC2 | 我们改 assignee/labels → issue_assigned/labeled/reopened 回流（窗口内） | 同上拦 | test_issue_meta_recent_hit_blocks |
| S30-UC3 | 发送响应未落盘竞态（TTL 集合命中） | 拦（TTL 路径不受窗口影响） | test_ttl_set_hits_before_insert（既有） |

**S30b 场景：窗口外的用户手动操作（误拦消除）**

前置：我们曾关过/改过该 issue 元数据，台账记录已超 30 分钟（保留期内但窗口外）。

| 用例 | 前置/步骤 | 预期 | 测试代码 |
|------|----------|------|---------|
| S30-UC4 | 用户手动关同一 issue → issue_closed 回流 | 窗口外旧记录不参与对账 → 放行（归档动作正常） | test_issue_close_stale_record_allows_user |
| S30-UC5 | 用户手动改 assignee/labels/reopen → 对应回声 | 放行 | test_issue_meta_stale_record_allows_user |
| S30-UC6 | 窗口边界：记录恰在窗口内（29 分钟前） | 拦（窗口含边界）；边界外（31 分钟前）放行 | test_issue_close_window_boundary |

**兼容性**：评论族 / issue_edit / PR 族（pr/pr_merge）对账逻辑零变化；窗口判定失败（created_at 解析异常）按窗口外放行（零误杀兜底）；MySQL 方言——cutoff 在 Python 侧计算（与 30 天清理同模式）。




**设计语义**：台账在 PR 上的目标不是"少处理"，是"该处理的处理、不该的不处理"——agent PR 的处理方式为"跳过重复触发（vod/issue 不再当新需求）、保留 qa 审查"，实现取 bot PR 直接不进台账判定（与"命中再分流"等价且更干净）。

**v2 精确语义落地（E2/S13 细化，拦回声不拦流转）**：pull_request_updated 命中台账时，有新 commit（oldrev/commits 非空）→ 放行给 qa 复审（Issue #46 QA↔coding 回流闭环不断）；纯文本编辑（无 commit）→ 拦。测试：test_pr_update_with_commit_flows_to_qa_review / test_pr_update_text_only_edit_blocked。

## Technical Approach

### 修复点 1：评论族入口统一拦截（S1–S3）

`src/api/event_pipeline.py` 2.0 节：在 `if actor and repo_key:` 内、`resolve_config_level`
调用之前新增：

```python
if event_type in ("issue_comment", "note_hook_pr") and _is_bot_actor:
    log.info(f"Pipeline skip: self-loop detected (actor={actor} in bot_usernames, "
             f"level-independent, repo={repo_key})")
    append_skipped_event(repo_key, event_type, "self_loop", actor, issue_id=_pre_issue_id)
    return result
```

`_is_bot_actor` 复用流水线已提前计算的值（Issue #318 引入），不新增查询。reason=`self_loop`
（与现有 repo_bot 拦截同枚举）。

### 修复点 2：指令与状态变更通道身份前置校验（S4）

2.0b 指令检测段分支入口处，非 bot actor 才进入指令解析与澄清恢复：

```python
if event_type in ("issue_comment", "note_hook_pr"):
    if _is_bot_actor:
        log.info(f"Pipeline: bot actor={actor} skips command/resume parsing (issue #453)")
    else:
        ...  # 原指令解析 + 澄清恢复逻辑不变
```

与修复点 1 的关系：2.0b 在 2.0 之前——只有修复点 1 时指令通道在拦截前已执行完毕，两道闸缺一不可。

### 修复点 3：redispatch 重派重校验（S7）

`src/api/routes/events.py` `redispatch_event`：重派前查 `event_skipped` 表，存在
`reason in ('self_loop', 'self_loop_path_b')` 记录时拒绝重派（HTTP 409，message 指引管理台
人工处置）。判定只依赖留痕，与 token 级别无关。

## Affected Modules

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/event_pipeline.py` | 修改 | 2.0 节评论族入口统一拦截 + 2.0b 指令/澄清恢复 bot 守卫 |
| `src/api/routes/events.py` | 修改 | `redispatch_event` 增加被拦事件拒绝重派 |
| `tests/test_issue_453_self_loop.py` | 新增/修改 | 覆盖 S1–S7 用例的单元测试（随 CI pytest） |

## Acceptance Criteria

- [ ] S1-1/S1-2：session 级仓库 bot 评论事件被跳过并留痕，不派发任务
- [ ] S1-3/S2-2：用户评论在 session 级与 repo_bot 级仓库均正常受理
- [ ] S2-1：repo_bot 级仓库 bot 评论仍被拦截（无回归）
- [ ] S2b-1：存量仓重存配置后的 session 级污染态同样拦截；S2b-2 用户评论正常受理
- [ ] S3-1：bot 创建的「## 父任务」issue_opened 仍放行（问题 37 硬约束）
- [ ] S3-3：PR 族事件路径不受影响
- [ ] S4-1~S4-4：bot（含引用回声）评论含指令关键词时不执行 confirm/cancel/resume
- [ ] S4-5/S4-6：人类用户指令语义保留
- [ ] S4-7/S4-8：澄清恢复仅对人类 actor 生效
- [ ] S6-1/S6-2/S6-3：actor/repo_key 为空、bot 用户名查询失败时不拦截不抛错
- [ ] S7-1/S7-2：被 self_loop/self_loop_path_b 拦截的事件 redispatch 被拒绝
- [ ] S7-3：正常事件 redispatch 无回归
- [ ] S8-1：脏数据在场拦截仍生效；S8-5：存量仓用户评论/非评论族路径零变化
- [ ] S9-1：两实现并存语义兼容；S9-2：合入后双主逐实例核对一致（纳入 runbook）
- [ ] 修复点 2（2.0b 指令守卫）实现补齐——当前无对应代码，合入前必须落地
- [ ] S6-6/S6-7：路径 B 窗口行为确认（本期不拦，不误杀托管人本人评论）
- [ ] S10 完成度口径：验收报告区分"止血完成"与"根治未完成"，不得宣称 #453 整体关闭
- [ ] S11（根治锚点，本期不实现）：代发表落地后 S11-1/S11-3/S11-4 为主验收用例；本期不得引入与评论 ID 对账冲突的设计
- [ ] S12-1：五个写入点代发后代发表全数有记录（不得只测网关端点）
- [ ] S12-3：TTL 集合在 INSERT 前可查询命中；S12-4 崩溃窗口确认 best-effort
- [ ] S13-1/S13-2：GitCode 先查表后 marker（顺序断言）；GitHub marker fallback 保留无回归
- [ ] S14-1/S14-2：代编辑回声命中 (issue_id+指纹) 对账拦截；用户本人编辑放行
- [ ] S15-2/S15-3：chat/IM 代建 issue 不自动进流水线；用户真人评论后正常触发
- [ ] 现有测试套件（含 test_path_b_self_loop_r20、test_issue_372_user_token）全部通过
- [ ] S28-UC1：账号名漂移——actor 不在 bot 名单但 cid 台账命中 → 拦（outbound_ledger_hit），不派任务
- [ ] S28-UC2：同 actor、cid 不在台账 → 放行（名单 miss 不误拦）；S28-UC3/UC4：第三方 bot 双 miss 现状登记为遗留（熔断/配额），本期不实现
- [ ] S29-UC1/UC2：网关 PR 评论/建 PR 端点代发入账，回流台账命中拦截
- [ ] S29-UC3：网关建 PR 入账后 bot PR 回流仍放行给 QA（S24 红线不断）
- [ ] S29-UC4：端点响应无 ID 时入账 best-effort 零变化（回落基线行为）
- [ ] S29-UC5：台账 30 天保留期过后，outbound_ledger_hit 留痕仍拒绝该事件重派（G3）
- [ ] S29-G4：origin_channel 死字段从验收口径移除；事件清单按实测 20 种重写归档
- [ ] S30-UC1/UC2：close/meta 窗口内回声编号+时效双命中拦截
- [ ] S30-UC4/UC5：窗口外（>30 分钟）旧记录不参与对账，用户手动关/改元数据/reopen 放行（误拦窗口从 30 天收窄到 30 分钟）
- [ ] S30-UC6：窗口边界行为符合预期（29 分钟拦 / 31 分钟放行）

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 用户名与 bot 同名的真实用户评论被跳过 | Low | `event_skipped` 留痕可复核；同名即同一账号语义 |
| bot 被授意执行指令（管理员让 bot 代发 /done） | Low | 走管理台/API 通道，不走 webhook 评论 |
| issue_updated 等非评论族漏检 | Low | 无放大回路；扩展另行评估（根治侧见 S14） |
| redispatch 拒绝后确有正当重派需求 | Low | 留痕可查；误拦走管理台人工修正，不提供 API 绕过 |
| 范围外问题（#2 A2A、#3 bot PR、#6 unknown、#16 层级、#27/28 验签幂等、#30 配额、#35 告警、准入层等） | — | 各自立 issue 跟踪；准入层开发期间由本 spec 兜底判断过渡保护（问题 43） |
| 窗口期（问题 42）：重存配置继续制造 session 污染 | Low | 判定不依赖 session 数据态，上线即全覆盖；窗口期避免批量重存配置 |
| 双实现并存窗口：79 主手工版未入库、174 主旧版 | Medium | 收口路径 S9-2：本分支合入并部署双主替代手工版 |
| 修复点 2 未实现：bot 含指令关键词评论仍可执行 | Medium | 合入前补齐 2.0b 守卫（设计已定稿，S4 用例就绪） |
| bot 名单不完整时兜底同步开洞（问题 24/33） | Medium | 名单覆盖度抽查缓解；根治属代发记录表 |
| 路径 B 用户身份自回声循环（问题 4/5） | High（未修，已知） | 根治 = 代发记录表 + 评论 ID 对账（S11+S12）；窗口期按事故 runbook 手工止血 |
| "一个写入点"与实测五点不符——只埋网关端点则四处漏记 | High（根治失效） | 按 S12 五点清单收口；验收 S12-1 全点覆盖 |
| issue_updated 代编辑回声零防线（S14） | Medium | 代发表设计纳入编辑动作记录；本期维持放行（无放大回路） |
| INSERT 前进程崩溃账缺失（S12-4） | Low | TTL 集合挡毫秒竞态；崩溃窗口知情接受，不宣称消灭 |
| issue 描述"78ffda608 已提交/双主同步"与代码事实脱节 | Medium（验收误导） | 改写为真实 commit 或补推；"双主一致"入库前视为不可验证，验收逐实例核对 |
| chat/IM 代建 issue 误触发流水线 | Low | 选项 A：代发命中即拦，真人评论才触发 |
| 验收误判：本期通过被解读为 #453 已根治 | Medium | S10 完成度口径显式写入验收清单；根治路线独立 issue |
