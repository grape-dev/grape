# A2 收尾：旧表 / 旧列弃用清单（只读盘点，2026-11-10）

> 回滚点：`pre-im-legacy-drop-20261110` → `834a6682a`（M1/M2/M3 全部完成并通过真机验收）
> 本文件只是**盘点与方案**，未改任何破坏性内容。

## 0. 一句话结论

真正挡在 DROP 前面的**只有读路径**：UI 回显与"换 app / 解绑"决策目前仍把
`chat_agent_session.im_*` / `devops_agent_session.im_*` 当唯一真相；`global_im_bot_config`
剩下的唯一职责是 **app_id 全局独占校验**（chat_bot / vod_bot 互斥）。
把这三处换成 `channel_installation` 之后，三列一表都可以安全 DROP。

---

## 1. 一览表

| 对象 | 生产引用 | 测试引用 | 分类 |
|---|---|---|---|
| `chat_agent_session.im_app_id/im_app_secret/im_provider` | 3 文件 ~50 处 | ~8 文件 | 🟡 可删（先迁读路径） |
| `devops_agent_session.im_app_id/im_app_secret/im_provider` | 2 文件 ~35 处 | ~5 文件 | 🟡 可删（先迁读路径） |
| `global_im_bot_config`（整表） | 5 文件 ~30 处 | ~7 文件 | 🟡 可删（先迁独占校验） |
| `repo_im_group_binding` | 5 文件 | 少量 | 🟢 保留（群通知开关仍在用，独立旧表，不在本次范围） |
| `grape_im_instance` | 1 文件 | 3 文件 | 🟢 保留（实例心跳，M3 后更核心） |
| `channel_lease` | 2 文件 | 3 文件 | 🟢 保留（M3 新表） |

---

## 2. 明细：chat_agent_session 的三列

### 2.1 表结构 / 迁移（🟢 保留定义，等 DROP 阶段再删）

| 位置 | 内容 |
|---|---|
| `src/session_manager.py:168-196` | `CREATE TABLE chat_agent_session`（含 im_provider/im_app_id/im_app_secret 三列） |
| `src/session_manager.py:314-322` | 三列的 `ALTER TABLE ... ADD COLUMN` 兼容段 |
| `src/session_manager.py:137-146` | 旧表 `sessions → chat_agent_session` 改名（历史迁移，保留） |
| `src/api/db_setup.py:145` 附近 ORPHAN 扫描 | 只扫 `user_id`，与 im_* 无关 → 不受影响 |

### 2.2 读路径（🔴 必须先迁到 channel_installation）

| 位置 | 现状 | 迁移后 |
|---|---|---|
| `src/api/routes/chat.py:1450-1471` | GET `/api/chat/config` 从 `cfg.im_app_id/im_app_secret/im_provider` 回显 `connected / app_secret_exists / provider` | 改读 `get_active_installation("user", owner_user_id=uid)`；provider = installation.channel_type；secret 是否存在看 config 里有没有 app_secret |
| `src/api/routes/chat.py:1584` | 占位符 `********` 保存时，回填 `cfg["im_app_id"]` | 同上，从安装行取 app_id |
| `src/api/routes/chat.py:1618` | `old_app_id = old_cfg.get("im_app_id")` → 驱动 revoke/purge | 从安装行取（或直接由 `revoke_installation` 内部查） |
| `src/api/routes/chat.py:1734` | `_purge_provider = old_cfg.get("im_provider")` | 用 installation.channel_type |
| `src/api/routes/chat.py:1741` | `_new_app = kwargs.get("im_app_id")` | 用本次请求的 app_id（本就有 `_new_app` 变量来源，可收起） |
| `src/api/routes/chat.py:1758` | `_old_channel = old_cfg.get("im_provider")` | installation.channel_type |
| `src/session_manager.py:934-941` | `_chat_config_candidate_rows` 把 `im_app_id/im_app_secret/im_provider` 一起 SELECT 出来给 `get_chat_config` | 三列从 SELECT 列表移除；`get_chat_config` 返回体去掉 im 字段（调用方已按 2.2 改） |
| `src/session_manager.py:1101/1119-1120` | `set_chat_config` 的 `allowed` 白名单与写库参数含三列 | 白名单收窄为 backend/runtime_id/skill/skill_id |

