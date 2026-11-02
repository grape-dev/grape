"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.engine.agent

Kept as the worker entrypoint so `python -m src.runtime.agent` (systemd /
install scripts, tarball layout) launches the canonical module unchanged.
"""
from src.domains.runtime.engine.agent import *  # noqa: F401,F403
from src.domains.runtime.engine import agent as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl

if __name__ == "__main__":
    from src.domains.runtime.engine.agent import main
    main()
