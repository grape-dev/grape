"""Issue #3469 重构守卫：验证码链路的「值对象 / 仓储 / 服务门面」职责边界。

三件事各一组断言：

1. **值对象无 IO**：``verification_code.py`` 只 import 标准库（+ jwt_utils 取密钥），
   源码里没有表名、没有 SQL、模块上不存在 ``db`` 门面；UTC 换算不随本地时区漂移。
2. **仓储是唯一 SQL 落点**：``verification_store.py`` 出现且只出现
   ``verification_codes`` 一张表；``verification.py`` 里没有该表字面量、
   没有 INSERT/SELECT/UPDATE/DELETE，编排一律经仓储对象。
3. **public API / patch 点可导出**：门面保留全部对外名，且 send/verify 直接调用
   本模块全局名（``generate_code`` / ``is_dev_verification_code`` /
   ``get_email_service`` / ``_stored_hash``），保证 ``verification.<name>``
   上的 monkeypatch 继续生效。
"""
import ast
import io
import os
import pathlib
import re
import time
import tokenize

import pytest

os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")
os.environ.setdefault("DEVOPS_DB_PATH", "/tmp/test_issue_3469_verification_modules.db")
os.environ["OBS_LOG_DB"] = "0"

AUTH_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "domains" / "mgmt" / "auth"

CODE_MODULE = "verification_code.py"
STORE_MODULE = "verification_store.py"
FACADE_MODULE = "verification.py"

SQL_KEYWORDS = ("INSERT ", "SELECT ", "UPDATE ", "DELETE ")


def _source(name: str) -> str:
    return (AUTH_DIR / name).read_text(encoding="utf-8")


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name))


def _docstring_spans(tree: ast.Module) -> set:
    """模块/类/函数级 docstring 的起始坐标（用于把「文档里的表名/SQL」排除在外）。"""
    spans = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            spans.add((body[0].lineno, body[0].col_offset))
    return spans


def _code_only(name: str) -> str:
    """去掉注释与 docstring 后的源码（保留真正的字符串字面量，即 SQL 语句）。

    守卫针对的是**可执行 SQL**：文档/注释里说明「仓储是唯一落点」不算越界。
    """
    src = _source(name)
    spans = _docstring_spans(ast.parse(src))
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in spans:
            continue
        kept.append(tok.string)
    return " ".join(kept)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"模块里没有顶层函数 {name}")


