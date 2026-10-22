# 需求文档：#372 用户 Token 驱动引入的误判与静默失效修复（PAT / 设置页入口 / 路径 B）

> 状态：需求定义
> 基线：weekly-08（a91f1decd）/ main
> 优先级：P0（会自动删除用户数据 / 发布阻断：需求归属全量错乱 + token 过期误判每请求刷新 + 双会话机制（应用层+表级双层根治））
> 问题归属：**全部由 #372 用户 Token 驱动体系引入或激活**——R27 PAT 绑定（e9b21e126，2026-10-12）引入窄 scope token 物种、#343（2026-10-15）把探活 token 源切到用户存储 token、#340 同日给误判路径接上级联删数据、R25（db2a86a90，2026-10-20）Token UI 改版放大暴露频率、设置页快捷入口随 #372 配置体系上线。
> 边界：仅收 #372 引起的问题。历史债（明文存储/日志/secret 架构/限流/残留等）见独立文档《token-legacy-debts-requirements.md》。
> 代码优化：止血分支合入后的重复代码提取与坏味道消除见独立文档《issue372-code-quality-requirements.md》（FR-CQ-1 ~ FR-CQ-10，P2 不拦发布，独立 MR 实施）。

## 1. 问题背景

本版新增**发布阻断项**（FR-11/位点 15）：R25 §36（db2a86a90）将用户映射查询改为 `(gitcode_user=? OR github_login=?)`，assignee 空串入参与 D32 迁移灌入的空串列值交叉命中，需求中心建单归属错捞为任意用户（LIMIT 1 无排序），回补只救 user_id=0 导致错误归属永久固化。weekly-08 不修直接发布将造成生产全量需求归属错乱，合出门槛前必须完成修复+回归。

以下为原有问题背景：

用户在设置页保存（或更新）GitCode/GitHub 个人访问令牌（PAT）后，其名下已托管仓库会显示为"非托管"，且托管相关的关联数据（需求中心 Issue 卡片、流水线记录、场景关联）被自动级联清空。

根因：探活逻辑使用"当前存储的用户 token"查询 VCS，并把"查询返回 404 / 空列表"解释为"仓库已删除 / webhook 已丢失"，进而自动执行级联清理 + 解除托管。该判定在只有 OAuth 全 scope token 的年代是安全的；R27 允许保存窄 scope PAT 后"查不到"出现了第二种解释（token 视角问题），但判定逻辑未随之更新；#340 又给误判路径接上了删数据能力。

另一组问题在写路径：路径 B 仓库（用户 token 驱动）的 host 保存窄 PAT 后 agent 回帖/建单全部 403 静默失败；设置页 DevOps Bot Token 快捷入口绕过 R15 全部校验，无效 token 直接写库即断流。

## 2. 受影响范围

| # | 位置 | 误判模式 | 后果 | 严重度 |
|---|------|----------|------|--------|
| 1 | `/repos/member` → `_probe_stale_repos`（repos.py:143） | 窄 scope PAT 查 hooks 空列表 → 判"webhook 丢失"；查仓库 404 → 判"已删除" | 级联清空关联数据 + remove_managed_repo | P0 |
| 2 | `/repos/sync`（repos.py:437）同款探活 | 同上 | 同上 | P0 |
| 3 | `_probe_stale_repos` GitHub 侧（fine-grained PAT 无 webhooks 读权限） | 同上（GitHub 侧） | 同上 | P0 |
| 4 | `find_existing_webhook` 系列（五处调用） | PAT 查 hooks 空 → 判"无 webhook" → 走新建/删除重建分支 | 重复建 webhook、误删重建导致密钥不一致 | P1 |
| 5 | `_check_admin`（host_repo，repos.py:973） | PAT 查协作者权限失败被 except 吞掉 → 返回 False | 真实 owner 被误报"无权限托管" | P1 |
| 6 | `fetch_repos_with_role`（repo_manager.py:271） | PAT scope 覆盖不全 → 拉回列表缺失 | 前端仓库列表显示不全（有缓存兜底，不删数据） | P2 |
| 7 | `_rebuild_webhook_with_bot_token` / R25.2 A→B 降级的旧 webhook 查找 | 用户 token（可能窄 PAT）查 hooks 空 → 误删重建走错分支 | 新 webhook secret 与存储不一致，事件永久 403 断流 | P1 |
| 8 | A2A 跨仓信任域（"两仓 bot token 相同"判定） | 更新某仓库 bot token 后同 token 关联仓信任域静默失效 | A2A 跨仓调用被拒，无提示 | P2 |
| 9 | OAuth refresh 失败静默降级（deps.py，`return new_token or token`） | 刷新失败返回旧 token 不标记；30 分钟内持续拿坏 token 调 API | 写操作连环 401，无 expired 健康状态 | P1 |
| 10 | 探活兜底 token（get_repo_token_with_refresh，路径 B 返回用户 token） | FR 修复后兜底路径仍可能把窄 PAT 送进判定 | 误判复活 | P1 |
| 11 | 设置页 `update_gitcode_repo_config`（settings.py:565）绕过 R15 全部校验 | 零校验写库即触发 webhook 重建 | 无效 token → 事件立即 403 断流 | P0 |
| 12 | 路径 B 窄 PAT 写路径半瘫（UserToken._fetch 驱动全部 VCS 写） | host 缺 issue 写权限 PAT → 每个 VCS 写 403；纯路径 B 回帖 fallback 空 → 静默 | 用户侧"agent 什么都没回复"，无根因 | P1 |
| 13 | auto_upgrade 升级瞬间身份语义漂移 | 升级后 host comment 不再被自循环拦截 → agent 重跑；运行中任务身份割裂 | host 莫名看到 agent 响应自己的评论 | P1 |
| 14 | UserToken 过期短路的 level 漂移（resolve vs resolve_config_level） | 执行链与自循环检测链对 level 认知不一致 | 四条件边界误放/误拦 | P2 |
| 15 | R25 §36 空串匹配需求归属错乱（db2a86a90，session_store.py:125 建档 + 7 处同族查询 `(gitcode_user=? OR github_login=?)`） | assignee 空串入参 × 列值空串（D32 迁移灌入）交叉命中，LIMIT 1 无排序错捞任意行；回补只救 user_id=0，错捞非 0 永久固化 | 需求卡片归属错人（"我的需求"看不到自己建的）；连带错建任务会话（同 Issue 双会话）；issues_center.py:95 / session_manager.py:433 空入参可返回错误飞书 open_id（通知错发风险） | **P0（发布阻断）** |
| 16 | 双空 token 仓静默降级链（managed_repos 63 仓 bot_gitcode_token 空且 host_user_id=0） | `_fetch_issue_assignee` token 空直接 return "" → assignee 空 → 接"空 creator 取最早用户"解析；路径 B 走不了（host=0）；匿名同步只救 public 仓 | 空值链上游放大器：token 空 → assignee 空 → 归属靠猜（非直接 bug，是归属错乱的供应商） | P1 |
| 17 | 孤儿需求全员可见+可操作+属主丢失（63 条 user_id=0 且 assignee='' 行） | 仓库 Issue 列表（list_repo_issues）无 user 过滤全员可见；任务中心 mine `user_id=? OR user_id=0`（P2 拍板"展示不隐藏"）可见；**单条操作端点（issues.py:655/685/739）同样 `OR user_id=0`——任何用户可确认完成/取消/重派孤儿需求**；真正属主在"我的需求"（严格 user_id=?）里丢失 | 越权操作面（超出"展示"设计意图）+ 无主不可找回 | P1 |
| 18 | bot 用户名清单无唯一性校验（get_all_bot_usernames 从 managed_repos.bot_username 聚合） | 任何用户把仓 bot 配置成与某真人相同的用户名 → 该真人全部 issue 评论/操作被自循环过滤静默 skip | 真人操作被静默忽略，无告警无发现机制（当前 4 个 bot 名无对应真人是数据巧合非机制保障） | P2 |
| 19 | 飞书 pending 空 uid 建会话（handler_async.py 会话复用按 uid+repo+issue 查，查不到即 create） | uid 来自空 actor 反查链（位点 16 同款），猜错时制造同 Issue 双会话 | 双会话生成机制（1602 案例同构，与位点 15 的错 uid 来源不同） | P1 |
| 20 | A2A 空 actor 仍在新增（R25 后 dev 新产生 2 条空 actor 的 a2a_event） | A2A 链路仍有 actor 丢失点未堵完 | 持续喂"空 creator 取最早用户"解析（归属错乱的活跃供应商） | P2 |
| 21 | token 过期时间时区混写（grape_account_db 写入 gmtime UTC 字符串，deps.py:383/542 读取 strptime 按本地 +08 解释） | 实测复算：2h 后过期的 token 被读取侧算成"已过期 6h"，need_refresh 恒真——**每个请求都触发刷新**，不是"提前 8h 进窗口" | GitCode refresh 正常时仅多调用（无害）；refresh 抖动/限流时全链路瘫痪——与 FR-7"刷新失败静默降级"叠加成完整事故；user_credentials int epoch 路径与 TEXT 路径两套格式并存 | **P0（发布阻断第二项）** |
| 22 | INSERT OR REPLACE 覆盖丢配置（handler_async.py:1091/1170/1521 repo_im_group_binding；grape_account_db.py:1418 binding_tickets；user/db.py:306 exchange_codes） | REPLACE = DELETE+INSERT，未带列回落 DEFAULT——实测复现：用户开启的 notify_enabled=1 重新绑定时被静默清回 0；binding_tickets 覆盖未消费旧票；exchange_codes 是 oauth_states 同款加固（db.py:280 已修）没同步的兄弟行 | IM 群通知/auto_impl 配置在重新绑定时静默丢失（用户可重配但无感知）；OAuth 票据静默覆盖 | P2 |
| 23 | LIKE 前缀通配符无转义（grape_auth.py:980/990/1036/1056，含 user_repos DELETE 链） | `repo_key LIKE f"{provider}:%"` 值无 %/_ 转义，当前 provider 为枚举不可利用 | "靠枚举值保护"的防御性缺口：未来 provider 值若来自用户输入可扩大删除范围 | P2 |
| 24 | chat_conversations 缺 (user_id, repo_key, issue_number, conv_type) 唯一约束（仅 id TEXT PRIMARY KEY） | 双主并发复用查询（按 uid+repo+issue 查无行即 create）无表级兜底——1602 双会话的表级根因；注：issue 表已有 UNIQUE idx_tasks_repo_issue（10-06 ccd769d3a 修复过历史普通索引），双卡片竞态被约束兜死，本项仅剩会话表 | 同 Issue 双会话概率窗口；唯一约束缺失使 N4/位点 19 的应用层防护成为唯一防线 | P1 |
| 26 | fetchone()[0] 空解引用面 87 处（绝大多数 COUNT(*) 恒非 NULL 安全；少数非 COUNT 管理链空结果 TypeError） | 当前全部有 try/except 降级，"靠异常兜底"风格 | 与空串守卫同款模式：安全靠调用方/异常不靠类型安全；无活 bug | P2 |

无此问题的路径（不需要改）：webhook 删除事件（平台推送，可信信号）；手动解除托管（显式确认）；issues/scenes 只执行不判死；**issue 表 (repo_key, issue_number) 唯一约束已在位**（UNIQUE idx_tasks_repo_issue，2026-10-06 ccd769d3a 修复过历史普通索引，MySQL 实测 Non_unique=0，双主并发双卡片竞态已被表级兜死——第五轮扫描初判"普通索引"为误报，已修正）；分布式锁（INSERT+TTL）、oauth_refresh 双锁、decrypt 显式抛错（D46 P1-10）均核验安全。

## 3. 需求目标

**核心原则**：探活是读操作。读不到只代表"看不清"，不代表"东西没了"。任何数据删除只能由平台推送的删除事件或用户显式确认触发。token 不可用/权限不足时，唯一动作是提示用户。

## 4. 功能需求

### FR-1 探活判定收紧（P0，位点 1/2/3）

1.1 hooks 查询返回**空列表**时判"不确定"，跳过该仓库。仅当"hooks 列表非空且不含 expected_wh"才允许判"webhook 丢失"。

1.2 仓库存在性 404 判定加**双信号交叉验证**：仓库 404 时，若该仓库仍出现在本次 VCS 拉回的 user_repos 列表中，跳过不判。

1.3 探活开始前做 **token 能力前置检查**：调用 `/api/v5/user` 验证 token 有效；401 → 放弃整轮并标记"token 失效"；200 后对任一托管仓库做 hooks 探测，403/空 → 放弃整轮并标记"token 权限不足"。

### FR-2 清理动作分级与保险丝（P0）

2.1 **webhook 丢失（仓库仍存在）不再删除任何数据**：只置"待修复"状态 + 前端"重检修复"横幅（复用 dashboard `_repairHost()`），引导一键自愈。

2.2 **仓库删除（双信号确认）保留级联清理**，trigger 标记 `polling-confirmed`，清理前后各写审计日志（含两路信号）。

2.3 **单轮清理上限**：单次探活判 stale > 2 时整轮放弃 + 告警日志。禁止"一夜清库"。

### FR-3 token 不可用/过期的用户提示（P0）

3.1 探活触发 FR-1.3 的"token 失效/权限不足"时，设置页凭据区显示告警条："当前 Token 无法校验仓库托管状态（已失效或权限不足），请更新 Token 或改用 OAuth 登录"。

3.2 告警持久化，用户更新 token 后自动消除；同账号 1 小时去重防风暴。

3.3 **不做**任何"token 失效时自动解绑/自动清理托管关系"——只提示，不处置。

### FR-4 PAT 保存时能力校验（P1，入口防护）

4.1 `update_credentials` 保存 gitcode_token/github_token 时，除 `/user` 有效性校验外，追加权限探测（至少 repo 读；建议 hooks 读）。

4.2 权限不满足时**不阻断保存**，但返回结构化提示："该 Token 缺少 X 权限，保存后仓库状态探活/托管校验将不可用，建议勾选 project/webhook scope"。前端绑定弹窗内展示。

### FR-5 同款位点顺带修复（P1/P2）

5.1 `find_existing_webhook`：空列表返回"不确定"语义，调用方跳过新建分支（宁可漏建由重检修复兜底，不可误建双 webhook/误删重建）。

5.2 `_check_admin`：异常路径区分"验证失败（网络/权限不足）"与"确认无权限"，前者提示"Token 权限不足或已过期，请更新后重试或改用 OAuth"。

