"""引擎/设备身份：容器化部署下 hostname 每次重建都会变（上游 Issue #13 / GitCode #2094）。

问题：`socket.gethostname()` 被当作「引擎/设备身份」写入 runtime 记录并参与匹配
（匹配键 = name+scope+owner_id+host_name）。容器/Pod 每次重建 hostname 都不同 →
每次部署多一条失联记录，旧记录永不清除；重部署后「本机直连」路由还可能命中旧记录。

本模块提供唯一入口 `engine_host()`：**优先显式 `GRAPE_HOSTNAME`，回退系统 hostname**。
容器化部署把 `GRAPE_HOSTNAME` 固定成「服务名」即可保持身份稳定。

⚠️ 多副本注意：多个副本不能共用同一个常量值，否则心跳互相覆盖导致状态抖动；
   应使用「服务名 + 副本 ID / Pod 名」这类副本级稳定值（如 GRAPE_HOSTNAME=grape-$POD_NAME）。

⚠️ 只用于「引擎/设备身份」用途。以下语义必须继续用真实 hostname，不要换成这里：
   - `src/domains/runtime/im/bot_lease.py`  飞书事件租约（需真实 host）
   - `src/kernel/lock.py`                   分布式锁（需唯一）
   - `src/api/patrol.py`                    巡检 key = hostname#pid
"""
import os
import socket


def engine_host() -> str:
    """返回「引擎/设备身份」：优先环境变量 GRAPE_HOSTNAME，其次系统 hostname。"""
    explicit = (os.environ.get("GRAPE_HOSTNAME") or "").strip()
    if explicit:
        return explicit
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"
