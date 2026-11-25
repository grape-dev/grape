# 邮箱验证码链路加固设计（Issue #3469）

日期：2026-11-19 · 分支：fix/issue-3469-email-verification-hardening · 需求：email-verification-requirements.md
基线：origin/arch-weekly-11 @ f10dc01a0

## 对象模型定位

邮箱在 grape 里同时承担三个角色：**登录凭据**（找回密码/设密码/解锁）、**账号唯一标识**
（`uq_active_email`）、**通知通道**（改绑提醒/注册提醒）。三者对"这个地址算不算绑定"的判据
必须同源，否则同一账号在不同路径被当成"有/没有/占位"——Issue 正文 N 描述的正是这种分叉。

本设计把判据收到 `auth/identity_email.ProviderEmailPolicy` 一处（形态 `is_valid` / 可投递
`is_deliverable` / 私有中转 `is_private` / 入库归一 `for_storage`），账号层
（`grape_account_db`）只负责**写**，路由层只负责**编排**，验证码服务只负责**码的生命周期**。

### D1 时间语义单一来源：UTC 写 + `calendar.timegm` 读

所有时间列（`verification_codes.created_at/expires_at`、`login_failures.created_at`）由
`time.gmtime()` 写成 UTC 墙钟字符串，因此读取必须 `calendar.timegm(strptime(...))`。
`time.mktime` 按进程本地时区解释，东八区偏移 8 小时，是 M1/M2 的唯一根因。

- 解析收口：`verification_code.UtcTimestamp.parse()`（值对象，`calendar.timegm`）与
  `login_throttle._utc_ts(s)`（本模块私有，两模块不互相 import，避免 auth 子包循环依赖）。
- 不改存储形态（仍是 `YYYY-MM-DD HH:MM:SS` UTC 串），不迁移历史数据——历史数据本来就是 UTC，
  修的是读取端，存量语义无需变更（Issue 风险表 §Risks 同口径）。
- 门禁：仓库内 `grep -rn "time.mktime(time.strptime" src/` 必须为 0（S257 反证 + 守卫用例）。
- 复审修复的定点用例见 S293–S299；MySQL 分支由 `tests/api/test_issue_3469_mysql_gate.py`
  在 `GRAPE_GATE_TEST=1` 下真库复算（CI 无 MySQL 时 skip，不当作通过）。

### D2 可投递判据单一来源

`ProviderEmailPolicy.is_deliverable(email)` = 形态合法 ∧ 非私有中转（`noreply.*` /
`privaterelay.appleid.com` / 非邮箱字面串）。"有 email 列但发不出信"的账号（现网 28 行）
在**所有业务判据**上按"未绑定邮箱"处理：发码入口引导先绑定，注销验证方式不提供"邮箱验证码"，
但 **改绑入口必须仍可达**（它是这批账号自救的唯一路径）。

### D3 `update_email()` 三列同写 + 解绑判据锁定

一条 UPDATE 同时写 `email`、`email_source='user'`、`email_is_placeholder=0`、
`email_verified=1`、`updated_at`；清空语义写 `email=NULL, email_source=NULL,
email_is_placeholder=1, email_verified=0`。

通知降级：旧邮箱不可投递（`noreply.*`/空）时不发邮件，改经 runtime 域契约
`src.domains.runtime.public.schedule_notify()` 发站内信/IM——跨域只走 public 契约，不碰外域表。

