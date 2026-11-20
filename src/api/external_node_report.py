"""Issue #2792（#2747 子单4）外部执行者结论上报：幂等键派生 + 节点事件字段。

背景：Spec/标签/负责人/QA 审查这类节点动作有时由**外部执行者**完成——用户（或
以 `tk_` 会话在本机跑的开发者）在 GitCode 上做完了该节点的活，但 Grape 侧没有任何
执行实例，`POST /api/tasks/complete` 也没法收尾（没有 task_id）。结果 Issue 页面
把「已执行」显示成空洞：事件链上看不到这一段的结论。

Spec（Issue 评论 id 201190348）实现路径 4：`tasks complete` 增加**用户态外部上报
入口**——无 `task_id` 且携带 `issue_number` + 节点标识 + 结论时，按
`ensure_issue_task` 解析/补建 Issue 任务，把结论写为**对应节点事件**
（`source=external`、`status=completed`、结构化 result），并按
`(issue, node, session)` 幂等去重；已终结的不覆盖。

本模块只做纯函数（无 DB / 无网络），路由层负责鉴权、任务解析与落库：

    key = external_report_idempotency_key(issue, node, session)
    fields = build_external_node_event(issue, node, result_text, ...)
    append_event(repo_key=repo_key, **fields)

幂等为什么复用 `idempotency_key`：events 表有 `(repo_key, idempotency_key)` 唯一
索引，撞键时 `append_event` 返回**已存在 seq 的负值**——这正是「重复上报不产生
重复事件」的现成机制，不必另建去重表或二次查询（也就没有查询-写入的竞态窗口）。
"""
from __future__ import annotations

import re

__all__ = [
    "EXTERNAL_SOURCE",
    "NODE_EVENT_TYPE",
    "external_report_idempotency_key",
    "build_external_node_event",
]

#: 外部执行者写入的事件 source 标识（Spec 验收①：Issue 页面可见 source=external）
EXTERNAL_SOURCE = "external"

#: 节点事件类型：与流水线段的唯一类型一致（`PIPELINE_SEGMENT_EVENT_TYPES` 只认
#: a2a_event），否则外部结论虽写进事件链，却不会被视为「这一段跑过了」。
NODE_EVENT_TYPE = "a2a_event"

# events.idempotency_key 的格式约束（与 A2A 入口同款：1-128 位安全字符）
_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9_:.\-]")
_KEY_MAX_LEN = 128


def _normalize_issue_number(issue_number) -> str:
    """Issue 号归一：去掉 `#` 前缀，保留数字/字母数字 ident（如 IK*）。"""
    return str(issue_number or "").strip().lstrip("#")


def _issue_number_value(issue_number):
    """payload 里的 issue_number 取数值形态（数字 ident），非数字 ident 原样保留。"""
    _num = _normalize_issue_number(issue_number)
    return int(_num) if _num.isdigit() else _num


def external_report_idempotency_key(issue_number, node: str, session: str = "") -> str:
    """派生 `(issue, node, session)` 三元幂等键（满足 events 幂等列格式约束）。

    中文节点名、带空格的会话名等不安全字符统一替换为 `-`；超长按 128 位截断
    （键前缀保留 issue+node，截断只会吃掉尾部会话名——同 issue 同节点的上报
    仍然唯一，不会退化成跨节点碰撞）。
    """
    raw = f"ext-{_normalize_issue_number(issue_number)}-{node or ''}-{session or ''}"
    key = _KEY_SAFE_RE.sub("-", raw)
    return key[:_KEY_MAX_LEN]


def build_external_node_event(issue_number, node: str, result_text: str, *,
                              task_id: int = 0, session: str = "",
                              actor: str = "", title: str = "") -> dict:
    """组装「外部执行结论」节点事件字段（`append_event` 的 kwargs，不含 repo_key）。

    - `agent_type=node`：事件归属到被完成的那个节点，Issue 事件视图按节点顺序显示；
    - `source=external`：与 A2A 派发（`a2a`）/ webhook 事件区分，页面一眼看得出
      「这段是外部执行者报的」；
    - `status=completed` + `result=结构化结论`：终态与结论一同入库，`completed_at`
      由 `append_event` 在同事务补齐；
    - `payload` 保留 issue/node/task/session 原始字段，便于核对「谁在哪个会话里
      报了哪个节点的结论」，也便于按 `external=true` 二次筛选。
    """
    _num = _normalize_issue_number(issue_number)
    _node = str(node or "").strip()
    return {
        "event_type": NODE_EVENT_TYPE,
        "title": title or f"外部执行结论：{_node}",
        "issue_id": f"#{_num}" if _num else "",
        "source": EXTERNAL_SOURCE,
        "agent_type": _node,
        "actor": actor or "",
        "summary": f"外部执行结论上报: node={_node}",
        "payload": {
            "external": True,
            "source": EXTERNAL_SOURCE,
            "node": _node,
            "issue_number": _issue_number_value(issue_number),
            "task_id": int(task_id or 0),
            "session": str(session or ""),
        },
        "status": "completed",
        "result": result_text or "",
        "idempotency_key": external_report_idempotency_key(_num, _node, session),
    }