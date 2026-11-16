"""Task status vocabulary shared by kernel and the runtime domain.

The runtime domain owns the agent_tasks table, but kernel-level token
verification also needs to know which task states mean "finished". kernel must
not import domains (check-arch R1), so the vocabulary lives here and the
domain references it, not the other way round.
"""

TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled", "timeout_failed")
ACTIVE_TASK_STATUSES = ("queued", "dispatched", "running", "pending_self_report")
