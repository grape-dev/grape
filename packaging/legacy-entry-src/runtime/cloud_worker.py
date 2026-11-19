"""``python -m src.runtime.cloud_worker`` 兼容入口 → ``src.domains.runtime.engine.cloud_worker``。

沙箱 worker 的旧启动脚本走的是这个模块路径。转发语义与 ``runtime/agent.py`` 相同：
runpy 执行 canonical 模块，``__main__`` 分支保持原样（包括进程退出码）。
"""

CANONICAL_MODULE = "src.domains.runtime.engine.cloud_worker"

if __name__ == "__main__":
    import runpy

    runpy.run_module(CANONICAL_MODULE, run_name="__main__")
else:
    def __getattr__(name):
        """按需代理，避免 import 本模块就把整条引擎依赖链拉起来。"""
        import importlib

        return getattr(importlib.import_module(CANONICAL_MODULE), name)