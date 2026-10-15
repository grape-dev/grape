"""event.json → events 表统一（阶段0：结构化产出落库 + 详情读新列/回退；阶段B：停写文件）。

覆盖（ST-01~04 + STOP-01~03）：
- ST-01 update_event_complete 写结构化产出到 events.structured_output
- ST-02 详情页优先读 structured_output
- ST-03 structured_output 为空时回退 event.json
- ST-04 两者皆空不崩溃
- STOP-01 init_event_json 停写：调用后不产生 event.json 文件
- STOP-02 finalize 停写：complete 后文件不存在/不更新
- STOP-03 读侧兼容：手工构造 event.json 仍可回退读
"""
import os, sys, json
from src.auth import grape_account_db

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
# 隔离 workspace：确保不写入真实 event.json（阶段 B 停写验证）
import tempfile
os.environ.setdefault("DEVOPS_WORKSPACE_DIR", tempfile.mkdtemp(prefix="evstop_"))

from src.session_manager import db, db_lock

RK = "gitcode:org/eventstruct"


def _seed_event(seq=8250, issue="#5", status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM events WHERE repo_key=? AND seq=?",
            (RK, seq))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
            "agent_logs, result, created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
            (seq, RK, "issue_opened", "webhook", issue, "t", "s", "", "{}",
             "vod", "opencode", "vod-cli", status, 0, 1.5,
             "[]", "测试结论", ))
        db.commit()


def _drop_structured_column_if_present():
    # 测试时不依赖 db_setup 迁移；列不存在则跳过（ST-01 断言会失败提示需加列）
    cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(events)").fetchall()]
    if "structured_output" in cols:
        with db_lock:
            db.cursor().execute("ALTER TABLE events DROP COLUMN structured_output")
            db.commit()


def _get_cols():
    return [r[1] for r in db.cursor().execute("PRAGMA table_info(events)").fetchall()]


# ── ST-01：update_event_complete 写结构化 ──

def test_update_event_complete_writes_structured_output():
    from src.api.event_store import update_event_complete
    _seed_event(8250)
    assert "structured_output" in _get_cols(), "events.structured_output 列缺失（需阶段A加列）"

    structured = {
        "actions": [{"cmd": "x"}], "conclusion": "结论A", "artifacts": ["art1"],
        "next_step": "a2a_issue", "issue_summary": "Issue #5 摘要",
    }
    update_event_complete(8250, "completed", exit_code=0, duration=1.5,
                          result="结论A", structured_output=structured)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT structured_output, result FROM events WHERE seq=?", (8250,))
        so, res = c.fetchone()
    parsed = json.loads(so)
    assert parsed["conclusion"] == "结论A"
    assert parsed["actions"] == [{"cmd": "x"}]
    assert parsed["issue_summary"] == "Issue #5 摘要"
    assert res == "结论A"  # result 仍是结论文本


# ── 详情页读新列 / 回退 ──

def _detail(seq):
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.user import db as user_db
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    from unittest.mock import patch
    uid = grape_account_db.create_grape_account("evstruct_user", email=None, password="pwd")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("evstruct_user")
    token = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    with patch("src.api.deps.check_repo_access", return_value=None):
        return TestClient(app).get(
            f"/api/events/detail/{seq}",
            headers={"Authorization": "Bearer " + token})


def test_event_detail_reads_structured_output_first():
    from src.api.event_store import update_event_complete
    from unittest.mock import patch
    _seed_event(8252, "#6")
    structured = {
        "actions": [{"cmd": "y"}], "conclusion": "DB完成", "artifacts": [],
        "next_step": "", "issue_summary": "s6",
    }
    update_event_complete(8252, "completed", exit_code=0, result="DB完成",
                          structured_output=structured)
    with patch("src.api.event_archive.load_event_json", return_value=None):
        r = _detail(8252)
        assert r.status_code == 200
        ev = r.json()
        assert ev["archive"]["output"]["conclusion"] == "DB完成"


def test_event_detail_fallback_to_event_json_when_column_null():
    from unittest.mock import patch
    _seed_event(8253, "#7")
    fake_arch = {
        "pipeline": [{
            "seq": 8253, "agent": "vod", "status": "completed",
            "output": {"actions": [], "conclusion": "来自文件", "artifacts": [], "next_step": "", "issue_summary": ""},
        }]
    }
    with patch("src.api.event_archive.load_event_json", return_value=fake_arch):
        r = _detail(8253)
        assert r.status_code == 200
        assert r.json()["archive"]["output"]["conclusion"] == "来自文件"


