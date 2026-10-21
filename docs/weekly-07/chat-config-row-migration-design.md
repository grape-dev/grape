# chat_agent_session 配置行迁移设计（第三步：结构性合并）

> 状态：**方案（未实施）**　｜　前置：诊断脚本 + 基线报告 `chat-config-duplicate-rows-audit.md`（§9 为最新工作表）
> 目标库：云端库；本地库同理，量小可作演练

---

## 0. 目标与不变量

**做**：把同一"人"的多条全局 Chat 配置行**合并为一条**（id 规范到现网写路径 id），归一 `repo_key`，
加唯一约束防复发。

**不做 / 不变量**（迁移后必须逐人核对）：

| 不变量 | 含义 | 验收方式 |
|---|---|---|
| 生效配置不变 | 除**明确决定**的例外（下表 6 人），每个用户的"当前生效引擎/技能/IM 绑定"与迁移前一致 | 迁移前后各跑一次 `_pick_chat_config_row`，逐人比对 |
| 不动 repo 级会话行 | `repo_key<>''` 的行（会话行）完全不碰 | `WHERE repo_key='' OR repo_key IS NULL` 限定 |
| 不丢未列出的列 | 合并写用 `REPLACE`/`INSERT..ON DUPLICATE` 时必须**列全需要保留的列**（`runtime_session_id`/`status`/`event_history`/`skill*`/`im_*`） | 迁移后逐列 diff |
| 可回滚 | 每一步都有备份与反向脚本 | 见 §8 |

---

## 1. 三条硬约束（来自 §9.4）

1. **身份必须经 `unified_user_mapping` 解析**（`gitcode_id → user_accounts.id`），**禁止用算术偏移**（+36 只是本次重构的产物）。
2. **目标 id 取"现网写路径 id"**：`id = 'sess-cfg-' + <现网对该人使用的 user_id>`。
3. **跨空间行优先处理**：25 行的 id 落在另一个 id 空间的同名位置上（例：`sess-cfg-37` 的 `user_id=1`），
   若新空间那位今后保存配置，`REPLACE INTO` 会**直接覆盖**这行 → 必须先处置。

---

## 2. 阶段 0（先做，否则后面全是猜）：把 id 空间口径**实证**定下来

现状（已核实）：

```
unified_user_mapping  106 行，user_id 1..106  —— 身份权威（gitcode_user/feishu_name/app_id/gitcode_id）
user_accounts          94 行，id    37..131   —— 账号表，与 mapping **数值重叠 69 个 id**
两个空间同一人：mapping[X].gitcode_id == user_accounts[X+36].gitcode_id（1<->37 已验证；逐条可验）
配置行 user_id 与 mapping 同编号：12 行有 im_app_id，12/12 命中 mapping[user_id]，0 命中 mapping[user_id-36]
实证：'sess-cfg-1'（user_id=1）今天 11:43 被**重新创建**（created_at==last_active_at，引擎 13030=opencode）
      → 现网写路径当前用的就是 `sess-cfg-{user_id}` 且该 user_id = 1 → 与 user_accounts.id=37 不是同一个号
```

**结论**：现网 `user_id` 走的是 **mapping 空间**（至少对 user 1 成立），而 `user_accounts.id` 是另一套号。
迁移前必须把这个口径**钉死**，做法（只读 + 一次可控写）：

1. 找一个测试账号，在云端 UI 保存一次 chat 配置；
2. 观察被写行：若出现/更新的是 `sess-cfg-{mapping_id}` → 口径 = mapping 空间；若是 `sess-cfg-{account_id}` → 口径 = 账号空间；
3. 把结论写进本文件（下表填空）与配置行审计报告。

**结论（2026-10-18 已定）**：

