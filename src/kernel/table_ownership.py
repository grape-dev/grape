"""表归属守卫：进程只允许创建/修改"自己域"的表（防止拆库后四进程各建一份全量表）。

依据：`configs/table_ownership.json`（由 information_schema + 人工裁决生成，随仓维护）。

设计要点：
- **未知表放行**：清单里没有的表不拦（兼容新增表；随后由清单刷新纳入）
- **可关闭**：`GRAPE_DDL_OWNER_GUARD=0` 时完全透传（应急/单机调试）
- **只读语义**：本模块只做判定，不执行任何 DDL
- 域判定：优先 `DEVOPS_DB_NAME`（grape_dev_mgmt → mgmt），否则 `GRAPE_PROCESS_ROLE`
"""
from __future__ import annotations

import json
import os
import pathlib
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


def _domain_from(name: str) -> str | None:
    """从库名/角色名推断域。"""
    low = (name or "").strip().lower()
    if not low:
        return None
    if low.startswith("grape_") and low.endswith("_db"):
        low = low[len("grape_"):-len("_db")]
    if low.startswith("grape_dev_"):
        low = low[len("grape_dev_"):]
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