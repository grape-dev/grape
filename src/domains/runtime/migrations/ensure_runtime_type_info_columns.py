"""给 agent_runtimes 补 runtime_type / runtime_info 两列（迁移 202611180002）。

背景（2026-11-18 生产事故，Issue #113 / seq 9946）：

  任务被 claim 后零输出挂死，155 秒后判 failed：
    result_text: claimed but the engine session never started
                 (runtime_session_id empty; the worker never spawned the engine process)
  worker 日志持续刷：
    (1054, "Unknown column 'r.runtime_info' in 'field list'")
    (1054, "Unknown column 'r.runtime_type' in 'field list'")

根因：`7794d05ee`（sandbox + engine-uid overhaul）在 7 处代码里引用了这两列
（engine/service.py 的 claim 查询、runtime.py 的 scope 路由、db_setup 的回填等），
但**从未把列加进表里**，`db_setup.py` 的 agent_runtimes DDL 里也没有它们。

后果不是「列缺失」这么简单：claim 查询是一整条 SELECT，列解析失败 → 1054 →
整个 claim 响应构造失败 → worker 拿不到 runtime 属性 → **引擎进程从未 spawn**。
而 local_claimer 循环也被 `r.runtime_type` 打挂，日志里每 30s 一条 loop error。
表现为「任务显示 running、实时日志空白、最后失败」。

两列语义（照代码用法推定，注释已写在列上）：
  runtime_type  VARCHAR(64)   引擎类型标识；表里已有 engine_type，两者并存不影响读取
  runtime_info  TEXT          云沙箱路由用 JSON（template_id 等）；本机引擎为空 → bwrap

domains=("runtime",)：列在 runtime 库的表上，由 runtime 角色的进程加。
"""
from __future__ import annotations

_COLUMNS = (
    # (列名, 列定义)
    ("runtime_type", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("runtime_info", "TEXT"),
)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool((cur.fetchone() or [0])[0])


def ensure_runtime_columns() -> None:
    """幂等补列：已存在就跳过。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    added = []
    with db_lock:
        c = db.cursor()
        for col, ddl in _COLUMNS:
            try:
                if _column_exists(c, "agent_runtimes", col):
                    continue
                c.execute(f"ALTER TABLE agent_runtimes ADD COLUMN `{col}` {ddl}")
                added.append(col)
            except Exception as exc:  # noqa: BLE001 - 单列失败不阻断另一列
                log.warning(f"202611180002 补列 {col} 失败（跳过）: {exc}")
        db.commit()
    if added:
        log.info(f"202611180002 agent_runtimes 已补列: {', '.join(added)}")
    else:
        log.info("202611180002 agent_runtimes 两列已就位，无需变更")


MIGRATION = {
    "version": "202611180002",
    "name": "ensure-runtime-type-info-columns",
    "kind": "safe",
    "fn": ensure_runtime_columns,
    "domains": ("runtime",),
}
