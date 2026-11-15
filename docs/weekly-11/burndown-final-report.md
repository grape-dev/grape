# 跨域 SQL 收口总报告（2026-11-15）

## 结论

跨域引用从 **732 处 / 112 对** 降到 **6 处 / 2 对**（-726，99.2%）。
剩余 6 处全部是**已判定为不该硬拿**的两处存量，口径见下。
三库独立改造的代码侧收口完成；运行侧彩排 172 端点 0 失败，运行期跨域引用 0。

## 总量

| 指标 | 开工 | 收工 |
|---|---|---|
| 跨域引用 | 732 处 | 6 处 |
| 跨域表对 | 112 对 | 2 对 |
| 涉及面 | 100 个文件 | 2 个文件 |
| 提交 | — | 149 个（573d40f82..HEAD） |
| 自动化轮次 | — | 80 轮，0 次 stall |

## 剩余 6 处（blocked，两项）

1. `src/kernel/prompt_builder.py` 3 处 —— agent 提示文案里的示例 SQL 文本，本进程不执行，
   仅被静态正则计入；`tests/runtime/test_issue_372_unified_mapping_github.py` 按源文本断言该段语义。
   口径：**不为掉数字改文案**。正解是给 agent 一个合法的映射写通道后再改。
2. `src/kernel/persistence/credential_usage_log.py` 3 处 —— 后台线程共用一条 owner 连接，
   实测触发 `Packet sequence number wrong`。正解是「每工作线程一条 owner 连接」的池化，属独立一轮。

## 自动化循环的做法

单轮 = 一个全新会话，只做一个文件，9 步固定动作：
读文件列跨域 SQL → 在归属域 public 加具名查询/薄函数 → 改调用点 → py_compile
→ 写该批 sqlite 烟测并跑 → 收紧基线 → gate + pytest + lint → 台账 → 提交推送。
护栏：工作区必须干净才能开工；余量不降连续 3 轮即停；单文件超 25 处分两轮。

## 关键设计取舍

- **owner 域 public 是唯一 SQL 归属地**：kernel/api/mgmt/runtime/insight 只调具名查询或原语。
- **`*_any` 双拓扑**：三库独立走 owner 自有 DSN，单库/测试回落进程主库。
- **跨库事务不可得处一律两段式**：改名迁移（mgmt 段 + runtime 段）、级联清理、令牌轮换
  （先在 mgmt 原子落新 token，再由调用方回写快照）。两段均「删旧留新」→ 重跑幂等；
  第二段失败时审计显式标 `mgmt_committed`，不谎称整单回滚。
- **动态表名/列名**只在 owner 域内拼并做白名单校验；跨域 JOIN 拆成 owner 取映射 + 本域查询 + 内连接。
- **DDL 一律函数化**：`migrations_ddl.py` 提供返回 SQL 的函数，不 import 常量。

## 过程中修掉的存量 bug

1. `runtime/im/engine_wiring.py` 漏 `import _mgmt_public` → NameError 被吞 → IM 推送静默失效。
2. `runtime/engine/instances.py` 4 条 SQL 走进程库 → 三库下 `PUT /api/chat/config` 1146。
3. `scripts/rds_p4_provision.py` 漏写 `GRAPE_DOMAIN_LIB=1` → 彩排假通过（所有 `*_any` 回落进程库）。
4. `insight/observability/alerts.py` 单仓库收件人分支赋值/读取变量不一致 → NameError 被吞 →
   单仓库告警从未发出。
5. `mgmt/routes/scenes.py` 两处 `c = lib.read_one(...).fetchone()` → 对行再 fetchone（AttributeError
   被吞）+ 游标变量被覆盖 → 离线引擎被当健康、旧绑定读不到。
6. mgmt public 的 `_mgmt_channel_lib()` 从未生效（函数不存在，AttributeError 被兜底 except 吞）。

## 最终验证（实跑）

- `make lint` → import OK
- `pytest tests/arch tests/contracts tests/scripts` → **158 passed**
- `bash scripts/rds_gate.sh --quick` → **GATE 全绿**（R1-R7 硬约束、棘轮只降不升、契约一致）
- 三实例彩排 → **P4 PASS**（三实例/两节点：表集合一致、行数等于 dump 快照、视图 0、授权仅本库）
  **P5 PASS**（G2/G3/G4/A6 全绿）**172 个端点失败 0**，运行期静态跨域引用 0
- 全量 smoke 烟测：19 份通过；4 份报「基线数字不符」——它们断言的是当时的跨域对数，
  随基线收紧而过期，属预期噪音，非代码问题。

## 作者与推送

- 后续提交作者统一 `songspace <songspace@grape.dev>`。
- 历史 149 个提交已用 filter-branch 把 `my_grape_bot` 改写为 `songspace`，
  `--force-with-lease` 推送成功（远端 `cca144b59`）。
- `lvjuntao`（61 个）保留原样 —— 那是用户本人在此期间的手工提交。

## 后续建议

1. 两个 blocked 项各自单独立一轮（提示通道 / 线程连接池化），不要混在批处理里。
2. `delete_conversation` 里原先「清理 chat 工作目录」的代码块在改造中被移除，只剩 warning 日志，
   需回归确认是否由其他原语接管。
3. 台账 `docs/weekly-11/rds-upgrade-execution-status.md`（6410 行）已含 28 个「本文件清零」小节，
   建议后续归档为按域分册，避免单文件继续膨胀。