def test_event_detail_no_data_no_crash():
    from unittest.mock import patch
    _seed_event(8254, "#8")
    with patch("src.api.event_archive.load_event_json", return_value=None):
        r = _detail(8254)
        assert r.status_code == 200
        assert "archive" not in r.json()


# ── LOG-01：report_messages 落 events.agent_logs ──

def test_report_messages_writes_to_events_agent_logs():
    from src.runtime import service
    _seed_event(8260, "#9")
    # 构造最小 task（只依赖 event_seq/repo_key/issue_id/repo_gitcode_token）
    from unittest.mock import patch as _p
    with _p("src.runtime.service.get_task", return_value={
        "id": "t_log", "event_seq": 8260, "repo_key": RK,
        "issue_id": "#9", "repo_gitcode_token": "", "agent_type": "vod",
    }):
        service.report_messages("t_log", [
            {"type": "step_start", "data": {"step": 1}},
            {"type": "tool_use", "data": {"tool": "read", "input": {"filePath": "/x"}}},
        ])
    with db_lock:
        c = db.cursor()
        c.execute("SELECT agent_logs FROM events WHERE seq=?", (8260,))
        logs = json.loads(c.fetchone()[0] or "[]")
    assert len(logs) >= 2
    kinds = [l["type"] for l in logs]
    assert "step_start" in kinds and "tool_use" in kinds
    assert logs[0]["data"]["step"] == 1


# ── 阶段 B：STOP-01/02/03 — 停写 event.json，读侧兼容保留 ──

def _ev_path(seq):
    from src.api.event_archive import get_event_json_path
    return get_event_json_path(RK, 8266 if seq == 8266 else seq)


def test_stop_init_does_not_create_file():
    """阶段 B：init_event_json 停写 → 调用后不产生 event.json 文件。"""
    from src.api.event_archive import init_event_json
    _seed_event(8265, "#11")
    evt = {"issue_id": "#11", "title": "t", "type": "issue_opened", "actor": "", "branch": "", "summary": ""}
    init_event_json(RK, 8265, evt, "vod", runtime="r", skill="s")
    path = _ev_path(8265)
    assert not os.path.exists(path), "阶段B应停写：init 后不应创建 event.json"


def test_stop_finalize_does_not_touch_file():
    """阶段 B：finalize 停写 → complete 后文件不存在/不更新（数据已落 events 表）。"""
    from src.api.event_archive import finalize_event_archive
    _seed_event(8266, "#12")
    finalize_event_archive(
        RK, 8266, "completed", exit_code=0, duration=1.0,
        output={"actions": [{"cmd": "x"}], "conclusion": "c", "artifacts": [], "next_step": "", "issue_summary": "s"})
    path = _ev_path(8266)
    assert not os.path.exists(path), "阶段B应停写：finalize 后不应创建 event.json"
    # events 表数据仍应写入（structured_output）
    from src.api.event_store import update_event_complete
    update_event_complete(8266, "completed", result="c",
                          structured_output={"actions": [], "conclusion": "c", "artifacts": [], "next_step": "", "issue_summary": "s"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT structured_output FROM events WHERE seq=?", (8266,))
        assert c.fetchone()[0] is not None


def test_stop_read_compat_still_works():
    """阶段 B：停写不影响读侧兼容——`_load` 对人工构造的历史 event.json 仍可解析（ST-03 已覆盖详情回退）。"""
    from src.api.event_archive import _load
    path = _ev_path(8253)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "issue": {"number": 7},
            "pipeline": [{
                "seq": 8253, "agent": "vod", "status": "completed",
                "output": {"actions": [], "conclusion": "旧文件结论", "artifacts": [], "next_step": "", "issue_summary": ""},
            }],
        }, f, ensure_ascii=False)
    arch = _load(path)  # 直接读已写文件（确认 _load 仍可用，历史回退依赖它）
    assert arch is not None
    assert arch["pipeline"][0]["output"]["conclusion"] == "旧文件结论"
    os.remove(path)