| 口径项 | 结论 |
|---|---|
| 现网写路径 user_id 空间 | ✅ **unified_user_mapping 空间（1..106）** |
| 依据 1 | 本人 11:43 在云端保存 chat 配置 → 被写行为 `sess-cfg-1`（**不是** `sess-cfg-37`） |
| 依据 2 | 最近 14 次配置写入**全部**满足 `id == 'sess-cfg-' + user_id`，且这些 user_id 与 `user_accounts.id` 不同名（user 66 vs 账号 103、83 vs 120、19 vs 55…） |
| 依据 3 | 姊妹表同口径：`chat_conversations` / `agent_tasks` 的 user_id 也在 1..106（user 1 今天 11:43 仍有写入） |

> ⇒ **目标 id = `'sess-cfg-' + 该人的现网 user_id`（mapping 空间）**；`user_accounts`（37..131）**只用于认人**，不参与 id 推导。

---

## 2.5 阶段 0.5（新增，必须先做）：归属冲突行的**人工裁决**

反查 `im_app_id`（飞书 app 是登录凭证，只有本人能注册 → 唯一权威的"app→人"依据）发现 **4 行的 app/密钥属于别人**：

| 行 | 行内 user_id → 该 id 对应的人 | 行内 app 真正属于 | 密钥 | 两侧活跃度（会话/任务） |
|---|---|---|---|---|
| sess-cfg-112 | 3 → user-3 | mapping 39/75 | 32 位 | 154/954 **vs** 1/5 |
| sess-cfg-78 | 6 → user-6 | mapping 42 | 32 位 | 88/478 **vs** —/2 |
| sess-cfg-119 | 10 → user-10 | mapping 46 | 32 位 | 10/67 **vs** —/0 |
| sess-cfg-129 | 20 → user-20 | mapping 56/92 | 32 位 | 28/231 **vs** 3/14 |

两种解释都无法用数据排除（"app 字段被写错" vs "user_id 被写错"），**必须人工逐行裁定**：
候选人在各自界面上自查"我现在用的飞书 bot 是不是我自己的"即可判定。**脚本禁止自行猜测**。

**接口**：迁移脚本读一个决策文件（CSV，人工填）：

```
row_id,action,keep_owner,note
sess-cfg-112,reassign,<裁定后的 owner user_id>,<备注>
sess-cfg-78,delete,,本人确认不再使用
...
```

`row_id` 出现在冲突清单里而决策文件中**没有对应条目** → 脚本跳过该行并在报告里告警（默认不动）。

**处置状态（2026-10-18 已完成）**：这 4 行的 `im_app_id/im_app_secret` 已置空（`user_id/runtime_id/skill_id` 未动），
4 个 app 已不在任何配置行中（账号表也无副本）→ 相关方均需重新绑自己的飞书 app；
**归属之争仍未裁决**（本次只清凭据），合并前仍需按上面的决策文件逐行裁定。

---

## 3. 阶段 1：备份（必须先跑，且可重复）

```sql
-- 1) 整表快照（结构与数据都留）
CREATE TABLE bak_chat_agent_session_20261018 LIKE chat_agent_session;
INSERT INTO bak_chat_agent_session_20261018 SELECT * FROM chat_agent_session;
-- 2) 只留配置行的轻量快照（便于逐人比对；含"迁移前生效行"标记）
CREATE TABLE bak_chat_cfg_rows_20261018 AS
SELECT c.*, (SELECT MIN(c2.id) FROM chat_agent_session c2
              WHERE c2.user_id=c.user_id AND (c2.repo_key='' OR c2.repo_key IS NULL)) AS pre_read_row
  FROM chat_agent_session c WHERE c.repo_key='' OR c.repo_key IS NULL;
-- 3) 对账：行数必须相等
SELECT (SELECT COUNT(*) FROM chat_agent_session) AS 原表,
       (SELECT COUNT(*) FROM bak_chat_agent_session_20261018) AS 快照;
SELECT (SELECT COUNT(*) FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL) AS 配置行,
       (SELECT COUNT(*) FROM bak_chat_cfg_rows_20261018) AS 快照;
```

