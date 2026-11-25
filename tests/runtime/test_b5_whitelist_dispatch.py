"""B5 白名单收敛：handler_async IM 会话续接 + settings 引擎名平台解析。

- handler_async.py:1689 `in ("opencode","hermes")` → descriptor
  capabilities.im_session_valid 能力位派生（新引擎声明即接入，不抄名单）。
- settings.py:1167 `in ("hermes","opencode","codearts","codex","aishell")`
  → sandbox_installable 引擎的箱内 CLI 名集合派生（hwcloud 以别名 aishell
  出现——与 cubesandbox 白名单同源同口径，散布点证据=原元组）。
"""
import pytest


class TestImSessionValidCapability:
    def test_only_opencode_hermes(self):
        """IM 会话续接能力位：仅 opencode/hermes（codearts 忽略 session_id
        的既有语义，行为冻结）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        got = {c for c, d in reg.all().items()
               if d["capabilities"].get("im_session_valid")}
        # aishell-dsh 二期起支持 session/resume（arch 基线演进，融合保留）
        assert got == {"opencode", "hermes", "aishell-dsh"}, f"im_session_valid 漂移: {got}"

    def test_handler_dispatch_by_capability(self, monkeypatch):
        """handler_async 的 has_valid_session 判定经能力位派生，不抄名单。"""
        import inspect
        from src.domains.runtime.im import handler_async
        src = inspect.getsource(handler_async)
        assert 'in ("opencode"' not in src, "handler_async 残留手抄引擎名白名单（B5 散点）"
        assert "im_session_valid" in src, "应按 descriptor capabilities 派生"


class TestSettingsNamePlatformWhitelist:
    def test_sandbox_engine_names_match_legacy_tuple(self):
        """派生集合与原手抄元组一致（hwcloud 经 probe_cli=aishell 表示）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        derived = set(get_registry().sandbox_engine_names())
        legacy = {"hermes", "opencode", "codearts", "codex", "aishell"}
        assert derived == legacy, f"引擎名白名单漂移: {derived}"

    def test_settings_no_hand_copied_tuple(self):
        """settings.py 不得残留手抄引擎名元组判定。"""
        import inspect
        from src.domains.mgmt.routes import settings
        src = inspect.getsource(settings)
        assert '"hermes", "opencode", "codearts", "codex", "aishell"' not in src, \
            "settings 残留手抄引擎名白名单（B5 散点）"
        assert "sandbox_engine_names" in src, "应按 registry 派生集合判定"
