"""守卫：迁移的执行域必须与「4 进程 × 3 域 × 表属主」的真实拓扑对齐。

背景（2026-11-20 审计 F2/F3，主人补充「架构是 4 进程形态」）

真源是 `scripts/grape-systemd-run.sh`：
    mgmt    → grape_mgmt_db      → GRAPE_MIGRATION_OWNER=mgmt     （走 init_database）
    event   → grape_runtime_db   → GRAPE_MIGRATION_OWNER=runtime  （run_domain_migrations）
    worker  → grape_runtime_db   → GRAPE_MIGRATION_OWNER=""       （与 event 共库，不跑迁移）
    insight → grape_insight_db   → GRAPE_MIGRATION_OWNER=insight

两处缺省语义**不对称**，这是假绿的根：
- 执行器 `_migration_domain_allows`：`m.domains or ("mgmt",)` → 不写 domains 等价「只在 mgmt 跑」；
- `run_domain_migrations`：`m.domains or ()` → 不写 domains 的迁移在 runtime/insight **永不执行**。
所以「属主不是 mgmt 的迁移」若不显式声明属主域，就会：mgmt 里被 `_owned_skip` 跳过
（`_ensure_column` 默认静默返回 False）却照样盖章，属主域又压根不跑 → 台账有、列没有。

按 4 进程映射逐条判四类违规：
- V1 属主域（非 mgmt）未显式出现在 `m.domains`
- V2 属主域没有对应的「跑迁移的进程」（该迁移永远不会被执行）
- V3 会在非属主进程执行，但 fn 里没有任何存在性/类型探针（缺表即 1146/1347 空转）
- V4 在非属主进程执行且调 `_ensure_column`，却没开 `raise_on_guard=True`（跳过仍盖章）

跨库感知迁移（fn 含 `uses_own_dsn(` / `db_registry.transaction(` / `_migration_conn(`）
**不豁免 V1**（声明域仍要正确），V3/V4 由它们自己的类型探针兑现。

`EXCEPTIONS` 是存量例外登记（不是白名单树洞）：每条写明违规类别与原因，集合必须与实测
违规**逐条相等** —— 修掉一条要同步删一行，新增未登记的一律判红。
"""
import inspect
import json
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
for _p in (ROOT, os.path.join(ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SYSTEMD_SCRIPT = os.path.join(ROOT, "scripts", "grape-systemd-run.sh")
DOMAINS = ("mgmt", "runtime", "insight")

WRITE_DDL = [
    re.compile(r'_ensure_column\(\s*\w+\s*,\s*"([a-z0-9_]+)"'),
    re.compile(r'_owned_skip\(\s*"([a-z0-9_]+)"'),
    re.compile(r"ALTER TABLE\s+`?([a-z0-9_]+)`?"),
    re.compile(r"CREATE TABLE IF NOT EXISTS\s+`?([a-z0-9_]+)`?"),
]
PROBE_MARKERS = ("_base_table_present(", "_table_present(", "information_schema",
                 "PRAGMA table_info", "IF NOT EXISTS")

# 存量例外：version → (违规类别, 原因)。修好一条就删一行（集合必须精确相等）。
EXCEPTIONS = {
    "202611060002": ("V1,V3,V4",
                     "branch_guard_state 由 runtime/schema_ddl.py 建表（owner=runtime），"
                     "本迁移却缺省注册在 mgmt 且 _ensure_column 未开 raise_on_guard —— "
                     "与 F3 同族；整族收敛要连同 takeover 系列一起在三库环境验证"),
    "202611170001": ("V1", "takeover_sessions 同族：声明 mgmt，属主 runtime（列增删分在 0002~0004）"),
    "202611170002": ("V1,V3,V4", "takeover_client_session 列：缺省 mgmt + 未开 raise_on_guard"),
    "202611170003": ("V1,V3,V4", "finish_request 列：缺省 mgmt + 未开 raise_on_guard"),
    "202611170004": ("V1,V3,V4", "result_json 列：缺省 mgmt + 未开 raise_on_guard"),
    "202611170030": ("V1", "平台对象表 owner=insight，声明 mgmt；建表走 CREATE ... IF NOT EXISTS 幂等"),
    "202611170036": ("V1", "grape_t_repo_credentials owner=insight，列放宽幂等"),
    "202611200001": ("V1",
                     "takeover_sessions.user_id 放宽：故意跨库（uses_own_dsn + transaction('runtime')），"
                     "先探 information_schema.columns 再改，缺表即 no-op"),
}


def _processes() -> dict:
    """从 systemd 启动器解析 4 进程真相：module / db / domain / 谁跑本域迁移。"""
    script = open(SYSTEMD_SCRIPT, encoding="utf-8").read()
    modules = dict(re.findall(
        r"^\s+(mgmt|event|worker|insight)\)\s+module=\"([^\"]+)\"", script, re.M))
    dbs: dict = {}
    for roles, name in re.findall(r"^\s+([a-z|]+)\)\s*_db_name=\"([^\"]+)\"", script, re.M):
        for r in roles.split("|"):
            dbs[r] = name
    owners: dict = {}
    for roles, owner in re.findall(
            r"^\s+([a-z|]+)\)\s+export GRAPE_MIGRATION_OWNER=\"([^\"]*)\"", script, re.M):
        for r in roles.split("|"):
            owners[r] = owner

    def _domain(db_name: str) -> str:
        for d in DOMAINS:
            if db_name == f"grape_{d}_db":
                return d
        return ""

    out = {}
    for role in ("mgmt", "event", "worker", "insight"):
        db = dbs.get(role, "")
        out[role] = {"module": modules.get(role, ""), "db": db,
                     "domain": _domain(db), "migration_owner": owners.get(role, "")}
    return out


def _ownership() -> dict:
    with open(os.path.join(ROOT, "configs", "table_ownership.json"), encoding="utf-8") as f:
        return json.load(f)["tables"]


def _write_ddl_tables(fn_src: str, ownership: dict) -> set:
    return {t for pat in WRITE_DDL for t in pat.findall(fn_src) if t in ownership}


def _violations() -> dict:
    from src.api import db_setup

    ownership = _ownership()
    migrators = {p["migration_owner"] for p in _processes().values() if p["migration_owner"]}
    out = {}
    for m in db_setup._MIGRATIONS:
        if not m.fn:
            continue
        try:
            src = inspect.getsource(m.fn)
        except Exception:  # pragma: no cover - 动态/C 扩展函数
            continue
        tables = _write_ddl_tables(src, ownership)
        if not tables:
            continue
        owners = {ownership[t] for t in tables}
        declared = set(m.domains or ("mgmt",))
        executable = {d for d in migrators if d in declared}
        found = set()
        for o in owners:
            if o != "mgmt" and o not in declared:
                found.add("V1")
            if o not in migrators:
                found.add("V2")
        non_owner_exec = {d for d in executable if d not in owners}
        if non_owner_exec and not any(k in src for k in PROBE_MARKERS):
            found.add("V3")
        if non_owner_exec and "_ensure_column(" in src and "raise_on_guard=True" not in src:
            found.add("V4")
        if found:
            out[m.version] = (m.name, sorted(found), sorted(declared), sorted(owners),
                              sorted(executable))
    return out


def test_process_map_matches_four_process_topology():
    """4 进程 × 3 域 × 每域恰好一个迁移执行者（worker 与 event 共库且不跑迁移）。"""
    procs = _processes()
    assert set(procs) == {"mgmt", "event", "worker", "insight"}
    for role, p in procs.items():
        assert p["module"].startswith("src.api.grape_"), f"{role} 缺模块映射"
        assert p["domain"], f"{role} 的库名推不出域（{p['db']}）"

    assert procs["mgmt"]["domain"] == "mgmt"
    assert procs["event"]["domain"] == procs["worker"]["domain"] == "runtime", "event/worker 必须共库"
    assert procs["insight"]["domain"] == "insight"

    migrator_by_domain: dict = {}
    for role, p in procs.items():
        if p["migration_owner"]:
            assert p["migration_owner"] == p["domain"], (
                f"{role} 以 {p['migration_owner']} 的迁移身份跑在 {p['domain']} 库上")
            migrator_by_domain.setdefault(p["domain"], []).append(role)
    assert migrator_by_domain == {"mgmt": ["mgmt"], "runtime": ["event"], "insight": ["insight"]}, (
        f"每个域只能有一个迁移执行者，实际 {migrator_by_domain}")
    assert procs["worker"]["migration_owner"] == "", (
        "worker 不得导出 GRAPE_MIGRATION_OWNER：它与 event 共库，重复下发同一 DDL 会并发冲突")


def test_no_new_migration_violates_the_process_domain_contract():
    bad = _violations()
    unexpected = {v for v in bad if v not in EXCEPTIONS
                  or tuple(bad[v][1]) != tuple(EXCEPTIONS[v][0].split(","))}
    stale = [v for v in EXCEPTIONS if v not in bad]
    assert not unexpected, (
        "迁移执行域与 4 进程拓扑不符（会「台账有、列没有」或对缺表空转）：\n"
        + "\n".join(f"  {v} {bad[v][0]}: {bad[v][1]} declared={bad[v][2]} "
                    f"owners={bad[v][3]} exec={bad[v][4]}"
                    for v in sorted(unexpected))
        + "\n（确属存量且已定性，就登记进 EXCEPTIONS，写清类别与原因）"
    )
    assert not stale, f"EXCEPTIONS 里这些已不再违规，请删掉对应行: {stale}"


def test_default_domain_asymmetry_is_the_known_trap():
    """本守卫 V1 的依据：缺省域只等于 mgmt（执行器），runtime/insight 只见显式声明。"""
    from src.api import db_setup

    assert "m.domains or ()" in inspect.getsource(db_setup.run_domain_migrations), (
        "run_domain_migrations 的缺省应是「谁都不跑」；若改成 or ('mgmt',)，本守卫的 V1 判据要同步")
    # dataclass 已把「未声明」归一成 ("mgmt",)：静态判据据此认缺省域=mgmt
    mgmt_only = next(m for m in db_setup._MIGRATIONS if tuple(m.domains) == ("mgmt",) and m.fn)
    assert db_setup._migration_domain_allows(mgmt_only, "mgmt") is True, "缺省域只在 mgmt 跑"
    assert db_setup._migration_domain_allows(mgmt_only, "runtime") is False, (
        "缺省 mgmt 的迁移不得在 runtime 域执行（run_domain_migrations 用 m.domains or ()）")
    runtime_only = next(m for m in db_setup._MIGRATIONS if m.version == "202611183010")
    assert db_setup._migration_domain_allows(runtime_only, "mgmt") is False, "显式 runtime 不应在 mgmt 跑"


def test_issue_org_snapshot_migration_runs_in_the_owner_domain():
    """F3 回归：issue 属主 runtime → 登记在 runtime，且被跳过时必须显式失败不盖章。"""
    from src.api import db_setup

    mig = next(m for m in db_setup._MIGRATIONS if m.version == "202611183010")
    assert mig.domains == ("runtime",), f"202611183010 注册域应为 ('runtime',)，实际 {mig.domains}"
    assert "raise_on_guard=True" in inspect.getsource(mig.fn)


def test_org_table_rename_migration_skips_tables_missing_in_this_domain():
    """F2 回归：跨域迁移（6 表=mgmt、issue=runtime）逐表探基表，缺表跳过。"""
    from src.api import db_setup

    mig = next(m for m in db_setup._MIGRATIONS if m.version == "202611191000")
    assert set(mig.domains) == {"mgmt", "runtime"}
    assert "_base_table_present(" in inspect.getsource(mig.fn)
    assert "return True" in inspect.getsource(db_setup._base_table_present), "探针失败必须 fail-safe"


def test_base_table_present_probe_on_sqlite():
    """探针语义（SQLite）：有表 True / 缺表 False / 探针异常按存在处理。"""
    import sqlite3

    from src.api.db_setup import _base_table_present

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t_demo (id INTEGER)")
    c = conn.cursor()
    assert _base_table_present(c, "t_demo") is True
    assert _base_table_present(c, "t_missing") is False

    class _Boom:
        def execute(self, *a, **kw):
            raise RuntimeError("probe exploded")

    assert _base_table_present(_Boom(), "whatever") is True


def test_issue_snapshot_migration_fails_loudly_when_run_outside_owner_domain(monkeypatch):
    """行为自证：非属主进程执行 F3 那条迁移必须抛错（不盖章），不能静默返回。"""
    from src.api import db_setup
    from src.kernel import table_ownership

    monkeypatch.setattr(table_ownership, "foreign_owner",
                        lambda tbl: "runtime" if tbl == "issue" else "")
    mig = next(m for m in db_setup._MIGRATIONS if m.version == "202611183010")
    with pytest.raises(RuntimeError):
        mig.fn()


def test_issue_snapshot_migration_is_silent_for_the_owner_domain(monkeypatch):
    from src.api import db_setup
    from src.kernel import table_ownership

    monkeypatch.setattr(table_ownership, "foreign_owner", lambda tbl: "")
    mig = next(m for m in db_setup._MIGRATIONS if m.version == "202611183010")
    mig.fn()