---

## 4. 阶段 2：dry-run 报告（默认模式，不写任何数据）

脚本逐个用户输出决策表（CSV + markdown），并**给出建议**：

| 列 | 说明 |
|---|---|
| user_id / 身份 | 经 `unified_user_mapping` 解析（gitcode_user / 飞书名 / app_id）+ 账号表 id |
| 各行 | id · 引擎（runtime_id → name/是否存在）· last_active_at · skill/skill_id · im_app_id |
| 现网读到 | 调**真实函数** `_pick_chat_config_row`（与生产同一份逻辑） |
| 旧口径读到 | `MIN(id)`（历史行为，仅参考） |
| 建议保留 | 规则见 §5.1；标注"自动/需人工" |
| 置信度 | `im_app_id` 能与 mapping 对上 → 高；否则中/低（见 §5.3） |

**"需人工判定"的清单（按 2026-10-18 数据：6 人，引擎跨行冲突）**

| user_id | 身份 | 各行引擎 | 现在读到 | 建议 |
|---|---|---|---|---|
| 1 | user-1（本人） | `sess-cfg-1`=opencode(13030) · `sess-cfg-37`=hermes(1202, 8-31 僵尸) | opencode | 保留 `sess-cfg-1`（今天刚用） |
| 3 | user-3 | `112`=opencode · `39`=opencode · `75`=hermes(9-15) | opencode | 保留 `112`（最近活跃） |
| 6 | user-6 | `115`=hermes · `42`=hermes · `78`=opencode(9-17) | hermes | 需人工：hermes 最近用，但 opencode 是他 9-17 主动换的 |
| 16 | user-16 | `125`=hermes · `52`=**幽灵引擎(6102)** · `88`=hermes | hermes | 保留 `125`；`52` 直接丢弃（引擎已删） |
| 19 | user-19 | `128`=hermes · `19`=**幽灵引擎(13388)** | hermes | 保留 `128`；`19` 丢弃 |
| 33 | user-33 | `106`=hermes(10-01) · `69`=opencode(10-11) | opencode | 保留 `69`（更近） |

**引擎相同、可自动合并的 11 人**：4、7、10、12、14、20、23、28、29、38、54（保留"现在读到"那条，其余清理）。

---

## 5. 阶段 3：合并

### 5.1 保留规则（默认，可被人工清单覆盖）

1. 若现网 canonical 行（`id = 'sess-cfg-{目标 user_id}'`）**存在且引擎可用** → 保留它（写读同向）；
2. 否则保留**现在读到的那条**（= `_pick_chat_config_row` 的返回，也就是用户当前实际在被服务的配置）；
3. 若"现在读到"的引擎是幽灵 → 取最近活跃的**引擎可用**行（例：user 19 保留 `128`）；
4. 其余行：删除（但先入备份表；见 §8 回滚）。

### 5.2 合并动作（对每个人，幂等）

```sql
-- 伪代码：src 为该人"被保留的行"，dst = 'sess-cfg-' + 目标 user_id
-- 1) 把保留行的**全部业务列**写到目标 id（列全，避免 REPLACE 抹掉未列出的列）
REPLACE INTO chat_agent_session
  (id, user_id, repo_key, runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider,
   runtime, runtime_session_id, status, event_history, source, repository, created_at, last_active_at)
SELECT ?, user_id, '', runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider,
       runtime, runtime_session_id, status, event_history, source, repository, created_at, last_active_at
  FROM chat_agent_session WHERE id = ?;
-- 2) 删除该人的其他配置行（保留目标 id 那条）
DELETE FROM chat_agent_session
 WHERE (repo_key='' OR repo_key IS NULL) AND user_id = ?
   AND id <> ?;
```

约束：一次事务内按用户处理；每处理 1 人打印一行对账（读到的行、写到的行、删除的行数）；
`runtime_session_id` 若在被删行上非空、而目标行为空 → 需人工确认（会话续接指针，见风险 R4）。

