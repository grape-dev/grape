# chat_agent_session 配置行重复/错位 —— 只读诊断与迁移前基线报告

日期：2026-10-17　｜　范围：**只读诊断**（未修改任何数据）　｜　脚本：`scripts/diag/chat_config_rows_audit.py`

被测库：云端库、本地库。

---

## 0. 结论速览

**是个真 bug，机制已钉死**：`chat_agent_session` 里同一用户可能有多条配置行，
**写入只落 `sess-cfg-{user_id}`，读取却是"按 user_id 取 LIMIT 1 且不指定顺序"**，
两者在多行并存时指向不同的行 → 界面改配置写进去读不回来。

| 指标 | 云端库 | 本地库 |
|---|---|---|
| 配置行总数（repo_key 为空/NULL） | 86 | 2 |
| 有配置行的用户数 | 63 | 1 |
| 有 ≥2 条配置行的用户数 | **17** | 1 |
| 　其中：跨行引擎设置冲突 | **6** | 1 |
| 　其中：跨行 im_app_id 冲突 | **8** | 0 |
| 配置行 id 不合规（≠ `sess-cfg-{user_id}`）的行数 | **56** | 1 |
| 写读错位用户数（写目标 ≠ 当前会被读到的行） | **35** | 1 |
| 　其中：**永久不生效**（写目标排在读目标之后） | **19** | 1 |
| 　其中：下次写入会顶替（会生效，但留僵尸行） | 16 | 0 |
| 最坏交集（错位 + 跨行引擎冲突） | **5** | 1 |
| runtime_id 指向已不存在的引擎（幽灵引擎） | 3 行 | 0 |
| 自检：明细过滤是否生效 | ok | ok |

**未受影响（本次核实后确认）**：IM/飞书配置的读取**不走**这条查询（见 §4），
故我先前的"换 App 后 feishu_name 丢失同源"推断**撤回**。

**下一步**：见 §5。本报告只交付"第一步：可复现诊断 + 基线"，代码与数据改动待拍板。

---

## 1. 机制（每段都有代码坐标）

### 1.1 写路径
`src/session_manager.py:857`（云端部署同款）

```python
sid = f"sess-cfg-{user_id}"
c.execute("INSERT OR REPLACE INTO chat_agent_session (...) VALUES (...)", ...)
```

→ 对 user 3 只会写/替换 `sess-cfg-3`。MySQL 兼容层把 `INSERT OR REPLACE` 译为
`REPLACE INTO`（`src/mysql_compat.py:281`），**未列出的列会被重置为默认值**（见 §5 风险 4）。

### 1.2 读路径
`src/session_manager.py:777`

```sql
SELECT runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider
  FROM chat_agent_session
 WHERE user_id=? AND (repo_key='' OR repo_key IS NULL)
 LIMIT 1
```

**没有 ORDER BY**，而这张表**历史上被移除了 `UNIQUE(repo_key)` 约束**
（`src/api/db_setup.py` 中"重建 chat_agent_session，去掉陈旧的 repo_key UNIQUE 约束"）
→ 多行都能匹配，返回哪一行由存储顺序决定。

### 1.3 哪一行胜出（预测模型）
该表**只有主键索引**（`SHOW INDEX` 仅 PRIMARY(id)），查询计划是全表扫描
（`EXPLAIN`：`type=ALL`）→ InnoDB 按聚簇索引（= 主键）顺序返回 → **id 字符串序最小者**。

所以本报告用 `MIN(id)`（库内排序规则计算）预测"会被读到的行"。
**失效条件**：一旦给 `user_id` 加上索引、或表被重建/优化，扫描路径可能改变，预测需重算 ——
这本身也说明"未定义顺序"是靠不住的（脚本 §6 的自检指标会把预测结果打出来供复核）。

### 1.4 老行的来历
- `sess-cfg-{user_id}` 的命名从引入时就是 `user_id`（`git show a14c098874`：
  迁移旧表 `chat_config` 与写入都写 `sess-cfg-{r[0]}` / `sess-cfg-{user_id}`）；
- 但表经历过多次结构迁移，`db_setup.py:2691` 还留着注释：
  *"每次重启覆盖管理员/用户改绑(2026-08-18 实测 sess-cfg-* 全被重置为 1),跳过"*；
- user-id 体系本身也迁移过（`src/user/db.py:880` 用 username 把 `users.id`
  重映射成 `user_accounts.id`）—— **id 字符串不会跟着重映射走**，于是留下
  id 与 user_id 错位的老行。

---

## 2. 基线数据（云端库，2026-10-17 快照）

> ⚠️ 本节是**首次快照**（不含身份信息，且"永久不生效/会顶替"是修复前口径）。
> 账号体系迁移导致的 id 错位、带身份的最新工作表、以及修复后的复核结论 —— 见 **§9**（以 §9 为准）。

### 2.1 永久不生效用户清单（19 个：写目标 `sess-cfg-{user_id}` 排在"会被读到的行"之后）

