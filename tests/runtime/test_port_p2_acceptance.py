"""P2 验收：本批 6 组在 arch 布局上的行为契约（#1948 / #1962 / #1954 / #1961 / #1942 / #1963）。

只驱动真实函数与真实 git 临时仓；不复制业务逻辑。

跑法（仓库根目录）：PYTHONPATH=. python -m pytest -q tests/runtime/test_port_p2_acceptance.py
"""
import json
import os
import re
import subprocess

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:acme/port-p2"
NUM = "4001"
SCENE = "9001"


def setup_module():
    init_database()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_skipped WHERE repo_key=?", (REPO,))
        db.commit()


def _seed(agent, status="completed", offset=0, ctx="seed"):
    from src.api.event_store import append_event
    from src.api.time_utils import db_now_str
    seq = append_event(repo_key=REPO, event_type="a2a_event", title=f"A2A: {agent}",
                       issue_id=f"#{NUM}", source="a2a", agent_type=agent,
                       summary=f"A2A: → {agent}",
                       payload={"issue_number": NUM, "context": ctx}, status=status)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET created_at=?, scene_id=? WHERE seq=?",
                  (db_now_str(-offset), SCENE, seq))
        db.commit()
    return seq


NODES = [{"id": "vod", "next": "issue"}, {"id": "issue", "next": "coding"},
         {"id": "coding", "next": "qa"}, {"id": "qa", "next": "sre"}, {"id": "sre"}]


class TestReverseDispatchAndWindow:
    def setup_method(self):
        _clean()

    def test_caller_self_excluded_forwarding(self):
        """#1948：qa 自己 running 时打回 coding，排除自身后放行。"""
        from src.api.a2a_dispatch_guard import find_running_downstream
        qa_seq = _seed("qa", status="running")
        assert find_running_downstream(REPO, NUM, "coding", SCENE, NODES).get("seq") == qa_seq
        assert find_running_downstream(REPO, NUM, "coding", SCENE, NODES,
                                       exclude_seq=qa_seq) == {}

    def test_window_and_payload_exclusion(self):
        """#1962：120s 窗内拦、排除源事件后放行、2h 前出窗。"""
        from src.api.a2a_dispatch_guard import find_window_duplicate
        recent = _seed("coding", status="completed", offset=60)
        assert find_window_duplicate(REPO, NUM, "coding", SCENE).get("seq") == recent
        assert find_window_duplicate(REPO, NUM, "coding", SCENE, exclude_seq=recent) == {}
        _seed("coding", status="completed", offset=7200)

    def test_parse_ts_sqlite_semantics(self):
        """#1962：sqlite 下墙钟按 UTC 解释（不偏移 8 小时）。"""
        from src.api.a2a_dispatch_guard import _parse_ts
        assert _parse_ts("2026-11-11 10:09:37").isoformat() == "2026-11-11T10:09:37+00:00"


class TestDeliveryBaseline:
    def _mkrepo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        g = lambda *a: subprocess.run(["git", "-c", "user.name=alice",
                                       "-c", "user.email=alice@example.com", *a],
                                      cwd=path, capture_output=True, text=True)
        g("init", "-b", "main", "-q")
        for name in ("a.txt", "b.txt"):
            (path / name).write_text(name)
            g("add", name)
            g("commit", "-q", "-m", f"add {name}")
        c1 = g("rev-parse", "HEAD~1").stdout.strip()
        c2 = g("rev-parse", "HEAD").stdout.strip()
        g("update-ref", "refs/remotes/origin/feat/issue-9", c2)
        return g, c1, c2

    def test_alignment_clean_and_dirty(self, tmp_path):
        """#1954：干净树对齐到交付 sha；未跟踪残留按 AC 跳过（避免污染取证）。"""
        from src.domains.runtime.engine import repogit
        g, c1, c2 = self._mkrepo(tmp_path / "base")
        wt = tmp_path / "qa-wt"
        g("worktree", "add", "--detach", str(wt), c1)
        out = repogit.align_worktree_to_delivery(str(wt), c2)
        assert out["aligned"] is True and out["new_sha"] == c2
        (wt / "leftover.log").write_text("x")
        out2 = repogit.align_worktree_to_delivery(str(wt), c1)
        assert out2["aligned"] is False and out2["reason"] == "dirty_skip"

    def test_guard_blocks_on_mismatch_and_passes_on_missing(self, tmp_path):
        """#1954：sha 不一致 → 阻断；sha 缺失 → 降级放行。"""
        from src.domains.runtime.engine import repogit, worktree_guard
        g, c1, c2 = self._mkrepo(tmp_path / "base")
        wt = tmp_path / "qa-wt"
        g("worktree", "add", "--detach", str(wt), c1)
        bad = worktree_guard.check_delivery_alignment(str(wt), c2)
        assert bad["ok"] is False and bad["reason"] == "baseline_mismatch"
        ok = worktree_guard.check_delivery_alignment(str(wt), "")
        assert ok["ok"] is True and ok["reason"] == "delivery_sha_missing"


class TestStaticAndDocs:
    def test_frontend_versions_and_keys(self):
        """#1961：语言包键 + 三处版本串均已 bump。

        静态根在 Vue 迁移后由 src/api/static/ 迁到 src/api/frontend/shell/；
        具体版本号由缓存版本 ratchet 守卫（tests/api/test_frontend_cache_version_ratchet.py
        / test_i18n_locale_cache_ratchet.py），本用例只锁「语言包键 + 版本串机制」。
        """
        root = os.path.join(ROOT, "src", "api", "frontend", "shell")
        for lang in ("zh", "en"):
            with open(os.path.join(root, "locales", lang, "tasks.json"), encoding="utf-8") as f:
                assert json.load(f).get("searching"), lang
        with open(os.path.join(root, "index.html"), encoding="utf-8") as f:
            html = f.read()
        assert "__I18N_CACHE_VER__=" in html, "index.html 缺语言包缓存版本键"
        assert re.search(r'/static-vue/viewjs/[a-z0-9-]+\.js\?v=\d+', html), (
            "index.html 未带 ?v= 引用 Vue 视图 chunk"
        )

    def test_force_hint_and_docs(self):
        """#1942：提示三要素 + 两处文档口径。"""
        from src.domains.runtime.routes.webhook import _a2a_force_resend_hint
        txt = _a2a_force_resend_hint()
        for token in ("force=true to dispatch immediately", "top level",
                      "payload.force=true", "idempotency_key", "issue+scene+agent"):
            assert token in txt, token
        for doc in ("docs/openapi/12-a2a.md", "help/api/12-a2a.md"):
            with open(os.path.join(ROOT, doc), encoding="utf-8") as f:
                body = f.read()
            assert "payload.force" in body, doc
            assert "重复交办与恢复" in body, doc