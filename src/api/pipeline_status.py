"""流水线节点状态统一计算模块。

以菜单流水线页的"事件状态法"为基准，任务详情页对齐到同一套逻辑。
新增 cancelled(4) 和 skipped(5) 状态码。
"""

# 状态码（与菜单流水线页现有 0/1/2/3 兼容，新增 4/5）
STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_COMPLETED = 2
STATUS_FAILED = 3
STATUS_CANCELLED = 4    # 新增
STATUS_SKIPPED = 5      # 新增

# event status → node status 映射
EVENT_STATUS_MAP = {
    "pending": STATUS_PENDING,
    "running": STATUS_RUNNING,
    "completed": STATUS_COMPLETED,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
}

# 流水线终态阶段
DONE_STAGES = ("done", "pending_review")


def compute_node_statuses(nodes, issue_events, current_stage):
    """统一计算流水线节点状态。

    参数：
        nodes: 节点列表（来自场景配置或 AGENT_ORDER 兜底）
        issue_events: 该 Issue 的事件列表（按 seq ASC）
        current_stage: issue_state.stage

    返回：(nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表
    """
    # 深拷贝 nodes 避免修改原始配置
    nodes = [dict(n) for n in nodes]
    is_done = current_stage in DONE_STAGES

    # 按 agent_type 分组，保留最新一条事件（后覆盖前）
    events_by_agent = {}
    for e in issue_events:
        at = e.get("agent_type", "")
        if at:
            events_by_agent[at] = e

    statuses = []
    for n in nodes:
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)

        if latest_event:
            # 有事件：按最新 event 状态映射
            evt_status = latest_event.get("status", "pending")
            status = EVENT_STATUS_MAP.get(evt_status, STATUS_PENDING)

            # done 阶段：cancelled 视为 completed
            if is_done and status == STATUS_CANCELLED:
                status = STATUS_COMPLETED
        else:
            # 无事件
            if is_done:
                status = STATUS_SKIPPED  # 流水线已结束但该节点未执行
            else:
                status = STATUS_PENDING

        n["node_status"] = status
        n["latest_event"] = latest_event
        statuses.append(status)

    return nodes, statuses
