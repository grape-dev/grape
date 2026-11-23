"""上游策略缓存必须有失效路径（架构复审 P2-4）。

背景：`_POLICY_CACHE` 曾按 `scheme://host` 永久缓存 ca_bundle/重定向策略，管理面
改平台卡后必须重启进程才生效 —— 运维无法在线修 TLS/重定向。改为带 TTL 的缓存。
"""
import src.kernel.upstream_http as uh


def test_policy_cache_expires_after_ttl(monkeypatch):
    calls = []

    class _Card:
        api_base = "https://gl.example.com/api/v4"
        ca_bundle = "/tmp/gl-ca.pem"
        dialect = "gitlab"

    class _Cards:
        @staticmethod
        def list_vcs_platforms():
            calls.append(1)
            return ["gitlab"]

        @staticmethod
        def get_platform_card(_p):
            return _Card()

    monkeypatch.setattr("src.kernel.ports.vcs_cards.list_vcs_platforms", _Cards.list_vcs_platforms,
                        raising=False)
    monkeypatch.setattr("src.kernel.ports.vcs_cards.get_platform_card", _Cards.get_platform_card,
                        raising=False)
    uh.reset_cache()
    monkeypatch.setattr(uh, "_POLICY_TTL_SECONDS", 30)

    first = uh.policy_for("https://gl.example.com/api/v4/projects/1")
    assert first == ("/tmp/gl-ca.pem", False), first
    hits_before = len(calls)

    # TTL 内直接命中缓存，不再查卡
    uh.policy_for("https://gl.example.com/api/v4/projects/2")
    assert len(calls) == hits_before

    # 把时间推进到 TTL 之外 → 必须重新解析（这就是「改卡不需要重启」）
    import time as _time
    base = _time.time()
    monkeypatch.setattr(_time, "time", lambda: base + 31)
    uh.policy_for("https://gl.example.com/api/v4/projects/3")
    assert len(calls) > hits_before

    monkeypatch.undo()
    uh.reset_cache()


def test_unregistered_host_keeps_legacy_policy():
    uh.reset_cache()
    assert uh.policy_for("https://unknown.example.com/x") == ("", True)