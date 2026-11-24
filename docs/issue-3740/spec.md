# Spec: Issue #3740 — RepoBotToken legacy 列回落降噪与 bot 凭据回填闭环

> 基线分支：`arch-weekly-11-ljt` | 优先级：P2 | 状态：实现中（M1 代码止血 + M2 回填手段）

## 1. 目标

1. 消除 `RepoBotToken` legacy 列回落 WARNING 洪泛（实测 8,872 条/h，占同期全部告警 46%），恢复告警面可观测性。
2. 让「回填是否已执行」可被代码验证：提供 bot 凭据回填的**可执行路径 + 盖章 + 验证手段**（生产执行由 sre 承接），使 rc（`grape_t_repo_credentials`，`cred_type='bot'`）命中取代 legacy 列回落。

## 2. 现状（已核对，基线 `arch-weekly-11-ljt`）

| 位置 | 事实 |
|------|------|
| `src/kernel/token_resolver.py` `RepoBotToken._fetch()` | 先查 rc（恒 miss）→ 回落 legacy 列 → **每次命中无条件** `log.warning("RepoBotToken: historical column fallback hit (rc miss) …")` |
| 同函数 | 无 `(repo_key, field)` 缓存：一次收尾请求内 3-4 次重复解析 = 3-4 条同仓库同列名日志 + 3-4 行 `credential_usage_log(operation=repo_bot_hit_legacy_column)` |
| `src/api/db_setup.py` | 仅注册了 gitee/gitlab 四列回填迁移（`202611150004`），**gitcode/github 的 `bot_*_token` 列从未有回填入口**；`backfill_repo_credentials_from_columns()` 在历史 squash 合并中丢失（3 个测试模块 import 即 ERROR） |
| 指标面 | 无 repo bot legacy 回落 counter，只能靠日志计数 |

## 3. 范围

### In Scope
1. **降噪**：legacy 命中日志沿用基线 `arch-weekly-11-ljt` Issue #3897 D 组口径——「DEBUG 常态 + 每 `(repo_key, field)` 每小时最多 1 条 WARNING」，日志文本保持原样便于 grep 定位；限频后「持续偶发回落」可能零告警，**日志不再作为下线判据**——判据 = counter 窗口增量 == 0 且 `repo_bot_hit_legacy_column` 审计零命中。本单不另立采样状态（两套状态会让「打不打」出现两处真相）。
2. **指标**：新增 counter `grape_repo_bot_legacy_fallback_total{provider,repo}`，与日志限频解耦、按实际 legacy 列存储读全量计数。
3. **缓存**：`(repo_key, field)` 进程内短 TTL 缓存（TTL ≤60s，可环境变量关闭/调小），缓存最终解析结果（rc 命中 / legacy 命中 / 全空），同请求不再重复解析。
4. **回填路径**：恢复丢失的 `backfill_repo_credentials_from_columns()`；新增 bot 列专用回填 `backfill_repo_bot_token_columns_to_rc()`（幂等、密文归一、可 dry-run）、验证 `verify_repo_bot_credential_backfill()`、盖章 `stamp_repo_bot_credential_backfill()`，并提供 CLI `scripts/backfill_repo_bot_credentials.py`。

### Out of Scope
- 删除 legacy 回落分支 / DROP COLUMN（回填验证前禁止；须按四步 checklist 另单处理）。
- #3713 读侧双轨（换券链可见 rc bot 行、建树链不可见）。
- 写路径置空（每次轮换自动清列）之外的存量列清理。

## 4. 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 降噪策略 | 纯降 DEBUG / 按仓库限频 | 沿用基线 #3897：DEBUG 常态 + 每 `(repo_key, field)` 每小时 1 条 WARNING | 基线已落地的口径，不重复实现；WARNING 面保留 grep 定位能力，全量信息由 counter 承担 |
| 计数与日志的关系 | 日志计数 / 指标计数 | 指标计数（counter 逐次递增） | 日志受限频压制，只有 counter 能承担下线判据与规模观测 |
| 缓存对象 | 仅命中 / 含 miss | 全结果（rc/legacy/全空） | 同请求重复解析连 rc 读与解密都省；miss 也缓存避免重复打库 |
| 缓存可见性 | 进程内 TTL | 进程内 TTL ≤60s + 写路径/过期标记主动失效 | 多进程下轮换延迟 ≤60s（spec 已接受）；同进程轮换即时可见 |
| 审计口径 | 每次解析 / 每次存储读 | **每次实际存储读**（缓存命中不写审计） | Issue 明列「写放大」为待消除项；`repo_bot_hit` 在回填后自然上升（0 → 有）即可满足验收 |
| 回填执行 | 启动自动迁移 / 脚本 | 脚本 + 显式盖章 | spec 明确 M2 由 sre 承接；避免部署即自动写生产数据（回填错写风险 High） |

## 5. 验收标准（映射）

- [ ] legacy 回落日志沿用基线 #3897 限频（DEBUG 常态 + 每小时 1 条 WARNING），日志文本不变（仅作 grep 定位；下线判据 = counter 窗口增量 + legacy 审计零命中）。
- [ ] `grape_repo_bot_legacy_fallback_total{provider,repo}` 可查、按实际存储读全量计数、不随日志限频丢失。
- [ ] 同请求/同 TTL 窗口内重复解析只打一次 DB、只打一条日志（缓存生效）。
- [ ] 回填脚本可 dry-run / apply / verify / stamp；回填后 rc bot 行覆盖全部持有 legacy bot 值的仓库；`managed_repos` 探列失败（`probe_ok=False`）时 verify/stamp 必须失败退出，不得以空缺口伪装通过。
- [ ] 现有测试全绿（含恢复 `backfill_repo_credentials_from_columns` 后此前 import 失败的 3 个模块）。
- [ ] 新增测试覆盖：日志限频与计数解耦（限频窗口内 1 条 WARNING、counter 逐次全量、按仓库隔离）、缓存（命中折叠、TTL 失效、轮换、主动失效）、回填（幂等、dry-run 零写、盖章、验证报告）。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 缓存致凭据轮换延迟 | 中 | TTL ≤60s；rc 写路径 upsert/clear 主动失效；`mark_expired` 失效 |
| 日志限频漏看回落规模 | 中 | counter 全量计数承担观测与下线判据；WARNING 每小时 1 条保留受影响面 grep |
| 回填错写致 token 解析失败 | 高 | `INSERT OR IGNORE` 不覆盖既有 rc 行；解密失败逐行 raw-copy 不阻断；脚本默认 dry-run，须显式 `--apply` |
| 回填误在未备份环境执行 | 高 | 文档/CLI 明确「先备份 + 先 --dry-run + 先 --verify」；执行与验收归 sre |
| 删除 legacy 分支过早 | 高 | 本单不改回落分支；退出 checklist 保持四步 |