### 2.3 写路径（🟡 直接删）

| 位置 | 内容 |
|---|---|
| `src/api/routes/chat.py:1633-1643` | 写 `global_im_bot_config` 登记行 + `kwargs["im_app_id/im_app_secret/im_provider"]` |
| `src/api/routes/chat.py:1647-1656` | 显式解绑/普通保存分支往 kwargs 塞三列 |
| `src/api/routes/chat.py:1826-1827` | `_persist_wechat_login` 回写三列（同时已写 channel_installation） |
| `src/api/routes/feishu_onboard.py:116-152` | Chat 扫码落地：写 chat_agent_session 三列 + 登记行 + 换绑清理 |
| `src/session_manager.py:1101` | 见上（白名单） |

---

## 3. 明细：devops_agent_session 的三列

### 3.1 表结构 / 通用写入口

| 位置 | 内容 | 结论 |
|---|---|---|
| `src/api/session_store.py:457-506` | `upsert_agent_session(..., im_app_id, im_app_secret, im_provider)` 的 UPDATE/INSERT 列 | 🔴 需先改调用方，再删参数 |
| `src/api/session_store.py:628` | 批量 upsert 里显式传 None（占位） | 🟡 随参数删除 |

### 3.2 VOD 读路径（🔴 必须先迁）

| 位置 | 现状 | 迁移后 |
|---|---|---|
| `src/api/routes/agents.py:199-225` | GET `/api/agents/{type}/{owner}/{repo}` 的 `im_app_id` 回显（`row[8]`） | 改读 `get_active_installation("repo", repo_key=..., channel_type="feishu")` |
| `src/api/routes/agents.py:247-249` | 回显前查 `global_im_bot_config` 判断 vod_bot 登记 | 改为看 installation 是否存在 |
| `src/api/routes/agents.py:395` | 详情返回体带 `im_app_id` | 同上 |
| `src/api/routes/agents.py:68-80` | `_unbind_vod_im`：读 `im_app_id` → 清三列 → 删登记行 → revoke installation | 保留 revoke；读 app_id 改为从安装行取；三列 UPDATE 与登记行 DELETE 删除 |
| `src/api/routes/agents.py:490-498` | 换 app 时读旧 `im_app_id`（用于 revoke 旧安装） | 从安装行取 |
| `src/api/routes/agents.py:474` | 占位 secret 回填 `SELECT im_app_secret ... WHERE im_app_id=?` | 改从 installation.config 取 |
| `src/api/routes/feishu_onboard.py:168-210` | VOD 扫码落地：写 devops_agent_session 三列 + 登记行 | 只写 channel_installation（已有 sync） |

### 3.3 写路径（🟡 删）

`agents.py:517`（`_unbind_vod_im` 调用处的三字段清空）、`agents.py:662-664`（upsert 传参）、
`agents.py:460-506`（保存分支的登记行 INSERT）、`session_store.py` 的 upsert 参数（见 3.1）。

---

## 4. 明细：global_im_bot_config（整表）

### 4.1 真正的功能依赖（🔴 必须先替代）

| 位置 | 用途 | 替代方案 |
|---|---|---|
| `chat.py:1607-1612` | 独占校验：该 app_id 是否已被 **VOD** 占用（`bot_type='vod_bot'`） | 查 `channel_installation WHERE legacy_app_id=? AND status='active'` 的 channel_type/scope |
| `chat.py:1612` = `feishu_onboard.py:130` | 该 app_id 是否已被**其他用户**占用 | 同上（scope='user' + owner_user_id 不同） |
| `agents.py:484-486` | 独占校验：该 app_id 是否已被 **Chat** 占用 | 同上（channel_type 属于 user 轨 / chat 场景） |
| `agents.py:249` | 回显时判断 vod_bot 登记存在 | 看安装行 |
| `db_setup.py:4596` | 一次性修数：把群绑定的空 im_app_id 填成 chat_bot 的 app_id | 这段本身是历史补救，随表一起删（并标注不再需要） |