### 5.3 置信度分级

| 级别 | 条件 | 处理 |
|---|---|---|
| 高 | 该人某行的 `im_app_id` == `unified_user_mapping[user_id].app_id` | 自动执行 |
| 中 | `im_app_id` 为空，但 id/user_id 关系自洽（`id == 'sess-cfg-{user_id}'` 或跨空间偏移成立） | 自动执行，日志标注 |
| 低 | `im_app_id` 非空但**对不上 mapping**（用户换过 app，如 user 3/6/10/20）或行 id 与 user_id 双双异常 | **人工确认后再执行** |

---

## 6. 阶段 4：归一 `repo_key`

```sql
UPDATE chat_agent_session SET repo_key='' WHERE repo_key IS NULL;   -- 当前实测 0 行，保留步骤
```
（MySQL 的 UNIQUE 允许多个 NULL，归一后才能保证唯一约束真正生效。）

---

## 7. 阶段 5：加唯一约束（防复发）

```sql
-- 前置校验：必须为 0
SELECT user_id, COUNT(*) c FROM chat_agent_session
 WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id HAVING c>1;
-- 建索引（repo_key 是 TEXT，MySQL 必须给前缀长度）
ALTER TABLE chat_agent_session ADD UNIQUE KEY uk_chat_cfg_user (user_id, repo_key(191));
```

- 若报 `Duplicate entry` → 说明合并还漏了人，回阶段 2 重跑 dry-run；
- 若要更严格（同时约束 repo 级会话行），可改为生成列 + 唯一索引（`repo_key=''` 时才参与），本次**不做**（会话行 id 由另一套逻辑生成，风险高）；
- 落地后回归：`get_chat_config` 全量走一遍（见 §8 验收），并跑 `tests/test_chat_config_row_selection.py`。

---

## 8. 验收与回滚

**验收（迁移后立即跑，全部必须通过）**

```sql
-- 1) 配置行 1 人 1 行
SELECT COUNT(*) FROM (SELECT user_id FROM chat_agent_session
  WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id HAVING COUNT(*)>1) t;          -- 期望 0
-- 2) 无孤儿（每个配置行的 user_id 在 mapping 或账号表里能认到人）
SELECT COUNT(*) FROM chat_agent_session c WHERE (c.repo_key='' OR c.repo_key IS NULL)
  AND NOT EXISTS (SELECT 1 FROM unified_user_mapping m WHERE m.user_id=c.user_id);        -- 期望 0
-- 3) 幽灵引擎行归零
SELECT COUNT(*) FROM chat_agent_session c WHERE (c.repo_key='' OR c.repo_key IS NULL)
  AND c.runtime_id>0 AND NOT EXISTS (SELECT 1 FROM agent_runtimes ar WHERE ar.id=c.runtime_id);  -- 期望 0
-- 4) 行数对账
SELECT (SELECT COUNT(*) FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL) AS 现值,
       (SELECT COUNT(DISTINCT user_id) FROM bak_chat_cfg_rows_20261018) AS 迁移前人数;
```

**逐人比对（脚本 `--verify`）**：对迁移前的每个 user_id，分别计算"迁移前 `_pick_chat_config_row`"与
"迁移后 `_pick_chat_config_row`"的 `(runtime_id, skill, skill_id, im_app_id)`；输出差异清单，
**除 §4 人工清单里明确决定的人以外，差异必须为 0**。

**回滚**：

```sql
-- 单步回滚（清空并回灌快照；会话行不受影响）
DELETE FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL;
INSERT INTO chat_agent_session SELECT * FROM bak_chat_agent_session_20261018
 WHERE repo_key='' OR repo_key IS NULL;
-- 唯一索引若已建，先 DROP INDEX uk_chat_cfg_user
```

---

