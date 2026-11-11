# D96：生产部署 Checklist——MySQL 首启回填观察（方案 v2，2026-11-08）

**来源**：挂账 #9 剩余件。v1 方案经外部评审打回 4 处必修 + 1 处待确认，
本稿逐条修正后的终版。**回填链零改动、只诊断不治疗、不碰挂账 #12**（边界不变）。

---

## 一、v1 → v2 修正对照（评审意见逐条落位）

### 修 1：明文残留检测判据——改密文格式正向断言

v1 判据"`{` 开头明文行不再有"只覆盖 JSON 形态，**裸 token 行（ghp_xxx 等）
会漏报——假阴性**，实锤采纳。

**v2 判据（正向断言密文形态）**：`credential_crypto.encrypt` 输出格式固定为
`{key_id}:{nonce_b64}:{ciphertext_b64}`（三段冒号，base64 字符集）。脚本对
`user_credentials.config_json`（非空行）与 `grape_t_repo_credentials.value`（非空行，
空串=无凭据直通不算残留）逐行断言：

```
^[^:]{1,64}:[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$
```

不匹配即 FAIL——同时覆盖 JSON 明文与裸 token 两形态。该判据与
`credentials_backfill_n1._is_plaintext`（"非三段密文形态=明文"）互为镜像，
语义同源。

**格式标识遗漏确认**：credential_crypto 无独立 `v1:` 类版本前缀，key_id 即
版本锚点（rotation 走 key_id 切换，`_get_key_by_id` 支持旧密钥解密）——
rotation/检测锚点已在，非 D-2A 遗漏；三段正则中的 `[^:]{1,64}` 段即 key_id，
未来加算法版本只需扩展 key_id 命名（如 `v2-xxx`），正则无需变更。

### 修 2：盖章与回填的时序/原子性——完成判定权重重排

实码因果（db_setup 迁移执行器 + credentials_backfill_n1）：

- 盖章迁移（202611070001-0003）是 **safe 建表/加列**（UPC 表/CUL 表+索引/
  rc id 列），`fn 正常返回=盖章`——它们不包含四条数据回填；
- 四条数据回填在 `credentials_backfill_n1.run()`（user/db.py init_db 链调用），
  **各自 try/except 吞成 warning，失败不影响启动、不影响任何盖章**；
- 结论：**版本表三行在册只证明"迁移已触发且 DDL 成功"，与数据回填成败
  无因果**。

**v2 完成判定（权重写死）**：

| 信号 | 权重 |
|---|---|
| 明文残留=0 且 空 id 残留=0 且 `backfill failed` warning 零命中 | **完成判据（三项全绿才可收工）** |
| 版本表三行在册 | 辅助信号（仅证明迁移已触发） |

运维文档显式警示："看到三行在册就收工"正是最危险状态——数据回填失败
被吞 warning 后服务照常起，版本表不受影响。

### 修 3：脚本连库凭据与只读保障——生产安全边界写死

- **连接来源**：环境变量 `GRAPE_CHECK_DB_URL`（标准 SQLAlchemy URL），
  脚本不读应用主配置（configs/db-backend.json 等）、不硬编码任何凭据；
  未设置时输出使用说明并 exit 2（无法检查≠检查失败，exit code 区分）。
- **只读双保险（运行时级，非仅静态）**：
  - MySQL：连库后立即 `SET TRANSACTION READ ONLY`（且脚本会话不执行任何
    写语句）；
  - SQLite：`PRAGMA query_only = ON`。
- **静态守卫**：UC39④ 扫描脚本**全部字符串常量**（ast.walk + Constant），
  禁含 `INSERT`/`UPDATE`/`DELETE`/`REPLACE`/`ALTER`/`CREATE`/`DROP` 子串
  （评审具体化意见采纳：AST 节点类型检查看不到 SQL 语义，字符串常量扫描
  才是有效断言）。
- **账号建议**（写入 checklist，脚本不强制的部分由部署流程保证）：生产
  执行用只读账号（SELECT 权限）最佳；无只读账号时应用账号 + 上述双保险
  兜底。

### 修 4：FAIL 处置指引——每类 FAIL 跟下一步动作