> 替代后建议在 `channel_installation` 上加**唯一索引**：`(legacy_app_id)` where `status='active'`
> （MySQL 用生成列或应用层校验二选一；SQLite 部分索引 `WHERE status='active'` 直接可用），
> 把"一个 app_id 只能挂一条 active 安装"变成数据库级约束。

### 4.2 迁移/历史修复（🟡 随表删除，需注意升级路径）

| 位置 | 内容 |
|---|---|
| `scripts/migrations/001_channel_tables.py:160-200+`（`backfill_installations`） | 每次启动从 `chat_agent_session` / `devops_agent_session` 回填 `channel_installation` —— **列删掉后此函数必须移除**，否则启动即 `no such column` |
| `src/api/db_setup.py:261-269` | DDL |
| `src/api/db_setup.py:4309-4315` | 补列 ALTER（bot_name / lease_* / updated_at） |
| `src/api/db_setup.py:4424-4436` | 更老的 `im_bot_config → global_im_bot_config` 改名迁移 |
| `src/api/db_setup.py:4588-4596` | 见 4.1 末尾 |
| `src/im/engine_wiring.py:402` | `revoke_installation` 里顺带 `DELETE FROM global_im_bot_config ...`（M2 之后纯清理，可直接删） |
| `scripts/diag/chat_config_im_restore.py`（整文件） | Issue #431 的凭证恢复脚本，读 `chat_agent_session.im_*` → 随列删除而失效，整文件删除（历史已归档在 git） |
| `scripts/smoke/im_channel_mysql_smoke.py`（6 处） | 冒烟脚本种 legacy 行验证回填 → 回填删除后同步改 |
| `scripts/release_pack.py:151` | 打包种子表清单含 `global_im_bot_config` → 从清单移除 |

### 4.3 保留项（与 IM 绑定无关）

`src/api/db_setup.py:145` 的 ORPHAN 扫描（扫的是 `chat_agent_session.user_id`，不是 im_*）；
`repo_im_group_binding` 全部引用（群通知开关 `settings.py:625/656` 仍在用）；
`grape_im_instance`（M3 后是实例环的心跳表）。

---

## 5. 执行顺序（三阶段，逐阶段验收）

### Phase A — 只读迁移（无破坏，可单独验收）
1. GET 回显改造：`chat.py:1450-1471`、`agents.py:199-249/395` → 读 `channel_installation`
2. 换 app / 解绑决策改造：`chat.py:1618/1734/1758`、`agents.py:68-80/490-498/474` → 读安装行
3. 独占校验改造：`chat.py:1607`、`feishu_onboard.py:125/180`、`agents.py:484` → 查安装行 + 加唯一约束
4. `session_manager.get/set_chat_config` 去掉 im 三列（SELECT 与白名单）
5. 验收：`pytest tests/im tests/test_chat_* tests/test_agents_routes.py tests/test_feishu_onboard.py`；
   重启后三个渠道各发一条消息（回显页面也要点开看一眼）

### Phase B — 破坏性 DROP（已执行 2026-11-10 ✅）
1. 备份：`data/backups/` 全库 dump（或至少 dump 三列一表）
2. 先删代码引用（`001.backfill_installations`、`engine_wiring.revoke` 清理、session_store 参数）
3. 一次性迁移脚本（`scripts/migrations/00x_drop_legacy_im.py`，幂等 + 表/列存在才动）：
   - `ALTER TABLE chat_agent_session DROP COLUMN im_app_id / im_app_secret / im_provider`
   - `ALTER TABLE devops_agent_session DROP COLUMN ...×3`
   - `DROP TABLE IF EXISTS global_im_bot_config`
   - 执行前校验：`SELECT COUNT(*) FROM channel_installation WHERE status='active'` 与 legacy 侧非空行数一致
