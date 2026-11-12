"""S113 worker chat 用户 token 注入链修复（D85，UC17）。

背景：worker 端 chat 任务的用户 token 注入（agent.py:2369-2378）与
issue_creator 身份解析（engine_select.py:439）依赖
src.api.deps + src.domains.mgmt.auth.grape_account_db + src.persistence(ORM)。
D84 前的 worker 包缺 deps.py / persistence/ / SQLAlchemy → import 被
调用方 try/except 吞成 log.warning → user_token 静默为空。
基线同条件演练同败 = 基线预存降级（非分支回归），D85 闭包补全收口。

语义口径（D85 设计验证）：
- SQLite 本地形态：grape_accounts/external_identities 表不存在 → 查空 →
  resolve=0 / token=""，与降级结果一致（本文件 sqlite_semantics_probe 锁）；
- MySQL 共享形态（DEVOPS_DB_BACKEND=mysql）：worker 与服务端同库 →
  查到真数据，功能恢复。
"""
import ast
import os
import re

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_ROOT = os.path.dirname(_SRC)

# worker chat token 链 + 身份解析链的入口符号（agent.py:2371-2372 / engine_select.py:439）
_CHAIN_IMPORTS = [
    ("src/api/deps.py", "get_user_token_for_repo"),
    ("src/domains/mgmt/auth/grape_account_db.py", "get_grape_account_by_id"),
    ("src/domains/mgmt/auth/grape_account_db.py", "resolve_user_id_by_platform_login"),
]


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestWorkerTokenChain:
    def test_manifest_contains_deps_and_persistence(self):
        """闭包守卫①：MANIFEST 含 deps.py 与 src/persistence/（D85 修复本体）。"""
        from src.kernel.worker_package import MANIFEST
        assert "src/api/deps.py" in MANIFEST, (
            "MANIFEST 缺 src/api/deps.py —— agent.py:2371 chat 链 import 即炸，"
            "token 注入静默降级（D85 回潮）"
        )
        assert "src/persistence/" in MANIFEST, (
            "MANIFEST 缺 src/persistence/ —— grape_account_db 模块级 ORM import 炸，"
            "身份解析降级（D85 回潮）"
        )

    def test_worker_requirements_contain_sqlalchemy_fastapi(self):
        """闭包守卫②：worker 清单含 SQLAlchemy + fastapi（deps.py 顶层依赖）。"""
        rw = _read("requirements-worker.txt")
        assert re.search(r"^SQLAlchemy\s*>=\s*2\.0", rw, re.M), (
            "requirements-worker.txt 缺 SQLAlchemy>=2.0 —— persistence ORM 依赖缺失，"
            "grape_account_db import 炸（D85 回潮）"
        )
        assert re.search(r"^fastapi\s*>=", rw, re.M), (
            "requirements-worker.txt 缺 fastapi —— deps.py 顶层 from fastapi import 炸"
        )

    def test_chain_entry_symbols_importable_in_closure(self):
        """链探针守卫③：链入口符号在其宿主模块中真实存在（名单防死）。"""
        for rel, symbol in _CHAIN_IMPORTS:
            src = _read(rel)
            tree = ast.parse(src)
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
            assert symbol in names, (
                f"{rel} 不含入口符号 {symbol} —— 名单过期，更新 "
                f"test_s113 _CHAIN_IMPORTS（agent.py/engine_select.py 调用面可能已迁移）"
            )

    def test_callers_reference_chain(self):
        """名单完整性守卫④：调用方确实引用这些入口（名单与现实一致）。"""
        agent = _read(os.path.join("src", "domains", "runtime", "engine", "agent.py"))
        assert "from src.api.deps import get_user_token_for_repo" in agent, (
            "agent.py 不再 import get_user_token_for_repo —— 调用面迁移，更新名单"
        )
        es = _read(os.path.join("src", "domains", "runtime", "engine", "engine_select.py"))
        assert "resolve_user_id_by_platform_login" in es, (
            "engine_select.py 不再引用 resolve_user_id_by_platform_login —— 名单过期"
        )

    def test_grape_account_db_persistence_import_face(self):
        """依赖面守卫⑤：grape_account_db 的 persistence 顶层 import ⇒ 包内必须有 persistence/。

        D85 特有形态：src/auth/ 整目录早就在 MANIFEST，但 grape_account_db.py L11-12
        的模块级 ORM import 使它成为 persistence 的"传染源"——此守卫锁定这对因果关系
        （改掉模块级 import 时守卫自动失效，改回时自动生效）。
        """
        from src.kernel.worker_package import MANIFEST
        ga = _read(os.path.join("src", "auth", "grape_account_db.py"))
        tree = ast.parse(ga)
        toplevel_orm = False
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.persistence"):
                toplevel_orm = True
                break
        if toplevel_orm:
            assert "src/persistence/" in MANIFEST, (
                "grape_account_db 顶层 import src.persistence 但 MANIFEST 无该目录"
                "（D85 回潮：模块级 ORM import 与包闭包脱同步）"
            )

    def test_sqlite_semantics_probe(self, tmp_path, monkeypatch):
        """语义演练⑥：SQLite 本地形态（空库）链行为与降级等价——零异常、token 空。

        真形态演练：worker 本地 sessions.db 无 grape_accounts/external_identities 表
        （D85 设计时实测 tables=[]），链必须优雅返回空而非抛异常。
        """
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "probe_sessions.db"))
        import src.kernel.session_manager as sm
        _probe_conn = sm.sqlite3.connect(str(tmp_path / "probe_sessions.db"), check_same_thread=False)
        # 隔离要点：grape_account_db L9 from-import 持有 sm.db 的模块级副本——
        # 只 monkeypatch sm.db 不够，必须同时重绑 grape_account_db.db/db_lock
        # （否则裸 SQL 查询走共享测试库，混跑被先跑文件的 lp_test_* 种子数据污染）
        monkeypatch.setattr(sm, "db", _probe_conn)
        import src.domains.mgmt.auth.grape_account_db as _gad
        monkeypatch.setattr(_gad, "db", _probe_conn)
        monkeypatch.setattr(_gad, "db_lock", sm.db_lock)
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id, resolve_user_id_by_platform_login
        # 空库：查询优雅返回空（0/None），不抛"no such table"
        uid = resolve_user_id_by_platform_login("gitcode", "nobody")
        assert uid == 0 or uid is None, f"空库身份解析应返回空，实得 {uid!r}"
        acc = get_grape_account_by_id(1)
        assert acc is None or acc == {}, f"空库账号查询应返回空，实得 {acc!r}"
