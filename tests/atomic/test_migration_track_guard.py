"""迁移双轨守卫（Issue #995 复盘）：防止新增补列再写进已冻结的老轨。

背景
----
`grape_t_schema_migrations` 被 legacy-stamp 盖帽（202610300001）后，存量库
启动时只跑注册式迁移（`register_migration`），老轨 `_run_all_migrations()`
完全不执行。把 ALTER TABLE 写进老轨不会报错、也不会执行，直到线上查询该列
报 `Unknown column (1054)` —— 而调用方普遍 except 后返回空列表，症状是
「接口 200 但页面全空」（2026-11-06 的 issue.vcs_exists / Issue #995）。

两道守卫
--------
1. test_legacy_track_is_frozen：AST 比对 `_run_all_migrations()` 里的迁移调用
   集合与 `LEGACY_TRACK_FROZEN` 快照，多一条（有人塞新迁移）即失败；
2. test_registered_versions_unique：注册式迁移的 version 必须全局唯一，
   同号会被版本表判「已执行」永久跳过（202611050001 / 202611060001 两次事故）。
"""
import ast
import collections
import pathlib

DB_SETUP = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "db_setup.py"
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


def _registered_versions() -> list:
    """提取所有 register_migration(...) 的 version（位置参数或关键字参数）。"""
    versions = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "register_migration":
            continue
        if node.args:
            versions.append(ast.literal_eval(node.args[0]))
        else:
            for kw in node.keywords:
                if kw.arg == "version":
                    versions.append(ast.literal_eval(kw.value))
    return versions


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


def test_registered_versions_unique():
    """注册式迁移版本号必须唯一：同号会被版本表判「已执行」而永久跳过。"""
    versions = _registered_versions()
    dup = {v: c for v, c in collections.Counter(versions).items() if c > 1}
    assert not dup, (
        f"注册式迁移版本号撞号: {dup}。同号迁移会被判「已执行」永久跳过，"
        "请改用未占用的号（日期+序号顺延）。"
    )


def test_registered_versions_format():
    """版本号格式统一为 12 位数字（YYYYMMDD+4位序号），便于排序与人工核对。"""
    bad = [v for v in _registered_versions() if not (isinstance(v, str) and len(v) == 12 and v.isdigit())]
    assert not bad, f"迁移版本号格式不规范（应为 12 位数字）: {bad}"