"""流水线/A2A 共享常量（Issue #2050）。

原先 confirm_minutes 默认值、issue 终态集合、handoff 目标来源与返回值在
``patrol.py`` / ``pipeline_status.py`` / ``routes/webhook.py`` 各写一份并已漂移
（例如 pipeline_status 的终态集合含 cancelled，patrol 侧口径不一致）。此处单源
定义，三个模块统一引用，避免后续再次分叉。
"""
from __future__ import annotations

# 断链确认窗默认值（meta patrol:confirm_minutes 的代码默认；单位：分钟）
DEFAULT_CONFIRM_MINUTES = 10

# issue 生命周期终态（issue.stage / issue.status 口径）
TERMINAL_ISSUE_STAGES = ("done", "pending_review", "cancelled")
# issue/task 状态终态（agent_tasks.status / issue.status 口径）
TERMINAL_TASK_STATUSES = ("completed", "cancelled", "pending_review")
# 「等用户」暂停态：既非终态，也不代表流水线停滞/断链（人工等待中，禁止自动重派发）
PAUSED_ISSUE_STATUSES = ("awaiting_input",)
# 人工处置可恢复/可收敛的 issue.status 集合（new_carrier/reassign/close 共用单源）
RESUMABLE_ISSUE_STATUSES = ("active", "processing") + PAUSED_ISSUE_STATUSES + ("pending_review",)
# 允许被置为人工态（暂停）的起始状态：避免把已完成/已取消单据拉回。
# Issue #2816 复审：pending_review 亦是终态载体可命中的在途状态，命中后同样要落
# 「stage 置人工态」，故纳入可标记集合（completed/cancelled 仍排除）。
MARKABLE_PAUSE_STATUSES = ("active", "processing", "pending_review")
# 人工态 stage 口径（与暂停 status 同名，供 patrol/UI 直接识别「等用户」）
MANUAL_ISSUE_STAGE = PAUSED_ISSUE_STATUSES[0]

# A2A/人工处置可派发的 agent 白名单（单源：入口校验、改派校验共用）
DISPATCHABLE_AGENTS = ("vod", "issue", "coding", "qa", "sre")

# Issue #2508：终态重放拦截词表（事件口径）——仅 completed/cancelled 不可重放，
# failed/timeout 保持既有失败重试语义。任务口径以 service.TASK_TERMINAL_STATUSES
# 为单源、调用处显式排除 failed/timeout_failed（见 event_store/auth_token）。
REPLAY_BLOCKING_EVENT_STATUSES = ("completed", "cancelled")
TERMINAL_EVENT_REPLAY_REASON = "terminal_event_replay"
REISSUE_EVENT_PAYLOAD_KEY = "_reissue"

# 流水线「段」事件：一次 agent 执行对应一条 a2a_event；跨场景移交另有
# cross_scene_handoff 标记，但段本身仍以 a2a_event 计量。
PIPELINE_SEGMENT_EVENT_TYPES = ("a2a_event",)
# 明确不参与「是否有新段」判定的审计/元数据事件（防 admin 操作永久抑制停滞）。
NON_PIPELINE_EVENT_TYPES = ("admin_action", "assignee_changed")

# resolve_handoff_target 的来源标签（替代散落的魔法字符串）
TARGET_SOURCE_TEXT = "text"
TARGET_SOURCE_LEGACY_TEXT = "legacy_text"
TARGET_SOURCE_PIPELINE = "pipeline"
TARGET_SOURCE_TERMINAL = "terminal"
TARGET_SOURCE_NOT_HANDOFF = "not_handoff"
TARGET_SOURCE_UNKNOWN = ""

# 结构化 handoff 返回值（POST /api/a2a 的 handoff_result）
HANDOFF_RESULT_OK = "ok"
HANDOFF_RESULT_FAILED = "failed"
HANDOFF_RESULT_REJECTED = "rejected"
