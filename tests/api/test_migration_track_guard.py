"""迁移双轨守卫（Issue #995 复盘）：防止新增补列再写进已冻结的老轨。

背景
----
`grape_t_schema_migrations` 被 legacy-stamp 盖帽（202610300001）后，存量库
启动时只跑注册式迁移（`register_migration`），老轨 `_run_all_migrations()`
完全不执行。把 ALTER TABLE 写进老轨不会报错、也不会执行，直到线上查询该列
报 `Unknown column (1054)` —— 而调用方普遍 except 后返回空列表，症状是
「接口 200 但页面全空」（2026-11-06 的 issue.vcs_exists / Issue #995）。

静态守卫
--------
1. test_legacy_track_is_frozen：AST 比对 `_run_all_migrations()` 里的迁移调用
   集合与 `LEGACY_TRACK_FROZEN` 快照，多一条（有人塞新迁移）即失败；
2. test_registered_versions_unique：注册式迁移的 version 必须全局唯一，
   同号会被版本表判「已执行」永久跳过（202611050001 / 202611060001 两次事故）；
3. test_registered_versions_unique_at_runtime：直接读运行时注册表 `_MIGRATIONS`
   再比一次。**为什么 2 不够**（PR #2157 门禁复审 Critical）：`register_migration(
   **MIGRATION)` 的展开式注册（`repo-vcs-health-breaker-columns` 就是这么登记的）
   在源码里没有 `version=` 关键字，旧的 AST 解析看不到 —— 于是
   `202611250001` 实际被 4 条迁移共用，静态守卫却报唯一。此处 2 已扩展为解析
   `**` 展开（读被导入模块的 `MIGRATION = {...}` 字典），3 再从运行时兜一层。

运行时守卫（Issue #1412）
------------------------
本文件的静态守卫只比对源码内唯一性，看不见「源码 vs 目标库」的跨分支撞号
（同号可能已被其它分支落库的迁移盖章）。执行器 `_run_migrations_with_conn`
在 checksum 漂移判定前新增「同号不同名」守卫：库内盖章名 ≠ 本次注册名时
log.error（含 version / 库内名 / 注册名 / 修复指引），行为仍跳过不执行以
保证启动可用性。运行时用例见
tests/api/test_issue_536_migrations.py:
`test_same_version_diff_name_reports_error_and_skips`。
"""
import ast
import collections
import pathlib
import re

DB_SETUP = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "db_setup.py"
REPO_ROOT = DB_SETUP.parents[2]
LEGACY_CALL_PREFIXES = ("_migrate_", "_mig_", "_ensure_")


def _tree():
    return ast.parse(DB_SETUP.read_text(encoding="utf-8"))


def _legacy_track_calls() -> set:
    """提取 _run_all_migrations() 函数体内的迁移类调用名。"""
    fn = next(n for n in _tree().body
              if isinstance(n, ast.FunctionDef) and n.name == "_run_all_migrations")
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            nm = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if nm and nm.startswith(LEGACY_CALL_PREFIXES):
                names.add(nm)
    return names


def _module_str_consts(tree) -> dict:
    """模块级 `NAME = "literal"` 常量表（version 允许引用常量，见 QA I3）。"""
    consts = {}
    for n in tree.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)):
            consts[n.targets[0].id] = n.value.value
    return consts