| user_id | 会被读到 | 写入目标 | 行数 | 不同引擎 | 各行 id=引擎 |
|---|---|---|---|---|---|
| 3 | sess-cfg-112 | sess-cfg-3 | 3 | 2 | sess-cfg-112=1203,sess-cfg-39=1203,sess-cfg-75=1202 |
| 4 | sess-cfg-113 | sess-cfg-4 | 2 | 1 | sess-cfg-113=1202,sess-cfg-40=1202 |
| 5 | sess-cfg-41 | sess-cfg-5 | 1 | 1 | sess-cfg-41=1202 |
| 6 | sess-cfg-115 | sess-cfg-6 | 3 | 2 | sess-cfg-115=1202,sess-cfg-42=1202,sess-cfg-78=1203 |
| 7 | sess-cfg-116 | sess-cfg-7 | 3 | 1 | sess-cfg-116=1202,sess-cfg-43=1202,sess-cfg-79=1202 |
| 8 | sess-cfg-117 | sess-cfg-8 | 1 | 1 | sess-cfg-117=1202 |
| 9 | sess-cfg-118 | sess-cfg-9 | 1 | 1 | sess-cfg-118=1202 |
| 14 | sess-cfg-123 | sess-cfg-14 | 3 | 1 | sess-cfg-123=1202,sess-cfg-50=1202,sess-cfg-86=1202 |
| 16 | sess-cfg-125 | sess-cfg-16 | 3 | 2 | sess-cfg-125=1202,sess-cfg-52=6102,sess-cfg-88=1202 |
| 19 | sess-cfg-128 | sess-cfg-19 | 2 | 2 | sess-cfg-128=1202,sess-cfg-19=13388 |
| 20 | sess-cfg-129 | sess-cfg-20 | 2 | 1 | sess-cfg-129=1202,sess-cfg-56=1202 |
| 21 | sess-cfg-130 | sess-cfg-21 | 1 | 1 | sess-cfg-130=1202 |
| 28 | sess-cfg-100 | sess-cfg-28 | 2 | 1 | sess-cfg-100=1202,sess-cfg-64=1202 |
| 29 | sess-cfg-102 | sess-cfg-29 | 2 | 1 | sess-cfg-102=1202,sess-cfg-65=1202 |
| 31 | sess-cfg-104 | sess-cfg-31 | 1 | 1 | sess-cfg-104=1202 |
| 33 | sess-cfg-106 | sess-cfg-33 | 2 | 2 | sess-cfg-106=1202,sess-cfg-69=1203 |
| 38 | sess-cfg-111 | sess-cfg-38 | 2 | 1 | sess-cfg-111=1202,sess-cfg-38=1202 |
| 54 | sess-cfg-127 | sess-cfg-54 | 2 | 1 | sess-cfg-127=1202,sess-cfg-90=1202 |
| 58 | sess-cfg-131 | sess-cfg-58 | 1 | 1 | sess-cfg-131=1202 |

判读：这些用户现在**在界面上改引擎/技能，写了也读不回来**（读路径始终返回旧行）。

### 2.2 决策工作表（17 个"有 ≥2 条配置行"的用户，共 40 行）

