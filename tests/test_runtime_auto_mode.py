"""执行模式 auto 路由测试（execution-mode-auto-routing-design.md §五 / §3.1 / §3.6）

覆盖：runtime_execution_kind 宿主推导、未绑定/离线/吊销兜底、强制模式覆盖、
auto 下 pipeline 入队与本地执行、chat 配置 runtime_id 透出。
"""
import json, os, uuid
from datetime import datetime, timezone

from src.session_manager import db, db_lock
from src.api import event_pipeline
from src.runtime.service import (
    runtime_execution_kind,
    EXEC_MODE_DIRECT, EXEC_MODE_REMOTE,
)


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
        # 引擎隔离：清掉全部远端/全局引擎（保留本机 credential_id=0 兜底），
        # 避免其他测试残留的全局引擎被 pick_engine 选中导致执行模式不确定（随机顺序 flaky）
        c.execute("DELETE FROM agent_runtimes WHERE credential_id>0")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR id IN (900001)")
        db.commit()


def _seed_credential(name, scope="global", status="active", last_seen_at=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("h" + uuid.uuid4().hex, name, "dev-" + name, scope, "tester", status, last_seen_at),
        )
        cid = c.lastrowid
        db.commit()
        return cid


def _seed_runtime(name, credential_id, status="online", last_seen_at=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES (?,?,?,?,?,'global','',1,1)",
            (name, name, credential_id, status, last_seen_at),
        )
        rid = c.lastrowid
        db.commit()
        return rid


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class TestRuntimeExecutionKind:
    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "auto"

    def teardown_method(self):
        _clean()
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def test_remote_engine_returns_queue_remote(self):
        cid = _seed_credential("__auto_w_remote")
        rid = _seed_runtime("__auto_remote", cid, last_seen_at=_now_iso())
        assert runtime_execution_kind(rid) == EXEC_MODE_REMOTE

    def test_local_engine_returns_direct(self):
        rid = _seed_runtime("__auto_local", 0)
        assert runtime_execution_kind(rid) == EXEC_MODE_DIRECT

    def test_unbound_auto_falls_back_direct(self):
        assert runtime_execution_kind(0) == EXEC_MODE_DIRECT
        assert runtime_execution_kind(999999) == EXEC_MODE_DIRECT

    def test_stale_remote_engine_falls_back_direct(self):
        cid = _seed_credential("__auto_w_stale")
        rid = _seed_runtime("__auto_stale", cid, last_seen_at="2020-01-01T00:00:00+00:00")
        assert runtime_execution_kind(rid) == EXEC_MODE_DIRECT

    def test_revoked_credential_falls_back_direct(self):
        cid = _seed_credential("__auto_w_revoked", status="revoked")
        rid = _seed_runtime("__auto_rev", cid, last_seen_at=_now_iso())
        assert runtime_execution_kind(rid) == EXEC_MODE_DIRECT

    def test_forced_mode_overrides_auto(self):
        cid = _seed_credential("__auto_w_force")
        rid = _seed_runtime("__auto_force", cid, last_seen_at=_now_iso())
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        assert runtime_execution_kind(rid) == EXEC_MODE_REMOTE
        # queue_local 已废除：历史强制值按本机 direct 处理（engine-selection-design.md v2）
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        assert runtime_execution_kind(rid) == EXEC_MODE_DIRECT


def _evt_body(issue_id="#5", etype="issue_opened"):
    return {
        "event_type": etype,
        "payload": {"issue": {"number": 5, "title": "登录超时", "body": "x"}, "action": "opened"},
        "issue_id": issue_id,
        "repository": "owner/repo",
    }


class TestAutoPipelineRouting:
    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "auto"

    def teardown_method(self):
        _clean()
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def _bind_session(self, agent_type, runtime_id):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                "VALUES (?,?,?,?,'idle',1)",
                (str(uuid.uuid4()), "gitcode:owner/repo", agent_type, runtime_id),
            )
            db.commit()

    def test_auto_remote_binding_enqueues(self):
        cid = _seed_credential("__auto_w_pipe")
        rid = _seed_runtime("__auto_pipe", cid, last_seen_at=_now_iso())
        self._bind_session("issue", rid)
        raw = json.dumps(_evt_body()).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        event_pipeline.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            tasks = db.cursor().execute("SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=?", (rid,)).fetchone()[0]
        assert tasks >= 1, "auto + 远端绑定应入队 agent_tasks"

    def test_auto_local_binding_executes_direct(self):
        rid = _seed_runtime("__auto_pipe_local", 0)
        self._bind_session("issue", rid)
        raw = json.dumps(_evt_body()).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        event_pipeline.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            tasks = db.cursor().execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
        assert tasks == 0, "auto + 本地绑定应就地执行，不入队 agent_tasks"


class TestChatConfigRuntimeId:
    """Chat 特化（§3.6）：get_chat_config 透出 runtime_id，未绑定默认 direct。"""

    def setup_method(self):
        _clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "auto"

    def teardown_method(self):
        _clean()
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def test_get_chat_config_returns_runtime_id(self):
        from src.session_manager import get_chat_config, set_chat_config
        cid = _seed_credential("__auto_chat_w", scope="user")
        rid = _seed_runtime("__auto_chat_eng", cid, last_seen_at=_now_iso())
        set_chat_config(999, backend="__auto_chat_eng")
        cfg = get_chat_config(999)
        assert cfg["runtime_id"] == rid
        assert runtime_execution_kind(cfg["runtime_id"]) == EXEC_MODE_REMOTE

    def test_unconfigured_chat_defaults_direct(self):
        from src.session_manager import get_chat_config
        cfg = get_chat_config(888)
        assert cfg["runtime_id"] == 0
        assert runtime_execution_kind(cfg["runtime_id"]) == EXEC_MODE_DIRECT
