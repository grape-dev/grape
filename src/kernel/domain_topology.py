"""域拓扑自检：三库模式下的「跨域依赖是否齐备」。

为什么需要（2026-11-20 实锤）：`db_registry.domain_db()` 在缺 `GRAPE_DSN_<DOMAIN>`
时返回 None，调用方一律走了"回落到进程主库"的降级分支。三库拓扑下这等于读
另一张同名 **VIEW**（列快照），基表加列即 1054 → `/api/dashboard/{owner}/{repo}`
全站 500。降级本身没错（单库/共库拓扑就该回落到同一个库），错的是**没有自检**：
配置漏一项，代码静默换路径，症状在业务层才爆。

本模块只做判定，不做连接：给定环境变量回答"这次启动的跨域接线是否完整"，
不完整就拒绝启动（fail-closed）。
"""
from __future__ import annotations

import os

DOMAINS = ("mgmt", "runtime", "insight")
DSN_ENV = {d: f"GRAPE_DSN_{d.upper()}" for d in DOMAINS}
DB_ENV = {d: f"GRAPE_{d.upper()}_DB_NAME" for d in DOMAINS}


def _db_from_dsn(dsn: str) -> str:
    """从 DSN 取库名：mysql://u:p@h:3306/<db>?charset=… / postgres://…/<db>。"""
    raw = (dsn or "").strip()
    if not raw or "://" not in raw:
        return ""
    tail = raw.split("://", 1)[1]
    if "/" not in tail:
        return ""
    path = tail.split("/", 1)[1]
    return path.split("?", 1)[0].split("/", 1)[0].strip()


def domain_dbs() -> dict[str, str]:
    """env 里声明的域库名（未声明的不出现）。

    两处来源都要认：`GRAPE_<DOM>_DB_NAME`（显式库名）与 `GRAPE_DSN_<DOM>`（连接串）。
    只认前者时，按 DSN 接线的部署会判定成单库 → 三库安全网（启动拒绝/视图自检/
    跨域回落告警）全部静默失效，正是这道自检要防的"配置漏一项、代码静默换路径"。
    """
    out: dict[str, str] = {}
    for dom, key in DB_ENV.items():
        name = (os.environ.get(key) or "").strip()
        if not name:
            name = _db_from_dsn(os.environ.get(DSN_ENV[dom]) or "")
        if name:
            out[dom] = name
    return out


def own_db() -> str:
    return (os.environ.get("DEVOPS_DB_NAME") or "").strip()


def own_domain() -> str:
    """进程自己的域：优先显式 GRAPE_PROCESS_DOMAIN，否则按库名反查。"""
    explicit = (os.environ.get("GRAPE_PROCESS_DOMAIN") or "").strip().lower()
    if explicit in DOMAINS:
        return explicit
    db = own_db()
    for dom, name in domain_dbs().items():
        if name == db:
            return dom
    return ""


def multi_db_mode() -> bool:
    """声明了 ≥2 个不同的域库名 → 三库（多库）拓扑。"""
    return len(set(domain_dbs().values())) >= 2


def missing_dsns() -> list[str]:
    """多库拓扑下，本进程需要读、却没给 DSN 的域（按 DOMAINS 顺序）。"""
    if not multi_db_mode():
        return []
    dbs = domain_dbs()
    own = own_domain()
    missing: list[str] = []
    for dom in DOMAINS:
        if dom not in dbs or dom == own:
            continue
        if not (os.environ.get(DSN_ENV[dom]) or "").strip():
            missing.append(dom)
    return missing


def wiring_problems() -> list[str]:
    """启动前需要人工修的问题清单（空列表 = 接线完整）。"""
    if not multi_db_mode():
        return []
    own = own_db()
    if not own:
        return ["DEVOPS_DB_NAME 未设置：无法判定本进程的域库"]
    if not own_domain():
        return [f"DEVOPS_DB_NAME={own} 不在声明的域库集合 {sorted(domain_dbs().values())} 内"]
    problems = []
    for dom in missing_dsns():
        problems.append(
            f"缺 {DSN_ENV[dom]}：跨域读 {dom} 会回落到进程库 {own}"
            f"（应为 {domain_dbs()[dom]}），同名 VIEW 列漂移即 1054/1146"
        )
    return problems


def assert_cross_domain_wiring() -> None:
    """多库拓扑接线不齐 → 拒绝启动（exit 6，与 db-guard 的错库 exit 5 区分）。

    单库/共库拓扑（域库名相同或未声明）直接返回：那本来就是同一个库。
    显式放行：GRAPE_CROSS_DOMAIN_STRICT=0（仅供单库迁移期/离线脚本使用）。
    """
    if (os.environ.get("GRAPE_CROSS_DOMAIN_STRICT") or "1").strip() == "0":
        return
    problems = wiring_problems()
    if not problems:
        return
    from src.kernel.config import log

    log.error("[db-guard] 跨域接线不完整，拒绝启动：")
    for p in problems:
        log.error(f"[db-guard]   - {p}")
    raise SystemExit(6)