| user_id | 行 id | 该行引擎 | 该行 last_active_at | 会被读到 | 写入目标 | 下次写入是否生效 |
|---|---|---|---|---|---|---|
| 1 | sess-cfg-1 | (引擎行不存在) | 2026-10-16 00:22:18 | sess-cfg-1 | sess-cfg-1 | yes |
| 1 | sess-cfg-37 | hermes | 2026-08-31 13:47:52 | sess-cfg-1 | sess-cfg-1 | yes |
| 3 | sess-cfg-112 | opencode | 2026-10-09 15:40:07 | sess-cfg-112 | sess-cfg-3 | no |
| 3 | sess-cfg-39 | opencode | 2026-09-22 19:58:48 | sess-cfg-112 | sess-cfg-3 | no |
| 3 | sess-cfg-75 | hermes | 2026-09-15 10:43:27 | sess-cfg-112 | sess-cfg-3 | no |
| 4 | sess-cfg-113 | hermes | 2026-10-09 15:33:58 | sess-cfg-113 | sess-cfg-4 | no |
| 4 | sess-cfg-40 | hermes | 2026-09-02 21:07:23 | sess-cfg-113 | sess-cfg-4 | no |
| 6 | sess-cfg-115 | hermes | 2026-10-09 11:05:04 | sess-cfg-115 | sess-cfg-6 | no |
| 6 | sess-cfg-42 | hermes | 2026-08-31 11:26:05 | sess-cfg-115 | sess-cfg-6 | no |
| 6 | sess-cfg-78 | opencode | 2026-09-17 09:35:26 | sess-cfg-115 | sess-cfg-6 | no |
| 7 | sess-cfg-116 | hermes | 2026-10-12 16:31:18 | sess-cfg-116 | sess-cfg-7 | no |
| 7 | sess-cfg-43 | hermes | 2026-08-31 18:52:00 | sess-cfg-116 | sess-cfg-7 | no |
| 7 | sess-cfg-79 | hermes | 2026-09-15 11:36:00 | sess-cfg-116 | sess-cfg-7 | no |
| 10 | sess-cfg-119 | hermes | 2026-10-13 09:11:49 | sess-cfg-119 | sess-cfg-10 | yes |
| 10 | sess-cfg-46 | hermes | 2026-09-01 21:45:06 | sess-cfg-119 | sess-cfg-10 | yes |
| 12 | sess-cfg-121 | hermes | 2026-10-14 09:43:31 | sess-cfg-121 | sess-cfg-12 | yes |
| 12 | sess-cfg-48 | hermes | 2026-09-02 09:37:59 | sess-cfg-121 | sess-cfg-12 | yes |
| 12 | sess-cfg-84 | hermes | 2026-09-17 17:02:40 | sess-cfg-121 | sess-cfg-12 | yes |
| 14 | sess-cfg-123 | hermes | 2026-10-13 15:13:41 | sess-cfg-123 | sess-cfg-14 | no |
| 14 | sess-cfg-50 | hermes | 2026-09-02 15:09:17 | sess-cfg-123 | sess-cfg-14 | no |
| 14 | sess-cfg-86 | hermes | 2026-09-18 11:02:21 | sess-cfg-123 | sess-cfg-14 | no |
| 16 | sess-cfg-125 | hermes | 2026-10-13 20:26:20 | sess-cfg-125 | sess-cfg-16 | no |
| 16 | sess-cfg-52 | (引擎行不存在) | 2026-09-23 10:30:55 | sess-cfg-125 | sess-cfg-16 | no |
| 16 | sess-cfg-88 | hermes | 2026-09-18 11:24:47 | sess-cfg-125 | sess-cfg-16 | no |
| 19 | sess-cfg-128 | hermes | 2026-10-14 11:40:29 | sess-cfg-128 | sess-cfg-19 | no |
| 19 | sess-cfg-19 | (引擎行不存在) | 2026-10-16 16:34:40 | sess-cfg-128 | sess-cfg-19 | no |
| 20 | sess-cfg-129 | hermes | 2026-10-14 15:46:08 | sess-cfg-129 | sess-cfg-20 | no |
| 20 | sess-cfg-56 | hermes | 2026-09-02 09:29:52 | sess-cfg-129 | sess-cfg-20 | no |
| 23 | sess-cfg-59 | hermes | 2026-09-02 15:20:38 | sess-cfg-59 | sess-cfg-23 | yes |
| 23 | sess-cfg-95 | hermes | 2026-09-22 23:02:10 | sess-cfg-59 | sess-cfg-23 | yes |
| 28 | sess-cfg-100 | hermes | 2026-09-24 15:26:34 | sess-cfg-100 | sess-cfg-28 | no |
| 28 | sess-cfg-64 | hermes | 2026-09-04 10:13:58 | sess-cfg-100 | sess-cfg-28 | no |
| 29 | sess-cfg-102 | hermes | 2026-09-24 18:03:58 | sess-cfg-102 | sess-cfg-29 | no |
| 29 | sess-cfg-65 | hermes | 2026-09-08 19:43:33 | sess-cfg-102 | sess-cfg-29 | no |
| 33 | sess-cfg-106 | hermes | 2026-10-01 14:59:31 | sess-cfg-106 | sess-cfg-33 | no |
| 33 | sess-cfg-69 | opencode | 2026-10-11 17:41:40 | sess-cfg-106 | sess-cfg-33 | no |
| 38 | sess-cfg-111 | hermes | 2026-10-08 15:05:22 | sess-cfg-111 | sess-cfg-38 | no |
| 38 | sess-cfg-38 | hermes | 2026-10-15 10:45:53 | sess-cfg-111 | sess-cfg-38 | no |
| 54 | sess-cfg-127 | hermes | 2026-10-14 10:10:14 | sess-cfg-127 | sess-cfg-54 | no |
| 54 | sess-cfg-90 | hermes | 2026-09-21 10:53:02 | sess-cfg-127 | sess-cfg-54 | no |

> "下次写入是否生效"= 写目标 `sess-cfg-{user_id}` 是否排在当前"会被读到的行"之前
> （`<=` 时写入会顶替成为新的被读行 → 生效，但历史行变成僵尸行）。
> 合并时**保留哪一行需要业务判定**（见 §5 第三步）。

---

## 3. 实证链（本地那一例，三条独立证据）

1. **读路径实测**：`get_chat_config(3)` 返回 `{backend:'hermes', runtime_id:3}`，
   精确等于老行 `sess-cfg-1` 的内容；而新行 `sess-cfg-3` 的内容是 `opencode/259` ——
   返回值不可能由新行产生（引擎名不同）。
2. **台账互证**：今天两轮 chat 的用量行 `source='hermes'`、`model_key='glm-5.3'`，
   与 `agent_runtimes.id=3 → name='hermes'` 一致（新行指向的 259 是 opencode，与 source 矛盾）。
