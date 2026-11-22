"""Agent 反馈 → Issue 转换器测试（feedback-openapi-expose-design.md §六 B）。

覆盖：
- 幂等：同一 (task_id, skill_name, title) 只建一个 Issue，组内全部回写
- 官方技能（scope=official）→ 官方技能仓库 + 全局 bot
- repo/user 技能 → 反馈所属仓库 + 仓库 bot token
- 转换成功回写 issue_url/number/converted_at；失败不回写留重试
- 仅 source='agent' AND issue_url='' 入选；is_deleted=1 排除
"""
import sys, os, uuid
from unittest import mock

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.kernel.session_manager import db, db_lock
import src.api.feedback_issue_converter as conv


def _insert(source="agent", title="测试", skill_name="", skill_scope="",
            repo_key="gitcode:o/r", task_id="", is_deleted=0,
            issue_url="", description="desc", issue_id=None,
            agent_type="coding", fail_summary="sum"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO user_feedback "
            "(user_id, username, category, title, description, repo_key, status, source, "
            "task_id, skill_id, skill_name, skill_scope, issue_url, issue_number, "
            "converted_at, is_deleted, agent_type, fail_summary, event_seq) "
            "VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (0, "", "bug", title, description, repo_key, source,
             task_id, 0, skill_name, skill_scope, issue_url, "",
             is_deleted, agent_type, fail_summary, issue_id))
        db.commit()
        return c.lastrowid


def _cleanup(*fids):
    with db_lock:
        for f in fids:
            db.execute("DELETE FROM user_feedback WHERE id=?", (f,))
        db.commit()


