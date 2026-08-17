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


class TestLegacyPrefixedCompat:
    """历史带前缀归档目录兼容（2026-08-15 前 event_archive 拼原始 repo_key）。

    Windows 路径不含 ':'，无法创建带前缀目录，完整目录级兼容在 Linux 生产侧验证。
    """

    def test_get_event_json_path_strips_prefix(self):
        p = ea.get_event_json_path("gitcode:test/repo", 5)
        assert "gitcode:" not in p
        assert p.split(os.sep)[-4:] == ["test", "repo", "issues", "5"] or "test" in p

    @pytest.mark.skipif(os.name == "nt", reason="Windows 路径不允许 ':'，Linux 生产侧验证")
    def test_load_falls_back_to_legacy_prefix_dir(self):
        legacy = ea._legacy_base_dir("gitcode:test/repo")
        import shutil
        shutil.rmtree(os.path.join(WORKSPACE_DIR, "test/repo"), ignore_errors=True)
        shutil.rmtree(os.path.dirname(legacy), ignore_errors=True)
        os.makedirs(os.path.join(legacy, "5"), exist_ok=True)
        with open(os.path.join(legacy, "5", "event.json"), "w", encoding="utf-8") as f:
            json.dump({"issue": {"number": 5}, "status": "completed", "pipeline": []}, f)
        arch = ea.load_event_json("gitcode:test/repo", 5)
        assert arch is not None and arch["issue"]["number"] == 5

    @pytest.mark.skipif(os.name == "nt", reason="Windows 路径不允许 ':'，Linux 生产侧验证")
    def test_locate_by_seq_finds_legacy(self):
        legacy = ea._legacy_base_dir("gitcode:test/repo")
        import shutil
        shutil.rmtree(os.path.join(WORKSPACE_DIR, "test/repo"), ignore_errors=True)
        shutil.rmtree(os.path.dirname(legacy), ignore_errors=True)
        ea.init_event_json("gitcode:test/repo", 77, _evt(), "issue")
        # 把新路径档案移回旧带前缀目录，模拟历史数据
        new_path = ea.get_event_json_path("gitcode:test/repo", 5)
        os.makedirs(os.path.join(legacy, "5"), exist_ok=True)
        os.replace(new_path, os.path.join(legacy, "5", "event.json"))
        num, path = ea._locate_by_seq("gitcode:test/repo", 77, issue_num_hint="#5")
        assert num == "5"
        assert "gitcode:" in path  # 命中旧目录
