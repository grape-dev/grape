"""接管上下文预算：A2A 交办结构化、prompt 全文+护栏、日志头尾保留。

对应 2026-11-10 决策：
- prompt 全文，超 32k 字符走「头 16k + 标记 + 尾 16k」（旧实现只取尾 4000，A2A 段会被截掉）
- 日志改成「字符预算 32k + 头 8k / 尾 24k + 条数 ≤400」，中间写省略标记
- A2A 交办（payload.context / carried_context）单独结构化带上，不依赖 prompt 里的那段
"""
import json
from datetime import datetime, timezone

import pytest

from src.domains.runtime.engine import takeover as svc
from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _bind_db():
    """每次用例前重新绑定「被测代码实际使用的」连接。

    全树收集（pytest -k / 全量）时，其它测试模块会在导入期改 DEVOPS_DB_PATH 甚至
    reload session_manager，模块级 `from ... import db` 会变成陈旧句柄 ——
    种子写进旧库、被测代码读新库，表现为"表里查不到"。绑定到 takeover 模块的
    globals 可以保证种子与读取指向同一个连接。
    """
    global db, db_lock
    from src.domains.runtime.engine import takeover as _svc
    db, db_lock = _svc.db, _svc.db_lock

REPO = "gitcode:ctxbudget/repo"
ISSUE = "#7"
TASK = "t_ctx_budget"
SEQ = 990001


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM task_messages WHERE task_id=?", (TASK,))
            c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
            c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
            db.commit()

    wipe()
    yield
    wipe()


def _seed(prompt_len: int = 40000, n_msgs: int = 500, msg_len: int = 200,
          a2a_context: str = "", carried: dict | None = None):
    prompt = "HEAD-REQ " + ("需求细节。" * (prompt_len // 6))[: prompt_len - 20] + " TAIL-RULE"
    payload = {"repository": "ctxbudget/repo", "issue_number": 7,
               "context": a2a_context or "上一节点（coding）交办：已实现音效开关，PR #3 待审。"}
    if carried:
        payload["carried_context"] = carried
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, owner_agent, "
            "scene_id, assignee) VALUES (1,?,7,'上下文预算测试','processing','coding','coding','default','')",
            (REPO,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,'a2a_event','#7','qa','running','a2a','t',?)",
            (SEQ, REPO, json.dumps(payload, ensure_ascii=False)))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, status, agent_type, skill, prompt, "
            "env_extra, cwd, event_seq, repo_key, issue_id, user_id, account, skill_files, created_at) "
            "VALUES (?,?,0,'running','qa','pr',?,'{}','/tmp/ctx-budget/qa',?,?, '#7',1,'','{}',?)",
            (TASK, "ses_ctx", prompt, SEQ, REPO, datetime.now(timezone.utc).isoformat()))
        for i in range(n_msgs):
            content = f"msg-{i:03d}-" + ("步骤内容。" * (msg_len // 6))[: max(0, msg_len - 12)]
            if i == 0:
                content = "FIRST-LOG " + content
            if i == n_msgs - 1:
                content = "LAST-LOG " + content
            c.execute(
                "INSERT INTO task_messages (task_id, seq, type, tool, content, input_json, output, actor) "
                "VALUES (?,?,?,?,?,'','', 'agent')",
                (TASK, i + 1, "text" if i % 3 else "tool_use", "bash" if i % 3 == 0 else "", content))
        db.commit()


def _bundle():
    task = svc.get_task(TASK)
    bundle, warnings, persist = svc.build_bundle(
        task=task, session_id="tkos_ctx", token="tko_dummy", token_expires_at=0,
        user={"id": 1, "username": "tester"})
    return bundle, warnings, persist


class TestContextBudget:
    def test_prompt_is_full_with_head_tail_guard(self):
        _seed(prompt_len=40000)
        b, _w, _p = _bundle()
        prompt = b["instructions"]["prompt"]
        assert "HEAD-REQ" in prompt, "开头（任务要求）必须保留"
        assert "TAIL-RULE" in prompt, "结尾（收尾约束）必须保留"
        assert "中间已省略" in prompt, "超 32k 时应标记中间省略"
        assert "## 原始任务 Prompt" in prompt

    def test_a2a_context_is_structured_not_from_prompt(self):
        _seed(carried={"branch": "feat/audio", "pr": 3, "files": ["src/a.ts"]})
        b, _w, _p = _bundle()
        assert "上一节点（coding）交办" in (b["task"]["a2a_context"] or "")
        assert "feat/audio" in (b["task"]["carried_context"] or "")
        prompt = b["instructions"]["prompt"]
        assert "## 上一节点交办（A2A）" in prompt
        assert "上一节点（coding）交办" in prompt
        assert "feat/audio" in prompt
        # 结构化字段同时落 persist（重启/重连后仍可查）
        assert "上一节点（coding）交办" in (_p["task"]["a2a_context"] or "")

    def test_history_head_and_tail_kept_middle_omitted(self):
        _seed(n_msgs=500, msg_len=200)
        b, _w, _p = _bundle()
        meta = b["history"]["meta"]
        assert meta["total_items"] == 500
        assert meta["truncated"] is True
        assert 0 < meta["included_items"] < 500
        assert meta["omitted_items"] == 500 - meta["included_items"]
        assert meta["included_chars"] <= svc.HISTORY_CHAR_BUDGET + 2000
        text = "\n".join(str(m.get("content") or "") for m in b["history"]["messages"])
        assert "FIRST-LOG" in text, "头部（任务起步）要保留"
        assert "LAST-LOG" in text, "尾部（最近进展）必须保留"
        assert "省略" in text
        assert "task logs --task_id" in b["instructions"]["prompt"], "prompt 要告诉模型怎么按需拉全量"

    def test_short_history_not_truncated(self):
        _seed(prompt_len=1000, n_msgs=10, msg_len=50)
        b, _w, _p = _bundle()
        meta = b["history"]["meta"]
        assert meta["truncated"] is False
        assert meta["omitted_items"] == 0
        assert len(b["history"]["messages"]) == 10
        assert "省略" not in b["instructions"]["prompt"]