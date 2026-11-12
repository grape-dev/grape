"""Issue #1770 / #1973 A2A 派发门禁测试共享夹具。

两个测试模块此前各自复制约百行 `_clean/_seed/_setup/_body/_seed_event`
（#1973 复审 Standards 判断项），此处抽出共享基类，避免两处夹具发散。
"""
import os
import shutil
import uuid

from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:owner/repo"


class A2AGuardTestBase:
    """提供清理、seed、客户端与事件查询等公共夹具方法。"""

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-guard-%'")
            c.execute(
                "DELETE FROM session_api_tokens WHERE repo_key LIKE 'gitcode:owner/repo%'")
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id IN "
                "(SELECT id FROM runtime_credentials WHERE name LIKE 'guard-cred-%')")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'guard-cred-%'")
            c.execute("DELETE FROM skill WHERE skill_name='implementation' AND scope='repo'")
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            db.commit()
        try:
            from src.kernel.config import SKILLS_DIR
            shutil.rmtree(
                os.path.join(SKILLS_DIR, "repo", "owner", "repo", "implementation"),
                ignore_errors=True)
        except Exception:
            pass

    def _seed(self):
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
            except Exception:
                pass
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                "VALUES (?,?,?, 'global', 'tester', 'active')",
                ("g" + uuid.uuid4().hex, "guard-cred-" + uuid.uuid4().hex[:6], "guard-dev"))
            _cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            for _agent in ("coding", "qa"):
                c.execute(
                    "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                    "VALUES (?, 'gitcode:owner/repo', ?, ?, 'idle', 1)",
                    ("sess-guard-" + _agent, _agent, _rid))
            try:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                    "skill_path, scope, owner_id, is_active) "
                    "VALUES ('implementation','implementation','test skill','coding',"
                    "'implementation','repo','gitcode:owner/repo',1)")
            except Exception:
                pass
            db.commit()
        from src.kernel.config import SKILLS_DIR
        _sdir = os.path.join(SKILLS_DIR, "repo", "owner", "repo", "implementation")
        os.makedirs(_sdir, exist_ok=True)
        with open(os.path.join(_sdir, "SKILL.md"), "w", encoding="utf-8") as _f:
            _f.write("# implementation\n\ntest skill\n")

    def teardown_method(self):
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        return TestClient(app)

    def _setup(self):
        self._clean()
        self._seed()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        return self._client()

    def _seed_event(self, agent="qa", status="running", issue_id="#5",
                    scene_id=""):
        from src.api.event_store import append_event
        return append_event(
            repo_key=REPO_KEY, event_type="a2a_event",
            title=f"A2A: {agent}", issue_id=issue_id,
            source="a2a", agent_type=agent, summary=f"A2A: → {agent}",
            payload={"issue_number": "5", "context": f"seed-{agent}"},
            status=status, scene_id=scene_id)

    def _seed_caller_session(self, event_seq, agent="qa", session_key="task-1973"):
        """为调用方签发会话 token，并把 agent_tasks.event_seq 指到其自身事件。"""
        from src.api.auth_token import issue_session_token
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
                "issue_id, repo_key, agent_type, prompt) "
                "VALUES (?, ?, 0, ?, '#5', ?, ?, 'x')",
                (session_key, "sess-guard-" + agent, event_seq, REPO_KEY, agent))
            db.commit()
        return issue_session_token(
            session_key=session_key, repo_key=REPO_KEY, agent_type=agent)

    def _events_for(self, agent="coding", issue_id="#5"):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT seq, status FROM events WHERE repo_key=? AND issue_id=? "
                "AND event_type='a2a_event' AND agent_type=? ORDER BY seq",
                (REPO_KEY, issue_id, agent)).fetchall()

    def _skips_for(self, issue_id="#5"):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT reason, detail FROM event_skipped WHERE repo_key=? "
                "AND issue_id=? AND event_type='a2a_event' ORDER BY id",
                (REPO_KEY, issue_id)).fetchall()