5.3 GitHub fine-grained PAT 等效防护同步落地。

5.4 `fetch_repos_with_role` 空结果返回 `_stale`/`_partial` 标记，前端提示"仓库列表可能不完整（Token 权限受限）"。

### FR-6 仓库级 Bot Token 相关（P1/P2）

6.1 `_rebuild_webhook_with_bot_token` / A→B 降级查找旧 webhook 改用 **bot token**（bot 是 maintainer，hooks 可见），不再用用户 token。空列表判"不确定"。

6.2 共用 bot token 信任域失效提示：更新仓库 bot token 时检测同 token 关联仓库，前端提示"该 Token 变更将影响 N 个关联仓库的跨仓信任域，请同步更新"。

6.3 不改动项：`bot_token_health`（401 仅标记 expired）与 `set_repo_bot_token` 无效即 400——用户 token 侧修复的参考范式。

### FR-7 OAuth refresh 失败的过期标记（P1，位点 9）

7.1 刷新失败时不再静默返回旧 token：打 expired 标记（对齐 R25.3 status=4 与 bot_token_health 范式），返回值不变（向后兼容）。

7.2 标记后 FR-3 提示链路生效（设置页"GitCode 登录已过期"）；重新 OAuth 后自动清除（复用 R25.3 清除路径）。

7.3 30 分钟节流保留，但**失效标记不受节流影响**（确认失效立即标记）。

### FR-8 探活兜底 token 收口（P1，位点 10）

8.1 `_probe_stale_repos` 全部 token 来源（含兜底 `get_repo_token_with_refresh`）统一走 FR-1.3 能力前置检查——堵死"主入口修了、兜底又送进坏 token"的复活路径。

### FR-9 设置页入口收敛（P0，位点 11）

9.1 `update_gitcode_repo_config` 的 bot_token 写入收敛到与 R15 相同校验语义：verify 失败 400 拒写（`TOKEN_INVALID`）；仓库不存在 404；权限校验与 R15 同口径（托管人/admin）。

9.2 副作用告警：webhook 重建失败时响应体返回 warning（"webhook 重建失败，请到仓库页执行重检修复"）。

9.3 长期：两入口合并为 service 层函数，消除"同一动作两套安全等级"。

### FR-10 用户 Token 驱动（路径 B）专项（位点 12/13/14）

10.1 **host PAT 写能力提示**（P1）：FR-4 校验对路径 B host 加强——保存 PAT 时检测该用户名下 host_user_id>0 的托管仓库，缺 issue 写权限则提示"此 Token 将驱动仓库 X/Y/Z 的 agent 回帖/建单，缺 issue 写权限将导致回复静默失败"。回帖链 RESULT_POST_FAILED 附注补充根因（"Token 权限不足（403），请更新 host Token"）。

10.2 **auto_upgrade 升级通知与身份衔接**（P1）：① 升级完成通知受影响仓库 host（语义变更说明）；② 升级瞬间有运行中 task 的仓库，收尾回帖沿用任务启动时的 token 层级（快照语义）；③ 升级批次窗口统计与事件丢失披露。

10.3 **level 一致性拉平**（P2）：resolve() 跳级时 TokenResult 附注 `config_level`；自循环检测等身份语义调用方一律读 config_level；执行链用过级后实际 token。


### FR-11 R25 §36 空串匹配修复（P0 发布阻断，位点 15）

11.1 **建档防护**：`session_store.py` ensure_row 建档——assignee 为空串时 user_id 直接落 0（不查表）；非空时按 repo_key 的 provider 分查对应列（gitcode_user / github_login），不 OR 扫两列。

11.2 **同族查询同步防护**（issues_center.py / session_manager.py / engine_select.py / pipeline.py / chat.py）：入参空串早退（返回 0/空）；有 repo_key 上下文的按 provider 分查对应列；无 repo_key 但参数名声明为 `gitcode_user` 的只查 gitcode_user 列。

11.3 **写入侧约束**（治本）：`unified_user_mapping` 的 gitcode_user / github_login / feishu_open_id 空串一律置 NULL 入库（写入路径 + 一次性存量清洗 '' → NULL），消除空串命中面——只堵查询侧是治标，空串数据是根。

11.4 **dev 存量善后**：错归属 issue 行修正（user_id 改回创建者）；错建会话归并或改属；清洗存量空串行。

11.5 **回归测试**：空 assignee 建档 → user_id=0（不错归属）；正常 GitCode/GitHub 用户名 → 正确映射（保住 §36 正向能力）；空入参查询 → 0/空（不错捞 open_id）。

### FR-17 跨平台分列查询（P1，FR-11 深度扩展）

**核心问题**：`(gitcode_user=? OR github_login=?)` 用一个用户名同时扫两列，而不是根据用户来自哪个平台查对应列。导致两类 bug：
- Bug A：空串交叉命中（FR-11 覆盖）
- Bug B：跨平台同名误命中——GitCode 用户 "user_b" 和 GitHub 用户 "user_b" 不是同一个人，OR 查询会返回错误行。当前 dev 库 0 个 GitHub-only 用户未暴露，GitHub OAuth 接入后必现。

**修复原则**：用户来自哪个平台，就查哪个平台的列。有 repo_key 时用 `parse_repo_key` 获取 provider；无 repo_key 但函数参数名声明为 `gitcode_user` 时，只查 gitcode_user 列（调用方从 grape_accounts.username 取值，一定是 GitCode 登录用户名）。

17.1 **按 provider 分查（有 repo_key 上下文）**：
- `_get_user_id_by_gitcode(gitcode_user, repo_key)`（issues_center.py）：已修 ✓
- `_repo_user_id(repo_key, issue_creator)`（engine_select.py）：已修 ✓
- `_resolve_feishu_name(gitcode_user, repo_key)`（pipeline.py）：加 repo_key 参数，按 provider 分查
- `session_store.py` ensure_row：按 repo_key provider 分查（替换 weekly-08 的两列都卡 `!=''` 写法）

17.2 **只查 gitcode_user 列（参数名已声明，无 repo_key 上下文）**：
- `get_feishu_open_id(gitcode_user, app_id)`（session_manager.py）：参数名声明 gitcode_user，只查 gitcode_user 列
- `update_unified_user_info(gitcode_user, ...)`（session_manager.py L474）：参数名声明 gitcode_user，只查 gitcode_user 列
- `_get_feishu_open_id(gitcode_user, repo_key="")`（issues_center.py）：加 repo_key 可选参数，有则分查，无则只查 gitcode_user
- `chat.py` L1348 UPDATE：参数来自 grape_accounts.username，只查 gitcode_user 列

17.3 **不改动的项**：
- `get_feishu_mapping`（session_manager.py L530）：全量导出不是按入参查询，OR 语义正确
- `issues_center.py` L87 EXISTS 子查询：外层已按 provider 分查，EXISTS 内 OR 不影响正确性（多余但不改，避免过度改动）

17.4 **回归测试**：GitCode 用户 "user_b" 和 GitHub 用户 "user_b" 同时存在时，GitCode 仓库查询只命中 gitcode_user 列的 alice，GitHub 仓库查询只命中 github_login 列的 alice。

### FR-12 空值链治理：孤儿需求 / 双空 token / bot 名单 / 空 actor（位点 16-20）

12.1 **双空 token 仓处置**（P1，位点 16）：对 bot_gitcode_token 空且 host_user_id=0 的托管仓库——① 列表化呈现给管理员（"N 仓无可用 token，需求归属解析将降级"）；② `_fetch_issue_assignee` token 空时记 warning 日志（当前静默 return ""），使归属降级可观测；③ 仓库页提示托管人补配 token 或 host。

12.2 **孤儿需求对账与操作面收紧**（P1，位点 17）：① 一次性对账：user_id=0 且 assignee 空的行按事件流 creator 回溯归属，回溯不到的显式标记"无主-仅展示"；② **单条操作端点（issues.py:655/685/739）收紧**：`OR user_id=0` 仅保留"查看"语义，确认完成/取消/重派等变更操作要求归属匹配或 admin（展示不动、操作收紧，尊重 P2 拍板的设计意图同时关闭越权操作面）；③ 任务中心/仓库列表的孤儿需求展示加"无主"角标，与正常需求区分。

12.3 **bot 用户名唯一性护栏**（P2，位点 18）：保存仓库 bot_username 时校验与 grape_accounts 真人用户名冲突——冲突即告警拒绝（或强提示）。低成本前置护栏，替代事后对账。

12.4 **飞书 pending 空 uid 防误建**（P1，位点 19）：handler_async 会话复用前 uid 为空/无效（actor 反查失败）时不新建 task 会话——降级为不带 issue 绑定的普通通知会话或记 pending 待 actor 可用时再建，杜绝同 Issue 双会话的第二个生成机制。

12.5 **A2A 空 actor 溯源收口**（P2，位点 20）：对 R25 后仍产生的空 actor a2a_event 逐条溯源（webhook payload 提取点），补齐提取失败时的 actor 兜底（事件 comment 作者/issue author），停止向"最早用户"解析喂料。

### FR-13 时区语义统一（P0 发布阻断，位点 21）

13.1 **读侧统一 UTC 解析**：deps.py:383/542 的 `strptime(...).timestamp()` 改为显式按 UTC 解释（`datetime.strptime(...).replace(tzinfo=timezone.utc).timestamp()`，与 im/bot_lease.py:57、feishu_async.py:1082 已有写法一致——仓库内已有正确范式可复用）。禁止"减 8h"式补丁（非 +08 服务器会再错）。

13.2 **写入侧格式收敛**（随修复顺带）：external_identities 的 token_expires_at 逐步收敛到与 user_credentials 一致的 int epoch（消除 TEXT/int 两套格式并存）；存量 TEXT 行由读侧 UTC 解析兼容，不强制迁移。

13.3 **回归测试**：写入"2h 后过期"→ 读取侧 need_refresh=False（提前 8h 场景不再误判）；写入"5 分钟后过期"→ need_refresh=True（正常临近过期仍触发）；服务器时区非 +08 部署下同样正确。

### FR-14 覆盖式写入加固（P2，位点 22）

14.1 repo_im_group_binding 三处 REPLACE 改 `INSERT ... ON CONFLICT DO UPDATE`（或 MySQL `ON DUPLICATE KEY UPDATE`）：只更新绑定列（chat/repo/source/bind_by/app_id），**保留 notify_enabled / auto_impl_enabled / im_channel_name 用户配置列**。

14.2 binding_tickets / exchange_codes 改 INSERT + 冲突异常重试（与 oauth_states P2 加固同款，db.py:280 注释即模板）。

14.3 回归：重新绑定群 → 通知开关保持；票据冲突 → 重试成功且旧票不被覆盖。

### FR-15 LIKE 参数转义（P2，位点 23）

15.1 grape_auth.py 四处 LIKE 参数统一 ESCAPE 转义（`LIKE ? ESCAPE '\'` + 值侧转义 %/_），provider 保持枚举校验双保险；防御性加固，当前不可利用。

### FR-16 并发加固（位点 24/26）

16.1 **chat_conversations 唯一约束**（P1，位点 24）：加 `(user_id, repo_key, issue_number, conv_type)` 唯一索引（issue_number/conv_type 为空的普通会话行不参与约束，用部分唯一索引或空值哨兵处理）；附存量双会话清理（同键多行 → 保留最新，旧会话消息归并）；与 FR-12.4（应用层空 uid 防误建）构成双层防护。

16.3 **fetchone 空解引用渐进加固**（P2，位点 26）：新增/改动代码禁止裸 fetchone()[0]（代码评审检查项）；存量 87 处按触碰时机顺带改造（`(row or (default,))[0]` 模式），不做专项批量改。

### FR-18 um 主路径查询缺陷止血修复（P0 发布阻断）

事实基线（评审先核）：
- 合入目标树：仓库根目录（engine_select.py:167-170、session_store.py:123-129 行号按此树）
- 数据库：grape_system_db@YOUR_DB_HOST
- R4 争议裁定：session_store.py:125 双列非空条件经四棵树全量核查不存在——grape-dev-repo 为裸 OR，其余树为单列查询或无此文件。原稿"0007 工作区"不可复验，按无证据处理。若评审现场在其他环境确有双列非空条件，则 18.2 升回 P0，其余不受影响

数据现状（2026-10-21 实测 grape_system_db）：
- um：user_id>0 共 113 行；gitcode_user 113/113 非空且全部=grape_accounts.username（注册名）；github_login 非空 0 行
- external_identities：gitcode 90 行（69 行 external_login 非空）、github 21 行（6 行非空）
- issue 表：2504 行，user_id=0 共 356（gitcode 侧 343/2377≈14%，github 侧 13/128≈10%）。0 的真实成因是 um.gitcode_user 存注册名而 issue assignee 是平台真名，查询落空——即 R1 本身

根因（三条，均实测证实）：
- R1 命名空间混存：backfill 把 Grape 自由注册名灌进 um.gitcode_user，三个命名空间混存两列。113/113 行实锤
- R2 可撞名冒领：回退链按 grape_accounts.username 匹配 actor，username 任何人自选
- R3 无唯一真源：um 平台列多写源打架（回填=注册名/PAT=真名/OAuth=不写），github_login 全库 0 行有效；um 无"该行经平台验证"标志

范围声明：只修查询正确性与最危险的写侧漏洞，不换锚、不建表、不清数据、无切换窗口。R3（无真源）、外部协作者识别、存量伪身份清洗，留给根治方案（identity 换锚，另行评审）。

18.1 主路径分列+空参守卫（P0，engine_select.py:167-170）：按 repo_key 的 provider 分查对应列（gitcode_user / github_login），列名来自 provider 受控枚举非用户输入无注入面。加 ?!='' 守卫。gitcode 侧当前 um=注册名，分列后行为与现状一致；github 侧列空不中，交回退链。

18.2 session_store.py:123-129 建卡归属同款分列（可选，语义统一）：本环境现状为裸 OR，无 R4 bug，故此项不修任何现行错误，价值仅是防未来 github 侧空串误配 + 与 18.1 语义统一。建议与 18.1 同 MR 顺手合入，不单独立项。若评审裁定其他环境存在双列非空版本，则此项升回 P0。

