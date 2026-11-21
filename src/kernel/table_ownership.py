"""表归属守卫：进程只允许创建/修改"自己域"的表（防止拆库后四进程各建一份全量表）。

依据：`configs/table_ownership.json`（由 information_schema + 人工裁决生成，随仓维护）。

设计要点：
- **未知表放行**：清单里没有的表不拦（兼容新增表；随后由清单刷新纳入）
- **可关闭**：`GRAPE_DDL_OWNER_GUARD=0` 时完全透传（应急/单机调试）
- **只读语义**：本模块只做判定，不执行任何 DDL
- 域判定：优先 `DEVOPS_DB_NAME`（<db_mgmt> → mgmt），否则 `GRAPE_PROCESS_ROLE`
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "configs" / "table_ownership.json"
_VALID_DOMAINS = ("mgmt", "runtime", "insight")

_cache_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = 0.0


def _load_manifest() -> dict:
    """带 mtime 的懒加载（改清单无需重启进程）。"""
    global _cache, _cache_mtime
    if not _MANIFEST.is_file():
        return {"tables": {}, "views": {}}
    try:
        mtime = _MANIFEST.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _cache_lock:
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        try:
            data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 清单损坏不能阻断启动
            data = {"tables": {}, "views": {}}
        _cache = {"tables": data.get("tables") or {}, "views": data.get("views") or {}}
        _cache_mtime = mtime
        return _cache


def owner_map() -> dict[str, str]:
    """返回 {table: owner_domain}，供脚本/测试统一取归属。

    唯一权威是 configs/table_ownership.json。原 configs/table_ownership.json 已合并进来并删除
    （两份清单曾漂移 19 张：json 有、tsv 无，其中 grape_t_oauth_channels /
    devops_agent_runtime_binding / grape_t_category_dict 三张是生产代码在用的）。
    """
    return dict((_load_manifest().get("tables") or {}))


def _domain_from(name: str) -> str | None:
    """从库名/角色名推断域。"""
    low = (name or "").strip().lower()
    if not low:
        return None
    if low.startswith("grape_") and low.endswith("_db"):
        low = low[len("grape_"):-len("_db")]
    for dom in _VALID_DOMAINS:
        if low == dom or low.endswith("_" + dom) or low.startswith(dom + "_"):
            return dom
    return None


def current_domain() -> str | None:
    for key in ("DEVOPS_DB_NAME", "GRAPE_PROCESS_ROLE"):
        dom = _domain_from(os.environ.get(key, ""))
        if dom:
            return dom
    return None


def db_name_domain() -> str | None:
    """仅按库名判定域（不看进程角色）。

    返回 None 表示「单库/共享库」——此时每个进程都是该库的 owner，应正常建表。
    分库模式下按库名识别 owner，用于把建表收敛到 owner 进程
    （`current_domain()` 会回落到角色，单库部署下会误判成"非 owner"）。
    """
    return _domain_from(os.environ.get("DEVOPS_DB_NAME", ""))


def guard_enabled() -> bool:
    """仅在 MySQL 分库模式启用。

    - sqlite（单文件、测试、CI）→ 关闭：单库 schema 需要完整建表
    - `GRAPE_DDL_OWNER_GUARD=0` → 显式关闭（应急）
    """
    backend = os.environ.get("DEVOPS_DB_BACKEND", "").strip().lower()
    if backend and backend != "mysql":
        return False
    return os.environ.get("GRAPE_DDL_OWNER_GUARD", "1").strip().lower() not in ("0", "false", "off")


def owner_of(table: str) -> str | None:
    return _load_manifest()["tables"].get((table or "").strip().lower())


def foreign_owner(table: str, domain: str | None = None) -> str | None:
    """返回"别家的域"（若该表不属于当前域）；不属于任何已知情况则返回 None。"""
    if not guard_enabled():
        return None
    dom = domain or current_domain()
    if not dom:
        return None
    owner = owner_of(table)
    if owner and owner != dom:
        return owner
    return None


def should_skip(table: str, domain: str | None = None) -> bool:
    return foreign_owner(table, domain) is not None


def reload_manifest() -> None:
    """测试用：强制下次读取重新加载。"""
    global _cache, _cache_mtime
    with _cache_lock:
        _cache = None
        _cache_mtime = 0.0

# ── 表归属副本审计（2026-11-21）───────────────────────────────────────────────
#
# 问题：契约层是单点的（跨域读只走 owner 域 public），但 DDL 层带历史副本 ——
# 每个进程都曾建全量表，拆库后 `_migration_conn` 在目标域 DSN 不可用时又会回落
# 进程库，于是非 owner 库里留下一张同名 **BASE TABLE**（不是视图）。裸 SQL 查它
# 成功返回 0 行、不报错，会被当成「没配置/数据丢了」（本仓已误判两次）。
#
# 这两个原语只做「发现 + 安全清理」，不改变任何读路径。

#: 表名白名单：MySQL 标识符上限 64 字符（`docs: identifier length limit`），
#: 只允许字母数字下划线 —— 表名虽来自 information_schema，仍按不可信输入校验。
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")

#: 行数未知的哨兵（COUNT 未跑：count_rows=False / 表名不合法 / 查询失败）。
#: 显式具名而不是裸 -1：调用方必须把「未知」与「非空」分开处理 —— 未知不得落
#: empty_copy（否则启动清理会把没数过的副本当空表删掉，可能丢数据）。
_ROWS_UNKNOWN = -1


def audit_foreign_copies(conn, dbname: str, *, count_rows: bool = True,
                        strict: bool = False) -> list[dict]:
    """列出本库里「归别域、但物理上是 BASE TABLE」的副本，并分类。

    - `empty_copy`     副本 0 行 → 数据在 owner 库，可安全清理（`drop_empty_copy`）
    - `divergent_copy` 副本非 0 行 → 可能这个部署的真源就在这（实测：某部署的 rc
                       基表在 runtime 而清单声明 owner=insight），**绝不自动删除**，必须人工裁决
    - `unknown_rows`   行数没数出来（`count_rows=False` / 表名不合法 / COUNT 失败）→
                       同样不得自动删除：没数过不等于空

    视图形态不会出现在结果里 —— 视图转发读到的就是 owner 数据，属正确形态。
    """
    dom = _domain_from(dbname)
    if not dom:
        return []
    tables = sorted(_load_manifest()["tables"].keys())
    if not tables:
        return []
    try:
        rows = conn.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'", (dbname,)).fetchall()
    except Exception as e:  # noqa: BLE001 - 单库/测试环境没有 information_schema
        # QA 第 6 轮 Informational 3(a)：空结果不得冒充「干净」。strict=True 时把
        # 「查不了」显式抛出去，让调用方不盖章、下次重试，而不是打一条 clean。
        if strict:
            raise RuntimeError(f"{dbname}: information_schema 不可用，无法审计: {e}") from e
        return []
    present = {str(r[0] or "").strip().lower() for r in rows}
    out: list[dict] = []
    for table in tables:
        if table not in present:
            continue
        owner = owner_of(table)
        if not owner or owner == dom:
            continue
        n = _ROWS_UNKNOWN
        if count_rows and _SAFE_IDENT.match(table):
            try:
                n = int(conn.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0] or 0)
            except Exception:  # noqa: BLE001
                n = _ROWS_UNKNOWN
        kind = ("empty_copy" if n == 0
                else "unknown_rows" if n == _ROWS_UNKNOWN
                else "divergent_copy")
        out.append({"table": table, "owner": owner, "rows": n, "kind": kind})
    return out


def drop_empty_copy(conn, table: str, *, execute_ddl=None) -> bool:
    """删除一张 **0 行** 的别域副本；非 0 行一律拒绝（数据可能只在这份里）。

    表名来自 information_schema，但仍做标识符白名单校验（防注入/异常名）。

    `execute_ddl`（可选）：把 DROP 交给调用方的统一危险 DDL 入口（过 min_code_version
    门槛 + MDL 处理）。返回 False 表示被门槛拦截 —— 此时抛错，让迁移不盖章、下次重试
    （QA C3：启动清理不能绕过 `_destructive_ddl` 直接发 DROP）。
    """
    if not _SAFE_IDENT.match(table or ""):
        raise ValueError(f"unsafe table name: {table!r}")
    row = conn.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()
    if int((row or [0])[0] or 0) != 0:
        raise RuntimeError(f"refuse to drop non-empty copy `{table}`")
    _ddl = f"DROP TABLE `{table}`"
    if execute_ddl is None:
        conn.execute(_ddl)
    elif execute_ddl(_ddl) is False:
        raise RuntimeError(f"cleanup gate 拦截 DROP（未盖章，下次重试）: {_ddl}")
    return True
