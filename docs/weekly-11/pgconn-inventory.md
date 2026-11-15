# 进程库连接清单（分库后的「连接走错库」风险面）

> 配套脚本：`scripts/scan_pgconn.py`（含 `--check` 棘轮）
> 基线：`scripts/pgconn_baseline.json`
> 判定依据：`configs/table_ownership.json`

## 1. 为什么需要这份清单

三库独立后，每个进程只认自己的库（`DEVOPS_DB_NAME`），跨域访问必须走 owner 域的
lib / DSN（`src.kernel.domain_db.cursor()`、`db_registry.cursor()`、`*_any_*`、`owner_db`）。

但域代码里仍可能有这样一类残留：

```python
from src.kernel.session_manager import db, db_lock   # ← 进程库连接

with db_lock:
    c = db.cursor()
    c.execute("SELECT ... FROM <本域表> ...")         # ← 本域 SQL，但走进程库
```

这段代码在**本域进程**里完全正确；一旦被其它进程加载（例如 mgmt 进程同时挂载
runtime / insight 的路由、IM 引擎与调度），同一个游标就指向了**别的库**：

- 目标库里若存在同名跨域视图 → 读到的是**旧副本**（静默错值，最危险）；
- 不存在视图 → `1146 Table ... doesn't exist`（会报错，容易发现）。

静态跨域扫描（`scripts/foreign_table_refs.py`）抓不到这一类：它比较的是
「文件所在域 vs 表归属」，而这里两者相同，**只有连接是进程库**。

## 2. 判定口径（与脚本一致）

| 概念 | 判据 |
|---|---|
| 站点 | `<cursor>.execute / executemany` 调用点 |
| 连接来源 | 游标由 `src.kernel.session_manager` 的 `db` 派生（含 `db.cursor()`、`with db_lock` 内的 `c = db.cursor()`） |
| 表 | SQL 文本命中「本域自有表」（`configs/table_ownership.json` owner == 文件域） |
| 排除 | `src.kernel.domain_db.cursor()`、`db_registry.cursor()`、`*_any_cursor()`、`read_one_any/read_all_any`、`owner_db/db_pair/owner_lock` |
| 风险域 | 文件域 ∈ {runtime, insight}（mgmt 域代码只在 mgmt 进程执行，库一致；event/worker 进程的库就是 runtime） |

## 3. 现状（复审 R2 修正后的实测）

```
进程库 SQL 执行点（src/domains/**）        543
  其中风险域 runtime                      277
  其中风险域 insight                        5
  合计风险                                  282
```

首批修复已并入：`src/domains/runtime/scene_precheck.py` 2 处（引擎在线查询 + 健康分）。
修复前 284 → 修复后 282，基线已同步。

> **复审 R2 口径修正**：模块环境不再混入函数体赋值（旧实现存在作用域混叠），
> 改为显式「参数来源传播 + 函数内别名导入识别」后，**总量 282 不变，分布有修正**：
> `engine/direct_executor.py` 3→10、`engine/scheduler.py` 14→15（旧算法漏计，实为 pg 站点）；
> `engine/recovery.py` 18→12、`routes/chat.py` 15→13（跨文件传参的游标来源单文件不可判定，
> 见 §6）。基线已按新口径刷新。

风险文件 top（处数）：

| 文件 | 处 |
|---|---:|
| src/domains/runtime/routes/runtime.py | 60 |
| src/domains/runtime/engine/cleanup.py | 16 |
| src/domains/runtime/engine/scheduler.py | 15 |
| src/domains/runtime/routes/webhook.py | 15 |
| src/domains/runtime/im/vod_handler_async.py | 14 |
| src/domains/runtime/engine/engine_select.py | 13 |
| src/domains/runtime/im/handler_async.py | 13 |
| src/domains/runtime/routes/chat.py | 13 |
| src/domains/runtime/routes/tasks.py | 13 |
| src/domains/runtime/engine/recovery.py | 12 |

完整清单：`python3 scripts/scan_pgconn.py --csv /tmp/pgconn.csv`

## 4. 优先级与分批修复排序

排序原则：**先「会静默读到旧副本」的读路径，再写路径；先入口/高频文件，再低频后台**。
静默错值（视图旧副本）比 1146 报错更危险，mgmt 进程挂载的路由优先于 worker 后台。