## 9. 风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | **翻转用户生效引擎** | §4 人工清单只在明确同意后执行；`--verify` 逐人比对，差异非零即回滚 |
| R2 | **跨空间行被覆盖**（约束 3） | 阶段 0 定口径后，逐行标注"属于谁的哪个空间"；对 25 行跨空间行先处置（合并进目标 id 或明确删除） |
| R3 | 身份认错（id 空间重叠 69 个 id） | 一律经 `unified_user_mapping` + `im_app_id` 交叉验证；置信度"低"的行人工过一遍 |
| R4 | 合并时丢 `runtime_session_id`（引擎会话续接指针） | 合并写列全；被删行该列非空而目标行为空时**停下报人工** |
| R5 | `REPLACE INTO` 抹掉未列出的列 | 所有迁移写操作显式列全列（§5.2），迁移后逐列 diff |
| R6 | 唯一索引建不上（重复未清干净 / TEXT 需前缀） | 阶段 2 前置校验 + `repo_key(191)` 前缀 |
| R7 | 迁移期间用户正在写配置 | 选低峰窗口；迁移前记录 `last_active_at`，完成后复查有无并发写入 |
| R8 | 本地与服务共用同库（如本机 dev） | 生产迁移只在云端库执行；本地库先演练一遍 |
| R9 | 无新账号的 若干用户（47/54/55/66/80/87/97/103）身份只在 mapping 里 | 口径已定（mapping 空间），照常按 user_id 合并即可；认人时标注"账号表无对应行" |
| R10 | **误删/覆盖他人配置**（归属冲突行，见 §2.5） | 决策文件缺条目即跳过；脚本禁止猜测；`--verify` 比对每行归属 |
| R11 | **他人飞书 app 密钥被错误配置行持有**（敏感数据） | 先人工确认（§2.5），必要时先置空 `im_app_*` 再合并；不要在报告/日志里回显密钥明文 |

---

## 10. 脚本接口设计（`scripts/diag/chat_config_rows_migrate.py`）

```
默认            --dry-run：只输出决策表与建议（CSV + markdown），零写入
--apply        执行阶段 1→5（先备份，再逐步），每步打印对账；任一步校验失败即中止
--phase 1..5   只跑某一阶段（用于分步演练/恢复）
--verify       迁移后逐人比对（迁移前快照 vs 现状）
--rollback     用备份表回灌配置行（需二次确认）
--report PATH  输出决策表路径
```

硬性要求：**默认 dry-run**；`--apply` 前打印"M 人将合并、N 人需人工（列出）"并要求显式确认；
所有写操作走单事务；每步都要有可打印的校验 SQL。

---

## 11. 待拍板（做之前需要你定的 5 件事）

1. ~~口径~~ ✅ 已定（mapping 空间，见 §2）；**新增**：§2.5 的 4 行归属冲突怎么裁（需当事人自查确认）
2. **6 人人工清单**：user 1/3/6/16/19/33 各保留哪条？（尤其 user 6：hermes 最近用 vs opencode 是他主动换的）
3. **僵尸行处置**：删除 or 只标记（如加 `merged_into` 注释行）？建议直接删除 + 备份表留档。
4. **唯一约束范围**：只约束配置行（推荐）还是同时约束 repo 级会话行（风险高，不建议）。
5. **窗口与授权**：何时执行、谁在旁确认；是否先在本机 `grape_test_db` 全流程演练一遍（推荐先演练）。

---

## 12. 「直接删历史行」的影响评估（2026-10-18，用户提问后补）

用户提问："迁移太费劲，这些历史行直接删掉有影响吗"。实测数据（云端）如下 —— **结论：影响很小，但有一类不能删**。

### 12.1 三类行与删除后果