3. **时间线**：`sess-cfg-3`（runtime_id=259 opencode）建于 21:29:23；21:33:07 与 21:37:48
   两轮对话**仍然跑 hermes**；4 条用量行 engine_id 全为 3。

脚本内的"交叉验证"查询把这条链做成了常规检查：只要有 `obs_llm_usage`，
就能比对"chat 用量用的引擎"与"配置会读到的行"，不等即复现。

---

## 4. 复核：上一轮说错的部分（撤回）

**撤回**："飞书 app 配置写进去也可能读不出来 / 换 App 后 feishu_name 丢失同源"。

核实依据：IM 侧读配置是**另外两条查询**，不经过 `get_chat_config`：

```sql
-- src/im/feishu_async.py:1519（注册可用 bot）
SELECT im_app_id, im_app_secret FROM chat_agent_session
 WHERE im_app_id!='' AND im_app_secret!='' AND user_id>0 AND (repo_key='' OR repo_key IS NULL)
-- src/im/handler_async.py:292 / src/api/routes/chat.py:313 / issues_center.py:949
--   都是 WHERE im_app_id=<?> 或 im_app_id!='' 的定向查询
```

带 `!=''` 过滤、**不带 LIMIT 1**、遍历多行去重 → 空值老行被跳过，**不会被老行遮挡**。
所以那条推断不成立，撤回。

**保留但需单独验**：若干用户的跨行 `im_app_id` 取值不同 → 上面那条"加载所有非空
(app_id, secret)"的逻辑会把**旧 bot 一起登记加载**（症状是"改绑不干净/旧 bot 还在"），
与 feishu_name 那件事无关，后果未验证。

**修正**：上一轮我说"若干用户改配置不生效"过于笼统。按字符串序细分后：
**19 个永久不生效**（写目标排在后面）+ **16 个下次写入会顶替**（会生效，但留僵尸行）。

---

## 5. 修复方案（三步骤）与风险

| 步骤 | 动作 | 风险 | 回滚 |
|---|---|---|---|
| **①（本次交付）** | 只读诊断脚本 + 基线报告 | 无（只读） | 无 |
| **②最小止血（已落地，见 §8）** | 读路径按候选序取：① `id='sess-cfg-{user_id}'` 优先 ② 遗留行按 `last_active_at DESC, id DESC`；**引擎不可用的候选行不选**（不把可用配置降级） | 保证"写进去的行一定被读到"；**不改数据**；实测云端仅 若干用户生效引擎变化（且都是"明确排序"的预期结果） | 还原 `get_chat_config` 那一段即可 |
| **③结构性** | 合并历史行（保留哪条需业务判定）→ `repo_key` 归一为 `''` → 加 `UNIQUE(user_id, repo_key)` 防复发 | **会改变 6 个引擎冲突用户的生效引擎**、若干用户的 IM 绑定；生产已有重复行，不先迁移则建唯一索引直接失败 | 迁移前全库备份 + 幂等可重跑脚本 + 回滚脚本 |

补充风险与约束：
1. **不能只加 `ORDER BY id DESC`**：字符串序下 `'sess-cfg-9' > 'sess-cfg-10'`，user_id ≥10 会选错行。
2. **不能按 `updated_at` 排序**：该表**没有这个列**（`get_chat_config` 返回的 `updated_at` 恒为空串，是代码里写死的）。
3. **`repo_key` 可空**：MySQL 的 UNIQUE 允许多个 NULL → 必须先把 NULL 归一为 `''`（当前实测 0 行 NULL，风险低）。
4. **顺带发现（本次审查新增，未改）**：写路径用 `REPLACE INTO` 且只列了部分列
   → 每次改 chat 配置会把该行的 `runtime_session_id`、`status`、`event_history`、
   `repo_key`、`source`、`repository` 等**未列出的列重置为默认值**。
   即"改配置 = 丢引擎会话续接指针"。当前两行该列都是空串，看不出后果，建议一并评估。
5. **单测盖不到**：`tests/conftest.py` 把测试钉在 SQLite，扫描顺序与 MySQL 不同，
   "多行时取哪行"必须在真 MySQL 上验（这也是它潜伏至今的原因之一）。
6. **影响面尚未扫全**：本次只覆盖云端库 与本地库；
   云上另有 管理库，其它环境需同样跑一遍脚本。

---

## 6. 复现方法

```bash
# 直连（本机 .venv 有 pymysql）
GRAPE_DB_HOST=... GRAPE_DB_PORT=3306 GRAPE_DB_USER=... GRAPE_DB_PASSWORD=... \
GRAPE_DB_NAME=<db> .venv/bin/python scripts/diag/chat_config_rows_audit.py

# 远端（目标机只有 mysql 客户端）
.venv/bin/python scripts/diag/chat_config_rows_audit.py --emit-sql > /tmp/audit.sql
ssh <host> "mysql -h<dbhost> -u<user> -p<pw> -t <db>" < /tmp/audit.sql
```