18.3 回退链 EXISTS 收严（P0，engine_select.py:176/190）：grape_accounts username 匹配仅在"该账号在平台侧验证过的 login（external_identities.external_login）恰好等于这个注册名"时生效——加 EXISTS 子查询验证 ei.external_login=ga.username。ei 行只能经 OAuth/PAT 创建（有授权语义），backfill 不碰它，攻击者拿别人名字注册时 ei 里不会有这个 login 的记录。注意：不能用 um 验证 um（backfill 写的 um.gitcode_user=ga.username 结构性恒真，EXISTS 必然通过，等于自证循环）。user_repos JOIN 处加同款条件。代价：回退链从"几乎总命中"变成"很少命中"，gitcode 侧 issue 归属 0 占比会上升，须监控。

18.4 写侧守卫（P1，防恶化）：
- update_unified_user_info（session_manager.py:469/497）：反查加空参守卫；UPDATE 改 CASE WHEN ?='' THEN gitcode_user ELSE ? END，空串不覆盖真值
- prompt_builder.py:328 提示词裸 SQL：加约束——空值不 UPDATE 该列、禁 user_id=0 占位行
- backfill 改源（session_manager.py:231）：INSERT OR IGNORE 只建 (user_id) 空行（不再写 gitcode_user=注册名）；紧接从 external_identities 回填平台列真值（仅填空值不覆盖 PAT 已写真值）。绑定过的人回填真 login（飞书 sync 传的本来就是平台 login，真 login 更对），未绑定的本地用户 um.gitcode_user 为空——这类用户的飞书绑定靠自己的注册/绑定动作建立。需跟飞书链路回归测试

18.5 其余 11 处 um OR 读点空参守卫（P1，issues_center:73/79/95、pipeline:53、chat.py:1328/1425、session_manager:431/433/469 等）：加前置 ?!='' 守卫，维持 OR（飞书反查无平台语境，OR 属合理兜底）。

18.6 OAuth 注册补写 um 平台列（P1，堵增量）：grape_auth.py create_external_identity 成功后，按 provider INSERT OR IGNORE (user_id) + UPDATE 对应平台列=external_login（照抄 settings.py:237 既有模式）。消除 GitHub OAuth 注册→重启间 um.github_login 空窗。gitcode OAuth 同理补写 gitcode_user=external_login。

不改动项（核验后确认现状正确）：注销删 um 整行；解绑飞书只清飞书三列；无 repo_key 语境的飞书通知反查维持查 um.gitcode_user（加空参守卫）；get_feishu_mapping 全量导出 OR 语义正确。

防复发判定（5 条链路 + 4 块拼图）：

5 条链路逐条判定：
- backfill 灌注册名（活口 1）：18.4 改源后堵住——INSERT 只建 (user_id) 空行 + ei 回填真值
- GitHub OAuth 注册不写 um（活口 2）：18.6 补写后堵住——注册成功即写 um.github_login
- 飞书注册名回写（活口 3，关键漏洞）：feishu_onboard:266 和 handler_async:1431 调 update_unified_user_info 传 grape_accounts 注册名，CASE WHEN 只判空串，注册名非空直接覆盖 gitcode_user → backfill 改源写入的真 login 被洗回注册名 → 伪身份再生。需 18.7 分语境写入堵住
- bot 裸 SQL 造伪身份：已堵（18.4 prompt_builder 约束）✓
- update_unified_user_info 空串洗值：已堵（18.4 CASE WHEN）✓

4 块拼图（缺一不可，防复发 MR 排期，有时序依赖）：

时序依赖（钉死）：
  代码 MR（堵）：块 3 分语境写入（硬前置）+ 块 1 OAuth 钩子 + 块 2 backfill 改源
     ↓ 合入后
  体检（只读）→ B 类人工甄别 → A 类分批清列（绑飞书子集单独评估）
     → github 回填 → 143 张 0 卡回填（assignee 非空部分）→ 全链路复测（含 OR 读点）
     ↓
  剩余：63 张 assignee 空 0 卡（sender 历史回填或接受）+ C 类登记（根治版换锚消化）

- 块 1：OAuth 注册钩子（18.6）——堵增量 2
- 块 2：backfill 改源（18.4）——堵增量 1（新注册用户不再灌伪身份）
- 块 3：update_unified_user_info 分语境写入（18.7）——堵飞书注册名回写。硬前置：不落地它，清洗就是给飞书回写准备已清空的靶行（user_d 实测威胁：18 行中 1 行绑飞书，任何一次 sync/扫码即洗回）
- 块 4：清洗清列保行——执行纪律，依赖块 3 先合入

4 条铁律：
1. 清列保行：UPDATE SET gitcode_user='' 保行，不可 DELETE 整行（INSERT OR IGNORE 按 user_id 主键判重，行在则跳过）。例外：A 类中 feishu_open_id 非空的行单独评估后再清（清列后 OR 读点按注册名反查飞书会断）
2. 清洗→回填顺序不可倒：先清洗伪身份，再回填 0 卡归属
3. B 类人工过目：B/C 区分需绑定状态甄别
4. 清洗硬依赖分语境写入先落地：不堵飞书回写就清洗 = 给洗回机制准备已清空的靶行

结论：4 块拼图全完后，5 条链路全堵。当前止血分支维持现状合入（声明只止血不防复发），4 块拼图作为紧随其后的防复发 MR 排期。

18.7 update_unified_user_info 分语境写入（P1，防复发 MR，堵飞书注册名回写）：
- 问题：update_unified_user_info 被三条路径共用——feishu_onboard:266（注册名语境）、handler_async:1431（注册名语境）、feishu_async:768（平台 login 语境）。前两条传 grape_accounts 注册名，CASE WHEN 判定非空直接覆盖 gitcode_user → 把 backfill 改源写入的真 login 洗回注册名
- 修复：拆两个入口——bind_feishu_identity(user_id, open_id, ...)（注册名语境，只写飞书三列，不碰平台列）与平台身份写入（PAT/OAuth/backfill 改源路径用原入口）。onboard:266 和 handler:1431 改调新入口（扫码绑定飞书凭什么动 gitcode_user？语义上就是错的），feishu_async:768 维持平台语境走原入口
- 量级：函数签名改动 + 3 处调用方迁移 + 飞书链路回归，中型 MR，独立排期不塞进止血分支

已知残留风险（止血版无解，防复发 MR / 根治版消除）：
1. 主路径撞名堵不住：主路径先查 um，分列后 actor=注册名仍直接命中攻击者的 um 行，EXISTS 在回退链根本走不到。um 表无"该行经平台验证"标志（R3），止血范围内无解。根治版换锚消除
2. github 侧 issue 归属 0 占比可能升高：现状 10%，收紧后回退链"碰巧命中"消失（本就是错配），方向正确但须监控确认升高部分来自"正确拒绝"而非漏配。external_identities github 有 6 条有效 login 可兜底
3. 飞书注册名回写：feishu_onboard:266 和 handler_async:1431 传注册名覆盖 gitcode_user。止血版 CASE WHEN 只堵空串不堵注册名。防复发 MR 18.7 分语境写入消除
4. 存量 113 行注册名伪身份、外部协作者识别——根治版

待评审确认点：
1. 事实基线确认（抬头三条）——尤其 18.2 的 P0/可选定性取决于此
2. 18.4 的 CASE 守卫对飞书链路的影响——唯一动写侧的点，需重点回归
3. EXISTS 性能：um 113 行 / grape_accounts 小表，无索引也可接受，确认即可
4. 防复发 4 块拼图的 MR 排期确认——18.7 分语境写入是量级最大的，独立成 MR

### FR-19 数据治理与根治方案（独立窗口，不拦发布）

19.1 identity 换锚方案（根治）：主路径从查 um 字符串列改为查 external_identities 绑定关系，从数据层杜绝撞名/一人多身份。另行评审，不在本版范围。

19.2 um 存量伪身份清洗（防复发 MR，依赖 18.7 先合入）：

分类（实测 dev 库，gitcode + github 双向对称）：

| 类 | gitcode 侧 | github 侧 | 处理 |
|----|-----------|-----------|------|
| A 伪身份 | 18 行注册名 | 0 行（列本来就空） | 清列保行 |
| B 撞名 | 注册名=他人 gitcode 真名 | gitcode_user 列=他人 github 真名（1 行实测 user33='user_a'=user2 的 github 真名） | 双向体检+人工甄别后清列 |
| C 合法同名 | — | gitcode 真名=github 真名跨人并存 | 不清，根治版消化 |
| D 死数据 | 孤儿行/死 identity | status=0 行（user_d user13 实测存在） | 排除出回填/降级 |
| E login 缺失 | — | login 空 + status=1（5 行实测 user24/25/27/29/32，external_id 有值） | 平台 API 按 external_id 反查 login 回填 identity，查不到才降级 status=0 |

A 类细分：
- 绑飞书子集（feishu_open_id 非空）：1 行（user13/user_d）。清列后 OR 读点按注册名反查飞书会断。单独评估：保留 gitcode_user 或确认通知走 user_id 查询路径
- 未绑飞书子集：17 行。直接清列

github 侧 4 个独立问题（原方案漏掉，补全）：
1. E 类 login 缺失行：5 行 status=1 但 external_login 空，原方案 D 类条件 status=0 漏网。处理：平台 API 按 external_id 反查 login 回填 identity（查不到才降级 status=0）
2. 反向 B 类撞名：um.gitcode_user='user_a'(user33) = user2 的 github 真名。B 类体检必须双向：撞 gitcode identity 真名 + 撞 github identity 真名
3. status=0 地雷：github identity (13,'user_d',status=0) 实测存在。回填 SQL 必须 AND ei.status=1（代码已写，方案钉死）
4. github 0 卡回填预期：28/29 活跃账号无 github 绑定，0 卡 4 张大部分救不回——这是"从未绑定"的正确语义，不是清洗失败。回填只覆盖绑定用户的历史卡

清洗时序（钉死，依赖 18.7 先合入）：
1. 块 3（分语境写入）合入 ← 硬前置，不堵飞书回写就清洗等于白洗
2. 体检（只读，双向）：
   - gitcode 侧：A 类 18 行 + B 类撞名清单
   - github 侧：E 类 5 行 login 缺失 + B 类反向撞名（user33） + D 类 status=0 行（user13）
3. B 类人工甄别（双向：gitcode 撞名 + github 反向撞名）
4. A 类分批清列：绑飞书子集单独评估 → 未绑飞书子集 UPDATE SET gitcode_user='' 保行
5. github 侧 E 类处理：5 行 login 空的 identity → 平台 API 按 external_id 反查 login 回填（查不到降级 status=0）
6. github 回填：从 ei 回填 um.github_login（条件钉死：ei.status=1 AND ei.external_login!=''）
7. 0 卡回填：
   - gitcode 侧：143 张 assignee 非空可回填，63 张 assignee 空不在范围
   - github 侧：4 张 0 卡，预期仅绑定用户可归位，其余正确无主
8. 全链路复测（含 OR 读点 + github 侧）：
   - _repo_user_id('github:仓', 补 login 后的 login) 命中验证
   - _repo_user_id('gitcode:仓', 清洗后的 assignee) 不误捞
   - get_feishu_open_id 反查验证 + pipeline feishu_name + session_store 建卡
   - OR 读点传反向撞名 login（user_a）的行为验证

4 条铁律：
1. 清列保行（不可 DELETE 整行，INSERT OR IGNORE 按 user_id 主键判重行在则跳过）。例外：绑飞书子集单独评估
2. 清洗→回填顺序不可倒
3. B 类人工过目（双向：gitcode 撞名 + github 反向撞名）
4. 清洗硬依赖分语境写入先落地

19.3 um 空串 '' → NULL 统一 + DEFAULT NULL：留待根治版。
19.4 external_identities 5 条空 external_login 死记录清理：留待根治版。

### FR-20 审计修复补完（P0/P1，E2E 审计发现）

三路子 agent 并行审计 + 基线对比测试发现的遗漏项，需在本分支补完。

**P0 级（发布阻断）**：

20.1 **FR-16.1 部分唯一索引误拦 general 会话修复**（P0，db_setup.py:1833）：当前条件 `WHERE issue_number IS NOT NULL AND issue_number != ''` 作用于 INTEGER 列——SQLite 类型亲和性使 `0 != ''` 求值为 true，导致 `issue_number=0` 的 general 会话行也被纳入唯一约束。同一用户在同一仓库下只能创建一个 general 会话，第二个 INSERT 报 `UNIQUE constraint failed`。修复：改为 `WHERE issue_number > 0 AND repo_key != ''`（利用 INTEGER 比较，排除 0 行）。

20.2 **FR-16.1 部分唯一索引 MySQL 不兼容修复**（P0，db_setup.py:1833 + mysql_compat.py）：MySQL 不支持部分索引（`CREATE INDEX ... WHERE`），`mysql_compat` 的 `_CREATE_INDEX_RE` 正则不匹配带 WHERE 的 SQL，翻译层跳过，原始 SQL 直送 MySQL → 语法错误，索引创建失败被 try/except 吞掉。修复：为 MySQL 生成等价全列唯一索引 + 应用层守卫（`create_conversation` 中先查再插），或在 `mysql_compat` 中扩展正则匹配 WHERE 子句并降级为普通唯一索引。

20.3 **FR-1.3 token 能力前置检查实现**（P0，repos.py）：设计文档 §4.1 定义了 `_check_token_capability()` 函数（调用 `/api/v5/user` 验证 token 有效性 + hooks 权限探测），但代码中零匹配。探活开始前不做 token 能力检查 → 窄 scope PAT 仍可能走完整个探活链路才在某一步失败。修复：实现 `_check_token_capability(token, provider)` 函数，在 `_probe_stale_repos` 入口处调用，401/403 时放弃整轮 + 设置页告警。

20.4 **FR-8 兜底 token 收口实现**（P0，repos.py）：FR-8 依赖 FR-1.3——兜底 `get_repo_token_with_refresh` 返回的 token 也必须走能力前置检查。当前兜底路径直接送 token 进判定逻辑，堵死"主入口修了、兜底又送进坏 token"的复活路径未落地。修复：兜底 token 同样调用 `_check_token_capability`，不过则放弃。

20.5 **FR-9.1 设置页托管人/admin 权限校验**（P0，settings.py）：`update_gitcode_repo_config` 只做了 `verify_token` 但无托管人/admin 校验——普通成员可写入 bot token。修复：复用 `host_repo` 的 `_check_admin` 逻辑，非托管人/admin 返回 403。

