"""发布冒烟 `smoke_chat_submit.py` 的闸门语义（2026-11-15）。

背景：断连 500（`InterfaceError(0, '')`）不经过 health/只读端点，发布闸门全绿也拦不住。
本文件钉住「什么算红、什么算 SKIP」，避免冒烟脚本本身把发布流程搞脆：

1. 200 + task_id(chat_*) → OK（退出码 0）；
2. 非 200 或缺 task_id → FAIL（退出码 1，闸门判红）；
3. 缺 JWT_SECRET / 账号查不到 / 网络不可达 → SKIP（退出码 2，不阻断发布）；
4. 默认撤回这轮消息（避免每次发布真跑一轮 agent），撤回失败不影响「提交可用」结论。
"""
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "smoke_chat_submit.py"


def _load():
    spec = importlib.util.spec_from_file_location("smoke_chat_submit", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


@pytest.fixture()
def env_ready(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("DEVOPS_DB_HOST", "127.0.0.1")
    return monkeypatch


def test_ok_when_200_and_task_id(mod, env_ready):
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    calls = []

    def fake_post(url, payload, token, timeout):
        calls.append(url)
        if url.endswith("/api/chat/submit"):
            return 200, {"task_id": "chat_abc", "conversation_id": "c1", "message_id": 7}
        return 200, {"recalled": True}

    env_ready.setattr(mod, "_post", fake_post)
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_OK and out["result"] == "ok"
    assert out["task_id"] == "chat_abc" and out["recalled"] is True
    assert calls[0].endswith("/api/chat/submit") and "/api/chat/messages/7/recall" in calls[1]


def test_fail_on_500(mod, env_ready):
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    env_ready.setattr(mod, "_post", lambda url, p, t, to: (500, {"detail": "boom"}))
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_FAIL and out["result"] == "fail"
    assert out["submit_status"] == 500


def test_fail_when_task_id_missing(mod, env_ready):
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    env_ready.setattr(mod, "_post", lambda url, p, t, to: (200, {"conversation_id": "c1"}))
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_FAIL and out["result"] == "fail"


def test_skip_without_jwt_secret(mod, monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(mod, "_load_env_file", lambda path: None)   # 不读仓库 .env
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_SKIP and out["result"] == "skip"


def test_skip_when_network_down(mod, env_ready):
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")

    def boom(url, payload, token, timeout):
        raise ConnectionRefusedError(111, "refused")

    env_ready.setattr(mod, "_post", boom)
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_SKIP and out["result"] == "skip"


def test_no_recall_flag_skips_recall(mod, env_ready):
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    seen = []

    def fake_post(url, payload, token, timeout):
        seen.append(url)
        return 200, {"task_id": "chat_x", "conversation_id": "c1", "message_id": 9}

    env_ready.setattr(mod, "_post", fake_post)
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", False, 5)
    assert code == mod.EXIT_OK and len(seen) == 1, "关掉 recall 时不应再发撤回请求"

def test_retries_warmup_401_then_ok(mod, env_ready):
    """刚重启的进程还没就绪（401）属于预热，重试后成功不应判红。"""
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    env_ready.setattr(mod.time, "sleep", lambda s: None)     # 不真等
    seq = [(401, {"detail": "expired"}), (401, {"detail": "expired"}),
           (200, {"task_id": "chat_ok", "conversation_id": "c1", "message_id": 11})]

    def fake_post(url, payload, token, timeout):
        if url.endswith("/api/chat/submit"):
            return seq.pop(0)
        return 200, {"recalled": True}

    env_ready.setattr(mod, "_post", fake_post)
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_OK and out["task_id"] == "chat_ok"
    assert out.get("submit_retries") == 2


def test_persistent_401_still_fails(mod, env_ready):
    """一直 401 = 鉴权真的坏了，重试到底后仍要判红（不静默放行）。"""
    env_ready.setattr(mod, "_mint_token", lambda account, timeout: "tok")
    env_ready.setattr(mod.time, "sleep", lambda s: None)
    env_ready.setattr(mod, "_post", lambda url, p, t, to: (401, {"detail": "expired"}))
    code, out = mod.check("http://127.0.0.1:38081", "songspace", "smoke", True, 5)
    assert code == mod.EXIT_FAIL and out["submit_status"] == 401
