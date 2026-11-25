"""Issue #3414：engine_model 导入降级 + branch_guard 钩子 canonical 依赖。

根因：自部署机 engine_model.py 陈旧缺 ``detect_engine_model``；现行基线经
``src.domains.runtime.public`` 惰性 wrapper 调用期失败已由内层 except 兜住，
残余 500 面是 public 门面自身陈旧缺符号时 ``_attach_engine_models`` 的
from-import 失败 → /agent-runtimes 500 → 引擎绑定/派单链路断裂（新任务永久 queued）；
branch_guard 钩子依赖 canonical ``src.kernel.lock``（本基线已由 #2706 满足）。
"""

import importlib
import sys

import pytest

from src.domains.mgmt.routes import settings as settings_routes
from src.domains.runtime.engine import branch_guard as bg


# ===================== settings._attach_engine_models 降级 =====================

def _rt(rt_id=1, name="opencode-local", model_name="", credential_id=0):
    return {"id": rt_id, "name": name, "model_name": model_name,
            "credential_id": credential_id}


@pytest.fixture()
def _restore_engine_model():
    yield
    sys.modules.pop("src.domains.runtime.engine.engine_model", None)
    sys.modules.pop("src.domains.mgmt.routes.settings", None)


def test_attach_engine_models_degrades_when_engine_model_import_fails(monkeypatch):
    """public 门面自身陈旧（缺 ``detect_engine_model`` 符号）时 from-import 抛 ImportError：
    新增外层降级必须告警并继续附加 recent_models，端点不得 500（Issue #3414）。"""
    from src.domains.runtime import public as runtime_public

    monkeypatch.delattr(runtime_public, "detect_engine_model", raising=False)
    warnings = []
    monkeypatch.setattr(settings_routes.log, "warning",
                        lambda msg, *a, **k: warnings.append(str(msg)))
    rts = [_rt(1, "opencode-local"), _rt(2, "hermes-remote")]
    settings_routes._attach_engine_models(rts)  # 不得抛 ImportError
    for rt in rts:
        assert "recent_models" in rt  # DB 聚合字段照常附加
        assert rt.get("model_source") != "local_config"  # 兜底降级为跳过
    assert any("engine_model import degraded" in w for w in warnings), warnings


def test_attach_engine_models_degrades_when_engine_model_call_fails(monkeypatch, _restore_engine_model):
    """陈旧 engine_model 缺符号：public wrapper 调用期 ImportError 由内层 except 兜住。"""

    class _StaleModule:  # 模拟自部署陈旧 engine_model.py：模块在、符号缺
        pass

    monkeypatch.setitem(sys.modules, "src.domains.runtime.engine.engine_model", _StaleModule)
    rts = [_rt(1, "opencode-local"), _rt(2, "hermes-remote")]
    settings_routes._attach_engine_models(rts)  # 不得抛 ImportError
    for rt in rts:
        assert "recent_models" in rt  # DB 聚合字段照常附加
        assert rt.get("model_source") != "local_config"  # 兜底降级为跳过


def test_attach_engine_models_attaches_local_config_model_when_import_ok(monkeypatch, _restore_engine_model):
    """正常部署：本机直连（credential_id=0）且未上报 model_name 时仍读本机配置兜底。"""

    class _OkModule:
        @staticmethod
        def detect_engine_model(name):
            return "provider/model-x"

    monkeypatch.setitem(sys.modules, "src.domains.runtime.engine.engine_model", _OkModule)
    rts = [_rt(1, "opencode-local")]
    settings_routes._attach_engine_models(rts)
    assert rts[0]["model_name"] == "provider/model-x"
    assert rts[0]["model_source"] == "local_config"


# ===================== branch_guard 钩子依赖 canonical 化 =====================

def test_hook_dep_modules_use_canonical_paths():
    """钩子依赖自检必须走 canonical 路径（不依赖 src/__init__ 的 legacy 别名 shim）。"""
    assert bg.HOOK_DEP_MODULES == ("src.kernel.lock", "src.kernel.session_manager")
    for name in bg.HOOK_DEP_MODULES:
        m = importlib.import_module(name)
        assert m.__name__ == name  # 逐名断言，不依赖元组顺序/只校验最后一个


def test_branch_guard_lease_apis_use_canonical_lock(monkeypatch):
    """三个租约入口（_lease_holder/acquire_branch_lease/release_branch_lease）
    都经 ``_lock_module`` 收敛到 canonical ``src.kernel.lock`` 同一模块对象。"""
    import src.kernel.lock as canonical_lock

    assert bg.LEASE_LOCK_MODULE == "src.kernel.lock"
    assert bg._lock_module() is canonical_lock  # 默认收敛点直接返回 canonical

    calls = []

    def _spy(quiet=False):
        calls.append(quiet)
        return canonical_lock

    monkeypatch.setattr(bg, "_lock_module", _spy)
    monkeypatch.setattr(canonical_lock, "acquire_lock", lambda *a, **k: True)
    monkeypatch.setattr(canonical_lock, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(bg, "load_lease_state", lambda *a, **k: {})

    assert bg._lease_holder("t-1") == f"t-1:{canonical_lock._holder}"
    lease = bg.acquire_branch_lease("owner/repo", "feat/x", task_id="t-1")
    assert lease["ok"] is True
    assert lease["holder"] == f"t-1:{canonical_lock._holder}"
    bg.release_branch_lease("owner/repo", "feat/x", task_id="t-1")
    # acquire/release 以默认 quiet=False 走收敛点；_lease_holder 走 quiet=True。
    assert calls.count(False) == 2
    assert calls.count(True) >= 1


def test_missing_hook_deps_reports_canonical_names(monkeypatch, capsys, caplog):
    """依赖缺失告警必须给出 canonical 模块名（可定位），且 fail-open 放行。

    #2923 去噪语义：canonical 模块名经 log.warning 定位，stderr 只保留可读
    中文结论 + 修复指引，不再携带原始 reason/异常文案。
    """
    real_import = importlib.import_module

    def _fake_import(name, *a, **k):
        if name == "src.kernel.lock":
            raise ImportError("cannot import name lock from src.kernel")
        return real_import(name, *a, **k)

    monkeypatch.setattr(bg.importlib, "import_module", _fake_import)
    monkeypatch.setattr(bg.sys, "stdin",
                        __import__("io").StringIO("refs/heads/x l-sha refs/heads/x r-sha\n"))
    import logging
    with caplog.at_level(logging.WARNING, logger=bg.log.name):
        assert bg.hook_main(["gitcode:owner/repo"]) == 0
    err = capsys.readouterr().err
    assert "fail-open" in err and "--no-verify" in err
    assert "src.kernel.lock" not in err
    assert "src.kernel.lock" in caplog.text