20.6 **repos.py:1126 的 401 检测收敛到 is_auth_401_error()**（P0，repos.py:1126）：host_repo 的 RuntimeError catch 用内联 `if "401" in _err or "403" in _err or "unauthorized" in _err:` 判断，而非 `is_auth_401_error()`。"token invalid"、"token expired" 等场景不会被命中，被当作 502 通用错误返回。修复：改用 `is_auth_401_error(_err) or "403" in _err`。

**P1 级（应修复）**：

20.7 **resolve() 性能优化**（P1，token_resolver.py:730）：`resolve()` 先调 `resolve_config_level()` 全链遍历，然后再遍历策略链调 `get_token()`——每次执行两次策略链扫描 + 两次 DB 查询。修复：将 config_level 填充延迟到找到胜出策略后，用胜出策略的 level 推算 config_level，或缓存结果。

20.8 **get_feishu_open_id / update_unified_user_info GitHub-only 用户回退**（P1，session_manager.py:425/468）：缩窄为仅查 gitcode_user 后，GitHub-only 用户（gitcode_user 为 NULL 但 github_login 有值）的飞书通知投递失败。修复：加 `OR github_login=?` 回退，或提供 repo_key 参数按 provider 分查（对齐 issues_center._get_feishu_open_id 模式）。

20.9 **deps.py mark_identity_expired 返回值语义**（P1，deps.py:396/560）：刷新失败时先 `mark_identity_expired` 设 status=4，然后 `return new_token or token` 返回旧 token。绕过 TokenResolver 直接调 deps 函数的调用方会拿到已标记过期的 token。修复：确认所有调用方都经过 TokenResolver 短路检查，或在返回值中标记 expired 状态。

20.10 **session_store 写入 resolve_config_level().token 含过期 bot token**（P1，session_store.py:549）：`resolve_config_level()` 的 `_fetch` 不过期检查，即使 bot token 已标记过期，原始值仍写入 `devops_agent_session.repo_gitcode_token`。agents API 返回 `repo_gitcode_token_exists=true` 误导。修复：用 `read_stored_token` 语义或额外检查 `_is_expired` 后决定是否写入空串。

20.11 **FR-9.2 webhook 重建失败 warning 返回**（P1，settings.py）：`update_gitcode_repo_config` 返回固定 `{"message": "GitCode Repo 配置已更新"}`，无 warning 字段。`update_managed_repo_bot_token` 内部重建 webhook 失败信息未传递到 API 响应。修复：`update_managed_repo_bot_token` 返回重建结果，settings API 返回 `{"message": ..., "warning": "webhook 重建失败，请到仓库页执行重检修复"}`。

20.12 **FR-6.1 find_existing_webhook 改用 bot token**（P1，repo_manager.py）：`find_existing_webhook` 函数签名未改，调用方仍传用户 token。路径 B 时用户 token 可能是窄 PAT，查 hooks 空列表误判"无 webhook"。修复：路径 B 时改用 bot token（bot 是 maintainer，hooks 可见），空列表判"不确定"。

20.13 **FR-10.2 升级通知实际送达 host**（P1，user/db.py）：当前 `auto_upgrade_path_b_repos` 仅 `log.info(...)` 记审计日志，无飞书通知/站内信发送。"运行中任务身份衔接"（快照语义）完全未实现。修复：升级完成后对受影响仓库 host 发送飞书通知（语义变更说明），运行中任务收尾回帖沿用启动时 token 层级。

20.14 **FR-2.2 审计日志补完**（P1，repos.py）：级联清理只有清理前一行 `log.info("deleted (polling-confirmed), auto-cleanup")`，无清理后确认日志，无"两路信号"记录。修复：清理前后各写审计日志（含两路信号：用户 token 404 + bot token 404）。

20.15 **FR-16.1 唯一索引补 conv_type 列**（P1，db_setup.py:1833）：设计文档要求 `(user_id, repo_key, issue_number, conv_type)` 四列唯一索引，实际实现只有三列缺 conv_type。同一 (user, repo, issue) 不同 conv_type 的会话会撞约束误报冲突。修复：补 conv_type 列，部分索引条件调整为 `WHERE issue_number > 0 AND repo_key != '' AND conv_type = 'task'`。

**P2 级（建议修复，不拦发布）**：

20.16 **搭车文件剥离**（P2）：`docs/weekly-08/events-fullscreen-zindex-*.md` 两个文件不属于 issue-372 范围，应独立分支提交。

20.17 **exchange_codes / binding_tickets 异常捕获收窄**（P2，user/db.py:307 / grape_account_db.py:1417）：`except Exception` 捕获所有异常后执行 DELETE+INSERT，可能掩盖非约束冲突的错误。修复：精确捕获 `sqlite3.IntegrityError` / MySQL 1062。

20.18 **update_managed_repo_bot_token 共享 token 提示返回**（P2，user/db.py:1748）：共享 bot token 的关联仓库列表仅 log.info，不返回给调用方。修复：将 `_related_repos` 返回给 settings API，作为 scope_warnings 展示。

## 5. 场景与用例

### 场景 A：用户保存/更换个人 Token（PAT）

| 用例 | 前置 | 操作 | 预期（修复后） | 覆盖 FR |
|------|------|------|----------------|---------|
| A1 保存窄 scope PAT（无 hooks 权限） | 已有 OAuth 绑定 + 3 个托管仓库 | 设置页保存 PAT → 打开项目页 | 仓库保持托管，零数据变动；探活正常 | FR-1.1/1.3 |
| A2 保存窄 scope PAT（无 repo 读） | 同上 | 同上 | 仓库保持托管；探活前置检查放弃整轮；设置页"Token 权限不足"告警 | FR-1.3/FR-3.1 |
| A3 保存过期 PAT | 同上 | 同上 | 保存时 400 拒绝；历史数据则探活 401 放弃 + 设置页"Token 已失效"告警 | FR-1.3/FR-3 |
| A4 PAT scope 只覆盖部分仓库 | 10 个仓库成员 | 打开项目页 | 列表只显示可见部分 + "列表可能不完整"提示；托管仓库不被误判 | FR-1.2/FR-5.4 |
| A5 保存权限不足 PAT 的入口提示 | 无 | 设置页保存 | 保存成功 + scope 建议弹窗 | FR-4 |
| A6 OAuth 用户正常使用（回归） | OAuth 全 scope | 打开项目页/刷新 | 行为与修复前完全一致 | 回归 |

### 场景 B：托管状态探活（自动后台）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| B1 webhook 真丢失 | 用户在 VCS 手动删 webhook（还有其他 hook） | 打开项目页触发探活 | hooks 非空且缺 expected_wh → "待修复"状态 + 横幅；**零数据删除** | FR-1.1/FR-2.1 |
| B2 webhook 查询空列表（视角不清） | 窄 scope PAT | 同上 | 判"不确定"跳过 | FR-1.1 |
| B3 仓库真删除（双信号） | 仓库在 VCS 已删 | 同上 | 双信号成立 → 级联清理 + 审计日志 | FR-1.2/FR-2.2 |
| B4 仓库 404 但 VCS 列表仍在（单信号） | PAT 无该仓库读权限 | 同上 | 交叉验证跳过不判 | FR-1.2 |
| B5 批量误判保险丝 | token 失效致 10 仓同时"stale" | 同上 | stale > 2 → 整轮放弃 + 告警，零变动 | FR-2.3 |
| B6 兜底 token 复活路径 | 路径 B 探活走兜底 token | 同上 | 兜底 token 过能力前置检查 | FR-8 |

### 场景 C：托管 / 修复 / 重建 webhook

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| C1 窄 PAT 托管仓库 | 用户 token 为 PAT | 点"托管" | 失败时提示"Token 权限不足或已过期，请更新后重试或改用 OAuth" | FR-5.2 |
| C2 保存仓库 bot token 触发重建 | 仓库已有 webhook | 仓库配置页保存 | 查旧 webhook 用 bot token；查不到判"不确定"保守分支 | FR-6.1 |
| C3 find_existing_webhook 空列表 | PAT 无 hooks 权限 | 触发任一调用点 | 判"不确定"跳过新建，不产生双 webhook | FR-5.1 |
| C4 共用 bot token 信任域 | 仓库 A、B 共用 token | 更新 A 的 bot token | 提示"将影响 B 等关联仓库的跨仓信任域" | FR-6.2 |

### 场景 D：OAuth token 生命周期

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| D1 OAuth 刷新成功（回归） | token 距过期 < 10 分钟 | 任意写操作 | 刷新成功返回新 token | 回归 |
| D2 OAuth 刷新失败 | refresh_token 也失效 | 同上 | 立即 expired 标记；设置页"GitCode 登录已过期"；返回值兼容 | FR-7.1/7.3 |
| D3 用户重新 OAuth | D2 之后 | 重新登录 | expired 标记自动清除 | FR-7.2 |
| D4 PAT 不误触发刷新（回归） | 无 refresh_token 的 PAT | 同 D1 | 直接返回 PAT | 回归 |

### 场景 E：设置页 DevOps Bot Token 快捷入口

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| E1 保存无效 token | 粘贴了过期/手滑 token | 设置页仓库配置保存 | 400 TOKEN_INVALID 拒写，DB 与 webhook 不动 | FR-9.1 |
| E2 非托管人保存 | 普通成员 | 同上 | 403 拒绝（与 R15 同口径） | FR-9.1 |
| E3 有效 token 但重建失败 | token 有效但 hooks 受限 | 同上 | token 写入 + warning"webhook 重建失败，请执行重检修复" | FR-9.2 |
| E4 查看配置状态（回归） | token 已配置 | 打开设置页 | "已配置，留空保持不变" + 权限范围分级显示正常 | 回归 |

### 场景 F：用户 Token 驱动（路径 B）专项

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| F1 host 保存缺 issue 写权限的 PAT | 用户是仓库 R 的 host，纯路径 B 部署 | 保存 PAT → 触发 agent 任务 | 提示"此 Token 将驱动仓库 R 的 agent 回帖/建单，缺 issue 写权限将导致回复静默失败"；回帖失败附注"Token 权限不足（403）" | FR-10.1 |
| F2 host 保存全权限 PAT（回归） | 同上但 PAT 含 issue 写 | 同上 | 无警告；回帖/建单正常 | FR-10.1 回归 |
| F3 auto_upgrade 升级后的 host 评论 | 仓库 R 从路径 B 升级为路径 A | host 手工 comment | 升级通知已送达 host（语义变更说明）；agent 行为符合新语义且 host 已知情 | FR-10.2 |
| F4 升级瞬间运行中任务的身份 | 仓库 R 有执行中任务时触发升级 | 任务收尾 | 收尾回帖沿用启动时的 token 层级，不出现 commit=host、评论=bot 割裂 | FR-10.2 |
| F5 level 过期漂移边界 | host token 过期 + 路径 B | 触发事件 | 自循环检测读 config_level 不受过期影响；执行链按实际 token；两侧认知一致 | FR-10.3 |
| F6 路径 B 自循环四条件（回归） | host token 有效，任务执行中 | host 的 agent 回帖产生事件 | 四条件正确判定自循环并跳过 | 回归 |

### 场景 G：R25 §36 空串匹配（发布阻断专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| G1 空 assignee 建档 | 库中存在空串 github_login 行（模拟 D32 迁移数据） | 需求中心建单（set_issue_scene 先行建档，assignee 空串） | issue.user_id = 0（不错捞任何行）；后续 assignee 回填时 UPDATE 正确归属 | FR-11.1 |
| G2 正常 GitCode 用户建档 | mapping 有 login_a 行 | 建单 assignee=login_a | user_id 正确映射到创建者 | FR-11.1 回归（§36 正向能力） |
| G3 GitHub 用户建档 | mapping 有 github_login=A 行 | 建单 assignee=A | user_id 正确映射（OR github_login 分支持续生效） | FR-11.1 回归 |
| G4 空入参查询早退 | mapping 有空串行 | issues_center.py:95 空入参查 open_id | 返回空（不返回错误 open_id），通知不发错人 | FR-11.2 |
| G5 非空入参 + 空串列共存 | mapping 混有正常行与空串行 | engine_select.py:169 查 repo_user_id | 只命中真实用户名行，空串列不干扰 | FR-11.2 |
| G6 写入侧空串置 NULL | 新用户绑定 GitHub 未登录过 | 写 unified_user_mapping | github_login 落 NULL 而非 ''，空串命中面不再新增 | FR-11.3 |
| G7 存量清洗幂等 | 库中 36 行空串 github_login | 执行清洗 | 全部转 NULL；重复执行无变化；正常行不受影响 | FR-11.3 |
| G8 dev 错归属修正 | issue 1602 user_id=1（错） | 执行善后脚本 | user_id=2（创建者）；错建会话归并/改属；"我的需求"可见 | FR-11.4 |
| G9 双会话归并 | 同 Issue 存在错建（user_id=1）与正确（user_id=2）会话 | 归并 | 任务会话单条且归属正确，消息不丢 | FR-11.4 |

### 场景 K：跨平台分列查询（FR-17 专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| K1 GitCode 仓库查 GitCode 用户 | mapping 有 gitcode_user=alice（user_id=5）+ github_login=alice（user_id=8），两个不同的人 | GitCode 仓库建单 assignee=alice | user_id=5（gitcode_user 列命中），不误捞 GitHub 的 alice | FR-17.1 |
| K2 GitHub 仓库查 GitHub 用户 | 同上 | GitHub 仓库建单 assignee=alice | user_id=8（github_login 列命中），不误捞 GitCode 的 alice | FR-17.1 |
| K3 飞书通知只查 gitcode_user | handler_async 从 grape_accounts.username=alice 取用户名 | 查飞书 open_id | 只查 gitcode_user 列，不扫 github_login | FR-17.2 |
| K4 pipeline 展示飞书昵称 | GitCode 仓库事件 actor=alice | 调 _resolve_feishu_name(alice, repo_key) | 按 repo_key provider 查 gitcode_user 列 | FR-17.1 |
| K5 chat 解绑清映射 | grape_accounts.username=alice 的用户解绑飞书 | UPDATE unified_user_mapping | 只清 gitcode_user=alice 的行，不动 github_login=alice 的行 | FR-17.2 |
| K6 session_store 建档按 provider 分查 | GitCode 仓库 assignee=alice | ensure_row INSERT | 子查询只查 gitcode_user 列，不 OR 扫两列，不错捞 GitHub alice | FR-17.1 |
| K7 全量导出不受影响（回归） | mapping 有混合行 | get_feishu_mapping() | 返回所有有任一平台用户名的行（OR 语义正确） | FR-17.3 回归 |