脚本特性：仅 SELECT/SHOW（且尽力 `SET SESSION TRANSACTION READ ONLY`）；
输出 13 项指标 + 明细工作表；**最后一项是自检**——比对"明细过滤后的用户数"与
"≥2 行用户数"是否一致，防止过滤条件静默失效。

> 教训（本次踩到）：写过滤条件时 `WHERE a='' OR a IS NULL AND user_id IN (...)` 因
> **`AND` 优先级高于 `OR`**，实际等价于 `a='' OR (a IS NULL AND ...)` → 过滤整体失效，
> 明细表把 86 行全打出来了（自检因此加入）。凡 `OR` 与 `AND` 混用，括号必须显式写出。

---

## 7. 附录

- 脚本：`scripts/diag/chat_config_rows_audit.py`（只读；凭据只从环境变量/命令行取，脚本内不存任何口令）
- 本次运行原始输出（临时文件，会随重启丢失，关键结论已固化进本报告）：
  `/tmp/cloud_clean.txt`（云端）、本地输出见 §0 表
- 交叉验证依赖 `obs_llm_usage`（仅本地测试库有此表）；无该表时该节报错可忽略
- 未做任何写操作：全程仅 SELECT/SHOW/EXPLAIN

---

## 8. 第二步落地记录（2026-10-17）

### 8.1 改动（只改读路径，不动数据、不动写路径）

`src/session_manager.py::get_chat_config` 重构为"候选行 + 可用性"：

```
_chat_config_candidate_rows()  候选序：① id='sess-cfg-{user_id}'（写路径那行）
                                        ② 历史遗留行 → ORDER BY last_active_at DESC, id DESC
_chat_config_row_engine()      该行引擎是否可用（引擎行不存在 / 未知引擎名 → 不可用）
get_chat_config()              取第一个"引擎可用"的候选行：
                                 · 写路径行可用 → 用它（写读同向，改配置生效）
                                 · 写路径行不可用 → 不降级，退回最近活跃的可用遗留行
                                 · 全不可用 → 保持既有兜底语义（runtime_id 置零 → 本机直连/无引擎）
                                 · 选中遗留行时打 log.info 提示"建议迁移合并"
```

### 8.2 影响面（改动前后生效引擎对比，云端 63 个有配置的用户）

```
生效引擎发生变化的用户：2 个
  user  1: (原) 幽灵引擎 13072 → 按未配置兜底     → (新) hermes 1202（遗留行，可用）
  user 33: (原) hermes 1202（读 sess-cfg-106）   → (新) opencode 1203（最近活跃行 sess-cfg-69）
读取的行变化但生效引擎相同：若干用户
其余 若干用户：完全不变
```

- user 1 的写路径行（`sess-cfg-1`）指向**已删除的引擎**，新逻辑跳过它、退回可用遗留行 hermes/1202
  —— 该用户的"最后选择"需要业务判定（第三步合并时保留哪条）。
- user 33 的变化是"最近活跃优先"的预期结果（`sess-cfg-69` 活跃于 2026-10-11，比 106 新）。

### 8.3 本地实测（端到端）

```
改动前：get_chat_config(3) → backend=hermes  runtime_id=3      （老行 sess-cfg-1）
改动后：get_chat_config(3) → backend=opencode runtime_id=259   （写路径行 sess-cfg-3 ✓ 写读同向）
候选序实测：[sess-cfg-3(259), sess-cfg-1(3)]
```

服务重启后同结论（health 200、无启动异常）；该用户下次对话在台账里应记 `engine_id=259`
（此前 4 条都是 3）——这是"改配置真生效"的最终业务确认点。

### 8.4 新增回归

`tests/test_chat_config_row_selection.py`（6 例，全绿）：

1. 老行 id 字符串序在前时，仍必须读到写路径那一行（线上"改了不生效"的形态）
2. 写路径行优先于"更晚活跃"的遗留行（写入即真理，不靠时间猜）
3. 写路径行不存在 → 退回**最近活跃**的遗留行（不是存储顺序上的任意一行）
4. 写路径行指向已删引擎 → 不把可用配置降级，退回可用遗留行
5. 全部候选不可用 → 不再把幽灵 runtime_id 当配置返回（保持既有兜底语义）
6. 一行都没有 → 保持原行为（本机直连兜底 + 物化默认行）

> 同批跑测试时的两个副作用坑（已在用例里显式清理）：
> ① `_chat_config_with_local_backend` 会调 `resolve_local_engine()`，后者**把结果缓存 30 秒**
> （模块级）→ 本文件先跑会让同批其他用例读到缓存值而不是自己种子里的引擎
> （实测把 `test_chat_backend_resolution` 的 E06 断成 `1 == 902`）；用例结束清缓存修掉。
> ② 无可用引擎时可能**自动物化**一行引擎 → 用例按 `agent_runtimes` id 快照回滚。
>
> 另：本次同步修掉了上一提交在 `chat.py` send 路由里的变量名错误
> （`engine_id` 用了不存在的 `rid` → NameError/500），由这批用例抓到。