| 类别 | 行/用户数 | 删掉的后果 |
|---|---|---|
| A. canonical 已存在且被读到，其余是僵尸行 | 若干用户 → 多余行仅 **3 行** | 功能无影响（读路径本来就取 canonical）；只丢该行独有内容（见 12.2） |
| B. canonical 缺失，配置**只**在老行上 | **若干用户**（单行 19 / 多行 14） | 删后走"无配置→物化默认"：引擎变成系统默认（`resolve_local_engine()` = min 本机直连 = **1202 hermes**）、技能清空、IM 绑定丢、系统会自动补一条 canonical 行（自愈） |
| C. 归属未裁决（4 行飞书冲突 + 25 行跨空间/错位） | 29 行 | **不能自行删**：删 = 替别人做决定（行的 user_id 与 app_id/行名证据冲突） |

### 12.2 删除会丢什么（实测）

```
引擎会变（当前读到 ≠ 默认 1202）的用户：仅 2 个  →  user 3、user 33（都是 opencode→hermes）
会丢技能的行：3 行（sess-cfg-106/user33 skill=chat/2598、sess-cfg-39/user3 skill=chat/2598、sess-cfg-69/user33 skill=im-notify/2597）
会丢 IM 绑定的行：7 行（sess-cfg-37/1、40/4、41/5、51/15、69/33、73/37、98/26）
        —— 这 7 行的 app 经 im_app_id 反查都是**本人自己的** app（不是别人的）→ 删掉后本人重绑即可
会丢会话续接指针的行：**0 行**（非 canonical 行的 runtime_session_id 全为空）
```

**特例（本人）**：user 1 的飞书绑定只挂在僵尸行 `sess-cfg-37` 上（canonical 行 `sess-cfg-1` 的
`im_app_id` 是空的 —— 11:43 那次保存把它写空了；IM 侧按"所有非空 app_id"加载，所以现在还能用）。
→ 若要走"删"，应**先把该行的 app_id/secret 合并进 canonical 行**，否则本人得重绑。

### 12.3 两条更省事的路（替代"逐人决策式迁移"）

1. **rename 代替搬数据**（推荐，脚本量砍一半）：
   - 单行用户（19 个）：一条 `UPDATE chat_agent_session SET id='sess-cfg-{user_id}' WHERE id='<老行>'` 即完成"合并"，**零数据搬移**；
   - 多行用户（14 个）：先按 §5.1 选定保留行，再 rename 该行、删其余；
   - A 类用户（30 个）：只删多余 3 行；
   - C 类（29 行）：跳过，等裁定（仍走 §2.5 决策文件）。
2. **什么都不做**：读路径已修好（写读同向），唯一代价是"别人保存配置时 `REPLACE` 可能顶掉一行历史行"
   （被顶掉的是僵尸/错位行，影响有限但不可控）。可只保留一个只读巡检。

### 12.4 建议顺序

```
① 先删 A 类 3 行僵尸（零功能影响）——可选
② B 类 19 个单行用户做 rename（一步到位、无需决策表）
③ B 类 14 个多行用户 + C 类 29 行：等裁决（§2.5 决策文件）
④ 最后（可选）加唯一约束
```

---

## 13. 预演结果与执行顺序（2026-10-18，rename 版脚本）

脚本：`scripts/diag/chat_config_rows_migrate.py`（默认 dry-run）
预演输出存档：`docs/weekly-07/chat-config-migration-plan-20261018.txt`

```
待执行 若干用户 ｜ 跳过（待裁决）若干用户

[A]  user 1   保留 sess-cfg-1（opencode），删 sess-cfg-37 —— **先把后者的 im_app_id/secret/im_provider 并进 canonical**（保住本人飞书绑定）
[A]  user 38  保留 sess-cfg-38（hermes），删 sess-cfg-111 —— 先并入其 runtime=opencode
[B1] 若干用户：5,8,9,11,13,15,17,21,22,24,26,27,31,32,34,36 —— 老行改名到 sess-cfg-{user_id}（零数据搬移）

跳过：
  · 多行且 canonical 缺失（14 个）：3,4,6,7,10,12,14,16,20,23,28,29,33,54
  · canonical 名字被**别人的行**占着（跨空间）或 canonical 引擎不可用（4 个）：19,37,53,58
```