class TestFeedbackIssueConverter:
    def test_idempotent_same_task_title(self):
        """相同 (task_id, skill_name, title) 只建一个 Issue，组内全部回写。"""
        f1 = _insert(title="同题", task_id="t-1", skill_name="sk", skill_scope="official",
                     repo_key="gitcode:o/r")
        f2 = _insert(title="同题", task_id="t-1", skill_name="sk", skill_scope="official",
                     repo_key="gitcode:o/r")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 11, "url": "gitcode:issues/11"}) as m, \
                 mock.patch.object(conv, "_official_skills_target",
                                   return_value=("gitcode", "grape-dev", "grape-skills", "GB")):
                conv.convert_pending_feedback(limit=10)
            assert m.call_count == 1
            with db_lock:
                rows = db.execute(
                    "SELECT issue_url, issue_number FROM user_feedback WHERE id IN (?,?)",
                    (f1, f2)).fetchall()
            assert {r[0] for r in rows} == {"gitcode:issues/11"}
        finally:
            _cleanup(f1, f2)

    def test_official_skill_target_repo(self):
        """官方技能反馈 → 官方技能仓库目标 + 全局 bot token。"""
        fid = _insert(skill_name="grape-cli", skill_scope="official",
                      repo_key="gitcode:o/r", task_id="t-2")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 12, "url": "u"}) as m, \
                 mock.patch.object(conv, "_official_skills_target",
                                   return_value=("gitcode", "grape-dev", "grape-skills",
                                                 "GLOBAL_BOT")):
                conv.convert_pending_feedback(limit=10)
            assert m.called
            assert m.call_args[0][:4] == ("gitcode", "grape-dev", "grape-skills", "GLOBAL_BOT")
            with db_lock:
                row = db.execute("SELECT issue_url FROM user_feedback WHERE id=?",
                                 (fid,)).fetchone()
            assert row[0] == "u"
        finally:
            _cleanup(fid)

    def test_repo_skill_target_feedback_repo(self):
        """repo/user 技能 → 反馈所属仓库 + 仓库 bot token。"""
        fid = _insert(skill_name="repo-sk", skill_scope="repo",
                      repo_key="gitcode:o/r", task_id="t-3")
        try:
            with mock.patch.object(conv, "_repo_bot_token", return_value="REPO_BOT") as t, \
                 mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 13, "url": "u2"}) as m, \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            assert m.called
            assert m.call_args[0][:4] == ("gitcode", "o", "r", "REPO_BOT")
        finally:
            _cleanup(fid)

    def test_success_writes_back(self):
        """转换成功回写 issue_url/number/converted_at。"""
        fid = _insert(skill_name="sk", skill_scope="repo", task_id="t-4",
                      repo_key="gitcode:o/r")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 14, "url": "u3"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            with db_lock:
                row = db.execute(
                    "SELECT issue_url, issue_number, converted_at FROM user_feedback WHERE id=?",
                    (fid,)).fetchone()
            assert row[0] == "u3" and str(row[1]) == "14" and row[2]
        finally:
            _cleanup(fid)

    def test_failure_not_written_retryable(self):
        """转换异常不回写，可重试。"""
        fid = _insert(skill_name="sk", skill_scope="repo", task_id="t-5",
                      repo_key="gitcode:o/r")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   side_effect=RuntimeError("net")) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            with db_lock:
                row = db.execute("SELECT issue_url, converted_at FROM user_feedback WHERE id=?",
                                 (fid,)).fetchone()
            assert row[0] == "" and row[1] is None
            # 第二轮重试仍会尝试
            with mock.patch.object(conv, "_create_provider_issue",
                                   side_effect=RuntimeError("net")) as m2, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            assert m2.called
        finally:
            _cleanup(fid)

    def test_only_agent_pending_selected(self):
        """仅 source='agent' AND issue_url='' 入选；user 反馈与已转反馈不转换。"""
        a = _insert(source="agent", title="agent的", task_id="t-6",
                    skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r")
        u = _insert(source="user", title="用户的", task_id="u-6",
                    skill_name="sk2", skill_scope="repo", repo_key="gitcode:o/r")
        done = _insert(source="agent", title="已转", task_id="t-6b",
                       skill_name="sk3", skill_scope="repo", repo_key="gitcode:o/r",
                       issue_url="u_done")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 15, "url": "u4"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            titles_called = [c[0][4] for c in m.call_args_list]
            assert "agent的" in titles_called
            assert "用户的" not in titles_called
            assert "已转" not in titles_called
        finally:
            _cleanup(a, u, done)

    def test_deleted_excluded(self):
        """软删除的 agent 反馈不入选。"""
        fid = _insert(source="agent", title="删", task_id="t-7",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r",
                      is_deleted=1)
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 16, "url": "u5"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")):
                conv.convert_pending_feedback(limit=10)
            assert not m.called
        finally:
            _cleanup(fid)

class TestFeedbackClaimAndTitleDedup:
    """占位（claim）+ 同标题开放单复用：治「先建单后盖章」导致的重复建单。"""

    def test_claim_blocks_concurrent_duplicate(self):
        """同一行只能被占位一次；两轮转换只允许 create 一次。"""
        fid = _insert(source="agent", title="并发题", task_id="t-c1",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 51, "url": "u51"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")), \
                 mock.patch.object(conv, "_find_existing_open_issue", return_value=None):
                assert conv._claim(fid) is True          # 第一次抢到
                assert conv._claim(fid) is False         # 第二次抢不到（行上已有占位）
                conv._release(fid)
                conv.convert_pending_feedback(limit=10)  # 第一轮：建单并回写
                conv.convert_pending_feedback(limit=10)  # 第二轮：issue_url 已非空，不再入选
            assert m.call_count == 1
            with db_lock:
                row = db.cursor().execute(
                    "SELECT issue_url, issue_number FROM user_feedback WHERE id=?",
                    (fid,)).fetchone()
            assert row[0] == "u51" and str(row[1]) == "51"
        finally:
            _cleanup(fid)

    def test_stale_claim_reclaimed(self):
        """进程崩溃留下的过期占位会被回收并重新建单。"""
        fid = _insert(source="agent", title="过期题", task_id="t-c2",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r",
                      issue_url="pending:1:deadbeef")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 52, "url": "u52"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")), \
                 mock.patch.object(conv, "_find_existing_open_issue", return_value=None):
                conv.convert_pending_feedback(limit=10)
            assert m.call_count == 1
            with db_lock:
                row = db.cursor().execute(
                    "SELECT issue_url FROM user_feedback WHERE id=?", (fid,)).fetchone()
            assert row[0] == "u52"
        finally:
            _cleanup(fid)

    def test_reuse_existing_open_issue_by_title(self):
        """目标仓已有同标题开放单 → 不新建，只回写该单号；标题按去空白/全半角归一比对。"""
        fid = _insert(source="agent", title="复用题（已存在）", task_id="t-c3",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r")
        try:
            seen = {}

            def _fake_request(method, url, token, body=None):
                seen["url"] = url
                return [{"number": 77, "title": "复用题 (已存在)"}]

            fake_mod = type("M", (), {
                "API_BASE": "https://example.invalid/api/v5",
                "request": staticmethod(_fake_request),
            })
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 99, "url": "u99"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")), \
                 mock.patch.object(conv, "_provider_module", return_value=fake_mod):
                conv.convert_pending_feedback(limit=10)
            assert not m.called                      # 没有新建
            assert "state=opened" in seen["url"]     # GitCode 开放态口径
            with db_lock:
                row = db.cursor().execute(
                    "SELECT issue_url, issue_number FROM user_feedback WHERE id=?",
                    (fid,)).fetchone()
            assert str(row[1]) == "77" and row[0]
        finally:
            _cleanup(fid)

    def test_claim_released_on_create_failure(self):
        """建单失败必须清占位，否则该条反馈永久卡死。"""
        fid = _insert(source="agent", title="失败题", task_id="t-c4",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r")
        try:
            with mock.patch.object(conv, "_create_provider_issue",
                                   side_effect=RuntimeError("boom")) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")), \
                 mock.patch.object(conv, "_find_existing_open_issue", return_value=None):
                res = conv.convert_pending_feedback(limit=10)
            assert m.call_count == 1 and res["failed"] == 1
            with db_lock:
                row = db.cursor().execute(
                    "SELECT issue_url FROM user_feedback WHERE id=?", (fid,)).fetchone()
            assert (row[0] or "") == ""              # 占位已清，下一轮可重试
        finally:
            _cleanup(fid)


class TestRepoKeyRouteUnpack:
    """Issue #3731 根因 A：`parse_repo_key()` 是三元组契约，2 元解包必抛 ValueError。"""

    def test_repo_target_three_tuple_unpack(self):
        """带前缀 / 裸键 / 空键三态均解出 (provider, owner, repo)。"""
        assert conv._repo_target("gitcode:o/r") == ("gitcode", "o", "r")
        assert conv._repo_target("github:o/r") == ("github", "o", "r")
        assert conv._repo_target("o/r") == ("gitcode", "o", "r")
        assert conv._repo_target("") == ("gitcode", "", "")

    def test_repo_bot_token_provider_from_three_tuple(self):
        """仓库 token 解析用三元组的 provider 定位平台模块（旧实现 2 元解包直接崩）。"""
        providers = []

        class _Mod:
            @staticmethod
            def bot_token(repo_key):
                return f"T:{repo_key}"

        def _pm(provider):
            providers.append(provider)
            return _Mod

        with mock.patch.object(conv, "_provider_module", side_effect=_pm):
            assert conv._repo_bot_token("github:o/r") == "T:github:o/r"
            assert conv._repo_bot_token("o/r") == "T:o/r"
        assert providers == ["github", "gitcode"]


class TestTitleClip:
    """Issue #3731 根因 B：平台标题上限 200 字符，超长标题必须截断且查重同键。"""

    @staticmethod
    def _fake_provider_module(issues=None, seen=None):
        class _Mod:
            API_BASE = "https://example.invalid/api/v5"

            @staticmethod
            def request(method, url, token, payload=None):
                if seen is not None:
                    seen.setdefault(method, []).append(
                        payload if payload is not None else url)
                if method == "GET":
                    return list(issues or [])
                return {"number": 88}

        return _Mod

    def test_long_title_clipped_before_create(self):
        """350 字符标题投递前截断到 <=200 且带尾部省略号；短标题原样。"""
        seen = {}
        mod = self._fake_provider_module(seen=seen)
        long_title = "长" * 350
        with mock.patch.object(conv, "_provider_module", return_value=mod), \
             mock.patch("src.domains.runtime.vcs.providers.translate_inbound",
                        side_effect=lambda p, res, act, neutral, ctx: neutral):
            conv._create_provider_issue("gitcode", "o", "r", "B", long_title, "body")
            conv._create_provider_issue("gitcode", "o", "r", "B", "短标题", "body")
        long_sent, short_sent = seen["POST"][0]["title"], seen["POST"][1]["title"]
        assert len(long_sent) <= 200 and long_sent.endswith("…")
        assert long_sent.startswith("长" * 10)
        assert short_sent == "短标题"

    def test_clipped_title_reuse_existing_issue(self):
        """超长标题截断后与目标仓已有单同键 → 复用不新建（查重与建单同口径）。"""
        long_title = "超" * 350
        clipped = "超" * 199 + "…"
        fid = _insert(source="agent", title=long_title, task_id="t-clip-1",
                      skill_name="sk", skill_scope="repo", repo_key="gitcode:o/r")
        seen = {}
        try:
            mod = self._fake_provider_module(issues=[{"number": 77, "title": clipped}],
                                             seen=seen)
            with mock.patch.object(conv, "_create_provider_issue",
                                   return_value={"number": 99, "url": "u99"}) as m, \
                 mock.patch.object(conv, "_repo_bot_token", return_value="B"), \
                 mock.patch.object(conv, "_repo_target",
                                   return_value=("gitcode", "o", "r")), \
                 mock.patch.object(conv, "_provider_module", return_value=mod):
                conv.convert_pending_feedback(limit=10)
            assert not m.called                       # 查重命中，未新建
            assert "state=opened" in seen["GET"][0]
            with db_lock:
                row = db.cursor().execute(
                    "SELECT issue_url, issue_number FROM user_feedback WHERE id=?",
                    (fid,)).fetchone()
            assert str(row[1]) == "77" and row[0]
        finally:
            _cleanup(fid)
