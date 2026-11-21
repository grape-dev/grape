"""跨域账号读的“连接归属”用例 S1–S5。

约定：跨域只走契约（runtime → mgmt.public），**连接选择属于 mgmt 域自己的实现**。
  · 本域进程 / 单库拓扑 → 主库引擎
  · 其它进程 + GRAPE_DSN_MGMT → 域引擎（DSN 建，不读主库配置）
  · 其它进程 + 缺 DSN → DomainEngineUnavailable（绝不回落主库；契约转成一次可见告警）
"""
from __future__ import annotations

import pathlib

import pytest

from src.kernel import db_registry
from src.kernel.persistence import engine as eng
from src.domains.mgmt.auth.dispatch_identity import service as svc


@pytest.fixture()
def routed(monkeypatch):
    state = {"lib": True, "current": "runtime", "dsn": "mysql://u:p@h:3306/mgmt_db", "built": []}

    monkeypatch.setattr(db_registry, "lib_enabled", lambda: state["lib"])  # 三库拓扑
    monkeypatch.setattr(db_registry, "current_domain", lambda: state["current"])
    monkeypatch.setattr(db_registry, "dsn_of", lambda d: state["dsn"] if d == "mgmt" else "")
    monkeypatch.setattr(eng, "get_engine", lambda: "MAIN-ENGINE")

    def _create(url, **kw):
        state["built"].append((url, kw))
        return "DOMAIN-ENGINE"

    monkeypatch.setattr("sqlalchemy.create_engine", _create)
    import src.kernel.persistence.engine as _e

    with _e._engine_cache_lock:
        _e._engine_cache.clear()
    return state


def test_s1_single_db_uses_main_engine(routed):
    """单库拓扑（无进程域）→ 主库引擎。"""
    routed["lib"] = False
    assert eng.get_domain_engine("mgmt") == "MAIN-ENGINE"
    assert routed["built"] == []


def test_s2_mgmt_process_uses_main_engine(routed):
    routed["current"] = "mgmt"
    assert eng.get_domain_engine("mgmt") == "MAIN-ENGINE"


def test_s3_cross_domain_uses_dsn_engine(routed):
    """跨域进程 + DSN → 用 DSN 建域引擎（不读主库配置）。"""
    assert eng.get_domain_engine("mgmt") == "DOMAIN-ENGINE"
    assert routed["built"] and "mgmt_db" in routed["built"][0][0]


def test_s4_cross_domain_without_dsn_raises(routed):
    """三库拓扑缺 DSN → 显式不可用，绝不回落主库。"""
    routed["dsn"] = ""
    with pytest.raises(eng.DomainEngineUnavailable) as ei:
        eng.get_domain_engine("mgmt")
    assert ei.value.domain == "mgmt"


def test_s5_contract_degrades_with_single_visible_warning(monkeypatch):
    """契约层：DSN 缺失 → 返回空身份 + 按域只告警一次（不是 debug 静默）。"""
    warnings = []

    class _Log:
        def warning(self, msg, *a):
            warnings.append(msg % a if a else msg)

        def info(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    monkeypatch.setattr(svc, "log", _Log())
    svc._warned_domains.clear()

    import src.domains.mgmt.user.account_session as sess

    def _boom():
        raise eng.DomainEngineUnavailable("mgmt")

    monkeypatch.setattr(sess, "account_session", lambda: (_ for _ in ()).throw(eng.DomainEngineUnavailable("mgmt")))

    assert svc._account_id_by_username("alice") == ""
    assert svc._account_id_by_username("bob") == ""
    assert len(warnings) == 1, warnings
    assert "degraded" in warnings[0]


# 已存在的违约点（基线就有，非本轮引入）：只做棘轮，不允许新增。
# 清理它们要单独一单（属架构债），此处只保证我们的新代码不往里加。
_KNOWN_RUNTIME_CONTRACT_VIOLATIONS = {
    "issues_center.py",
    "vcs/api_client.py",
    "engine/env_extra_security.py",
    "routes/events.py",
    "routes/issues.py",
    "routes/runtime.py",
}


def test_s6_runtime_only_imports_the_management_contract():
    """架构棘轮：runtime 只允许 `mgmt.public`；基线已有的违约点登记在案，不得新增。"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "domains" / "runtime"
    offenders = []
    for f in root.rglob("*.py"):
        rel = str(f.relative_to(root))
        for line in f.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t.startswith(("from src.domains.mgmt", "import src.domains.mgmt")):
                if "mgmt.public" not in t and "mgmt import public" not in t:
                    offenders.append(rel)
    new = sorted({o for o in offenders if o not in _KNOWN_RUNTIME_CONTRACT_VIOLATIONS})
    assert not new, f"新增跨域直连（应改走 mgmt.public）：{new}"


def test_s6b_our_new_entry_points_use_the_contract_only():
    """本轮新增/改动的入口：只允许 public 契约。"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "domains" / "runtime"
    for rel in ("pipeline/dispatch_identity.py", "pipeline/event_pipeline.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            t = line.strip()
            if t.startswith(("from src.domains.mgmt", "import src.domains.mgmt")):
                assert "mgmt.public" in t or "mgmt import public" in t, f"{rel}: {t}"
