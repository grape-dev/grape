"""chat 路由不得直查 runtime 自有表（三库拓扑下必 1146）。

事故（2026-11-15 Issue #81/#82）：`/api/chat/task/{id}/status` 与 `.../stream` 在
MGMT 进程用 `session_manager.db`（= mgmt 库）查 `chat_messages`，而该表归 runtime：
- 进程内存里没有该任务（跨机提交 / 进程重启）时走 DB 兜底 → 1146 → 500 / SSE 卡到超时；
- 前端表现就是「提问没有响应」「（无返回内容）」，日志只有一行 1146。

本文件把三条纪律钉住：
1. chat 路由里不许再出现对 chat_messages / chat_conversations 的直查直删；
2. 需要的具名查询必须登记在 runtime public（由 lib 选对连接）；
3. 状态/SSE 兜底必须走 `messages_by_task`，且列序固定为
   id, role, generate_status, content, conversation_id（写错列序会让 SSE 一直等）。
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
CHAT = REPO / "src" / "domains" / "runtime" / "routes" / "chat.py"
RT_PUBLIC = REPO / "src" / "domains" / "runtime" / "public" / "__init__.py"

FORBIDDEN = [
    r"FROM\s+chat_messages\s+WHERE\s+task_id",
    r"DELETE\s+FROM\s+chat_conversations",
    r"DELETE\s+FROM\s+repo_im_group_binding",
    r"DELETE\s+FROM\s+bot_seen_messages",
    # 引擎/技能下拉（2026-11-15：这三张表也归 runtime，直查 → 弹层永远「加载中」）
    r"FROM\s+agent_runtimes",
    r"FROM\s+runtime_credentials",
    r"FROM\s+skill\b",
]


def _chat_src() -> str:
    return CHAT.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉 # 注释（按行），避免注释里的 'from skill table' 被当成 SQL。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def test_no_direct_cross_domain_sql_in_chat_routes():
    src = _strip_comments(_chat_src())
    hits = []
    for pat in FORBIDDEN:
        for m in re.finditer(pat, src, flags=re.I):
            line = src[:m.start()].count("\n") + 1
            hits.append(f"{pat} @ chat.py:{line}")
    assert not hits, "chat.py 里仍有对 runtime 自有表的直查直删：\n" + "\n".join(hits)


def test_named_queries_registered_in_runtime_public():
    src = RT_PUBLIC.read_text(encoding="utf-8")
    for name in ("messages_by_task", "conversation_delete_by_im",
                 "repo_im_delete_by_app", "bot_seen_delete_by_app",
                 "chat_local_engines", "chat_user_engines",
                 "chat_skills_all", "chat_skill_scope_by_id",
                 "chat_engine_by_id", "chat_skill_id_scope",
                 "chat_skill_id_scope_owner"):
        assert f"'{name}'" in src, f"runtime public 缺少具名查询 {name}"


def test_messages_by_task_column_order_is_fixed():
    """列序被改会让 status/SSE 的取值错位（曾经让 SSE 一直等到超时）。"""
    src = RT_PUBLIC.read_text(encoding="utf-8")
    m = re.search(r"'messages_by_task':\s*\((.*?)\),", src, flags=re.S)
    assert m, "未找到 messages_by_task 定义"
    block = " ".join(m.group(1).split())
    assert "SELECT id, role, generate_status, content, conversation_id" in block
    assert "WHERE task_id=? ORDER BY id" in block


def test_engine_and_skill_pickers_use_named_queries():
    """引擎/技能下拉的取数也必须走 runtime public（否则 mgmt 进程 1146 → 空列表）。"""
    src = _chat_src()
    for name in ("chat_local_engines", "chat_user_engines",
                 "chat_skills_all", "chat_skill_scope_by_id",
                 "chat_engine_by_id", "chat_skill_id_scope",
                 "chat_skill_id_scope_owner"):
        assert f'"{name}"' in src, f"chat.py 未使用具名查询 {name}"


def test_status_and_stream_use_the_named_query():
    src = _chat_src()
    assert src.count('read_all_any("messages_by_task"') >= 2, \
        "status 与 stream 的 DB 兜底都应走 messages_by_task"