### 场景 H：空值链治理（孤儿需求 / 双空 token / bot 名单 / 空 actor）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| H1 双空 token 仓建单 | 63 仓之一（bot 空 + host=0），私有仓 | 需求中心建单 | assignee 解析降级路径留 warning 日志（不再静默）；归属不落"最早用户"猜测（见 P2-4 修复）而是 0+管理员可见清单 | FR-12.1 |
| H2 管理员空 token 清单 | 存在双空仓 | 管理员查看告警 | 仓列表 + 影响说明（"N 仓需求归属解析降级"） | FR-12.1 |
| H3 孤儿需求对账 | 63 条 user_id=0 且 assignee 空行 | 执行一次性对账 | 能回溯的回填真实属主；回溯不到的标"无主-仅展示" | FR-12.2 |
| H4 孤儿需求操作越权收紧 | 孤儿需求 id=X | 普通用户（非属主非 admin）调确认完成/取消/重派 | 拒绝（403）——展示保留但操作收紧 | FR-12.2 |
| H5 孤儿需求展示角标 | 对账后仍无主的需求 | 任务中心/仓库列表查看 | "无主"角标与正常需求区分 | FR-12.2 |
| H6 bot 名与真人冲突 | 用户把仓 bot_username 配成某真人用户名 | 保存仓库配置 | 告警拒绝或强提示"与真人账号冲突，该用户操作将被自循环过滤" | FR-12.3 |
| H7 飞书 pending 空 uid | 空 actor 事件触发飞书任务通知，uid 反查失败 | 通知派发 | 不新建 task 会话（降级普通通知或 pending）；不产生同 Issue 双会话 | FR-12.4 |
| H8 A2A 空 actor 收口 | 构造 actor 提取失败的 A2A 事件 | 事件入库 | actor 有兜底（comment/issue 作者），不再空串入库；不喂"最早用户"解析 | FR-12.5 |
| H9 正常 bot 自循环过滤（回归） | bot_username 正确配置 | bot 的 issue 评论 | 仍被自循环过滤跳过（现有正确行为保持） | 回归 |

### 场景 I：时区 / 覆盖写入 / LIKE（第四轮扫描）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| I1 token 2h 后过期（+08 服务器） | OAuth 登录成功，expires_in=7200 | 立即发起任意请求触发 get_user_token_with_refresh | need_refresh=False（不误判过期），不发 refresh 调用 | FR-13.1 |
| I2 token 5 分钟后过期 | expires_in=300 | 同上 | need_refresh=True，正常刷新 | FR-13.1 回归 |
| I3 非 +08 服务器部署 | UTC 时区服务器 | 同 I1 | 判定同样正确（UTC 显式解析不依赖服务器时区） | FR-13.1 |
| I4 refresh 限流叠加（组合事故回归） | I1 修复前场景：每请求触发刷新 + GitCode refresh 限流 | 请求持续 | 修复后不再每请求刷新；FR-7 静默降级路径不再被持续喂料 | FR-13.1/FR-7 组合 |
| I5 群重新绑定保配置 | 用户已开 notify_enabled=1 的群绑定 | 解绑后重新绑定（或换仓重绑） | notify_enabled 保持 1（实测复现的丢失场景关闭）；auto_impl/im_channel_name 同理 | FR-14.1 |
| I6 绑定正常路径（回归） | 新群首次绑定 | 绑定 | 默认值正常落库，功能不变 | FR-14.1 回归 |
| I7 票据冲突重试 | 同一 OAuth 流程重复发起（旧票未消费） | 保存新票 | INSERT 冲突 → 重试成功，旧票不被静默覆盖 | FR-14.2 |
| I8 LIKE 转义（防御性） | provider 值包含 % 的假设输入 | 走解绑删除链 | 转义后仅匹配字面量前缀，不扩大删除范围 | FR-15.1 |

### 场景 J：并发建档（第五轮扫描）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| J1 双主并发建同一会话 | 双主共库，节点 A/B 几乎同时为同一 (uid, repo, issue) 建会话 | 并发 INSERT | 一方撞唯一约束失败降级为复用已有行；库中单行 | FR-16.1 |
| J2 普通会话不受约束影响 | issue_number 为空的普通聊天会话 | 创建多个 | 不触发唯一冲突（部分唯一/空值哨兵正确处理） | FR-16.1 回归 |
| J3 存量双会话清理 | 1602 类同 Issue 双会话存量 | 执行清理迁移 | 保留最新会话，旧会话消息归并不丢；单行结果 | FR-16.1 |
| J4 fetchone 渐进加固 | 触碰任一存量 fetchone()[0] 位点 | 改造 | 该处改为空安全模式；未触碰位点不动（不做批量改） | FR-16.3 |

### 场景 L：um 主路径查询缺陷止血修复（FR-18 专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| L1 gitcode 仓 + PAT 绑定用户主路径命中 | um.gitcode_user=mm6667（PAT 写真值），repo_key=gitcode:xxx | engine_select 查 user_id | 按 provider 分列查 gitcode_user 命中 user_id | FR-18.1 |
| L2 issue 建卡分列对齐 | um.gitcode_user=login_a，repo_key=gitcode:xxx | session_store ensure_row 建卡 | provider 分列查询对齐主路径；不宣称全 0 修复（0 的成因是 R1 本身），防未来 github 侧空串误配 | FR-18.2 |
| L3 撞名注册者不冒领 | 攻击者注册 username=mm6667（与受害者 GitCode login 同名），um 行 gitcode_user=注册名 | engine_select 回退链查 grape_accounts | EXISTS 校验 um.gitcode_user=ga.username → 攻击者 um 行的 gitcode_user 是自己的注册名而非受害者平台身份 → 拒绝 | FR-18.3 |
| L4 空串 actor 不捞行 | 空串 issue_creator | engine_select / issues_center 查询 | 空参守卫 ?!='' 拦截，返回 0/None | FR-18.1/18.5 |
| L5 github 仓分列不误捞 | um 有 gitcode_user=alice（user_id=5），github_login 全空 | GitHub 仓库 actor=alice | 分列查 github_login 不命中（列空）→ 回退链 EXISTS 正确拒绝 → 落 0（不误捞 gitcode 的 alice） | FR-18.1/18.3 |
| L6 update_unified_user_info 空串不洗真值 | um.gitcode_user=alice 已有真值 | 调 update_unified_user_info(gitcode_user='') | UPDATE CASE WHEN '' THEN gitcode_user ELSE ? → 真值保留不覆盖 | FR-18.4 |
| L7 backfill 改源（防复发） | 新用户注册 grape_accounts + 绑定 GitCode OAuth | 服务重启触发 backfill | um 行只建 (user_id) 空行；gitcode_user 从 external_identities 回填真 login（不灌注册名）；未绑定用户 gitcode_user 为空 | FR-18.4 |
| L7b backfill 改源后飞书链路回归 | 已绑定用户 backfill 回填真 login | 飞书 sync 调 update_unified_user_info 传平台 login | 真 login 写入 um.gitcode_user（比注册名更对）；飞书反查命中 | FR-18.4 回归 |
| L7c backfill 改源后未绑定用户 | 未绑定平台的本地注册用户 | 服务重启触发 backfill | um.gitcode_user 为空；飞书反查不命中（该用户需自己注册/绑定后建立飞书映射） | FR-18.4 |
| L8 prompt_builder SQL 约束 | agent 执行飞书昵称匹配流程 | bot 执行提示词中的 SQL | user_id=0 时跳过；gitcode_user 为空时不 UPDATE 该列 | FR-18.4 |
| L9 飞书通知读点不受影响（回归） | um.gitcode_user=alice 行有 feishu_open_id | 调 get_feishu_open_id(alice) | 仍查 um.gitcode_user 列命中（加空参守卫），行为不变 | FR-18.5 回归 |
| L10 全量导出不受影响（回归） | um 有混合行 | get_feishu_mapping() | OR 语义正确，返回所有有平台用户名的行 | FR-18.5 回归 |
| L11 注销/解绑行为不变（回归） | 用户注销 / 解绑飞书 | 注销删 um 整行 / 解绑清飞书三列 | 现有行为保持不变 | 回归 |
| L12 其余 11 处 OR 读点空参守卫 | 空串入参 | 调 pipeline/chat/session_manager 的 um OR 查询 | ?!='' 守卫拦截，不捞行 | FR-18.5 |
| L13 主路径撞名残留（已知风险） | 攻击者注册名=受害者 GitCode 真名，um.gitcode_user=注册名 | engine_select 主路径查 um | 分列后仍直接命中攻击者 um 行（止血版无解，EXISTS 在回退链走不到）。已知残留，根治版换锚消除 | 已知残留 |
| L14 github 侧归属 0 监控基线 | issue 表 github 侧 128 行中 13 行 user_id=0（10%） | 上线后监控 | 收紧后"碰巧命中"消失，0 占比可能升高；须逐条核对升高部分来自"正确拒绝"而非漏配。ei github 6 条有效 login 可兜底 | 已知残留 |
| L15 飞书绑定/反查全链路回归 | 4 处调用 update_unified_user_info 传注册名（get_feishu_open_id:431/433、pipeline:53、issues_center:95、feishu_onboard.py:266） | 18.4 CASE 守卫上线后飞书绑定流程 | CASE 守卫不误伤飞书链路——传注册名时 ?≠'' 走 ELSE 分支正常写入；仅空串时保留真值 | FR-18.4 回归 |
| L16 OAuth 注册补写 um 平台列 | 新用户 GitHub OAuth 注册成功 | grape_auth create_external_identity | um 行 INSERT OR IGNORE (user_id) + UPDATE github_login=external_login（空窗消除） | FR-18.6 |
| L17 OAuth 注册补写后飞书不命中 | 新 GitHub OAuth 用户注册后立即发飞书通知 | 飞书反查 um.github_login | 命中（补写后 github_login 非空） | FR-18.6 回归 |
| L18 防复发完整性验证（4 块拼图） | backfill 改源 + OAuth 补写 + 分语境写入 + 清洗清列保行 全部上线后 | 重启 + OAuth 注册 + 飞书扫码绑定 | 新用户 um.gitcode_user 从 ei 回填真值；github_login 注册即写入；飞书扫码绑定不覆盖平台列；清洗后不重灌 | FR-18.4/18.6/18.7 |
| L19 飞书注册名回写（防复发 MR 验证） | backfill 改源后用户扫码绑飞书（onboard:266 传注册名） | update_unified_user_info 调用 | 18.7 分语境后走 bind_feishu_identity 只写飞书三列，不碰 gitcode_user；真 login 保留不被洗回注册名 | FR-18.7 |
| L20 清洗清列保行纪律 | 存量 113 行伪身份清洗 | 执行清洗脚本 | UPDATE SET gitcode_user='' 保行（不 DELETE 整行）；下次重启 backfill INSERT OR IGNORE 命中已有行不重灌 | 执行纪律 |

### 场景 M：审计修复补完（FR-20 专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| M1 general 会话不受唯一约束 | 用户 U 已有 repo R 的 general 会话（issue_number=0） | 同用户在同仓库再建一个 general 会话 | INSERT 成功，不触发 UNIQUE constraint failed | FR-20.1 |
| M2 task 会话仍受约束 | 双主并发为同 (uid, repo, issue) 建 task 会话 | 并发 INSERT | 一方撞约束降级为复用；库中单行 | FR-20.1/20.2 回归 |
| M3 MySQL 部署索引创建 | MySQL 后端启动 | _create_indexes() 执行 | 部分唯一索引在 MySQL 下正确创建（降级为全列唯一或应用层守卫），不被 try/except 吞掉 | FR-20.2 |
| M4 token 能力前置检查-401 | 用户保存了过期 PAT | 打开项目页触发探活 | _check_token_capability 调 /api/v5/user 返回 401 → 放弃整轮 + 设置页"Token 已失效"告警 | FR-20.3 |
| M5 token 能力前置检查-403 | 用户保存了无 hooks 读权限的 PAT | 同上 | _check_token_capability 对托管仓库做 hooks 探测返回 403 → 放弃整轮 + 设置页"Token 权限不足"告警 | FR-20.3 |
| M6 兜底 token 能力检查 | 路径 B 探活走兜底 token | 同上 | 兜底 token 也调 _check_token_capability，不过则放弃（堵死复活路径） | FR-20.4 |
| M7 非托管人保存 bot token | 普通成员（非托管人非 admin） | 设置页仓库配置保存 bot token | 403 拒绝（与 R15 / host_repo 同口径） | FR-20.5 |
| M8 401 检测收敛 | token 过期触发 host_repo RuntimeError("token expired") | 点"托管" | is_auth_401_error 命中 → 403 TOKEN_PERMISSION_INSUFFICIENT（非 502 通用错误） | FR-20.6 |
| M9 resolve 性能 | webhook 事件触发 token 解析 | 调 resolve(repo_key) | 只遍历一次策略链（非两次）；config_level 从胜出策略推算 | FR-20.7 |
| M10 GitHub-only 用户飞书通知 | 用户绑了 GitHub OAuth + 飞书，gitcode_user 为 NULL | 发飞书通知 | get_feishu_open_id 按 github_login 回退命中，通知投递成功 | FR-20.8 |
| M11 过期 token 不写 session | bot token 已标记 status=4 | 重建 agent session | session 行 repo_gitcode_token 写空串；agents API 返回 repo_gitcode_token_exists=false | FR-20.10 |
| M12 webhook 重建失败 warning | token 有效但 hooks 受限 | 设置页保存 bot token | 响应返回 {"message": "...", "warning": "webhook 重建失败，请到仓库页执行重检修复"} | FR-20.11 |
| M13 find_existing_webhook 用 bot token | 路径 B 仓库，用户 token 是窄 PAT | 触发 webhook 重建 | find_existing_webhook 改用 bot token 查 hooks；空列表判"不确定"跳过新建 | FR-20.12 |
| M14 升级通知送达 host | 仓库 R 从路径 B 升级为路径 A | auto_upgrade 执行 | host 收到飞书通知（语义变更说明）；非仅 log.info | FR-20.13 |
| M15 级联清理审计日志 | 仓库双信号确认删除 | 探活触发级联清理 | 清理前 + 清理后各写审计日志，含两路信号（用户 token 404 + bot token 404） | FR-20.14 |
| M16 conv_type 不撞约束 | 同 (uid, repo, issue) 已有 task 会话 | 建 conv_type='chat' 的会话 | 不触发唯一冲突（conv_type 列区分） | FR-20.15 |

