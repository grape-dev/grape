"""CodeArts 后端实现（兼容转发层，M0 后迁至 src.domains.runtime.engine.codearts）。

旧路径仍需转出运行态与安装态共用的 trust/env 符号，否则 `install-worker.sh`
的 5.8 段（`from src.domains.runtime.agents.codearts import write_trust_settings`）
在 M0 迁移后会 ImportError，导致 worker 自动升级回滚（Issue #3642 / #2525）。
"""

from src.domains.runtime.engine.codearts import (
    CodeArtsRuntime as CodeArtsBackend,
    probe_env,
    write_trust_settings,
)

__all__ = ["CodeArtsBackend", "write_trust_settings", "probe_env"]
