"""CLI 死代码清理（P0b 收尾）：executor hermes iso-home 不可达块。

P0a A3 后 hermes 本机执行恒走 ACP（HermesRuntime.acp_backend 能力钩子，
executor.py:807 在 CLI 命令构造段之前 return）——CLI 段的
`backend.name == "hermes"` iso-home 隔离块（HERMES_HOME=mkdtemp + config
复制 + :memory: .pth）不可达，删除（用户裁定：不留死代码/兼容层）。
"""
import inspect


def test_hermes_local_always_acp():
    """前提实证：hermes backend 自报 acp_backend 钩子（本机恒 ACP）。"""
    from src.domains.runtime.agents.executor import get_backend
    b = get_backend("hermes")
    assert callable(getattr(b, "acp_backend", None))


def test_executor_no_dead_hermes_iso_home():
    """executor 不得残留按名的 hermes iso-home 死分支。"""
    from src.domains.runtime.agents import executor
    src = inspect.getsource(executor)
    assert 'backend.name == "hermes"' not in src, \
        "executor 残留 hermes 名字分支（iso-home 死代码，hermes 恒走 ACP 不可达）"
    assert "hermes-iso" not in src, "iso-home 死代码目录前缀应一并删除"