`clear_placeholder_email()` 的契约锁死为"`email_is_placeholder=1` **且** `email_source ==
provider` 才清"——D3 生效后，用户自绑的真实邮箱 `source='user'`、`placeholder=0`，解绑不再误删
（C1）。`clear_placeholder_email()` 清空时写 `email=NULL` + `email_is_placeholder=1` +
`updated_at`（UTC），不再造出"email 为 NULL 但 placeholder=0"这种 007 归一脚本要修的形态。
冲突判定 `is_email_registered()` 去掉 `email_is_placeholder` 过滤（占位行持有的真实邮箱
必须参与冲突，C3），保留私有邮箱排除，并把 `email != ''` 纳入（与 D10 的 `''`→NULL 同向）。

### D4 改绑二次认证：有因子才要求，无因子放行（G + Q 的逃生口）

`change-email` 提交前按账号实际拥有因子选路：

| 账号形态 | 二次因子 | 说明 |
|---|---|---|
| 有密码 | `password`（当前密码） | 阻止"会话+改绑+找回密码"无口令接管 |
| 无密码 + 原邮箱可投递 | `old_email_code`（scene `change_email_confirm`） | 原邮箱确认 |
| 无密码 + 原邮箱不可投递/空（占位账号） | `none` | **不得要求**，否则 28 个不可投递邮箱账号永久锁死（Q 明文约束） |

**执行顺序（复审修复）**：新邮箱验证码先以 `verify_code(..., consume=False)` 只判定，
二次因子通过后才 `consume=True` 正式消费——否则密码输错一次就把码烧掉，用户要等 60 秒
重发、还吃每小时 5 次上限。

`POST /change-email/send-code` 在响应里回 `require_factor`，让前端一次性渲染正确输入框；
`POST /change-email` 缺因子返回 400 `NEED_SECOND_FACTOR` + `require_factor`，错密码
`PASSWORD_INVALID`。审计日志对 `none` 分支打 `email_change_no_secondary_factor` 便于事后追。

> 已裁定不采纳"改绑后 24h 禁止找回密码"：它需要新列 + 迁移，且同样会把不可投递邮箱账号
> 的恢复窗口推后，与 Q 的逃生口冲突。

**残余风险（记入 follow-up）**：`NONE` 档账号（无密码 + 原邮箱不可投递）的"会话+改绑+找回"
接管链仍成立。复审建议对该档要求"用已绑 provider 重新授权"——它们有绑定，不会锁死。这需要
把 delete-account 的 OAuth 重授权机制扩展出 change-email 的 state kind，属独立批次，本 PR 不吸收。

### D5 验证码绑定目标邮箱（Q）：key 统一 `user:<id>` + target 入哈希盐

现实现把码存在 `(旧邮箱, scene)` 下，与 `new_email` 无关 → "发往 A 的码可确认 B"，且同一
占位字面串（`"未公开邮箱"`）的多个账号共享 key、互相顶 60s 间隔与 3 次窗口。

本批口径：

- 改绑链的**存储键统一 `user:<id>`**（`rate_limit_key`），不再随旧邮箱漂移。
- `new_email` **纳入 HMAC 盐**：`digest = HMAC(key, f"{target}\x00{code}")`，`target` 为空时
  退化为原输入（向后兼容未带 target 的旧调用）。表结构不变（不加列），满足 Q 的"或纳入哈希盐"。
- 校验时 `verify_code(key, scene, code, target=new_email)` 必须与发送时的 target 一致，否则
  `CODE_INVALID`。
- 不因此收紧改绑路径本身（D4 逃生口）。

### D6 失败原因结构化（P）

`verify_code()` 返回 `error_code ∈ {CODE_NOT_FOUND, CODE_EXPIRED, CODE_USED, CODE_INVALID}`，
`message` 保持中文人话。改绑链四处 400 全部带 `error_code`（`EMAIL_FORMAT` / `EMAIL_SAME` /
`EMAIL_TAKEN` / `CODE_INVALID` / `CODE_EXPIRED`），前端按码出文案、只在未知码时回退 `message`。
前端已把 `detail.error_code` 挂到 `Error.error_code`（`shell/js/api.js:302/325/342`），本批只补
渲染侧与 locale 词条。

`unlock/send-code` 对"账号不存在 / 邮箱不可投递"统一回 200 + 通用 hint（不区分存在性，
防枚举），避免用户静默等待一封永远不会来的信（复审 N3）。两套前端的可用验证方式判据
统一读后端 `email_deliverable`（`/auth/me` 与 `/auth/profile` 都提供，复审 N4）。

### D7 限流：影子记录 + 按 `user:<id>` 计频

`check_rate_limit()` 在检查通过后落一条**影子记录**（`code_hash=''`、`used=1`、`expires_at=now`），
让未注册邮箱 / 不发码的枚举探测也推高 IP 维度计数（H）。影子行 `used=1` 不参与验证码比对，
`_cleanup_expired()` 自然回收。

改绑限流是**双维度**（E 复审补齐）：

- 用户维度：验证码/计数键 `user:<id>`，每小时 5 次换绑上限（`count_sends`）；
- 新地址维度：`check_rate_limit(new_email, 'change_email', ip)`——60s 间隔 + 3 次/10 分钟，
  命中返回 `CHANGE_EMAIL_TARGET_LIMITED`。只按用户计时的话，一个账号可对任意多个未注册地址
  发码、多账号可对同一地址反复发，发信额度与骚扰面都失控。

### D8 哈希存 kid（I）

`code_hash` 形如 `"{kid}:{digest}"`（无 kid 退化为 digest）。校验按 kid 取
`jwt_utils._get_verify_secret(kid)`，未知 kid 或旧格式回退当前 `_get_signing_secret()`——
密钥轮换期间 10 分钟窗口内的码不再集体失效。

**列宽**：`"{kid}:{digest}"` 最长 = kid 长度 + 65，原 `VARCHAR(64)` 装不下（MySQL 严格模式
1406、非严格模式截断，SQLite 不校验所以单测测不出）。DDL 改 `VARCHAR(191)`，存量库由迁移
`202611190102`（`migrations/verification_hash_width.py`）`MODIFY` 加宽并复查
`CHARACTER_MAXIMUM_LENGTH >= 191`，失败即 raise 不盖章。

### D9 多码并存（J）

校验取该 key+scene 下**所有** `used=0` 行，逐行判过期；命中任一即通过，并把同 key+scene 其余
未使用行一并置 `used=1`。全部不命中时对**所有仍有效行**各 `attempts+1`、到 `MAX_ATTEMPTS`
即置 `used=1`——只废最新行会让旧码成为无限次爆破入口。

## 数据治理（P3）

### D10 `uq_active_email` 收敛为"活跃邮箱唯一"（O）

目标语义：`active_email = IF(status IN (1,3), email, NULL)`。status=1（活跃）与 3（冷静期）
仍占邮箱（冷静期保留是为了账号恢复，Issue 正文 O 的公式即如此），status=0/2/4 不再占键；
`''` 一律归一 NULL（`''` 会撞唯一键且绕过 NULL 语义）。

双后端等价实现（MySQL 无部分索引）：

- **SQLite**：`DROP INDEX uq_active_email` → `CREATE UNIQUE INDEX uq_active_email ON
  grape_accounts(email) WHERE status IN (1,3) AND email IS NOT NULL`。
- **MySQL/MariaDB**：加 STORED 生成列
  `active_email VARCHAR(255) AS (CASE WHEN status IN (1,3) THEN email ELSE NULL END) STORED`，
  在 `active_email` 上建 `UNIQUE uq_active_email`，再 DROP 旧索引。
  **不走 `mysql_compat` 的部分索引降级**（它丢弃 WHERE 建全列唯一，语义相反）。
- 新装库的 `_GRAPE_ACCOUNTS_INDEX_SQLITE` 建索引点同批改（保持新装/升级终态一致）。
- 迁移：新版本号（现有链最大 +1）注册，`kind="backfill"`，`domains=("mgmt",)`；
  先 `''`→NULL 归一再换索引；DROP/ALTER 走 `_destructive_ddl(kind_hint="cleanup")`；
  DDL 后复查索引存在性与旧索引已消失，不满足即 `raise`（不盖章，下次启动重试）。

### D11 存量邮箱语义归一（N）

`scripts/migrations/007_normalize_email_semantics.py`，默认 dry-run：

1. 非邮箱形态 / `noreply.*` → `email=NULL`、保留 `email_source`、`email_is_placeholder=1`；
2. `email IS NULL` 且 `placeholder=0` → 置 1；
3. 持有真实（可投递）邮箱但 `email_verified=0` → `email_verified=1`（真实邮箱要么是用户
   经验证码绑定、要么是 provider 断言所有权；只认 `source='user'` 会漏掉现网 62 行里的 61 行，
   等于 C2 未修——复审实测口径）；`email_source IS NULL` 的同时补 `user`；
4. `email=''` → NULL。

`--apply` 前先 `CREATE TABLE grape_accounts_bak_<utc>` 全表备份并校验行数守恒，打印改前/改后
分类计数；脚本幂等（第二次 dry-run 待改为 0）。

**执行顺序（真库演练后修正）：先跑 D11 归一，再让 D10 迁移建索引。** 归一过程先把
`noreply.*` / 非邮箱形态 / 空串清成 NULL，活跃行的重复邮箱随之消失；反过来先建唯一索引，
活跃行若有重复（两个 status IN (1,3) 同邮箱）会 1062 失败、迁移不盖章并每次启动重试刷日志。
007 脚本本身不依赖新索引，先跑零风险。**机制补充**：`202611190101` 是开机迁移，线上
无法保证"先 007 后迁移"，因此 007 在迁移后补跑同样成立——旧的全列唯一索引保证活跃行本来
不重复，迁移不会 1062；残留的 `noreply.*` 行由 007 事后清成 NULL。

### D12 验收门禁与部署纪律

- 单测：新增 4 个 Python 文件 + 1 个 node 文件（见需求 S 表映射）。时区用例统一
  `TZ=Asia/Shanghai` + `time.tzset()`，结束恢复；**必须在修复前代码上红**（S257 反证）。
- 结构守卫：`grep -rn "time.mktime(time.strptime" src/` 为 0；`uq_active_email` 不得再以
  无条件 `(email)` 形态出现；改绑链 400 必须带 `error_code`。
- 迁移门禁：`tests/api/test_issue_3469_email_index_migration.py` 双态用例（正常 + DDL 空转必抛），
  真库演练按 `grape-db-migration` 技能的 MySQL 门禁流程执行。
- 部署：热更须多台主控**同时**替换文件（md5 一致）后重启 mgmt——只改一台会造成一半请求成功
  一半失败，比全坏更难排查（Issue 约束 3）。迁移在重启时随 `init_database()` 执行，执行前先
  备份 `sessions.db`。
- O 的双机竞态：两台同时跑 `202611190101` 时，先到者 DROP+CREATE 后，后到者可能
  `Duplicate key name` → 该迁移不盖章、下次启动重试（无害）；不想看到这条 ERROR 日志就先停
  另一台。`202611190102`（列宽）同理。
- N 归一脚本**进发版清单**：先 dry-run 核对待改行数（现网实测 114 行）再 `--apply`，三库环境
  必须显式给 `DEVOPS_DB_*`（只给 `GRAPE_DSN_*` 会回落本地 SQLite）。

## 实施顺序

`A+B（热更） → C+D+F+J+P → E+G+H+I+K+L+M → Q（与 G 同批，前端联动） → O 迁移 → N 归一`
（与 Issue 正文修复顺序一致；本分支一次交付，部署仍按此序分批上线）。

## 模块落位（实现形态）

按"巨型文件只做接线、判定与 SQL 各自归位"落码：

| 模块 | 形态 | 职责 |
|------|------|------|
| `auth/verification_code.py` | 值对象 | 验证码生成、`kid:digest` 存储形态、`target` 入盐、UTC 串解析/过期判断；零 IO、零 SQL |
| `auth/verification_store.py` | 仓储 | `verification_codes` 表的**唯一** SQL 落点（插入/取活跃行/标记 used/attempts/计数/影子记录/清理） |
| `auth/verification.py` | 领域服务门面 | 限流策略 + send/verify 编排 + 结构化 `error_code`；对外 API 与既有 patch 点不变 |
| `auth/identity_email.py` | 值对象/策略 | 邮箱形态、可投递、私有中转、入库归一（单一来源） |
| `auth/email_change.py` | 领域服务 | `EmailFactor` / `EmailChangeRequest|Command|Result` / `LimitPolicy` 值对象 + `AccountPort`/`VerificationPort`/`NotificationPort` 三个契约 + `EmailChangePolicy` + `EmailChangeService`（G/P/Q 编排）；路由只留异常→HTTP 映射 |
| `mgmt/migrations/active_email_uniqueness.py` | 迁移对象 | `ActiveEmailUniquenessIndex`（双后端等价实现 + 复查）+ `ActiveEmailUniquenessMigration`（owner 守卫 `_table_present`/`_is_base_table` → `_NonOwnerObject`）；`db_setup.py` 只保留 `register_migration(...)` 一行接线 |

三库四进程口径：本轮触及的 `grape_accounts` / `verification_codes` / `login_failures` 在
`configs/table_ownership.json` 全为 `mgmt`，实现里没有外域表 SQL、没有跨域 import；跨层只走
端口协议（`*Port`），迁移在非 owner 库抛 `_NonOwnerObject` 跳过且不盖章。

已知残留（记入 PR 说明）：`active_email` 是 MySQL 侧的 STORED 生成列，若
`reconcile.schema_drift` 将来在 MySQL 上跑，会报"DB 有模型无"的漂移；SQLite 单测不受影响。

## 影响面

- 7 个邮箱验证码场景（注册/找回密码/设密码/改绑/注销/解锁/OAuth 关联）全部恢复。
- 登录失败锁定（账号 + IP 双维度）恢复生效——上线后暴力破解会真正被 429。
- 27 个 `noreply.*` + 1 个字面串账号的设密码/解锁/注销路径从"结构性不可用"变为"引导先绑定"。
- OAuth 新建账号默认形态（占位 + 私有邮箱）不再在解绑时误删用户自绑邮箱，也不会占用他人邮箱。
- 唯一索引语义变化对 grape-admin 的影响：它裸 SQL 读写同库，`status=2/4` 行原本"占键但不可见"，
  改后释放——只会减少 1062，不影响其读写路径。
## D13 站内信降级：从「调度」改成「调度或同步入队」

`runtime.public.schedule_notify` 原样返回 `None`，同步路由（threadpool）里
`asyncio.get_running_loop()` 必失败 → 只打一行 `no running loop, skip`，调用方
（`email_change` 的通知适配器）却 `return True`——这就是「降级通道等于没生效」。

改法（契约不变，语义加严）：
- `schedule_notify(user_id, kind, text) -> bool`：有循环 → `create_task`；无循环 →
  `enqueue_user_notify()` 同步把通知写进 IM outbox（与 `notify_user` 共用
  `active_targets/p2p_chat_id/_make_payload/out_track_id` 口径，投递交给 outbox 消费进程）。
- `user_id` 归一收敛到 `kernel.user_ids.norm_uid`（`notify` 与 `outbox` 共用）：纯数字转 int
  （**数字 id 的 sha1 输入不变**，历史 pending 行不会被当成新事件重发），UUID 原样保留。
  这是评审 P1 修复的第一版只改了 `notify` 侧、漏了 `outbox.out_track_id` 的补漏（缺陷 #3510：
  出单号处裸 `int()` → ValueError → 入队失败，站内信对 UUID 账号仍然送不到）。（D105 后 `grape_accounts.id` 是 TEXT UUID，
  原 `int(user_id)` 必然 ValueError 且被上层 `except` 吞掉）。
- 返回假值即"没人会被通知"，调用方据此记 warn，不得再对外宣称成功。

## D14 账号变更的三个判定面各自要有闸

| 面 | 原状 | 现口径 |
|----|------|--------|
| 新邮箱 | 只校验格式 | `is_valid` + `is_deliverable`（400 `EMAIL_UNDELIVERABLE`）——否则会把「有 email 收不到信」形态与中转地址唯一键再造回来 |
| 当前密码（第二因子） | 无节流 | `login_throttle` 键 `change_email_pwd:<账号 id>`：每错一次 `record_failure`，成功 `clear_failures`，超限 429 |
| IP | 一次改绑写 2–3 行都计入，5 次/10min 被放大 | 计数排除 `change_email` 的 target 影子行；超限单独 `IP_LIMITED`，不冒充目标邮箱限流 |

## D15 本轮 P3 处置

- `clear_placeholder_email` 清空时 `email_verified=0`（D3 口径，007 不覆盖该形态）。
- 删除死常量 `_GRAPE_ACCOUNTS_INDEX_SQLITE`，新装索引只走 `ensure_active_email_index()`。
- 007 横幅按后端打印真实目标（`mysql host:port/db`），MySQL 下不再显示 `<session default>`。
- `test_issue_3469_routes.py` 的 `client` 改 session 级：222s → 24s（每个用例重建 TestClient 会
  触发 openapi 四进程 peer 渲染）。
- 部署单仍需写清：MySQL 上 `DROP INDEX → CREATE INDEX` 之间是自动提交窗口（205 行表，可接受）。

## D16 限流拒绝的结构化错误码与契约副本同步

- `verification._rate_limit_rejection` 的三种拒绝各自带码：60s 间隔/窗口次数 → `RATE_LIMITED`
  + `retry_after`；IP 维度 → `IP_LIMITED`。此前 60s 间隔只给 message，调用方
  `error_code or "CODE_SEND_FAILED"` 会兜成"发信失败"，与真正发信故障混淆（复审小瑕疵）。
- `EmailChangeError` 增 `retry_after`，路由 detail 带出，前端可倒计时。
- 契约副本（`help/api/openapi-baseline.json`、`shell/openapi.json`）的 `ChangeEmailBody`
  必须与实现模型同字段：二次因子 `password`/`old_email_code` 不在 schema 里，消费方按契约
  生成的客户端面对 `400 NEED_SECOND_FACTOR` 无从补参。既有两个守卫（端点完整性、只拦收缩）
  都抓不到请求字段漂移，新增 `tests/api/test_openapi_request_schema_drift.py` 补盲区。

## D17 占键状态集合与「禁用账号」的歧义消除

`status`：0=管理员禁用、1=活跃、2=已注销、3=冷静期、4=merged。

- 原口径 `active_email = IF(status IN (1,3), email, NULL)` 把 **0** 漏在外面：
  禁用行仍留着 email 却不占键 → 他人可拿同邮箱注册成功，读侧
  `WHERE email=? LIMIT 1` 命中两行时可能返回"已禁用"那行，登录/找回落到错误账号。
  现口径 **`status IN (0,1,3)`**：禁用也占键（管理员的禁用是可逆的，邮箱归属不该被别人抢走）；
  2/4 仍是释放态（email 置 NULL）。
- 读侧同时加确定性排序（`CASE WHEN status IN (1,3) THEN 0 ELSE 1 END, updated_at DESC`），
  历史脏数据也不会撞运气取行。
- 口径升级对已上线库是**重建索引**：迁移比较 `active_email` 生成列表达式，表达式不符即
  `DROP INDEX` + `DROP COLUMN` + 重建；受 `_destructive_ddl` 的 cleanup 门槛（`meta.min_code_version`）约束，
  门槛不过则 raise、不盖章、下次启动重试 —— **上线前必须确认现网 meta 门槛值已放行到本版本**，
  否则索引与列宽会一直停在旧形态（复审 P2）。
- 交付形态：本分支把 #3469（邮箱链路）与 #3510（IM 通知 UUID）放在同一提交，
  回滚会连带回滚通知修复；若要求可独立回滚，应拆两条 MR（复审 P2，未按要求实施，记入发版说明）。

## D18 三库启动的后端开关与限流错误码透传

- **启动器**：`GRAPE_3DB_ENV` 分支从 DSN 拆出 host/port/user/password/name，但**后端开关
  `DEVOPS_DB_BACKEND` 不在 DSN 里**。缺这一行时 `session_manager` 的判定退回 sqlite，
  四个进程各自落到 `data/sessions.db`，而 `[db-guard]` 照旧打印 MySQL DSN —— 看起来连上了，
  实际写的是本地文件（本机实测：账号/验证码写在 sqlite，MySQL 侧全空）。已在
  `scripts/grape-systemd-run.sh` 的三库分支显式 `export DEVOPS_DB_BACKEND=mysql`。
- **限流错误码**：`send_code` / `check_rate_limit` 已带 `error_code`/`retry_after`，但路由把
  失败映射成 `{"code":429,"message":…}`，错误码在边界被丢掉。现统一走 `_rate_limit_detail()`，
  注册/找回/设密码/注销/解锁五处发码口一致透出。
- **站内信前置**：`im_notify_outbox` 只在 IM 通道初始化时建表；未开 IM 的部署里降级通道
  `schedule_notify` 会返回 False（有 warn，不崩）。上线若依赖 F 的降级，需确认该表存在。