### 场景 P：um 伪身份清洗（FR-19.2 专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| P1 分语境写入是清洗硬前置 | 18.7 未合入 | 执行清洗 | 不允许清洗——飞书回写会把清空的 gitcode_user 洗回注册名（user_d 实测威胁） | FR-18.7/19.2 铁律 4 |
| P2 A 类未绑飞书子集清列 | 17 行 A 类未绑飞书行 | UPDATE SET gitcode_user='' 保行 | gitcode_user 置空，行保留（user_id 不变），下次重启 backfill INSERT OR IGNORE 命中已有行不重灌 | FR-19.2 铁律 1 |
| P3 A 类绑飞书子集单独评估 | 1 行 A 类绑飞书（user13/user_d, feishu_open_id 非空） | 清列前评估通知链路 | 清列后 OR 读点按注册名反查飞书会断。决策：保留 gitcode_user（接受撞名风险）或确认通知走 user_id 查询路径 | FR-19.2 铁律 1 例外 |
| P4 B 类人工甄别 | B 类行（gitcode_user 有值 + ei 有绑定） | 逐行核对 | gitcode_user 值=ei.external_login 则保留；不等则标记待修正 | FR-19.2 铁律 3 |
| P5 清列保行不 DELETE | A 类行清洗 | DELETE 整行 vs UPDATE SET 列空 | DELETE 后下次重启 backfill 重灌（洗了白洗）；UPDATE 保行后 INSERT OR IGNORE 命中不重灌 | FR-19.2 铁律 1 |
| P6 0 卡回填（assignee 非空） | 143 张 user_id=0 且 assignee 非空的 issue 卡 | 按 assignee 查 ei 回填 user_id | 能回填的 UPDATE user_id 归位 | FR-19.2 步骤 6 |
| P7 0 卡不回填（assignee 空） | 63 张 user_id=0 且 assignee 空的 issue 卡 | 回填脚本 | 不在回填范围（数据在 events 表，需 sender 历史回填或接受不归位） | FR-19.2 步骤 6 |
| P8 OR 读点复测 | 清洗 + 回填完成后 | get_feishu_open_id / pipeline feishu_name / session_store 建卡 | 清洗受益验证：user_a 从歧义捞行变单行命中；OR 读点行为符合预期 | FR-19.2 步骤 7 |
| P9 清洗→回填顺序不可倒 | 先回填 0 卡再清洗伪身份 | 错误顺序执行 | 回填按伪身份 assignee 查到错误 user_id。必须先清洗再回填 | FR-19.2 铁律 2 |
| P10 防复发完整性 | 4 块拼图全完后清洗 + 回填 | 全链路复测 | 伪身份不再产生（backfill 改源 + OAuth 补写 + 分语境写入 + 清洗清列保行） | FR-18.4/18.6/18.7/19.2 |
| P11 github E 类 login 缺失回填 | 5 行 identity login 空 + status=1（user24/25/27/29/32） | 平台 API 按 external_id 反查 login | 查到 login 的回填 identity.external_login；查不到的降级 status=0 | FR-19.2 步骤 5 |
| P12 github 反向 B 类撞名 | um.gitcode_user='user_a'(user33) = user2 的 github 真名 | B 类双向体检 | 体检输出 user33 反向撞名清单；人工甄别后清列 gitcode_user | FR-19.2 铁律 3 双向 |
| P13 github status=0 地雷排除 | identity (13,'user_d',status=0) 实测存在 | 回填 SQL 执行 | AND ei.status=1 排除该行；不把已解绑洗成有效绑定 | FR-19.2 步骤 6 |
| P14 github 0 卡回填预期 | 4 张 github 仓 user_id=0 卡，28/29 活跃账号无 github 绑定 | 回填脚本执行 | 仅绑定用户的历史卡可归位；其余正确无主（不是清洗失败） | FR-19.2 步骤 7 |
| P15 github 复测命中 | E 类补 login 后 | _repo_user_id('github:仓', 补 login 后的 login) | 主路径命中 grape_user_id | FR-19.2 步骤 8 |
| P16 反向撞名 OR 读点行为 | OR 读点传 'user_a'（user33 的 gitcode_user = user2 的 github 真名） | get_feishu_open_id / pipeline feishu_name | 清洗后 user33 gitcode_user 已清列 → OR 读点不误捞 user33 | FR-19.2 步骤 8 |

## 6. 非功能需求

- 全部改动在主控侧（repos.py / settings.py / repo_manager.py / 前端 js），worker 无感知，无 DB schema 变更。
- 修复需 hotfix 双挂 main + weekly-08（缺陷判定逻辑与级联清理均已进 main）。
- 向后兼容：旧字段/旧行为保留；新增判定只收紧不放宽。
- 审计：所有"放弃探活/跳过判定/触发清理"决策均落日志，带 token 能力检查结果。

## 7. 验收标准

- [ ] 保存窄 scope PAT 后打开项目页/刷新，托管仓库保持托管，无数据删除（A1/A2）
- [ ] 保存过期 PAT → 设置页告警，托管关系不变（A3）
- [ ] 真实 webhook 删除 → "待修复"状态 + 横幅，零数据删除，修复后恢复（B1）
- [ ] 真实仓库删除（双信号）→ 级联清理 + 审计日志（B3）
- [ ] 单轮 stale > 2 → 整轮放弃 + 告警，零变动（B5）
- [ ] 保存权限不足 PAT → 成功 + scope 建议（A5）
- [ ] find_existing_webhook 空场景不新建重复 webhook（C3）
- [ ] OAuth 用户行为与修复前完全一致（A6/D1/D4 回归）
- [ ] OAuth 刷新失败 → 立即 expired + 设置页提示，重登录后清除（D2/D3）
- [ ] 兜底 token 过能力前置检查（B6）
- [ ] 设置页无效 bot token → 400 拒写，DB 与 webhook 不动（E1）
- [ ] host 窄 PAT 保存提示 + 回帖失败根因附注（F1/F2）
- [ ] auto_upgrade 升级通知 + 运行中任务身份衔接（F3/F4）
- [ ] level 一致性：自循环检测读 config_level（F5）
- [ ] 空 assignee 建档 → user_id=0，不发生错归属（G1）
- [ ] 正常 GitCode/GitHub 用户映射保持正确（G2/G3，§36 能力回归）
- [ ] 空入参查询不再返回错误 open_id（G4）
- [ ] 写入侧空串置 NULL + 存量清洗幂等（G6/G7）
- [ ] dev 错归属与双会话善后完成（G8/G9）
- [ ] 双空 token 仓：降级可观测 + 管理员清单（H1/H2）
- [ ] 孤儿需求：对账回填 + 操作端点收紧（非属主 403）+ 无主角标（H3/H4/H5）
- [ ] bot 名与真人冲突：保存时告警拒绝（H6）
- [ ] 空 uid 不再误建双会话（H7）；A2A 空 actor 有兜底（H8）
- [ ] token 2h 过期不再被误判（I1）；非 +08 服务器正确（I3）
- [ ] 群重绑保留通知配置（I5）；票据冲突重试不覆盖（I7）
- [ ] LIKE 参数转义（I8，防御性）
- [ ] 双主并发建会话单行落地 + 存量双会话清理（J1/J3）
- [ ] 主路径分列命中：gitcode 仓 PAT 绑定用户正确命中 user_id（L1）
- [ ] issue 建卡分列对齐：不宣称全 0 修复，防未来 github 侧空串误配（L2）
- [ ] 撞名注册者不冒领：EXISTS 收严拒绝（L3）
- [ ] 空串 actor 不捞行（L4）；github 仓分列不误捞 gitcode 用户（L5）
- [ ] update_unified_user_info 空串不洗真值（L6）；backfill 改源——不灌注册名，从 ei 回填真值（L7）
- [ ] backfill 改源后飞书链路回归——真 login 反查命中（L7b）；未绑定用户 gitcode_user 为空（L7c）
- [ ] prompt_builder SQL 约束：禁 user_id=0 占位 + 空值不 UPDATE（L8）
- [ ] 飞书通知读点行为不变（L9/L10 回归）；注销/解绑不变（L11）
- [ ] 其余 11 处 OR 读点空参守卫（L12）
- [ ] OAuth 注册补写 um 平台列——空窗消除（L16）；补写后飞书命中（L17）
- [ ] 防复发完整性：4 块拼图全完后伪身份不再产生（L18）
- [ ] 飞书注册名回写堵住：分语境写入后扫码绑定不覆盖平台列（L19）
- [ ] 清洗清列保行：不 DELETE 整行，backfill 不重灌（L20）
- [ ] 主路径撞名残留风险已知接受——止血版无解，根治版消除（L13）
- [ ] github 侧归属 0 监控基线：13 行（10%），上线后升高须逐条核对（L14）
- [ ] 飞书绑定/反查全链路回归：CASE 守卫不误伤飞书链路（L15）
- [ ] general 会话不受唯一约束误拦（M1）；MySQL 部署索引正确创建（M3）
- [ ] token 能力前置检查：401 放弃整轮 + 告警（M4）；403 放弃整轮 + 告警（M5）；兜底 token 同样检查（M6）
- [ ] 非托管人保存 bot token → 403 拒绝（M7）
- [ ] 401 检测收敛到 is_auth_401_error（M8）
- [ ] resolve() 只遍历一次策略链（M9）
- [ ] GitHub-only 用户飞书通知投递成功（M10）
- [ ] 过期 token 不写 session 行（M11）
- [ ] webhook 重建失败返回 warning（M12）
- [ ] find_existing_webhook 改用 bot token（M13）
- [ ] 升级通知实际送达 host（M14）
- [ ] 级联清理审计日志含两路信号（M15）
- [ ] conv_type 不撞约束（M16）
- [ ] 分语境写入是清洗硬前置：18.7 未合入不允许清洗（P1）
- [ ] A 类清列保行不 DELETE + 绑飞书子集单独评估（P2/P3/P5）
- [ ] B 类人工甄别（P4）
- [ ] 0 卡回填：143 张 assignee 非空可回填，63 张 assignee 空不在范围（P6/P7）
- [ ] OR 读点复测：清洗受益验证（P8）
- [ ] 清洗→回填顺序不可倒（P9）
- [ ] 防复发完整性：4 块拼图全完后伪身份不再产生（P10）
- [ ] github E 类 login 缺失回填：5 行 API 反查 login（P11）
- [ ] github 反向 B 类撞名：双向体检+清列（P12）
- [ ] github status=0 地雷排除：AND ei.status=1（P13）
- [ ] github 0 卡回填预期：仅绑定用户可归位（P14）
- [ ] github 复测命中 + 反向撞名 OR 读点不误捞（P15/P16）
- [ ] 单测覆盖全部分支（空列表/404 交叉验证/单轮上限/前置检查失败/刷新失败标记/设置页拒写/分列查询/撞名冒领/EXISTS 收严/空参守卫/唯一索引/general 会话不受约束/MySQL 兼容）

## 8. 实施建议

- Commit 0（P0 发布阻断，最优先）：FR-11（空串匹配修复 + 写入侧约束 + 回归测试）——weekly-08 合出门槛前必须完成 11.1/11.2/11.5；11.3/11.4 善后可随后。
- Commit 0b（P0 发布阻断第二项）：FR-13（时区 UTC 解析，两处一行级修复 + 回归）——与 FR-7 静默降级叠加效应支撑拦发布定性。
- Commit 1（P0 堵漏）：FR-1 + FR-2 + FR-8，集中在 `_probe_stale_repos` 及全部 token 入口，约 60-80 行。
- Commit 2（P0 入口）：FR-9（设置页校验收敛，快速止血）。
- Commit 3（提示闭环）：FR-3 + FR-7，设置页告警条 + expired 标记。
- Commit 4（同款位点 + 用户 token 驱动）：FR-4 + FR-5 + FR-6 + FR-10。
- Commit 4b（空值链治理，发布前必修 12.4 双会话机制；其余随发布排期）：FR-12。
- Commit 4c（覆盖写入 + 防御性）：FR-14 + FR-15（不拦发布，随发布排期）。
- Commit 4d（并发加固）：FR-16.1（会话唯一约束 + 存量清理，并入发布前必修的双会话根治）；FR-16.3 排期。
- Commit 5（P0 发布阻断——um 止血修复）：FR-18.1-18.3（主路径分列+空参守卫 + session_store 建卡修复 + 回退链 EXISTS 收严，engine_select / session_store 两处主路径）。
- Commit 5b（写侧止血 + 读点守卫）：FR-18.4-18.5（backfill 停灌 + update_unified_user_info 守卫 + prompt_builder SQL 约束 + 11 处 OR 读点空参守卫）。
- Commit 6（根治方案，独立窗口）：FR-19（identity 换锚 + 存量清洗，另行评审，不拦发布）。
- Commit 7（P0 审计修复——索引+前置检查+权限校验）：FR-20.1-20.6（唯一索引修复 + MySQL 兼容 + token 能力前置检查 + 兜底收口 + 设置页权限校验 + 401 收敛）。
- Commit 7b（P1 审计修复——性能+飞书+warning+webhook+通知+日志）：FR-20.7-20.15（resolve 性能 + GitHub-only 飞书 + 过期 token 不写 session + webhook warning + find_existing_webhook bot token + 升级通知送达 + 审计日志补完 + conv_type 列）。
- Commit 7c（P2 审计修复）：FR-20.16-20.18（搭车文件剥离 + 异常捕获收窄 + 共享 token 提示返回）。
- Commit 8（P0 GitHub 对称性修复——第四轮深度审计发现）：FR-21.1-21.2（repos.py _repo_platform_token 未定义变量 + feishu_async GitHub 协作者写错列）。
- Commit 8b（P1 GitHub 对称性补完）：FR-21.3-21.6（探活 GitHub-only 触发 + settings GitHub 入口 + repo_cleanup 前缀通用化 + 附属 P2 项）。
- 测试：`python -m pytest tests/ -v`（覆盖率 ≥ 70%）；场景 A/B 参数化为 pytest（mock VCS 空列表/404/部分列表），场景 E 走 API 集成测试，场景 F 含路径 B 语义断言，场景 L 含分列查询 + 撞名冒领 + EXISTS 收严 + 空参守卫，场景 M 含唯一索引 + general 会话 + MySQL 兼容 + token 能力前置检查，场景 N 含 GitHub 对称性验证。