### 13.1 ⚠️ 预演中发现并已修复的回归（重要）

第二步"按 id 取 canonical"在云端会**命中别人的行**：`sess-cfg-37` 的 id 属于 user 37 的命名，
但该行的 `user_id` 是 1（你）→ 只按 id 取会让 user 37 读到**你**的配置（含 app_id/secret）。
实测证据：user 37 的"读到"是 `sess-cfg-37`（你的行），而不是他自己的 `sess-cfg-73`。

**修复**：canonical 查询改为**同时校验 id 与 user_id**
（`WHERE id=? AND user_id=? AND (repo_key='' OR repo_key IS NULL)`），
并加防回归用例 `test_cross_space_row_belonging_to_other_user_is_not_picked`
（已验证：退回旧逻辑该用例失败并打印"拿到了别人的 app_id"；修复后全过）。
本地无跨空间行，所以只有真机预演能暴露它 —— 这也是"预演必须先跑"的实例。

### 13.2 执行顺序（A 类先做，能顺带解开 4 个被占用用户）

```
① [A] user 1 / user 38：合并字段 + 删僵尸行
② [B1] 16 个单行用户改名
   —— ①② 完成后，'sess-cfg-37' 已被删除 → 原本"名字被占"的 user 37 即可进入 B1；
      user 19/53/58 同理需先清掉占名的那一行（19 的占名行是它自己的死引擎行，可直接处置）
③ 多行且 canonical 缺失的 若干用户 + 4 个占名用户：按 §2.5/§5.1 决策后再处理（决策文件）
④ 最后（可选）加唯一约束
```

---

## 14. 迁移执行日志（2026-10-18，rename 版，已执行）

工具：`scripts/diag/chat_config_rows_migrate.py`（--apply --yes；每轮先建整表备份）

```
第 1 轮  bak_chat_cfg_migrate_20261018_130827（100 行）
  [A]  user 1   合并 sess-cfg-37 的 im_app_id/secret/im_provider → sess-cfg-1，删除 sess-cfg-37
  [A]  user 38  合并 sess-cfg-111 的 runtime → sess-cfg-38，删除 sess-cfg-111
  [B1] user 5,8,9,11,13,15,17,21,22,24,26,27,31,32,34,36  共 16 个 → 老行改名到 sess-cfg-{user_id}
第 2 轮  bak_chat_cfg_migrate_20261018_130902（98 行）—— 第 1 轮删除"释放"了被占用的名字
  [B1] user 37 sess-cfg-73→sess-cfg-37 ｜ user 53 sess-cfg-89→sess-cfg-53 ｜ user 58 sess-cfg-131→sess-cfg-58
第 3 轮预演：待执行 0 ｜ 跳过（待裁决）15
```

### 14.1 前后指标（云端库）

| 指标 | 迁移前 | 迁移后 |
|---|---|---|
| 配置行总数 / 表内总行数 | 86 / 100 | **84 / 98**（删掉 2 行僵尸） |
| ≥2 条配置行的用户 | 17 | **15**（只剩待裁决的那批） |
| 跨行引擎冲突用户 | 6 | **5**（user 1 已解） |
| 跨行 im_app_id 冲突用户 | 8 | **2**（4 行已置空 + 迁移合并） |
| 配置行 id 不合规 | 56 | **35** |
| 旧口径写读错位用户 | 35 | **15** |

### 14.2 校验（独立于脚本，用备份表逐用户比对）

```
18 个第一轮用户：AFTER 每个用户恰好 1 行、id 规范；其 (runtime_id, skill_id) 与改前某一行一致；
  绑定不丢且更完整：user 1 的 app 从被删的 sess-cfg-37 并回 canonical；5/15/26/38 的 app 原样保留
user 37/53/58：改名后 id 规范，内容未动
服务侧：读路径未改（云端跑的还是它自己的代码）→ 用户感知无变化；迁移后每人 1 行 → 旧读法也不再依赖扫描顺序
```

