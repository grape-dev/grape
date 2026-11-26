"""B5 bwrap 会话 overlay 挂载分派 descriptor 化（_compat_module:83 名字分支）。

盘点裁定（回执 B5 A类）：`backend_name == "hermes"`（hermes 会话 overlay
动态 rw-bind，issue #238：state.db 续传不割断）改为 descriptor
`sandbox.overlay_root` 驱动——动态目录算 quirk（md5(cwd)[:12] + makedirs
+ 精确子目录挂载）留代码，挂载与否/根路径值域归 descriptor。
静态挂载面（BWRAP_RO_ENGINE/BWRAP_RW_ENGINE）本就 config 按名分表，不动。
"""
import hashlib
import os

import pytest


@pytest.fixture
def bwrap_on(monkeypatch, tmp_path):
    """打开 bwrap 开关 + 假二进制 + 有效 cwd。"""
    import src.kernel.config as cfg
    from src.domains.runtime.agents.sandbox import _compat_module as cm
    monkeypatch.setattr(cfg, "GRAPE_AGENT_SANDBOX", True, raising=False)
    monkeypatch.setattr(cm, "_bwrap_bin", lambda: "/usr/bin/bwrap")
    return tmp_path


class TestDescriptorOverlayRoot:
    def test_only_hermes_has_overlay_root(self):
        """overlay_root 仅 hermes（hwcloud 走 BWRAP_RW_ENGINE 静态根挂载，
        粒度差异是既有行为，冻结不动）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        for c, d in reg.all().items():
            has = bool(d.get("sandbox", {}).get("overlay_root"))
            assert has == (c == "hermes"), f"{c} sandbox.overlay_root 漂移"
        assert reg.get("hermes")["sandbox"]["overlay_root"] == "~/.cache/hermes-acp-homes"


class TestBuildBwrapCmdOverlayDispatch:
    def test_hermes_binds_cwd_hash_overlay(self, bwrap_on):
        """hermes：overlay 根（descriptor 值）下的 cwd-hash 子目录被 rw-bind。"""
        from src.domains.runtime.agents.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd(str(bwrap_on), "hermes")
        assert cmd and cmd[0] == "/usr/bin/bwrap"
        h = hashlib.md5(str(bwrap_on).encode()).hexdigest()[:12]
        overlay = os.path.expanduser(f"~/.cache/hermes-acp-homes/{h}")
        assert overlay in cmd, f"hermes overlay 子目录未挂载: {overlay}"
        i = cmd.index(overlay)
        assert cmd[i - 1] == "--bind" and cmd[i + 1] == overlay  # bind 源=目标同址
        assert os.path.isdir(overlay), "overlay 目录应被预创建"

    def test_other_backend_no_overlay(self, bwrap_on):
        from src.domains.runtime.agents.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd(str(bwrap_on), "opencode")
        assert cmd is not None
        assert "--tmpfs" in cmd
        assert not any("hermes-acp-homes" in x for x in cmd), \
            "非 overlay 引擎不得挂载 hermes 会话目录"

    def test_dispatch_has_no_name_branch(self):
        """分派不读引擎名字面量（overlay 动态目录算法是 quirk 留代码）。"""
        import inspect
        from src.domains.runtime.agents.sandbox import _compat_module as cm
        src = inspect.getsource(cm.build_bwrap_cmd)
        assert '== "hermes"' not in src, "build_bwrap_cmd 残留名字分支（B5 散点）"
        assert "overlay_root" in src, "应按 descriptor sandbox.overlay_root 派发"
