"""Issue #2132 B1 回归（runtime 域内）：知识缓存预热入口与建单预算。"""
from src.domains.runtime.engine import service as svc


def test_warm_knowledge_cache_degrades_to_empty(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("obs down")

    monkeypatch.setattr(svc, "collect_knowledge_files", _boom)
    assert svc.warm_knowledge_cache("acme/demo") == {}


def test_warm_knowledge_cache_delegates(monkeypatch):
    monkeypatch.setattr(svc, "collect_knowledge_files",
                        lambda rk, **k: {"project_info": {"a": {"content": "x"}}})
    out = svc.warm_knowledge_cache("acme/demo", agent_type="coding")
    assert out["project_info"]["a"]["content"] == "x"


def test_service_collect_applies_defensive_cap(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MAX_FILES", "2")
    monkeypatch.setattr(svc, "load_project_info_with_obs", lambda rk: ({}, {}))
    import src.api.knowledge as _kb_alias
    monkeypatch.setattr(_kb_alias, "member_files_pi", lambda *a, **k: {})
    monkeypatch.setattr(_kb_alias, "agent_knowledge_files",
                        lambda *a, **k: {f"k{i}": {"content": "x"} for i in range(9)})
    out = svc.collect_knowledge_files("acme/demo")
    assert len(out["knowledge"]) == 2


def test_warm_budget_env_and_default(monkeypatch):
    from src.domains.runtime import issues_center as ic
    monkeypatch.delenv("KNOWLEDGE_WARM_BUDGET_SEC", raising=False)
    assert ic._knowledge_warm_budget() == 0.8
    monkeypatch.setenv("KNOWLEDGE_WARM_BUDGET_SEC", "1.5")
    assert ic._knowledge_warm_budget() == 1.5
    monkeypatch.setenv("KNOWLEDGE_WARM_BUDGET_SEC", "-2")
    assert ic._knowledge_warm_budget() == 0.8  # 非法回退默认