### 8.5 第三步待办（未做）

合并历史行（保留哪条 = 业务判定，见 §2.2 工作表；至少 user 1 需要人工确认）→ `repo_key` 归一 `''`
→ 加 `UNIQUE(user_id, repo_key)` 防复发；迁移前全库备份 + 幂等脚本 + 回滚脚本。


---

## 9. 工作表刷新（2026-10-18，含身份 & 修复后视角）

> 用户指出：**云端账号体系经过重构迁移，中间出过事故，user_id 与原 id 不再对应**。
> 本节按此重新核对身份与现状；§2 是 2026-10-17 的首次快照（当时没有身份信息，数字也未含修复后复核），以本节为准。

### 9.1 为什么 user_id 与 id 会错位（证据）

```
user_accounts              94 个账号，id 从 37 起（新 id 空间）
unified_user_mapping      106 行，user_id 从 1 起（旧 id 空间）→ gitcode_user/nickname/feishu_name/app_id/gitcode_id
两个空间同一人：旧 X <-> 新 X+36   （用 gitcode_id 逐条验证，例如 1<->37、2<->38、3<->39…）
配置行里的错位形态：
  · 54 行（若干用户）的 user_id < 37 → 旧空间，user_accounts 里查不到（靠 unified_user_mapping 才能认出人）
  · 25 行的 id 编码的是另一个空间：id == 'sess-cfg-{user_id+36}'
    （例：user 1 名下的 'sess-cfg-37'、user 37 名下的 'sess-cfg-73'）——事故留下的跨空间痕迹
  · 备份表 backup_org_merge_map_20261015 佐证 2026-10-15 那次重构
```

**对本次修复的意义**：读路径的"写路径那一行"= `id='sess-cfg-{当前 user_id}'`，只按 id 取（不按 user_id 过滤），
因此移到新空间后仍能对上同一人的行；但**第三步合并必须经 `unified_user_mapping` 映射身份后再判"保留哪条"**，
不能假设 +36（该偏移是这次重构的产物，不是契约）。

### 9.2 刷新后指标（云端库）

| 指标 | 数值 |
|---|---|
| 配置行总数 | 86（旧 id 空间 54 行 / 新 id 空间 32 行） |
| 有配置行的用户 | 63（旧空间 33 / 新空间 30） |
| 有 >=2 条配置行的用户 | 17（旧空间 15 / 新空间 2） |
| canonical 行（`sess-cfg-{当前 user_id}`）缺失、当前仍读遗留行 | 若干用户（旧空间 29 / 新空间 4） |
| 跨空间行（`id == 'sess-cfg-{user_id+36}'`） | 25 行 |
| runtime_id 指向已不存在引擎 | 2 行（user 16 的 `sess-cfg-52`=6102、user 19 的 `sess-cfg-19`=13388） |
| **修复后写读错位** | **0**（写路径行存在时必被读到；缺失的 若干用户在下一次写配置时自动建行并对齐） |
| **修复后生效引擎变化** | **若干用户**：user 33 明亮（hermes -> opencode，取"最近活跃"行） |
| **归属冲突行（安全相关）** | **4 行**携带他人的飞书 app_id + 32 位密钥（`sess-cfg-112/78/119/129`）→ 详见 §9.5，**迁移前必须人工裁决** |

### 9.3 工作表（17 个多行用户，含身份与修复前后对照）