### 14.3 遗留（等人工裁决）

```
若干用户：3,4,6,7,10,12,14,16,19,20,23,28,29,33,54
  · 14 个是"多行且 canonical 缺失" → 需要定"保留哪条"（报告 §4 的 6 人清单是其中的引擎冲突子集）
  · user 19 特殊：canonical 行 sess-cfg-19 指向已删引擎，被读的是 sess-cfg-128
    （处置：删/清 sess-cfg-19 后把 sess-cfg-128 改名为 sess-cfg-19）
```

### 14.4 回滚

```sql
-- 单轮回滚（以第 1 轮为例；配置行整段替换，repo 级会话行不受影响）
DELETE FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL;
INSERT INTO chat_agent_session SELECT * FROM bak_chat_cfg_migrate_20261018_130827
 WHERE repo_key='' OR repo_key IS NULL;
-- 两轮都回滚：先回第 2 轮备份，再回第 1 轮备份
```

### 14.5 追加执行（D 类，2026-10-18 第 3 轮）

```
bak_chat_cfg_migrate_20261018_131340（98 行）
  [D] user 19：删死 canonical sess-cfg-19（runtime 13388 已不存在）→ 把在读的 sess-cfg-128 改名成 sess-cfg-19
校验：user 19 现在 1 行（sess-cfg-19 / runtime 1202 hermes / last_active 2026-10-14，内容取自保留行）；
      脚本自校验 **canonical 异常用户 = 0**；配置行总数 83；仍多行用户 14（待人工裁决）
```

规则（脚本 D 类，严格校验后才动）：canonical 行存在但**引擎不可用** + 该用户只有另一行且它在被读
→ 先并独有非空字段 → 删死行 → 把在读行改名成 canonical。
校验条件：死行 user_id 必须等于本人（防删别人的跨空间同名行）、目标行必须存在且引擎可用、仅 2 行时适用。
本地已用合成场景（user 998）验证分类产出，随后清理合成行。

---

## 15. 「剩下 14 个先不动」的影响评估（2026-10-18）

### 15.1 关键结论：真正的解药是**把第二步修复推上线**，不是合并这些行

| 前提 | 影响 |
|---|---|
| **云端现状（旧读法）** | 14 个里 **11 个"改配置不生效"仍然存在**（3,4,6,7,14,16,20,28,29,33,54：写 `sess-cfg-{uid}` 但旧读法取字符串序最小的老行）；另 3 个（10,12,23）下次保存会生效、并留僵尸行 |
| **第二步修复上线后** | 上述"改了不生效"消失（读法确定性 + 写读同向）；用户**每次保存配置都会自动写出 canonical 行**并成为被读行 → 数据**随使用自愈**，可以不做迁移 |
| 僵尸行本身 | 无害：读路径不依赖扫描顺序、不依赖行数；只影响运维观感（诊断脚本会持续报"14 个多行用户"） |

### 15.2 跨空间占用名字的风险：实测可忽略

34 行占着"别人的 canonical 名字"（如 `sess-cfg-112` 属 user 3，名字属于 112）。风险是"名字主人保存配置时
`REPLACE INTO` 顶掉这一行"。实测：**被占名字的主人 X 几乎全是 0 会话 0 任务**（唯一例外 `sess-cfg-48`：
X=48 有 3 会话，而 48 与 12 是同一个人的两个 id 空间）→ 实际不会有人去写这些名字 ✓。

### 15.3 结论与建议

```
可以"先不动"，条件是：把第二步读路径修复推上线（消除 若干用户的"改了不生效"）
不需要为这 若干用户做迁移：修复上线后，他们一保存配置就自愈成规范形态
剩下的 14 个多行用户 = 纯整洁性问题，等"保留哪条"的讨论结果再收尾即可
```
