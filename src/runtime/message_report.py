"""兼容别名：message_report 已迁到 src.domains.runtime.engine.message_report（旧导入路径保留）。"""
from src.domains.runtime.engine.message_report import *  # noqa: F401,F403
from src.domains.runtime.engine.message_report import (  # noqa: F401
    build_msg_entry_id,
    degrade_failed_batch,
    real_task_id,
    truncate_message,
)