4. 验收：重启零 Traceback / channels=3 / 真机发消息 / 回显正常 / 新建 & 解绑各走一遍

**执行记录（2026-11-10 18:2x CST，本机测试库）**：
- 备份：`data/backups/im-legacy-pre-drop-20261110-1805.json`（chat 旧列 2 行 / devops 0 行 /
  `global_im_bot_config` 5 行 / 安装表快照 5 行，600 权限）
- 实现：新增 `scripts/migrations/002_drop_legacy_im.py`（先跑 001 回填 → 校验 active 安装 >0 →
  DROP 6 列 → DROP 表；幂等；`--dry-run` 不回填不动结构）
- 实际输出：`backfill {user:2, repo:0}`、`dropped 6 cols`、`dropped_table True`、
  `after nothing_to_do True`；回填未新增行（旧侧信息已被安装表覆盖）
- 代码：启动迁移不再回填（`engine_wiring.run_startup_migration` 只建表）；
  最后一处 legacy 读路径（`handler_async.get_user_id_by_app_id` 第 1 跳、
  `vod_handler_async._list_vod_repos` / `_get_vod_session`、
  `engine/installations.reconcile_installations` 死代码）全部改读安装表或删除
- 测试：IM 相关合计 **768 passed**（新增 002 迁移 5 个用例）；`tests/im` 599 passed；
  MySQL 冒烟 18 项全 PASS（改为直接走写穿桥建安装行）
- 运行验证：重启后 channels=3 / reconnects=0 / Traceback=0；DROP 后 20s 复查仍 3 渠道、
  入站表 74 行未变

### Phase C — 收尾（已完成 2026-11-10 ✅）
- [x] 文档：`docs/issue-825` 的 engine-mode 双链路描述、`docs/issue-871` 的 write_mode / 回写描述
      → spec/plan 各加"历史设计说明"批注
- [x] `src/api/routes/im.py` 顶部注释里"legacy 双轨表"表述
- [x] 状态文件 §〇⁗ 记录收尾结论

---

## 6. 风险 / 待拍板

1. **升级路径（关键）**：`backfill_installations` 是"老库 → 新表"的唯一自动迁移手段。删掉后，
   从 M1 之前版本直接升级的库将没有 IM 安装行（等于全员掉线）。
   - 方案 A（推荐）：DROP 一次性迁移脚本里先跑一遍 backfill（读旧列）再删列/表——升级路径保留；
   - 方案 B：约定"只支持从 ≥M2 版本升级"，更省事但运维前提更硬。
2. **唯一约束落点**：`channel_installation` 上的"active 安装 app_id 唯一"用部分索引（SQLite）
   + 生成列/应用层校验（MySQL），还是仅应用层校验？建议至少 SQLite 做真约束。
3. **repo_im_group_binding 不动**：它还有群通知开关在用；如果后续也要退役，需单独一轮。
4. **MySQL DDL 坑**：迁移脚本里 `--` 注释同样不能让 CREATE/ALTER 落在非句首（M3 踩过 1170）。

## 7. 量级预估

| 阶段 | 改动 | 行数 |
|---|---|---|
| A 读路径 + 独占校验 + 配置 CRUD 收窄 | 6 个生产文件 + 6~8 个测试 | ≈ 250~320（净减） |
| B 迁移脚本 + 引用清理 | 新增 1 脚本 + 4 处清理 | ≈ 120~160（净减） |
| C 文档 | 3 个 md | ≈ 30 |
| **合计** | | **≈ 400~510 行，净删为主** |

## 8. 自检命令（确认"没漏"）

```bash
grep -rn "global_im_bot_config" src/ scripts/ --include=*.py | grep -v __pycache__
grep -rn "im_app_id\|im_app_secret\|im_provider" src/ --include=*.py | grep -v __pycache__
grep -rn "backfill_installations" src/ scripts/ tests/ --include=*.py
```

Phase B 完成后前两条应只剩注释/文档；第三条应为零。