def _called_globals(func: ast.FunctionDef) -> set:
    """函数体内以「裸名字」调用的函数名（即走本模块全局查找的调用）。"""
    return {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


# ---------------------------------------------------------------------------
# 1. 值对象：纯计算，零 IO
# ---------------------------------------------------------------------------

def test_value_object_imports_are_pure():
    """值对象不得 import 数据库/仓储/框架；只允许标准库 + 取密钥的 jwt_utils。"""
    imported = set()
    for node in ast.walk(_tree(CODE_MODULE)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    allowed = {"calendar", "hashlib", "hmac", "secrets", "time",
               "src.kernel.auth_sdk.jwt_utils"}
    assert imported <= allowed, f"值对象引入了非纯依赖: {sorted(imported - allowed)}"
    assert not any("session_manager" in m for m in imported)


def test_value_object_source_has_no_sql():
    src = _code_only(CODE_MODULE)
    assert "verification_codes" not in src
    assert not any(kw in src for kw in SQL_KEYWORDS)
    assert "cursor" not in src
    assert "commit" not in src


def test_value_object_module_exposes_no_db_handle():
    from src.domains.mgmt.auth import verification_code as vo

    for banned in ("db", "db_lock", "_store", "session_manager"):
        assert not hasattr(vo, banned), f"值对象模块不该出现 {banned}"


def test_value_object_hash_is_kid_prefixed_and_target_salted():
    from src.domains.mgmt.auth import verification_code as vo

    vc = vo.VerificationCode("123456", " A@Example.com ")
    assert vc.hmac_input() == "a@example.com\x00123456"

    stored = vc.stored_hash
    assert re.fullmatch(r"([^:]+:)?[0-9a-f]{64}", stored), stored
    assert vo.VerificationCode.matches(stored, "123456", "a@example.com")
    # Q：target 是盐，换 target 必须失败
    assert not vo.VerificationCode.matches(stored, "123456", "b@example.com")
    assert not vo.VerificationCode.matches(stored, "000000", "a@example.com")
    assert not vo.VerificationCode.matches("", "123456", "a@example.com")


def test_value_object_matches_legacy_digest_without_kid():
    """I 的兼容面：无 kid 前缀的旧 digest 也要能校验（回退当前签名密钥）。"""
    from src.domains.mgmt.auth import verification_code as vo

    legacy = vo.VerificationCode("654321").digest(vo.signing_secret()[0])
    assert re.fullmatch(r"[0-9a-f]{64}", legacy)
    assert vo.VerificationCode.matches(legacy, "654321")
    assert not vo.VerificationCode.matches(legacy, "123456")


def test_value_object_utc_conversion_is_timezone_stable():
    """A：UTC 串读写互逆，TZ=Asia/Shanghai 下不得出现 8 小时漂移。"""
    from src.domains.mgmt.auth import verification_code as vo

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        epoch = 1700000000.0  # 2023-11-14 22:13:20 UTC
        text = vo.utc_str(epoch)
        assert text == vo.UtcTimestamp.parse(text).format()
        assert abs(vo.parse_utc(text) - epoch) < 1.0
        assert text == "2023-11-14 22:13:20"
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


# ---------------------------------------------------------------------------
# 2. 仓储：唯一 SQL 落点
# ---------------------------------------------------------------------------

def _sql_strings(name: str) -> list:
    """模块里的 SQL 语句字面量（按 SQL 关键字筛出的 str 常量）。"""
    tree = ast.parse(_source(name))
    docspans = _docstring_spans(tree)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and (node.lineno, node.col_offset) not in docspans
        and any(kw in node.value for kw in SQL_KEYWORDS)
    ]


def test_store_is_the_only_sql_landing_point():
    facade_code = _code_only(FACADE_MODULE)
    assert "verification_codes" not in facade_code, "门面里仍有 verification_codes 代码引用"
    assert _sql_strings(FACADE_MODULE) == [], "门面里仍有 SQL 语句字面量"

    sql = " ".join(_sql_strings(STORE_MODULE))
    assert "verification_codes" in sql
    tables = {
        m.group(1)
        for m in re.finditer(
            r"(?:FROM|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I
        )
    }
    assert tables == {"verification_codes"}, f"仓储越界访问其它表: {sorted(tables)}"


def test_store_exposes_repository_methods():
    from src.domains.mgmt.auth.verification_store import (  # noqa: F401
        VerificationCodeStore,
        VerificationStoreError,
    )

    expected = {
        "insert", "insert_shadow", "mark_all_used", "mark_used",
        "increment_attempts", "purge_expired",
        "active_rows", "has_any_code", "count_sends_since",
        "count_ip_since", "last_created_at",
    }
    assert expected <= set(dir(VerificationCodeStore))
    assert issubclass(VerificationStoreError, Exception)


def test_facade_orchestrates_through_repository(monkeypatch):
    """门面只经仓储读写：把仓储换成记录器，编排照常走（证明无内联 SQL 旁路）。"""
    from src.domains.mgmt.auth import verification as v

    class _RecordingStore:
        def __init__(self):
            self.calls = []

        def count_sends_since(self, email, scene, since):
            self.calls.append(("count_sends_since", email, scene))
            return 7

        def active_rows(self, email, scene):
            self.calls.append(("active_rows", email, scene))
            return []

        def has_any_code(self, email, scene):
            self.calls.append(("has_any_code", email, scene))
            return False

    store = _RecordingStore()
    monkeypatch.setattr(v, "_store", store)

    assert v.count_sends(" T3469_Mod@Example.com ", "register") == 7
    assert store.calls[-1] == ("count_sends_since", "t3469_mod@example.com", "register")

    r = v.verify_code("T3469_Mod@Example.com", "register", "123123")
    assert r["ok"] is False and r["error_code"] == "CODE_NOT_FOUND"
    assert ("active_rows", "t3469_mod@example.com", "register") in store.calls
    assert ("has_any_code", "t3469_mod@example.com", "register") in store.calls


def test_facade_store_binds_connection_provider():
    """仓储实例由门面装配，连接在调用时取（保证 ``verification.db`` 仍是 patch 点）。"""
    from src.domains.mgmt.auth import verification as v
    from src.domains.mgmt.auth.verification_store import VerificationCodeStore

    assert isinstance(v._store, VerificationCodeStore)
    db, db_lock = v._connection()
    assert db is v.db and db_lock is v.db_lock


# ---------------------------------------------------------------------------
# 3. public API / patch 点
# ---------------------------------------------------------------------------

PUBLIC_NAMES = [
    "generate_code", "send_code", "verify_code", "check_rate_limit", "count_sends",
    "SCENES", "CODE_TTL", "CODE_LENGTH", "MAX_ATTEMPTS",
    "SEND_INTERVAL", "MAX_SEND_PER_WINDOW", "MAX_IP_PER_WINDOW", "WINDOW_SECONDS",
    "is_dev_verification_code", "get_email_service",
    # 历史私有助手（存量测试直接用）
    "_hash_code", "_hmac_digest", "_stored_hash", "_match_stored_hash",
    "_utc_str", "_parse_utc_str", "_now_str", "_expires_str",
    "_count_recent_sends", "_count_recent_ip_requests", "_last_send_time",
    "_write_shadow_record", "_mark_all_used", "_mark_used",
    "_increment_attempts", "_cleanup_expired",
    "db", "db_lock",
]


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_api_exported(name):
    from src.domains.mgmt.auth import verification as v

    assert hasattr(v, name), f"verification 丢失对外名 {name}"


def test_public_api_values_unchanged():
    from src.domains.mgmt.auth import verification as v

    assert v.CODE_TTL == 600 and v.CODE_LENGTH == 6 and v.MAX_ATTEMPTS == 5
    assert v.SEND_INTERVAL == 60
    assert v.MAX_SEND_PER_WINDOW == 3 and v.MAX_IP_PER_WINDOW == 5
    assert v.WINDOW_SECONDS == 600
    assert {"register", "reset_password", "set_password", "change_email",
            "change_email_confirm", "unlock_account", "delete_account",
            "oauth_associate"} == set(v.SCENES)
    assert set(v.SCENES) == v.SCENES  # frozenset
    assert isinstance(v.SCENES, frozenset)


def test_patch_points_are_module_level_globals():
    """存量测试 patch 的四个名字必须是门面模块的全局名（且 send/verify 直接调用）。"""
    from src.domains.mgmt.auth import email_service
    from src.domains.mgmt.auth import verification as v

    assert v.is_dev_verification_code is email_service.is_dev_verification_code
    assert v.get_email_service is email_service.get_email_service
    assert v.generate_code.__module__ == v.__name__
    assert v.send_code.__module__ == v.__name__
    assert v.verify_code.__module__ == v.__name__

    tree = _tree(FACADE_MODULE)
    send_calls = _called_globals(_func(tree, "send_code"))
    assert {"generate_code", "_stored_hash", "get_email_service"} <= send_calls, send_calls
    verify_calls = _called_globals(_func(tree, "verify_code"))
    assert {"_parse_utc_str", "_match_stored_hash", "_mark_all_used"} <= verify_calls
    assert "is_dev_verification_code" in verify_calls


def test_facade_delegates_value_object_helpers():
    """门面私有助手是值对象的薄转发（公式只有一份）。"""
    from src.domains.mgmt.auth import verification as v
    from src.domains.mgmt.auth import verification_code as vo

    assert v._utc_str(0.0) == vo.utc_str(0.0)
    assert v._parse_utc_str("2024-01-02 03:04:05") == vo.parse_utc("2024-01-02 03:04:05")
    assert v._hmac_digest("secret", "112233", "a@b.c") == \
        vo.VerificationCode("112233", "a@b.c").digest("secret")
    assert v._stored_hash("112233", "a@b.c") == \
        vo.VerificationCode("112233", "a@b.c").stored_hash
    assert v._hash_code("112233") == vo.VerificationCode("112233").digest_with_signing_secret()