| user_id | 身份（gitcode/昵称 · 飞书 · 新id） | 行数 | canonical | 现在读到（修复后） | 修复前读到 | 各行明细（id · 引擎 · 最后活跃 · im_app_id） |
|---|---|---|---|---|---|---|
| 1 | gcw_3FZApeLT/liufei_cloud · 飞书刘菲 · 新id 37 | 2 | 有 | sess-cfg-1 · opencode(13030) | sess-cfg-1 | sess-cfg-1 · opencode · 2026-10-18 11:43 · —<br>sess-cfg-37 · hermes · 2026-08-31 13:47 · cli_aa0f66ae9378dcbb |
| 3 | songspace/吕军涛 · 飞书吕军涛 · 新id 39 | 3 | **缺** | sess-cfg-112 · opencode(1203) | sess-cfg-112 | sess-cfg-112 · opencode · 2026-10-09 15:40 · cli_aa422650ef785d23<br>sess-cfg-39 · opencode · 2026-09-22 19:58 · —<br>sess-cfg-75 · hermes · 2026-09-15 10:43 · — |
| 4 | mm6667/mm6667 · 飞书孟令闯 · 新id 40 | 2 | **缺** | sess-cfg-113 · hermes(1202) | sess-cfg-113 | sess-cfg-113 · hermes · 2026-10-09 15:33 · —<br>sess-cfg-40 · hermes · 2026-09-02 21:07 · cli_aa0874e7d2389bc3 |
| 6 | Sinkear/单佳豪 · 飞书— · 新id 42 | 3 | **缺** | sess-cfg-115 · hermes(1202) | sess-cfg-115 | sess-cfg-115 · hermes · 2026-10-09 11:05 · —<br>sess-cfg-42 · hermes · 2026-08-31 11:26 · —<br>sess-cfg-78 · opencode · 2026-09-17 09:35 · cli_aa2d729227b85bcb |
| 7 | wdmike/Xiahouyi · 飞书— · 新id 43 | 3 | **缺** | sess-cfg-116 · hermes(1202) | sess-cfg-116 | sess-cfg-116 · hermes · 2026-10-12 16:31 · —<br>sess-cfg-43 · hermes · 2026-08-31 18:52 · —<br>sess-cfg-79 · hermes · 2026-09-15 11:36 · — |
| 10 | younger_eric/吴扬 · 飞书— · 新id 46 | 2 | **缺** | sess-cfg-119 · hermes(1202) | sess-cfg-119 | sess-cfg-119 · hermes · 2026-10-13 09:11 · cli_aa473954d4b81cbc<br>sess-cfg-46 · hermes · 2026-09-01 21:45 · — |
| 12 | shuangzhangj/张爽 · 飞书— · 新id 48 | 3 | **缺** | sess-cfg-121 · hermes(1202) | sess-cfg-121 | sess-cfg-121 · hermes · 2026-10-14 09:43 · —<br>sess-cfg-48 · hermes · 2026-09-02 09:37 · —<br>sess-cfg-84 · hermes · 2026-09-17 17:02 · — |
| 14 | essie_km/essie_km · 飞书— · 新id 50 | 3 | **缺** | sess-cfg-123 · hermes(1202) | sess-cfg-123 | sess-cfg-123 · hermes · 2026-10-13 15:13 · —<br>sess-cfg-50 · hermes · 2026-09-02 15:09 · —<br>sess-cfg-86 · hermes · 2026-09-18 11:02 · — |
| 16 | miumiu_020523/弥生一梦 · 飞书— · 新id 52 | 3 | **缺** | sess-cfg-125 · hermes(1202) | sess-cfg-125 | sess-cfg-125 · hermes · 2026-10-13 20:26 · —<br>sess-cfg-52 · (引擎行不存在) · 2026-09-23 10:30 · —<br>sess-cfg-88 · hermes · 2026-09-18 11:24 · — |
| 19 | chenjingjie/陈晶杰 · 飞书陈晶杰 · 新id 55 | 2 | 有 | sess-cfg-128 · hermes(1202) | sess-cfg-128 | sess-cfg-128 · hermes · 2026-10-14 11:40 · —<br>sess-cfg-19 · (引擎行不存在) · 2026-10-16 16:34 · — |
| 20 | lccode/汪亮初 · 飞书— · 新id 56 | 2 | **缺** | sess-cfg-129 · hermes(1202) | sess-cfg-129 | sess-cfg-129 · hermes · 2026-10-14 15:46 · cli_aa458d0aa6e2dbfe<br>sess-cfg-56 · hermes · 2026-09-02 09:29 · — |
| 23 | cs_HWStaff/程曙 · 飞书— · 新id 59 | 2 | **缺** | sess-cfg-95 · hermes(1202) | sess-cfg-59 | sess-cfg-59 · hermes · 2026-09-02 15:20 · —<br>sess-cfg-95 · hermes · 2026-09-22 23:02 · — |
| 28 | Evan0603/褚蕴威 · 飞书— · 新id 64 | 2 | **缺** | sess-cfg-100 · hermes(1202) | sess-cfg-100 | sess-cfg-100 · hermes · 2026-09-24 15:26 · —<br>sess-cfg-64 · hermes · 2026-09-04 10:13 · — |
| 29 | yuanbeyond/孙思源 · 飞书— · 新id 65 | 2 | **缺** | sess-cfg-102 · hermes(1202) | sess-cfg-102 | sess-cfg-102 · hermes · 2026-09-24 18:03 · —<br>sess-cfg-65 · hermes · 2026-09-08 19:43 · — |
| 33 | lming112/明亮 · 飞书明亮 · 新id 69 | 2 | **缺** | sess-cfg-69 · opencode(1203) | sess-cfg-106 | sess-cfg-106 · hermes · 2026-10-01 14:59 · —<br>sess-cfg-69 · opencode · 2026-10-11 17:41 · cli_aa428753ba38dbda |
| 38 | liudewei_david/刘德伟 · 飞书刘德伟 · 新id 74 | 2 | 有 | sess-cfg-38 · hermes(1202) | sess-cfg-111 | sess-cfg-111 · hermes · 2026-10-08 15:05 · —<br>sess-cfg-38 · hermes · 2026-10-15 10:45 · cli_aa5a824ae3f8dc06 |
| 54 | SteveSLX/SteveSLX · 飞书— · 新id — | 2 | **缺** | sess-cfg-127 · hermes(1202) | sess-cfg-127 | sess-cfg-127 · hermes · 2026-10-14 10:10 · —<br>sess-cfg-90 · hermes · 2026-09-21 10:53 · — |

