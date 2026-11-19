"""``python -m src.runtime.agent`` 兼容入口 → ``src.domains.runtime.engine.agent``。

存量设备上的启动脚本仍是 ``-m src.runtime.agent``。这里用 runpy 跑 canonical 模块，
行为（含 root 身份 fail-fast 闸门、main() 的全部逻辑）与直接跑 canonical 模块完全一致；
本文件不留任何自己的逻辑。
"""

CANONICAL_MODULE = "src.domains.runtime.engine.agent"

if __name__ == "__main__":
    import runpy

    runpy.run_module(CANONICAL_MODULE, run_name="__main__")
else:
    def __getattr__(name):
        """按需代理，避免 import 本模块就把整条引擎依赖链拉起来。"""
        import importlib

        return getattr(importlib.import_module(CANONICAL_MODULE), name)