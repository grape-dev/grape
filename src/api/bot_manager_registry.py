"""跨进程 bot_manager 访问桥（Issue #596 P1）。

三进程拆分后 IM WebSocket 只在 **A 进程（grape_server.py）** 启动
（设计文档 §10.7）。历史代码通过 `from src.api.main import bot_manager`
取 IM bot 实例——这在多进程模式下 import 的是「导入方进程里的 main 模块」，
A 进程内仍然有效（A 加载 main 模块？否——A 用 grape_server 入口）。

因此引入统一桥：`src/api/bot_manager_registry.py` 维护一个进程级
`bot_manager` 槽位，谁启动 IM 谁注册：

- A 进程（grape_server.py）：lifespan 启动 BotManager 后
  `set_bot_manager(bm)`；
- 单进程模式（main.py）：lifespan 同样 `set_bot_manager(bm)`；
- B/C 进程：不注册——`get_bot_manager()` 返回 None（与未启动 IM 时一致），
  下游代码均已按 bot_manager 为 None 的降级路径处理（返回 None / 跳过通知）。

迁移策略：main.py 的模块级 `bot_manager` 变量与 registry 同步维护
（main.py lifespan 启动后 set_bot_manager(bot_manager)），老代码
`from src.api.main import bot_manager` 在单进程模式继续可用；
新代码/多进程路径一律走 `get_bot_manager()`。
"""
import threading

_lock = threading.Lock()
_bot_manager = None


def set_bot_manager(bm) -> None:
    """注册本进程的 IM BotManager（A / 单进程调用）。"""
    global _bot_manager
    with _lock:
        _bot_manager = bm


def get_bot_manager():
    """取本进程的 IM BotManager（未启动 IM 的进程返回 None）。"""
    with _lock:
        return _bot_manager