> "现在读到"= 第二步修复后的真实逻辑（直接调用 `_pick_chat_config_row`，非预测）；
> "修复前读到"= 旧逻辑（`WHERE user_id=? LIMIT 1` 无 ORDER BY，实际命中主键字符串序最小者）。
> 合并时"保留哪一条"= 业务判定：一般取**现在读到**的那条（用户实际在用的），其余行作僵尸清理；
> 但 user 19（陈晶杰）要留意：他的 canonical 行 `sess-cfg-19` 指向**已删除的引擎 13388**，
> 现在被"不可用不降级"规则跳过、读到 `sess-cfg-128`（hermes）——合并时应保留后者。

### 9.4 第三步的新增约束（由 id 空间问题引出）

> 基于本节三条约束的**迁移方案（含 dry-run/备份/验收/回滚/脚本接口）**见
> `docs/weekly-07/chat-config-row-migration-design.md`。

1. 合并前先建立**身份映射**：`unified_user_mapping.gitcode_id -> user_accounts.id`，逐个用户确认归属，
   不要用算术偏移（+36）替代映射表。
2. 合并目标 id 只能取**当前 user_id** 的写路径 id（`sess-cfg-{user_accounts.id}`）；
   旧空间用户（33 个）没有对应的新空间 user_id 时，需先确认他们在新空间的 id 再合并，
   否则会把配置行迁到无人认领的 id 上。
3. 跨空间行（25 行）特别危险：它们的 id 属于别人（新空间同名 id）的"写路径行"，
   若新空间那位用户今后写配置，`REPLACE INTO id='sess-cfg-{他的 user_id}'` 会**直接覆盖这一行**
   （同一行被两个身份争用）。迁移时这类行必须优先处理。

### 9.5 归属冲突行（安全相关，2026-10-18 新增发现）

用 `im_app_id`（飞书 app 是登录凭证，只有本人能注册 → **唯一权威的"app→人"依据**）反查，
发现 **4 行配置携带他人的 app_id 与 32 位密钥**，而它们的 `user_id` 指向另一个人：

| 行 | 行内 user_id → 该 id 对应的人 | 行内 app 真正属于 | 密钥长度 | 两侧活跃度（会话/任务） |
|---|---|---|---|---|
| `sess-cfg-112` | 3 → songspace/吕军涛 | 陈金昆（mapping 39/75） | 32 | 154/954 **vs** 1/5 |
| `sess-cfg-78` | 6 → Sinkear/单佳豪 | 孟令闯（mapping 42） | 32 | 88/478 **vs** —/2 |
| `sess-cfg-119` | 10 → younger_eric/吴扬 | 赵伟俊（mapping 46） | 32 | 10/67 **vs** —/0 |
| `sess-cfg-129` | 20 → lccode/汪亮初 | 朱金婷（mapping 56/92） | 32 | 28/231 **vs** 3/14 |

两种解释都无法用数据排除 —— "app 字段被写错"（则这些人**正在用别人的 bot 发消息**）
与 "user_id 被写错"（则这些人自己的配置被别人继承）—— **必须人工逐行裁定**；
每个候选人在自己界面自查"我用的飞书 bot 是不是我自己的"即可判定。
迁移脚本**禁止自行猜测**：这类行没有人工决策条目就跳过（见设计文档 §2.5 决策文件接口）。

**处置记录（2026-10-18，用户决定，已执行）**：

```
处置   4 行的 im_app_id='' + im_app_secret=''（user_id/runtime_id/skill_id/im_provider 均未动）
校验   受影响行数=4；读回 app_id 为空、密钥长度 0；"仍持有这 4 个 app 的配置行数"=0
副作用 这 4 个 app 从此不在任何配置行里（user_accounts 里也没有副本，实测 0 行）
      → 行名义主人（3 吕军涛 / 6 单佳豪 / 10 吴扬 / 20 汪亮初）需重新绑**自己的**飞书 app；
        app 真主（陈金昆 / 孟令闯 / 赵伟俊 / 朱金婷）若此前被借用该绑定，同样需要重新绑
未动   引擎/技能配置不变（runtime_id 仍为 1202/1203，skill_id 仍为 0）→ 聊天功能不受影响
快照   置空前完整内容见 `chat-config-conflict-rows-cleared-20261018.txt`（**密钥已脱敏，未保留明文**；
       重启绑定由当事人从飞书后台取自己的 secret）
遗留   user_id 归属之争**未解决**（本次只清了凭据）——合并前仍需按 §2.5 决策文件逐行裁定
```

⚠️ 处理原则：先缩小暴露面（已做），再谈合并；报告与日志中不回显密钥明文。

> 结论：§9.3 工作表里"某人名下的各行"在碰到上表这 4 行时**不可直接采信**（行名/user_id 与 app 证据冲突）。
> 迁移前请以本节的裁决结果为输入。
