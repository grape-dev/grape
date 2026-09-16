"""流水线节点状态统一计算模块。

消除任务详情页与菜单流水线页的节点状态不一致问题。
两个视图共享同一套计算逻辑，确保同一任务在任何入口看到的状态一致。
"""

# 节点状态枚举（前端展示用）
NODE_PENDING = 0      # 待处理
NODE_RUNNING = 1      # 进行中
NODE_COMPLETED = 2    # 已完成
NODE_FAILED = 3       # 失败
NODE_CANCELLED = 4    # 已取消
NODE_SKIPPED = 5      # 已跳过

# event status → node status 映射
EVENT_STATUS_MAP = {
    "pending": NODE_PENDING,
    "running": NODE_RUNNING,
    "completed": NODE_COMPLETED,
    "failed": NODE_FAILED,
    "cancelled": NODE_CANCELLED,
}

# issue_state.stage 值表示流水线已结束
DONE_STAGES = ("done", "pending_review")


def compute_node_statuses(scene_nodes, issue_events, current_stage):
    """统一计算流水线节点状态。

    参数：
        scene_nodes: 场景配置的 nodes 列表（含 id/tag/skill 等）
        issue_events: 该 Issue 的所有事件列表（按 seq ASC）
        current_stage: issue_state.stage 当前阶段

    返回：
        (nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表（与 nodes 一一对应）
    """
    # 深拷贝 nodes 避免修改原始配置
    nodes = [dict(n) for n in scene_nodes]
    node_ids = [n.get("id", "") for n in nodes]
    cur_idx = node_ids.index(current_stage) if current_stage in node_ids else -1
    is_done = current_stage in DONE_STAGES

    # 按 agent_type 分组事件（保留顺序，后覆盖前，最终保留最新）
    events_by_agent = {}
    for e in issue_events:
        at = e.get("agent_type", "")
        if at:
            events_by_agent[at] = e  # 后覆盖前，最终保留最新

    statuses = []
    for i, n in enumerate(nodes):
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)

        if is_done:
            # 流水线已结束：按最新 event 状态判定
            if latest_event:
                status = EVENT_STATUS_MAP.get(latest_event.get("status", ""), NODE_COMPLETED)
                # cancelled 在 done 阶段视为已完成（曾执行过）
                if status == NODE_CANCELLED:
                    status = NODE_COMPLETED
            else:
                status = NODE_SKIPPED  # 节点未执行
        elif i < cur_idx:
            # 当前阶段之前的节点：按最新 event 状态判定
            if latest_event:
                status = EVENT_STATUS_MAP.get(latest_event.get("status", ""), NODE_COMPLETED)
            else:
                status = NODE_SKIPPED  # 节点未执行（可能是场景中间跳过的节点）
        elif i == cur_idx:
            # 当前阶段
            if latest_event:
                evt_status = latest_event.get("status", "")
                if evt_status == "completed":
                    # 最新事件已完成但 stage 还没推进 → 可能 A2A 尚未触发下一阶段
                    status = NODE_RUNNING  # 标为进行中（等待下一阶段）
                elif evt_status == "failed":
                    status = NODE_FAILED
                elif evt_status == "cancelled":
                    status = NODE_CANCELLED
                elif evt_status == "running":
                    status = NODE_RUNNING
                else:
                    status = NODE_RUNNING
            else:
                status = NODE_RUNNING  # stage 到了但事件还没创建
        else:
            # 当前阶段之后
            status = NODE_PENDING

        n["node_status"] = status
        n["latest_event"] = latest_event
        statuses.append(status)

    return nodes, statuses
