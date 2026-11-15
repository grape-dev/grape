# 跨域读清单扫描器：判定口径、用法与基线维护

> Issue #2504 工具层交付：扫描器 `scripts/p4/scan_cross_domain_reads.py` +
> 基线棘轮 `scripts/p4/read_inventory_baseline.json` + `scripts/check-all.sh`
> 门禁钩子。基线在纯净 base（arch-weekly-11）上重新生成：**total 0 / bare 0**
> ——base 已用 `src/domains/*/public` 具名查询（`read_one_any`/`read_all_any`/
> `exec_any`）+ `src/kernel/domain_read.py` 按 DSN 判定的门面把跨域读清零，
> 棘轮自此从 0 起跳，任何新增跨域裸读都会让 `--check` 变红。
>
> 历史判定表（B8 共享桶 292/292 逐点判定表、B9 base 对齐后重算清单）**归档为
> 历史，不在本工具层维护**：见已弃用 PR #1411 的 `docs/lib-3db/read-inventory-diff.md`
> （git 历史 `25b0a5395` / `1dcc6bd57` 可查）。

## 1. 判定口径

| 概念 | 判据 |
|---|---|
| 读点（site） | `<cursor>.execute(<SQL>, ...)` / `executemany` 调用，SQL 以 SELECT/WITH 开头，且引用 ≥1 张「表归属清单（`configs/table_ownership.json`）」中归属 ≠ 宿主域 的表 |
| 宿主域（host） | `src/domains/<d>/` → 桶=域=<d>（进程归属固定）；`src/api/`、`src/kernel/` 与 `src/domains/contracts/`（GSB provider 本地实现，多进程注册加载）被多进程同时加载 → 域未知、桶单列 `api`/`kernel`，任一注册表都算跨域，stats 单列 shared_scope 不并入 W1 口径；其余前缀（scripts/、tests/、src 顶层）显式跳过不入清单 |
| 外域表 | SQL 文本 FROM/JOIN 语法位上出现的、在 `configs/table_ownership.json` 有归属的表名（复用 `src/kernel/db.py` 的表名语法位口径）；字符串以 f-string/拼接静态部分为准 |
| converted（已接线） | 外域表所属域 d 在宿主函数内有 lib 句柄（`x = <d>_lib()` / `from src.domains.d import public as x` / 同文件工厂），**且**本 execute 位于该句柄 guard 的兜底分支（`if 句柄 is not None: ... else: <execute>` 的 else，或 `if 句柄 is None:` / `if not 句柄.available():` 的 body，或早退守卫 `if 句柄: ... return ...` 之后同函数尾段）——兜底 SQL 属回滚通道，非欠账 |
| bare（裸读，真欠账） | 其余读点：无句柄，或句柄存在但 execute 不在 guard 兜底分支（lib 分支盖不住这条 SQL） |
| auto | SQL 全静态 + 紧随语句为 `X = <同游标>.fetchall()/fetchone()` 赋值——可机械改写为命名查询 |
| manual-动态列名 | SQL 动态部分出现在 SELECT 列表段（FROM 之前） |
| manual-动态 IN | SQL 动态部分出现在 `IN (` 之后（占位符列表拼接） |
| manual-动态其他 | 其余动态 SQL（表名/排序/条件动态） |
| manual-非 fetch 消费 | SQL 静态但消费形态非「fetch 赋值」：`for r in c.fetchall()`、`c.fetchone()` 内联表达式、聚合/子查询消费等 |

优先级：动态 > 非 fetch > auto（一处读点只归一类）。

## 2. 已知口径边界（静态盲区，靠 review/门禁兜底）

- `c.execute(sql_var)`（SQL 全在变量里）无法静态提取表名 → 不计入（如需覆盖须做过程间常量传播）；
- 海象赋值（`x := <d>_lib()`）、lambda 体内句柄引用、跨文件句柄传递不识别（与 `check_lib_query_names.py` 已知盲区一致）；
- 直连 public 模块的 `import src.domains.<d>.public as x`（整模块 import 形态）不识别别名；
- 动态分类按占位符位置判定（列段/IN 段），极少数含子查询的列段 SQL 可能归 `manual_dynamic_other`；
- 宿主桶口径：`src/domains/contracts/` 显式归 `kernel` 共享桶；scripts/、tests/、src 顶层等未知前缀**显式跳过**，不静默落入 kernel 桶；
- `--check` 门禁比对键 = (file, line, tables)，并对键上 status/classification 显式比对：**同位置 converted↔bare 翻转（如删除兜底 guard）与分类漂移均 FAIL**（含 `--json --check`，退出码 1）。

## 3. 扫描器用法

```bash
python3 scripts/p4/scan_cross_domain_reads.py                 # Markdown 清单（按表分组）
python3 scripts/p4/scan_cross_domain_reads.py --json          # 机器可读 JSON（CI 归档）
python3 scripts/p4/scan_cross_domain_reads.py --check          # 对照基线快照（check-all 门禁）
python3 scripts/p4/scan_cross_domain_reads.py --table <表名>    # 单表查询（表须在归属清单登记）
python3 scripts/p4/scan_cross_domain_reads.py src tests        # 指定路径
python3 scripts/p4/scan_cross_domain_reads.py --update-baseline  # 重扫结果写为基线快照（维护流程，见 §4）
```

退出码：`0` = 清单产出（`--check` 时与基线一致）；`1` = `--check` 与基线不一致（回归）；
`2` = fail-closed（没扫到文件 / 表归属清单缺失 / 未知表名 / 互斥旗标组合）。

互斥：`--check` / `--update-baseline` / `--table` 至多其一（过滤视图不得喂门禁/基线）；
`--json` 不与 `--update-baseline` 同用（更新基线为静默维护流程）。

## 4. 基线维护流程

1. **常规变更零维护**：读点位置/行号漂移不触发门禁（比对键 = file/line/tables，
   键稳定即 PASS）；`scripts/check-all.sh` 中的 `--check` 钩子保证清单不静默漂移。
2. **确实改变跨域读集合时**（新增/收口跨域读点、表归属变更）：
   `python3 scripts/p4/scan_cross_domain_reads.py --update-baseline` 重扫落盘，
   并在 PR 描述中逐条说明读点增/减/状态翻转的理由；
3. **禁止**：手工编辑 `read_inventory_baseline.json`（须由 `--update-baseline`
   生成，保证 stats 与 sites 同源）；
4. 门禁红了先看差异清单（`--json --check` 可见全量），确认属于有意变更才走 2，
   否则按新增裸读回归处理（收口后基线回落）。