### FR-21 GitHub 对称性修复（第四轮深度审计发现）

> 来源：三路子 agent 并行审计 + 主审计人独立全链路审查，发现 GitCode/GitHub 对称性缺口。
> 优先级：FR-21.1/21.2 为 P0（发布阻断），FR-21.3-21.6 为 P1（应修复）。

#### FR-21.1 _repo_platform_token 未定义变量 user_id（P0，repos.py:2139）

问题：`_repo_platform_token` 的 GitHub 分支 `get_user_github_token_with_refresh(username, user_id=user_id)` 中 `user_id` 未定义。GitCode 分支 L2153 正确定义 `uid = user.get("id") or 0`。GitHub 分支漏了。

影响：每次 GitHub 仓库的 chat agent git-credential 请求都会 `NameError: name 'user_id' is not defined`。

根因：基线代码 bug（非本分支引入），但 GitHub 仓库数量少未暴露。

修复：L2139 改为 `user_id=user.get("id", 0)`，与 GitCode 分支对齐。

#### FR-21.2 feishu_async.py sync_mapping GitHub 协作者写错列（P0，feishu_async.py:768）

问题：`sync_repo_feishu_mapping` 对所有 provider 统一调 `update_unified_user_info(login, ...)`，但 `update_unified_user_info` 把第一个参数写进 `gitcode_user` 列。GitHub 协作者的 `login` 是 GitHub 真名，写进 `gitcode_user` 列是错的——应该写 `github_login` 列或只写飞书三列。

影响：GitHub 仓库协作者的飞书映射写错列后，按 `github_login` 查不到飞书 open_id → GitHub 仓库的飞书通知投递失败。

根因：`update_unified_user_info` 无 provider 参数，统一写 `gitcode_user` 列。设计文档原判断"feishu_async:768 是平台 login 语境，正确"有误——GitHub 平台 login 不该写 `gitcode_user` 列。

修复方案：`sync_mapping` 按 provider 分流——GitHub 仓库的协作者先从 `login` 查 `grape_user_id`（通过 `external_identities`），再调 `bind_feishu_identity(user_id, open_id, ...)` 只写飞书三列不碰平台列。GitCode 仓库维持原逻辑（`update_unified_user_info` 写 `gitcode_user` 列，因为 GitCode 协作者的 `username` 就是平台 login，写 `gitcode_user` 列正确）。

#### FR-21.3 探活只对 gitcode 触发——GitHub-only 用户不探活（P1，repos.py:434-449）

问题：`_start_stale_probe_async` 的 `if "gitcode" not in providers: return` 门控导致只有 GitHub 绑定的用户永远不探活。即使 `_probe_stale_repos` 内部已支持 GitHub 仓库检查（L235-258/L276-280/L330-334）。

影响：GitHub 仓库的 webhook 丢失/仓库删除不会被检测 → 托管数据残留。

修复：门控改为 `"gitcode" in providers or "github" in providers`，token 获取改为按用户绑定平台取主 token。

#### FR-21.4 settings.py 无 GitHub 仓库配置入口（P1，settings.py:558-598）

问题：GET `/settings/gitcode-repo/` 硬编码查 `bot_gitcode_token` 列。GitHub 仓库的 bot token 状态在前端不可见（`has_bot_token` 恒 false）。

影响：用户无法通过设置页查看 GitHub 仓库的 bot token 配置状态。

修复：GET endpoint 按 repo_key 前缀选列——`bot_gitcode_token` 或 `bot_github_token`。PUT endpoint 已通过 `create_provider_from_repo_key` 处理（FR-9.1），GET 需对齐。

#### FR-21.5 repo_cleanup.py _normalize_keys 只处理 gitcode: 前缀（P1，repo_cleanup.py:55-64）

问题：GitHub repo_key 的 `github:` 前缀不被剥离 → 清理遗漏。

影响：GitHub 仓库删除时清理不彻底，残留数据。

修复：用 `parse_repo_key` 通用化——剥离任意 provider 前缀，返回 `[repo_key, f"{provider}:{base}", base]`。

#### FR-21.6 附属 P2 项（不拦发布，随发布排期）

- repos.py:1525 `list_managed_repos` 硬编码 `bot_gitcode_token`——GitHub 仓库 `has_bot_token` 恒 false
- repo_manager.py:779 `find_existing_github_webhook` 缺少重试逻辑——GitCode 版有 3 次重试
- engine_select.py:218 fallback 硬编码 `gitcode:` 前缀——空 issue_creator 时 GitHub 仓库查不到
- issues.py:903 A2A payload 硬编码 `replace("gitcode:", "")`——GitHub 前缀不被剥离
- repos.py:1250 GitCode host_repo 未用 `with_oauth_token` 包裹 webhook 查找
- repo_cleanup.py:257 workspace 清理硬编码 `gitcode:` 前缀

### 场景 N：GitHub 对称性验证（FR-21 专项）

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| N1 GitHub 仓库 chat agent git-credential | GitHub 仓库托管中，用户绑了 GitHub OAuth | chat 发消息触发 agent | 不报 NameError，正常获取 GitHub token | FR-21.1 |
| N2 GitHub 协作者飞书映射 | GitHub 仓库有协作者 alice(login)，alice 绑了飞书 | sync_mapping 执行 | alice 的飞书 open_id 写入 um 行，按 github_login 查能命中 | FR-21.2 |
| N3 GitHub-only 用户探活 | 用户只绑 GitHub 无 GitCode，有托管仓库 | 打开项目页触发探活 | GitHub 仓库被探活，webhook 丢失/仓库删除被检测 | FR-21.3 |
| N4 GitHub 仓库设置页查看 | GitHub 仓库已配 bot token | 打开设置页仓库配置 | has_bot_token=true，token 状态正确显示 | FR-21.4 |
| N5 GitHub 仓库删除清理 | GitHub 仓库托管中，在 VCS 删除 | 探活触发级联清理 | github: 前缀的 repo_key 数据被清理干净 | FR-21.5 |
| N6 GitCode 仓库回归 | GitCode 仓库，原有流程不变 | 同 N1-N5 对应操作 | 行为与修复前完全一致 | 回归 |

#### FR-21.7 unbind_provider 不清 um 平台列（P1，grape_auth.py:1046-1090）

问题：解绑平台时删除了 external_identities + user_repos + user_credentials，但不清 `um.gitcode_user` / `um.github_login` 列。解绑后 um 平台列残留 → 飞书反查仍命中旧平台 login → 通知发到错误的人。

影响：用户解绑 GitCode 后，um.gitcode_user 仍存旧 GitCode login。当该用户名被其他操作反查（飞书通知、issue 归属）时仍命中，产生错误归属/错误通知。

根因：unbind_provider 的清理链遗漏了 um 平台列——与 FR-18.4 写侧守卫（CASE WHEN 不覆盖真值）矛盾：解绑后 um 真值已失效（external_identities 已删）但 um 列仍保留旧值，飞书反查走 gitcode_user 列仍命中。

修复：unbind_provider 中删除 external_identities 后，追加 `UPDATE unified_user_mapping SET {provider列}='' WHERE user_id=?`。用 `um_column_for_provider(provider)` 选列名。

#### FR-21.8 ensure_default_agent_sessions 只写 repo_gitcode_token（P1，session_store.py:572）

问题：`devops_agent_session` 有 `repo_github_token` 列（db_setup.py:3583），但 `ensure_default_agent_sessions` 只写 `repo_gitcode_token`。GitHub 仓库的 bot token 不写入 session → agents API 报 `repo_gitcode_token_exists=false`。

影响：GitHub 仓库的 agent session 行 token 列为空，前端无法判断 token 是否已配置。

修复：`ensure_default_agent_sessions` 按 repo_key 的 provider 选列——GitHub 写 `repo_github_token`，GitCode 写 `repo_gitcode_token`。

#### FR-21.9 agents API 只查 repo_gitcode_token（P1，agents.py:198-223）

问题：agents API 的 SQL `SELECT ... repo_gitcode_token ...` 硬编码列名，返回 `repo_gitcode_token_exists`。GitHub 仓库的 token 在 `repo_github_token` 列，查不到。

影响：GitHub 仓库的 agents API 恒返回 `repo_gitcode_token_exists=false`，前端误判"未配置 token"。

修复：SQL 按 repo_key 前缀选列——GitHub 查 `repo_github_token`，返回 `repo_token_exists`（统一字段名）。

#### FR-21.10 _purge_chat_im_binding 只清 gitcode_user 列（P1，chat.py:1348）

问题：飞书解绑时 `WHERE gitcode_user=?` 只清 gitcode_user 列的行。GitHub 用户的飞书映射在 github_login 列，不被清除。

影响：GitHub 用户换绑飞书 app 后，旧飞书 open_id 不被清除 → 通知发到旧 open_id。

修复：改为同时清 gitcode_user 和 github_login 列——`WHERE gitcode_user=? OR github_login=?`。

### 场景 N（续）：账号/token 链路对称性验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| N7 解绑平台后飞书反查 | 用户绑了 GitCode + 飞书，解绑 GitCode | 解绑后查 get_feishu_open_id(gitcode_login) | 返回 None（um.gitcode_user 已清空） | FR-21.7 |
| N8 GitHub 仓库 agent session token | GitHub 仓库配了 bot token | ensure_default_agent_sessions 执行 | repo_github_token 列写入 token 值 | FR-21.8 |
| N9 GitHub 仓库 agents API | GitHub 仓库 session 已建 | GET /agents?repo_key=github:xxx | repo_token_exists=true | FR-21.9 |
| N10 GitHub 用户换绑飞书 | GitHub 用户绑了飞书，换 app | _purge_chat_im_binding 执行 | um 行 feishu_open_id 被清空（按 github_login 命中） | FR-21.10 |
| N11 GitCode 解绑回归 | GitCode 用户解绑飞书 | 同 N7 对应操作 | 行为与修复前一致（gitcode_user 列被清） | 回归 |

---

## 9. FR-22：兼容性修复（深度审计发现）

基线合并后的深度兼容性检查发现 5 个问题，按优先级分级。

### FR-22.1：cooldown.py DELETE feishu_onboard_task 缺少参数（P0）

问题：`_anonymize_account` 中 `c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?")` 有 `?` 占位符但未传 `(user_id,)` 参数。

影响：管理员匿名化账号时 SQLite 报 `incorrect number of bindings`，匿名化流程中断。

触发条件：管理员执行账号匿名化操作（cooldown 匿名化路径）。

修复：补传参数 `(user_id,)`。

### FR-22.2：issues.py 权限收窄 role 字段缺失（P1）

问题：confirm_issue_done / cancel_issue / resume_issue 三处 SQL 改为 `user_id=? OR (user_id=0 AND ?='admin')`，但 `get_current_user` 返回的 user dict 只有 `id, username, token, token_type, login_provider`——没有 `role` 字段。`user.get("role", "")` 永远返回空串。

影响：`user_id=0 AND ''='admin'` 永远 false，非创建者对 user_id=0 的 issue 做 confirm_done/cancel/resume 全部返回 404。原来任何登录用户都能操作 user_id=0 的 issue，现在完全锁死。

触发条件：issue.user_id=0（创建者未注册平台或 webhook 自动创建的 issue），非创建者登录用户尝试确认完成/取消/恢复。

修复：user_id=0 的 issue 允许任何登录用户操作（回退到原行为 `user_id=? OR user_id=0`）。权限收窄应在 get_current_user 补 role 字段后再做，不在本次修复范围。

### FR-22.3：engine_select EXISTS 子查询大小写不匹配（P1）

问题：二级回退和 user_repos 回退的 EXISTS 子查询用 `ei.external_login=LOWER(?)`，但 external_identities.external_login 存储时没有做 LOWER 处理（grape_account_db.py INSERT/UPDATE 直接存原值）。

影响：平台 login 含大写字母（如 "user_d"）时，`LOWER('user_d')` = `'jason7602'` 不匹配存储的 `'user_d'`，导致 issue_creator 解析不到 user_id，二级回退和 user_repos 回退全部失效。

触发条件：issue_creator 的平台 login 含大写字母，且一级查询（unified_user_mapping）未命中（新用户或未 backfill）。

修复：EXISTS 子查询用 `ei.external_login=?` 不做 LOWER，或改用 `LOWER(ei.external_login)=LOWER(?)` 两侧都 LOWER。

### FR-22.4：_is_constraint_error SQLite 不匹配（P2）

问题：`_is_constraint_error` 检查 'integrity'/'duplicate'/'1062'，但 SQLite 报 `UNIQUE constraint failed: test.code` 消息含 'constraint' 不含 'integrity'/'duplicate'/'1062'。

影响：`save_binding_tickets` 在 SQLite 下 UNIQUE 冲突时 `_is_constraint_error` 返回 False，异常被 re-raise 而非重试。违反 FR-14.2 设计意图。`save_exchange_code` 不受影响（用 `hasattr(__cause__)` 恒 True 的旧逻辑）。

触发条件：SQLite 环境下，同一 ticket code 在 10 秒 TTL 内被重新生成（概率极低）。

修复：`_is_constraint_error` 增加 'constraint' 关键词检测。

### FR-22.5：get_feishu_open_id username vs platform login（P2）

问题：`handler_async.py:1417` 调用 `get_feishu_open_id(username, app_id=ctx.app_id)`，username 来自 `grape_accounts.username`（注册名）。改动后 WHERE 只查 `gitcode_user=?`，而 FR-18.4 backfill 把 `gitcode_user` 从 external_identities.external_login 填充（平台真 login）。

影响：如果注册名 != 平台 login 且从未做 OAuth 绑定（未触发 FR-18.6 `_sync_um_platform_col`），查不到 feishu_open_id，飞书通知无法送达。

触发条件：用户注册名与平台 login 不同，且从未通过 OAuth 绑定流程（手动注册用户 + 飞书扫码绑定但平台列未同步）。

修复：`get_feishu_open_id` 增加 user_id 回退——username 查不到时，从 grape_accounts 查 user_id，再按 user_id 查 um 行的 feishu_open_id。