| FAIL 类型 | 处置指引（checklist 与脚本输出同文） |
|---|---|
| 明文残留（user_credentials） | 确认 `GRAPE_CREDENTIAL_KEYS` 已配置且与首启一致 → 重启服务（`credentials_backfill_n1.backfill_encryption` 幂等续跑）→ 复跑本脚本 |
| 明文残留（grape_t_repo_credentials） | 同上（`backfill_repo_credentials_encrypt`） |
| 空 id 残留 | 同上（`backfill_uuid` / `backfill_repo_credentials_uuid`） |
| 版本表缺盖章 | 查启动日志 `_run_pending_migrations` 段；safe 失败会响亮报错——修复后重启即补跑（迁移幂等） |
| `backfill failed` warning 命中 | 按日志中的步骤名定位：翻译层/方言问题查 `src/mysql_compat.translate_sql` 对该 SQL 形态的支持（894bf7626 已修 IN 子查询 LIMIT 等，新形态需扩展） |

### 待确认裁定：下线观察期统一 P-8 的 4 周口径

实查：`credentials_backfill_n1` docstring 只写"连续观察期内零 backfill: N rows"，
**无具体天数**；P-8 拍板口径为 4 周。裁定：**统一 4 周**，checklist 写明
"连续 4 周零 `backfill: N rows`（N>0）且零 `backfill failed`"为迁移完成
下线判据；模块 docstring 不改（"观察期内"语义兼容，具体值由 checklist
承载——避免为文档口径动迁移代码）。

## 二、采纳的补充项

1. **第 0 项=备份确认**：步骤 2（user_credentials 明文加密覆写）是破坏性
   写操作，回滚锚点仅 DB 备份。checklist 首项"备份已确认（时间戳+可恢复性）"
   独立勾选，前置条件非观察项。
2. **二启判定树覆盖失败分支**：首启有 `backfill failed` warning → 二启
   允许 N>0（这是幂等续跑，不是幂等失败）→ 排查修复后，**三启起必须零**。
   checklist 按判定树写，防机械执行"二启零出现"误判。
3. **UC39④ 实现方式具体化**：字符串常量扫描（见修 3），非 AST 节点类型。
4. **残余风险显式记录**：回填失败吞 warning、服务照常起，checklist 是唯一
   防线——运维不执行时失败静默。本批不加运行时告警（回填链零改动边界），
   作为残余风险写入 checklist 头部；**建议将校验脚本列为部署流程强制步骤**
   （写入 checklist 使用说明，由部署流程执行）。
5. **exit code 三态**：0=检查全绿；1=检查发现失败；2=无法检查（连接失败/
   环境变量未设）——"发现失败"与"无法检查"严格区分。

## 三、交付物（不变，判据按 v2）

1. `docs/weekly-09/multi-platform-decouple/deploy-checklist-mysql-backfill.md`——第 0 项备份 +
   首启观察 + 二启判定树（含失败分支）+ 完成判定（修 2 权重）+ 4 周下线
   判据 + FAIL 处置指引表 + 残余风险声明。
2. `scripts/check_deploy_backfill.py`——只读（运行时双保险 + 字符串常量
   静态守卫）、环境变量连库、三态 exit code、五项检查（盖章辅助信号/明文
   残留/空 id 残留/审计锚点可用性/日志 grep 指引输出）、FAIL 带处置指引。
3. `tests/test_uc39_deploy_checklist.py`——①已迁移库全绿；②伪造明文行
  （JSON 形态 + **裸 token 形态**）→FAIL 精确命中（修 1 回归锁）；③伪造空
   id 行/缺盖章→FAIL；④字符串常量静态守卫；⑤checklist 文档守卫（四条
   回填日志锚点 + 第 0 项 + 4 周判据在册）。

## 四、文档落位（不变）

design `06-ledger.md` 挂账 #9 状态更新 + requirements UC39 入
`12-usecases-consumer.md` + 对账行更新。

## 五、注册新账号走核心链路（D105-7 增补验证步，2026-11-09）

UUID 主键迁移（D105/D105b）后，部署完成/迁移盖章仅证明 schema 正确；**uuid 新用户的写入路径必须实弹走一遍**（混型/亲和问题只在 uuid 形态 id 写入时暴露）：

1. 注册一个全新账号（邮箱验证码或 OAuth），确认返回 id 为 36 字符 uuid；
2. 走 PAT/OAuth 绑定转移核心链路（binding_ticket save→确认→consume），确认零 410（不出现「确认会话已过期」）；
3. 触发一次凭据使用（如拉取仓库列表），确认零 500，`credential_usage_log` 落审计行且 `user_id`/`initiating_actor_id` typeof=text；
4. 自动化底线：`pytest tests/test_schema_fk_type_invariant.py tests/test_new_user_uuid_lifecycle.py` 全绿（不变式守卫+新用户闭环回归，加表漏同步 TEXT 化清单必红）。
