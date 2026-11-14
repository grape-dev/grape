"""跨域写补偿队列的运维入口（从 src.kernel.cross_write 迁出）。

理由：kernel 不能 import domains（check-arch R1）。原先这段运维 CLI 写在
kernel 模块的 ``__main__`` 里并 import ``src.domains.runtime.public``，
把域名依赖带进了内核层。handler 只服务运行域，因此整块放到运行域下。

用法::

    python -m src.domains.runtime.pipeline.xwrite_cli drain
    python -m src.domains.runtime.pipeline.xwrite_cli stats
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from src.domains.runtime import public as rt
    from src.kernel import cross_write as xw

    handlers = {
        "engine_cap_upsert": lambda p: rt.upsert_engine_capability(
            p["rid"], p["tag"], priority=p.get("priority", 50)),
        "engine_cap_update": lambda p: rt.update_engine_capability(
            p["rid"], p["tag"], p.get("priority", 50), p.get("enabled", 1)),
        "engine_cap_delete": lambda p: rt.delete_engine_capability(p["rid"], p.get("tag")),
        "engine_cap_delete_all": lambda p: rt.delete_engine_capability(p["rid"]),
    }
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "drain"
    if cmd == "stats":
        print(xw.stats())
        return 0
    if cmd == "drain":
        print(xw.drain(handlers))
        return 0
    print("usage: python -m src.domains.runtime.pipeline.xwrite_cli [drain|stats]")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