### 场景 O：兼容性修复验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| O1 匿名化账号 | 管理员执行匿名化 | _anonymize_account(user_id) 执行 | feishu_onboard_task 行被删除，um 行列被清空，不报错 | FR-22.1 |
| O2 user_id=0 issue 确认完成 | issue.user_id=0，非创建者登录 | POST /issues/{id}/confirm_done | 200 成功（回退原行为） | FR-22.2 |
| O3 user_id=0 issue 取消 | issue.user_id=0，非创建者登录 | POST /issues/{id}/cancel | 200 成功 | FR-22.2 |
| O4 user_id=0 issue 恢复 | issue.user_id=0，非创建者登录 | POST /issues/{id}/resume | 200 成功 | FR-22.2 |
| O5 大写 login issue_creator 解析 | issue_creator="user_d"，um 未命中 | _repo_user_id("gitcode:org/repo", "user_d") | 二级回退 EXISTS 匹配 external_login="user_d" 返回 user_id | FR-22.3 |
| O6 小写 login 回归 | issue_creator="jason"，um 未命中 | 同 O5 | EXISTS 匹配 external_login="jason" 返回 user_id | FR-22.3 回归 |
| O7 binding_tickets UNIQUE 冲突 | 同一 ticket 10s 内重新生成 | save_binding_ticket 第二次调用 | 冲突被捕获，删旧票重试成功 | FR-22.4 |
| O8 注册名查飞书 open_id | 用户注册名="user_b"，平台 login="login_b"，已绑飞书 | get_feishu_open_id("user_b", app_id="xxx") | gitcode_user 列未命中 → user_id 回退 → 按 user_id 查到 feishu_open_id | FR-22.5 |
| O9 平台 login 查飞书 open_id（回归） | 用户注册名=平台 login="user_c" | get_feishu_open_id("user_c", app_id="xxx") | gitcode_user 列命中，返回 feishu_open_id | FR-22.5 回归 |

---

## 10. FR-23：webhook_id 空串写 MySQL 整型列导致 UPDATE 回滚（现网实测发现）

### FR-23.1：_update_repo_bot_token webhook_id 空串 MySQL 不兼容（P0）

问题：`_update_repo_bot_token` 函数签名 `webhook_id: str = ""`，写入时 `str(webhook_id) if webhook_id else ""`。当 webhook 重建失败（如 Bot Token 对仓库无 webhook 管理权限、删旧 webhook 403）时，`_rebuild_webhook_with_bot_token` 返回空串 `""`，传给 `_update_repo_bot_token(webhook_id="")`，最终写 MySQL `webhook_id INTEGER DEFAULT 0` 列时报 `(1366, "Incorrect integer value: '' for column 'webhook_id' at row 1")`。

影响：MySQL 整型列严格类型检查拒绝空串，整个 UPDATE 回滚——bot token 根本没写进 DB。managed_repos 里 bot_gitcode_token 仍为空、host_user_id 不变，仓库卡在路径 B 用户 token 驱动。任何一次 webhook 重建失败都会导致 token 静默丢失。SQLite 端因类型亲和性容忍空串不报错，问题只在 MySQL 生产环境暴露。

触发条件：
1. 生产环境使用 MySQL 后端
2. 用户在设置页保存仓库 Bot Token
3. Bot Token 对该仓库没有 webhook 管理权限（verify_token 只校验 token 本身有效性，不校验仓库权限）
4. 删旧 webhook 返回 403 → `_rebuild_webhook_with_bot_token` 返回空串 → `_update_repo_bot_token(webhook_id="")` → MySQL 1366

根因：`webhook_id` 列 DDL 是 `INTEGER DEFAULT 0`，但函数签名和写入逻辑按字符串处理。空串在 SQLite 类型亲和性下被隐式转为 0，MySQL 严格模式直接拒绝。

修复：`_update_repo_bot_token` 中 webhook_id 写入时，空串/空值统一写 0（INTEGER 列的语义：0 = 无 webhook）。

调用路径全覆盖（3 处）：
- L1774: `update_managed_repo_bot_token` → `_update_repo_bot_token(webhook_id=_new_hook_id)`，_new_hook_id 可能是空串
- L2472: `auto_upgrade_path_b_repos` → `_update_repo_bot_token(webhook_id="")` 显式空串
- L2478: `auto_upgrade_path_b_repos` → `_update_repo_bot_token(webhook_id=_new_hook_id)`，_new_hook_id 可能是空串

### 场景 P：MySQL 兼容性修复验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| P1 webhook 重建失败写 MySQL | MySQL 后端，Bot Token 无 webhook 权限 | 保存 Bot Token，删旧 webhook 403 | webhook_id 写 0，bot_gitcode_token 写入成功，返回 warning | FR-23.1 |
| P2 webhook 重建成功写 MySQL | MySQL 后端，Bot Token 有 webhook 权限 | 保存 Bot Token，重建成功 | webhook_id 写新 ID（整型），bot_gitcode_token 写入成功 | FR-23.1 回归 |
| P3 auto_upgrade 重建失败写 MySQL | MySQL 后端，路径 B 升级时 webhook 重建失败 | auto_upgrade_path_b_repos 执行 | webhook_id 写 0，bot token 写入成功，仓库切路径 A | FR-23.1 |
| P4 SQLite 回归 | SQLite 后端 | 同 P1 | 行为不变（SQLite 类型亲和性原已容忍空串，写 0 语义更准确） | FR-23.1 回归 |
| P5 webhook_id 非零整型写入 | 重建返回 webhook_id=12345 | _update_repo_bot_token(webhook_id="12345") | webhook_id 写 12345，正常 | FR-23.1 回归 |

---

## 11. FR-24：解除托管校验用注册名而非平台 login（现网实测发现）

### FR-24.1：_check_unmanage_vcs_permission 注册名当平台 login（P0）

问题：`_check_unmanage_vcs_permission`（repos.py:1810）用 `user.get("username")`（grape 注册名）做 VCS 侧权限校验：
1. `if username == owner` 比较仓库所有者——注册名 != owner 时捷径不走
2. `get_member_permission(gc_token, owner, repo_name, username)` 查 GitCode 协作者权限——用注册名查 `/collaborators/{username}/permission`
3. GitHub 侧同理：`get_github_collaborator_permission(gh_token, owner, repo_name, username)` 用注册名查

当注册名 != 平台 login（如注册名 "user_a"，GitCode login "login_a"）时：
- owner 比较："user_a" != "login_a" → 捷径不走
- GitCode API：GET /collaborators/user_a/permission → 404 User not found
- 按"失败不兜底"设计 → 403 拒绝

影响：仓库所有者无法解除自己仓库的托管——注册名和平台 login 不同时，VCS 侧永远 404，权限校验永远失败。用户被锁在自己的仓库之外。

触发条件：
1. 用户 grape 注册名 != VCS 平台 login（手动注册用户、跨平台用户、注册名与 GitCode/GitHub 用户名不一致）
2. 用户尝试解除托管（POST /repos/remove）
3. 仓库 owner 是用户自己的平台 login（如 login_a），但 grape 注册名不同（如 user_a）

根因：与 FR-21 同类——平台身份应从 external_identities 或 VCS /user 接口解析，不应拿 grape 注册名当平台 login。托管侧（host_repo）已用 `with_oauth_token` + GET `/user` 拿真实 login，解除托管侧漏了。

修复：对齐 host_repo 模式——先用 `with_oauth_token` + VCS `/user` 接口获取真实平台 login，再用 login 做 owner 比较和协作者权限查询。

### 场景 Q：解除托管 login 解析修复验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| Q1 注册名≠login 仓库所有者解除托管 | 注册名 "user_a"，GitCode login "login_a"，仓库 owner="login_a" | POST /repos/remove | with_oauth_token 拿 login "login_a" → login==owner → 天然通过 | FR-24.1 |
| Q2 注册名≠login 非所有者 maintainer 解除 | 注册名 "user_a"，login "login_a"，仓库 owner="other"，有 maintainer 权限 | POST /repos/remove | login "login_a" → 查 /collaborators/login_a/permission → access_level=40 → 通过 | FR-24.1 |
| Q3 注册名≠login 非所有者 developer 解除 | 同 Q2 但 access_level=30 | POST /repos/remove | 查到权限但 access_level=30 < 40 → 403 拒绝 | FR-24.1 |
| Q4 注册名==login 回归 | 注册名 == 平台 login == "user_c"，owner="user_c" | POST /repos/remove | with_oauth_token 拿 login "user_c" → login==owner → 通过 | FR-24.1 回归 |
| Q5 GitHub 仓库所有者解除托管 | 注册名 "user_b"，GitHub login "login_b"，GitHub 仓库 owner="login_b" | POST /repos/remove | with_oauth_token 拿 GitHub login "login_b" → login==owner → 通过 | FR-24.1 |
| Q6 GitHub 非所有者 admin 解除 | 同 Q5 但 owner="other"，role="admin" | POST /repos/remove | login "login_b" → 查 GitHub collaborators → role=admin → 通过 | FR-24.1 |
| Q7 token 过期 | 用户 token 401 | POST /repos/remove | with_oauth_token 触发刷新，刷新失败 → 401 "授权已过期，请重新登录授权" | FR-24.1 |
| Q8 无平台授权 | 用户从未绑定 GitCode/GitHub OAuth | POST /repos/remove | with_oauth_token 无 token → 401 "授权已过期" | FR-24.1 |

---

## 12. FR-25：session_store issue 归属 EXISTS 守卫注册名误判（全仓扫描发现）

### FR-25.1：session_store EXISTS 守卫 ga.username vs m.gitcode_user（P1）

问题：`_sync_issue_fields`（session_store.py:130）的 issue 建档 COALESCE 子查询用 EXISTS 守卫关联 um.user_id 到 grape_accounts：

```sql
EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = m.user_id
        AND (ga.username = m.gitcode_user OR ga.username = m.github_login))
```

FR-18.4 后 `m.gitcode_user` 存的是平台 login（从 external_identities 回填），`ga.username` 是 grape 注册名。注册名 != 平台 login 时（如注册名 "user_a"，GitCode login "login_a"），`ga.username = m.gitcode_user` 恒假 → EXISTS 恒假 → user_id=0。

影响：issue assignee 归属解析对"注册名≠平台 login"的用户全部落到 user_id=0——需求待办、通知归属全断。与 issues_center._get_user_id_by_gitcode 不同，这里没有二级回退（issues_center 有 `SELECT user_id FROM unified_user_mapping WHERE {_col}=? AND user_id>0 LIMIT 1` 不带 EXISTS 的 fallback），是真缺陷。

触发条件：
1. 用户 grape 注册名 != VCS 平台 login
2. FR-18.4 backfill 后 um.gitcode_user 存平台 login
3. webhook 事件触发 _sync_issue_fields 建档 → EXISTS 守卫失败 → user_id=0

根因：EXISTS 守卫的原意是"防止 backfill 伪身份"——确保 um.user_id 对应的 grape_accounts 行的 username 和 um 平台列一致。FR-18.4 改源后 um 存平台 login、ga.username 存注册名，两者不必相等，守卫语义失效。

修复：EXISTS 改为验证 `ga.id = m.user_id AND ga.status = 1`（确保账号有效且未禁用），不再比较 username 和平台 login。backfill 伪身份防护已由 FR-18.4 改源（从 external_identities 回填而非 grape_accounts.username）保证。

### 场景 R：session_store EXISTS 守卫修复验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| R1 注册名≠login issue 归属 | 注册名 "user_a"，login "login_a"，um.gitcode_user="login_a"，ga.username="user_a" | _sync_issue_fields 建档 | EXISTS ga.id=m.user_id AND ga.status=1 → 命中 → user_id=正确值 | FR-25.1 |
| R2 注册名==login 回归 | 注册名==login=="user_c" | 同 R1 | EXISTS 命中 → user_id=正确值 | FR-25.1 回归 |
| R3 禁用账号不归属 | ga.status=0（已禁用账号） | 同 R1 | EXISTS ga.status=1 不命中 → user_id=0 | FR-25.1 |
| R4 um 无匹配行 | assignee 在 um 表无记录 | 同 R1 | COALESCE 回 0 → user_id=0 | FR-25.1 回归 |
| R5 GitHub 仓 issue 归属 | GitHub 仓，um.github_login="login_b" | _sync_issue_fields 建档 | 按 github_login 列查 → EXISTS 命中 → user_id=正确值 | FR-25.1 |

---

## 13. FR-26：模板 onboarding owner 优先级反转（全仓扫描发现）

### FR-26.1：templates.py owner 优先用注册名而非平台 login（P1）

问题：`templates.py:277` 的 `owner = user.get("username", "") or owner_login` 优先用 grape 注册名，`owner_login`（VCS 平台 login）做降级。注册名非空时 `owner_login` 永远不被用到。

owner 传给下游两个使用点：
1. `create_standard_task(..., assignee=owner, ...)` → `_post_gitcode_issue(bot_token, owner, rname, ..., assignee=assignee)` → GitCode API 指派 Issue，assignee 需要平台 login
2. `_get_user_id_by_gitcode(owner, repo_key)` → 查 `um.{_col}=?`，um 存的是平台 login

注册名 != 平台 login 时：
- GitCode API 用注册名指派 Issue → 指派失败或指错人
- _get_user_id_by_gitcode 用注册名查 um 平台列 → 查不到 → user_id=0

影响：模板创建仓库后 onboarding 任务的 Issue 指派到错误的人或失败，任务归属 user_id=0。

触发条件：
1. 用户通过模板一键创建仓库
2. 用户 grape 注册名 != VCS 平台 login
3. 模板 onboarding 触发 _create_onboarding_tasks

根因：与 A1/A2/A3/A4 同类——注册名被当平台 login 用。`owner_login` 已经从 VCS /user 接口拿到了正确平台 login，但优先级反了。

修复：`owner = owner_login or user.get("username", "")`——优先用平台 login，注册名做降级。`owner_login` 在 L190/198 赋值且为空时已 raise，到 L277 一定已赋值。

### 场景 S：模板 onboarding owner 修复验证

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| S1 注册名≠login 模板创建 | 注册名 "user_a"，login "login_a"，模板创建仓库 | trigger_onboarding 执行 | owner="login_a" → GitCode API 指派成功 → _get_user_id_by_gitcode 查到 user_id | FR-26.1 |
| S2 注册名==login 回归 | 注册名==login=="user_c" | 同 S1 | owner="user_c" → 指派成功 → user_id 正确 | FR-26.1 回归 |
| S3 GitHub 仓模板创建 | 注册名 "user_b"，GitHub login "login_b" | 同 S1 | owner="login_b" → GitHub API 指派成功 | FR-26.1 |
| S4 owner_login 降级 | owner_login 为空（不可能场景，前面已 raise） | 同 S1 | 降级用注册名 | FR-26.1 降级 |