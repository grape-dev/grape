"""event_archive 定位健壮性与结构化保护测试（weekly-04 集群 archive 空产出修复）。

覆盖：
- _locate_by_seq：events 表按 seq 精确查（不再依赖 limit=200）
- finalize_event_archive：非结构化收尾不降级已有结构化段
- finalize_event_archive：从未结构化时保持 init 空模板（旧语义）
- 定位失败：WARNING 日志 + 不 crash
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 隔离 workspace，避免污染真实事件归档
_TMP_WS = tempfile.mkdtemp(prefix="evarch_test_")
os.environ["DEVOPS_WORKSPACE_DIR"] = _TMP_WS

from src.session_manager import db, db_lock
from src.repo_key import workspace_repo_dir

RK = "gitcode:org/evarch"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        db.commit()
    import shutil
    ws = workspace_repo_dir(RK)
    if os.path.isdir(ws):
        shutil.rmtree(ws, ignore_errors=True)
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        db.commit()
    import shutil
    ws = workspace_repo_dir(RK)
    if os.path.isdir(ws):
        shutil.rmtree(ws, ignore_errors=True)


def _seed_event(seq: int, issue_id: str):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, seq, issue_id, event_type, status, created_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            (RK, seq, issue_id, "test", "completed"))
        db.commit()


def _write_archive(issue_num, segs):
    """构造 event.json（等价 init_event_json 产出）"""
    path = os.path.join(workspace_repo_dir(RK), "issues", str(issue_num), "event.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "issue": {"number": issue_num, "title": "t", "status": "open", "created_at": ""},
        "status": "running", "created_at": "", "updated_at": "",
        "pipeline": segs,
        "summary": "",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def _read_archive(issue_num):
    path = os.path.join(workspace_repo_dir(RK), "issues", str(issue_num), "event.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _seg(seq, agent="vod", output_kwargs=None):
    seg = {
        "seq": seq, "agent": agent, "event_type": "test", "runtime": "r", "skill": "s",
        "status": "running", "exit_code": None, "duration": None,
        "trigger": {}, "input": {},
        "output": {"actions": [], "conclusion": "", "artifacts": [], "next_step": "", "issue_summary": ""},
        "a2a_to": None, "logs": [],
    }
    if output_kwargs:
        seg["output"].update(output_kwargs)
    return seg


# ── _locate_by_seq 精确查 ──

def test_locate_by_seq_exact_hit_without_limit():
    """无 hint：events 表按 seq 精确查（不依赖 limit=200）命中 issue_num。"""
    from src.api.event_archive import _locate_by_seq
    _seed_event(7892, "#4")
    num, path = _locate_by_seq(RK, 7892, "")
    assert num == "4"
    assert path.replace("\\", "/").endswith("issues/4/event.json")


def test_locate_by_seq_miss_returns_empty():
    """events 无记录 + 无目录 → 返回 ("", "")"""
    from src.api.event_archive import _locate_by_seq
    num, path = _locate_by_seq(RK, 999999, "")
    assert num == "" and path == ""


def test_locate_by_seq_hint_beats_scan():
    from src.api.event_archive import _locate_by_seq
    _seed_event(7892, "#4")
    num, path = _locate_by_seq(RK, 7892, "#99")   # hint 优先
    assert num == "99"


# ── finalize 结构化保护 ──

def test_finalize_structured_writes_output():
    from src.api.event_archive import finalize_event_archive
    _write_archive(4, [_seg(7892)])
    finalize_event_archive(
        RK, 7892, "completed", exit_code=0, duration=1.5,
        output={"actions": [{"cmd": "x"}], "conclusion": "结论A", "artifacts": [], "next_step": "", "issue_summary": ""})
    arch = _read_archive(4)
    seg = arch["pipeline"][0]
    assert seg["output"]["conclusion"] == "结论A"
    assert seg["output"]["actions"] == [{"cmd": "x"}]
    assert seg["status"] == "completed"


def test_finalize_nonstructured_does_not_downgrade_structured():
    """已有结构化段 + 非结构化收尾 → 段不被降级。"""
    from src.api.event_archive import finalize_event_archive
    _write_archive(4, [_seg(7892, output_kwargs={"conclusion": "已结构化"})])
    # 先结构化 finalize
    finalize_event_archive(RK, 7892, "completed", exit_code=0,
                           output={"actions": [{"cmd": "x"}], "conclusion": "结论A", "artifacts": [], "next_step": "", "issue_summary": ""})
    # 再非结构化 finalize（output=None）
    finalize_event_archive(RK, 7892, "completed", exit_code=0, output=None)
    arch = _read_archive(4)
    seg = arch["pipeline"][0]
    assert seg["output"]["conclusion"] == "结论A"        # 未被覆盖
    assert seg["output"]["actions"] == [{"cmd": "x"}]


def test_finalize_nonstructured_on_never_structured_keeps_empty():
    """从未结构化 + 非结构化 → 保持 init 空模板（旧语义）。"""
    from src.api.event_archive import finalize_event_archive
    _write_archive(4, [_seg(7892)])                       # 空模板
    finalize_event_archive(RK, 7892, "completed", exit_code=0, output=None)
    arch = _read_archive(4)
    seg = arch["pipeline"][0]
    assert seg["output"]["conclusion"] == ""
    assert seg["output"]["actions"] == []
    assert seg["status"] == "completed"                   # 状态仍更新


def test_finalize_unlocatable_warns_and_noop():
    """定位失败：WARNING 日志 + 不 crash。"""
    from src.api.event_archive import finalize_event_archive
    # 无 events、无目录
    finalize_event_archive(RK, 88888, "completed", exit_code=0,
                           output={"actions": [{"cmd": "x"}], "conclusion": "c", "artifacts": [], "next_step": "", "issue_summary": ""})
    assert _read_archive(4) is None                        # 未写入任何文件