def _resolve_static_str(node, consts):
    """字面量或模块级常量名 → 字符串；解析不出返回 None（调用方判红）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _registered_versions() -> list:
    """提取所有 register_migration(...) 的 version（位置参数或关键字参数）。

    version 可以是字面量，也可以是模块级字符串常量（`_resolve_static_str` 解出）；
    解不出的表达式返回 None，由 test_registered_versions_format 判红，避免守卫
    静默漏看某条注册。

    `register_migration(**MIGRATION)` 展开式注册（PR #2157 门禁复审 Critical：
    `repo-vcs-health-breaker-columns` 就是这样登记的，旧解析器完全看不到它的号）
    按「模块级 `from ... import MIGRATION[_X] as _M_Y` + 该模块 `MIGRATION[_X]`
    字典的 version 键」静态解析；解不出同样返回 None 判红。
    """
    tree = _tree()
    consts = _module_str_consts(tree)
    spreads = _spread_imports(tree)
    versions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "register_migration":
            continue
        if node.args:
            versions.append(_resolve_static_str(node.args[0], consts))
            continue
        for kw in node.keywords:
            if kw.arg == "version":
                versions.append(_resolve_static_str(kw.value, consts))
            elif kw.arg is None:
                versions.append(_resolve_spread_version(kw.value, spreads))
    return versions


def _spread_imports(tree) -> dict:
    """`from <mod> import MIGRATION[_X] as _M_Y` → {_M_Y: (<mod>, MIGRATION[_X])}。"""
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module):
            continue
        for alias in node.names:
            if alias.asname and alias.name.startswith("MIGRATION"):
                out[alias.asname] = (node.module, alias.name)
    return out


def _dict_version(module: str, dict_name: str):
    """读 `<module>.py` 里 `MIGRATION[_X] = {...}` 字典的 version（字面量/常量）。"""
    path = REPO_ROOT.joinpath(*module.split(".")).with_suffix(".py")
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _module_str_consts(tree)
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == dict_name
                and isinstance(node.value, ast.Dict)):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "version":
                return _resolve_static_str(value, consts)
    return None


def _resolve_spread_version(node, spreads: dict):
    """`register_migration(**X)` 里的 X → 目标模块 MIGRATION 字典的 version。"""
    if not isinstance(node, ast.Name):
        return None
    target = spreads.get(node.id)
    if not target:
        return None
    module, dict_name = target
    return _dict_version(module, dict_name)


def test_legacy_track_is_frozen():
    """老轨已冻结：往 _run_all_migrations() 里新增迁移会让本测试失败。

    要让测试通过，正确做法是把新迁移写成 register_migration(...)（存量库才会跑），
    而不是把它加进 LEGACY_TRACK_FROZEN。只有确认必须留在老轨（如全新库建表顺序
    依赖）时才同步改常量，并在 PR 描述里写明理由。
    """
    from src.api.db_setup import LEGACY_TRACK_FROZEN

    actual = _legacy_track_calls()
    frozen = set(LEGACY_TRACK_FROZEN)
    added = actual - frozen
    removed = frozen - actual
    assert not added, (
        f"老轨 _run_all_migrations() 新增了迁移调用: {sorted(added)}。\n"
        "存量库启动只跑注册式迁移，写在这里的补列/补表永远不会执行（线上表现为 "
        "Unknown column 1054 + 接口静默返回空）。请改成 register_migration(...) 注册式迁移。"
    )
    assert not removed, (
        f"老轨 _run_all_migrations() 少了迁移调用: {sorted(removed)}。\n"
        "若确为有意删除（迁移已改注册式），请同步更新 LEGACY_TRACK_FROZEN 常量。"
    )


# 老轨 `_create_indexes()` 冻结时的索引名快照（Issue #3794 QA #1981 经验沉淀）。
# 为什么单列一份：`test_legacy_track_is_frozen` 只比对「迁移调用名」，拦不住有人把
# 新的补索引直接写进 _create_indexes() 的 DDL 列表 —— 那种改动在存量库永不执行
# （legacy-stamp 202610300001 之后老轨整段不跑），却能让新库/CI 变绿。
LEGACY_INDEX_DDL_FROZEN = frozenset({
    "idx_agent_runtimes_engine_uid",
    "idx_agent_sessions_repo",
    "idx_agent_tasks_engine_uid",
    "idx_agent_tasks_event",
    "idx_agent_tasks_issue_session",
    "idx_agent_tasks_repo_status",
    "idx_agent_tasks_runtime",
    "idx_agent_tasks_runtime_status",
    "idx_agent_tasks_session",
    "idx_agent_tasks_status",
    "idx_agent_type_runtime_pool_engine_uid",
    "idx_autopilot_exec_status",
    "idx_autopilot_exec_task",
    "idx_autopilot_tasks_enabled",
    "idx_autopilot_tasks_repo",
    "idx_bot_seen_messages_app_id",
    "idx_bot_seen_messages_seen_at",
    "idx_chat_conv_repo_issue",
    "idx_chat_conv_status",
    "idx_engine_capabilities_engine_uid",
    "idx_event_route_config_repo",
    "idx_events_issue_id",
    "idx_events_repo_issue",
    "idx_events_repo_key",
    "idx_events_status",
    "idx_issue_agent_session_repo",
    "idx_kwq_dim_created",
    "idx_kwq_source_seq",
    "idx_kwq_status_retry",
    "idx_latency_log_created",
    "idx_latency_log_duration",
    "idx_latency_log_path",
    "idx_latency_log_source",
    "idx_market_scene_id",
    "idx_runtime_credentials_credential_uid",
    "idx_scene_market_category_id",
    "idx_scene_market_is_featured",
    "idx_scene_market_repo_key",
    "idx_scene_market_scope",
    "idx_scene_market_source",
    "idx_scene_skill_ref_skill_name",
    "idx_skill_category_id",
    "idx_skill_is_featured",
    "idx_task_messages_engine_uid",
    "idx_task_messages_task",
    "idx_tasks_repo_issue",
    "idx_tasks_user_status",
    "idx_uploaded_files_ref",
    "idx_uploaded_files_scope",
})

_INDEX_DDL_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_]\w*)", re.I)


def _legacy_index_names() -> set:
    """提取 `_create_indexes()` 函数体内所有索引名（跨行字符串只取含名字的首段）。"""
    fn = next(n for n in _tree().body
              if isinstance(n, ast.FunctionDef) and n.name == "_create_indexes")
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = _INDEX_DDL_RE.match(node.value)
            if m:
                names.add(m.group(1))
    return names


def test_legacy_index_ddl_is_frozen():
    """老轨索引 DDL 也冻结：新增补索引写在 _create_indexes() 里会让本测试失败。

    Issue #3794 事故样本：`idx_agent_tasks_event_seq_status` 起初写进老轨，
    存量库（已盖 legacy-stamp）启动时根本不执行 → 1205 根因未消除，
    但新库与 CI 全绿。正确做法同补列/补表：register_migration(version, name,
    kind="safe", domains=(属主域,), fn=...)（参考 202611240004）。
    仅当确认索引必须留在老轨（全新库建表顺序依赖）时同步改常量并写明理由。
    """
    actual = _legacy_index_names()
    frozen = set(LEGACY_INDEX_DDL_FROZEN)
    added = actual - frozen
    removed = frozen - actual
    assert not added, (
        f"老轨 _create_indexes() 新增了索引: {sorted(added)}。\n"
        "存量库启动只跑注册式迁移，写在这里的补索引永远不会执行（查询仍全表扫）。"
        "请改成 register_migration(...)，不要把名字加进 LEGACY_INDEX_DDL_FROZEN。"
    )
    assert not removed, (
        f"老轨 _create_indexes() 少了索引: {sorted(removed)}。\n"
        "若确为有意删除（索引改注册式/被组合索引取代），请同步更新 "
        "LEGACY_INDEX_DDL_FROZEN 常量。"
    )


def test_registered_versions_unique():
    """注册式迁移版本号必须唯一：同号会被版本表判「已执行」而永久跳过。"""
    versions = _registered_versions()
    dup = {v: c for v, c in collections.Counter(versions).items() if c > 1}
    assert not dup, (
        f"注册式迁移版本号撞号: {dup}。同号迁移会被判「已执行」永久跳过，"
        "请改用未占用的号（日期+序号顺延）。"
    )


def test_registered_versions_unique_at_runtime():
    """运行时注册表版本号必须唯一（`**MIGRATION` 展开后仍唯一）。

    PR #2157 门禁复审 Critical 的回归锁：`repo-vcs-health-breaker-columns` /
    `events-cascade-compensation-columns` / `issue4339-events-payload-mediumtext` /
    `create-autopilot-payload-guard-table` 曾共用 202611250001，而 AST 守卫只看到
    其中三条字面量、仍判「唯一」。这里直接读 `_MIGRATIONS`，任何登记形态的撞号
    （字面量 / 常量 / `**` 展开 / 未来新增写法）都绕不过去。
    """
    from src.api import db_setup as ds

    versions = [m.version for m in ds._MIGRATIONS]
    dup = {v: c for v, c in collections.Counter(versions).items() if c > 1}
    names = {v: [m.name for m in ds._MIGRATIONS if m.version == v] for v in dup}
    assert not dup, (
        f"运行时注册表撞号: {names}。版本台账以 version 为键，同号后登记者"
        "会被判「已执行」永久跳过（payload-guard 这类无 post_check 的更是静默跳过），"
        "请 renumber 到未占用号（日期+序号顺延）。"
    )


def test_registered_versions_format():
    """版本号格式统一为 12 位数字（YYYYMMDD+4位序号），便于排序与人工核对。"""
    bad = [v for v in _registered_versions() if not (isinstance(v, str) and len(v) == 12 and v.isdigit())]
    assert not bad, f"迁移版本号格式不规范（应为 12 位数字）: {bad}"


def test_org_domain_text_migration_registered_for_both_domains():
    """D105 org 域字符串化必须注册，且 domains 同时覆盖 mgmt 与 runtime。

    为什么锁死这条：该迁移的前身 `_normalize_org_domain_text` 只是 db_setup 里
    一个「未注册」的函数，靠单测直接调用，MySQL 分支也缺，导致存量库 org_id
    永远是 INT，新代码写 '' 哨兵时新建凭据报 1366。只登记一个域同样会复发：
    mgmt 进程只跑 mgmt 库，runtime 库的 runtime_credentials/agent_tasks 等
    8 张表没人转换。
    """
    found = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "register_migration":
            continue
        kws = {kw.arg: kw.value for kw in node.keywords}
        nm = kws.get("name")
        if not (isinstance(nm, ast.Constant) and nm.value == "d105-org-domain-text-normalize"):
            continue
        doms = kws.get("domains")
        elts = getattr(doms, "elts", [])
        found.append([e.value for e in elts if isinstance(e, ast.Constant)])

    assert found, ("d105-org-domain-text-normalize 未注册：存量 MySQL 库的 org_id "
                   "将保持 INT，新建 runtime 凭据报 1366")
    assert len(found) == 1, f"同名迁移重复登记: {found}"
    assert {"mgmt", "runtime"} <= set(found[0]), (
        f"登记域不全: {found[0]}——缺哪个域，那个库的 org_id 就不会被转换")

def test_create_table_migration_must_carry_post_check(monkeypatch):
    """建表类迁移必须带 post_check（PR #2157 门禁复审 Critical 的配套）。

    `create-autopilot-payload-guard-table` 曾是 202611250001 四条同号迁移里**唯一
    没有 post_check** 的一条：建表 fn 是「表在则零写」，没有后置校验时，被守卫
    跳过 / 被同号顶替的库只会留一行 ERROR，版本号照盖、下次不再重试 —— 静默假绿。
    """
    import sqlite3

    import pytest

    from src.api import db_setup as ds

    migs = [m for m in ds._MIGRATIONS
            if m.name == "create-autopilot-payload-guard-table"]
    assert len(migs) == 1, f"迁移未唯一注册: {[m.version for m in migs]}"
    mig = migs[0]
    assert callable(mig.post_check), "建表迁移缺 post_check：目标缺失会被静默盖章"

    monkeypatch.setattr(ds, "_owned_skip", lambda table, what="": False)
    monkeypatch.setattr(ds, "_migration_conn",
                        lambda domain, default=None: sqlite3.connect(":memory:"))
    monkeypatch.setattr(ds, "_table_present", lambda c, table: False)
    with pytest.raises(RuntimeError) as ei:
        mig.post_check()
    assert "autopilot_payload_guard" in str(ei.value)

    monkeypatch.setattr(ds, "_table_present", lambda c, table: True)
    mig.post_check()  # 表在位 → 不抛（盖章放行）