| 批次 | 范围 | 判定 | 理由 |
|---|---|---|---|
| P0（已完成） | `scene_precheck.py` 2 处 | 已修 | #2524 现场症状（建单预检把在线引擎全判离线），mgmt 进程高频读 |
| P1 | `routes/`（runtime 60、chat 13、webhook 15、tasks 13 等） | mgmt 进程挂载的路由读路径 | 静默旧副本直接面向用户请求，风险最高 |
| P2 | 引擎读/判活路径：`engine_select.py` 13、`recovery.py` 12、`local_claimer.py` 9、`transient_retry.py` 9 | 引擎状态判定 | 判活/认领读到旧水位会误判引擎离线、重复认领 |
| P3 | `im/`（feishu_async 11、handler_async 13、vod_handler_async 14、bot_lease 8） | IM 回调路径 | mgmt/worker 混跑进程中的回调读，量中等 |
| P4 | 引擎写路径：`cleanup.py` 16、`scheduler.py` 15、`direct_executor.py` 10 | 写事务 | 写路径改连接需连带核对 autocommit 语义（§4 模板注意点 1），单独分批回归 |

每批完成后：`--update-baseline` 刷新棘轮 + 挑 1-2 个真实请求做「owner 实例数据一致性」验证。

## 5. 修法模板

```python
# before
from src.kernel.session_manager import db, db_lock

def f(...):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT ... FROM agent_runtimes ...", (x,))

# after（有自有 DSN 走自有实例，没有则回落进程库，回滚安全）
from src.kernel.domain_db import cursor

def f(...):
    with cursor("runtime") as c:
        c.execute("SELECT ... FROM agent_runtimes ...", (x,))
```

注意：
1. 写路径的提交语义不同——`domain_db` 连接是 autocommit，进程库需要显式 `commit()`；
   涉及事务的写仍应走 owner 域 public 原语，而不是直接 `cursor()`。
2. 若该文件已经用 `owner_db/db_pair("<域>")`，无需改动（脚本已排除）。
3. 改完用真实请求验证：数据应与 owner 实例一致（例如心跳时间戳为当前量级），
   而不是分库切换时的历史水位。

## 6. 棘轮（防止继续扩散）

```bash
python3 scripts/scan_pgconn.py --check          # 新增/上升 → 退出码 1
python3 scripts/scan_pgconn.py --update-baseline  # 数量下降后刷新基线
```

基线按「文件 → 处数」记录：允许下降、禁止上升；新增文件即视为上升。建议接入
`scripts/check-all.sh` 或三库收口门禁（`scripts/rds_gate.sh`）。

## 7. 已知局限

- 静态扫描看不到动态拼接的 SQL（表名/列名变量）；
- 游标来源为函数内启发式追踪，`**kwargs` 透传游标可能误判；
- 计数是代码点，不等于运行命中；实际触发取决于该模块在哪些进程被执行。

**表名匹配为子串启发**（复审 R2 记录）：判定 SQL 是否引用本域表用的是
`空格表名空格 / 反引号 / 表名(` 三种子串匹配，无词法锚点——
表名若是其它标识符的前缀（如列名 `meta_status` 对表 `meta`、函数调用
`get_user(` 对表 `user`）存在**误匹配**可能；反之 SQL 若是 f-string
（`ast.JoinedStr`），插值段（`FormattedValue`）在拼接时被丢弃——
表名仅出现在插值处的会**漏报**。两类的净效应都是计数偏差而非误修，
随修复落地（计数降为 0）自然消解。

**跨文件传参不可判定**（复审 R2 记录）：游标作为实参传给**另一个文件**的函数
（如 health.py 调 recovery.py 的 `note_spawn_failed(c, ...)`）时，单文件静态分析
无法确定来源，此类站点不计入（本文件内传参已用不动点传播覆盖）。
计数偏保守（漏计），修复时按 §3 文件清单逐文件核对可兜住。

**函数内同名变量后写覆盖**：同一函数内多个代码块复用变量名（如反复 `c = xx.cursor()`）
时，来源按遍历序后写覆盖——同函数内混用进程库与域游标且同名时可能误判，
要求修复时整函数核查（不应只看扫描器标记的行）。