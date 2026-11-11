# 生产部署 Checklist——MySQL 首启回填观察（D96）

> 适用：首次部署 / 存量库升级到含 N1-N4 迁移（版本 202611070001-0003）的版本。
> 校验脚本：`scripts/check_deploy_backfill.py`（只读诊断，零写入零修复）。
> 方案：deploy-checklist-design.md（v2）。

**残余风险声明**：回填步骤失败只吞 warning、服务照常启动——本 checklist 是
唯一防线，**运维不执行时失败是静默的**。因此校验脚本应作为部署流程的
**强制步骤**（非可选），每次部署后执行并把结果记入文末滚动观察表。

**下线判据（统一 P-8 的 4 周口径）**：连续 4 周（滚动观察表连续 4 周记录）
`backfill: N rows`（N>0）零出现且 `backfill failed` 零命中 → 迁移完成，
可删迁移模块（`src/user/credentials_backfill_n1.py` + init_db 调用行）。

---

## 第 0 项（前置条件，非观察项）：备份已确认

- [ ] DB 备份已完成且时间戳在本次部署之前（步骤 2「user_credentials 明文
  加密覆写」是**破坏性写操作**，回滚锚点仅有 DB 备份）
- [ ] 备份可恢复性已知（恢复流程/上次恢复演练时间）

## 一、首启观察

部署启动后执行：

```bash
GRAPE_CHECK_DB_URL='<生产库URL>' python3 scripts/check_deploy_backfill.py
echo $?   # 0=全绿 1=发现失败 2=无法检查
```

- [ ] 脚本 exit=**0**（全绿）→ 跳到「二、二启收敛验证」
- [ ] 脚本 exit=**1** → 按 FAIL 行内处置指引操作（确认密钥 → 重启幂等续跑
  → 复跑脚本），然后进「二」
- [ ] 脚本 exit=**2** → 环境问题（连接/认证/超时/URL 未设），先解决环境，
  **注意表/列不存在不是 2 是 1**（迁移未跑）
- [ ] 版本表盖章齐全（脚本 [1] 项）——**仅辅助信号**：证明迁移 DDL 已触发，
  不证明数据回填成功，不可据此收工
- [ ] 首启日志检查：
  - `grep -c 'backfill failed' <log>` → 应为 0；>0 则按处置指引排查
  - `grep 'backfill: [1-9]' <log>` → 存量库首启应出现（正在回填）；全新库
    应无

## 二、二启收敛验证（判定树，防机械误判）

重启服务后复跑脚本：

- **首启全绿（无 warning）** → 二启必须全绿，且日志 `backfill: N rows`
  零出现 → **迁移完成**，记入滚动观察表，进入 4 周观察期
- **首启有 `backfill failed` warning** → 二启**允许** `backfill: N rows`
  出现（这是幂等续跑，不是幂等失败）→ 按 FAIL 指引排查修复后，**三启起
  必须零出现** → 全绿后记入滚动观察表

## 三、完成判定（权重写死）

| 信号 | 权重 |
|---|---|
| 明文残留 = 0 且 空 id 残留 = 0 且 `backfill failed` 零命中 | **完成判据（三项全绿才可收工）** |
| 版本表三行盖章在册 | 辅助信号（仅证明迁移已触发） |

**警告**：看到版本表三行在册就收工是最危险状态——数据回填失败被吞
warning 后服务照常起，版本表不受影响。

## 四、FAIL 处置指引

| FAIL 类型 | 下一步 |
|---|---|
| 明文残留（user_credentials） | 确认 `GRAPE_CREDENTIAL_KEYS` 已配置且与首启一致 → 重启服务（`backfill_encryption` 幂等续跑）→ 复跑脚本 |
| 明文残留（grape_t_repo_credentials） | 同上（`backfill_repo_credentials_encrypt`） |
| 空 id 残留 | 同上（`backfill_uuid` / `backfill_repo_credentials_uuid`——四条回填日志形态：`user_credentials UUID backfill: N rows` / `user_credentials plaintext encryption backfill: N rows` / `grape_t_repo_credentials encryption backfill: N rows` / `grape_t_repo_credentials UUID backfill: N rows`） |
| 版本表缺盖章 / 表列不存在 | 查启动日志迁移执行段（safe 失败响亮报错不吞）→ 修复后重启即补跑（迁移幂等） |
| `backfill failed` warning 命中 | 按日志步骤名定位；翻译层/方言问题查 `src/mysql_compat.translate_sql` 对该 SQL 形态支持（894bf7626 已修 IN 子查询 LIMIT 等，新形态需扩展翻译层） |

## 五、滚动观察表（承载 4 周时序判据）

每次部署/巡检执行脚本后追加一行。**连续 4 周三零 → 迁移完成**。

| 周次 | 日期 | 脚本 exit | 明文残留 | 空 id 残留 | `backfill failed` 命中 | `backfill: N>0` 出现 | 执行人 |
|---|---|---|---|---|---|---|---|
| W1 | | | | | | | |
| W2 | | | | | | | |
| W3 | | | | | | | |
| W4 | | | | | | | |
| 结论 | | | ☐ 连续 4 周三零，迁移完成，可下线迁移模块 | | | | |

（续表按需复制；任何一周非三零则观察期重新起算。）

## 六、沿用观察点（D91/D92 既有告警，纳入同一面板）

- `historical column fallback hit`（挂账 #12 读侧回落判据，下线条件另行评审）
- `FILE fallback`（隐形第二权威源，清零是文件 fallback 下线判据之一）
- `global bot token missing`（GlobalBot 缺失告警）

## 七、外部消费方同步改造（grape-admin，强制项）

共享同一 MySQL 库的 grape-admin（Java）直接裸 SQL 读写 grape 的表，grape 侧
迁移/守卫覆盖不到它。本次变更涉及账户与仓库主键收敛（D105 UUID、user_accounts
死表收敛、user_repos.user_id 重映射），上线前必须同步核对：

- [ ] `console` 用户面：`ConsoleUserController` 的 `user_accounts` 列表/详情
  （`rs.getInt("id")`、`u.id = ur.user_id` 内连接）仍按旧表读——agent 侧若不再写
  `user_accounts`，admin 读到的将是**陈旧快照**（不是报错，是静默失真）
- [ ] 强制下线：admin `POST /{id}/reset-token` 写 `user_accounts.token_version`；
  grape 侧若已改读 `grape_accounts`/凭据域，该写**静默失效**（用户不会被踢下线）
- [ ] 统计面：`SELECT COUNT(*) FROM user_accounts` 类聚合会随旧表冻结而失真
- [ ] 联动确认：`managed_repos` / `user_repos` / `runtime_credentials` 的引用列
  类型改为 VARCHAR(36) 后，admin 侧按 int 读/拼 SQL 的点位必须逐条改（当前 dev
  分支未发现 `getInt("user_id")`，但新增代码需纳入评审）
