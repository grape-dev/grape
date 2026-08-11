"""event.json 事件档案模块测试（见 acceptance-plan M1-A1~A8）"""
import os, json, threading, tempfile
import pytest

from src.api import event_archive as ea
from src.config import WORKSPACE_DIR


def _evt(issue_id="#5", title="登录超时"):
    return {"issue_id": issue_id, "title": title, "type": "issue_opened",
            "actor": "alice", "branch": "main", "_raw_type": "Issue Hook", "summary": "新 Issue"}


@pytest.fixture(autouse=True)
def _clean_archive():
    # 每个用例前清理测试档案目录
    base = os.path.join(WORKSPACE_DIR, "test/repo")
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    yield
    shutil.rmtree(base, ignore_errors=True)


class TestInit:
    def test_init_builds_skeleton(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue", runtime="opencode", skill="issue")
        p = ea.get_event_json_path("test/repo", 5)
        assert os.path.exists(p)
        data = json.load(open(p, encoding="utf-8"))
        assert data["issue"]["number"] == 5
        assert data["status"] == "running"
        assert len(data["pipeline"]) == 1
        seg = data["pipeline"][0]
        assert seg["seq"] == 12 and seg["agent"] == "issue"
        assert seg["runtime"] == "opencode" and seg["skill"] == "issue"
        assert seg["status"] == "running"
        assert data["summary"] == "Issue #5 处理中，尚无结论"

    def test_init_zero_skips(self):
        ea.init_event_json("test/repo", 13, _evt(issue_id="#0"), "issue")
        assert not os.path.exists(ea.get_event_json_path("test/repo", 0))


class TestAppendLog:
    def test_append_logs_all_redacted(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue", skill="issue")
        ea.append_event_archive_log("test/repo", 12, "text", "hello sk_test_123",
                                    tokens=["sk_test_123"])
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        assert len(data["pipeline"][0]["logs"]) == 1
        assert "sk_test_123" not in data["pipeline"][0]["logs"][0]["msg"]
        assert "[REDACTED]" in data["pipeline"][0]["logs"][0]["msg"]

    def test_append_unknown_seq_noop(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue")
        ea.append_event_archive_log("test/repo", 999, "text", "x")
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        assert len(data["pipeline"][0]["logs"]) == 0


class TestFinalize:
    def _init(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue", runtime="opencode", skill="issue")

    def test_finalize_overrides_summary(self):
        self._init()
        out = {"actions": ["labeled:bug"], "conclusion": "分类为 bug",
               "artifacts": ["comment"], "next_step": "A2A coding",
               "issue_summary": "Issue #5：登录超时 | 已完成: 分类bug | 下一步: coding"}
        ea.finalize_event_archive("test/repo", 12, "completed", 0, 12.3, out)
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        seg = data["pipeline"][0]
        assert seg["status"] == "completed" and seg["exit_code"] == 0
        assert seg["output"]["conclusion"] == "分类为 bug"
        assert data["summary"] == out["issue_summary"]
        assert data["status"] == "completed"

    def test_finalize_keeps_summary_without_issummary(self):
        self._init()
        out = {"actions": [], "conclusion": "无结论"}
        ea.finalize_event_archive("test/repo", 12, "failed", 1, 3.0, out)
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        assert data["summary"] == "Issue #5 处理中，尚无结论"
        assert data["status"] == "failed"

    def test_finalize_idempotent(self):
        self._init()
        out = {"conclusion": "c1", "issue_summary": "s1"}
        ea.finalize_event_archive("test/repo", 12, "completed", 0, 1.0, out)
        ea.finalize_event_archive("test/repo", 12, "completed", 0, 2.0, out)
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        assert data["summary"] == "s1"


class TestConcurrency:
    def test_concurrent_append_no_corrupt(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue")
        errors = []

        def _w(i):
            try:
                ea.append_event_archive_log("test/repo", 12, "text", f"msg {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_w, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        data = json.load(open(ea.get_event_json_path("test/repo", 5), encoding="utf-8"))
        assert len(data["pipeline"][0]["logs"]) == 20


class TestBuildContext:
    def test_build_issue_context(self):
        ea.init_event_json("test/repo", 12, _evt(), "issue", skill="issue")
        out = {"conclusion": "分类为 bug", "artifacts": ["label"], "next_step": "A2A"}
        ea.finalize_event_archive("test/repo", 12, "completed", 0, 1.0, out)
        ctx = ea.build_issue_context("test/repo", 5)
        assert "Issue #5" in ctx
        assert "分类为 bug" in ctx
        assert len(ctx) <= 4000

    def test_build_context_no_archive(self):
        assert ea.build_issue_context("test/repo", 99) == ""
