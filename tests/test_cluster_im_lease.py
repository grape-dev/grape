"""集群 IM：一致哈希归属 + lease 接管保护 + 通知可观测（docs/weekly-06/cluster-im-lease-stability-design.md）。"""
import os
import sys
import asyncio

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.im import bot_lease as bl
from src.im import feishu_async as fa
from src.session_manager import db, db_lock


def setup_module():
    init_database()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_im_instance")
        c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'imtest-%'")
        db.commit()


class TestImRing:
    def test_deterministic_and_sticky(self):
        bots = ["cli_a1", "cli_b2", "cli_c3", "cli_d4", "cli_e5", "cli_f6"]
        ring = fa._im_ring(["inst-a", "inst-b", "inst-c"], 50)
        first = {b: fa._im_owner(ring, b) for b in bots}
        # 确定性：同环两次计算一致
        assert first == {b: fa._im_owner(ring, b) for b in bots}
        # 归属只能落在实例集合内
        assert set(first.values()) <= {"inst-a", "inst-b", "inst-c"}
        assert len(set(first.values())) >= 2  # 尽量覆盖多个实例

    def test_minimal_migration_on_join(self):
        bots = [f"cli_t{i}" for i in range(200)]
        ring_a = fa._im_ring(["a", "b", "c"], 50)
        ring_b = fa._im_ring(["a", "b", "c", "d"], 50)  # 新实例 d 加入
        moved = sum(1 for b in bots if fa._im_owner(ring_a, b) != fa._im_owner(ring_b, b))
        # 一致性哈希：新增实例只扰动一部分（多数保持粘性）
        assert 0 < moved < 120

    def test_empty_ring_owner_empty(self):
        assert fa._im_owner([], "x") == ""


class TestLeaseTakeoverProtection:
    def _bot(self, app_id="imtest-1", expires_sql=None, owner=""):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_id,))
            if expires_sql:
                c.execute(
                    f"INSERT INTO global_im_bot_config (bot_app_id, bot_type, is_active, lease_token, "
                    f"lease_expires_at, lease_owner) VALUES (?, 'chat_bot', 1, 'tok', {expires_sql}, NULLIF(?,''))",
                    (app_id, owner),
                )
            else:
                c.execute(
                    "INSERT INTO global_im_bot_config (bot_app_id, bot_type, is_active, lease_token) "
                    "VALUES (?, 'chat_bot', 1, 'tok')",
                    (app_id,),
                )
            db.commit()

    def _alive(self, inst, age_before_s=0):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_im_instance WHERE instance_id=?", (inst,))
            c.execute(
                "INSERT INTO grape_im_instance (instance_id, hostname, ip, last_heartbeat) "
                "VALUES (?, 'h', '', datetime('now', ?))",
                (inst, f"-{age_before_s} seconds"),
            )
            db.commit()

    def test_owner_alive_recent_expiry_not_stolen(self):
        # lease 已过期 20s，但 owner instB 心跳存活 → 60s 保护窗内不可被 C 接管
        self._bot(expires_sql="datetime('now','-20 seconds')", owner="instB")
        self._alive("instB", age_before_s=5)
        c = bl.BotLeaseStore(instance_id="instC")
        assert c.try_acquire("imtest-1", steal_after=60, instance_timeout=45) is None

    def test_owner_alive_past_steal_window_forced(self):
        # lease 已过期 200s，owner instB 仍心跳存活 → 超过 steal_after 允许强制接管（防假死）
        self._bot(expires_sql="datetime('now','-200 seconds')", owner="instB")
        self._alive("instB", age_before_s=5)
        c = bl.BotLeaseStore(instance_id="instC")
        tok = c.try_acquire("imtest-1", steal_after=60, instance_timeout=45)
        assert tok  # 允许接管

    def test_owner_dead_stolen(self):
        # lease 已过期，owner instB 心跳超时 → 可接管
        self._bot(expires_sql="datetime('now','-120 seconds')", owner="instB")
        self._alive("instB", age_before_s=300)  # 5 分钟前心跳 → 失联
        c = bl.BotLeaseStore(instance_id="instC")
        assert c.try_acquire("imtest-1", steal_after=60, instance_timeout=45)

    def test_heartbeat_and_active_instances(self):
        self._alive("imtest-heart", age_before_s=5)
        store = bl.BotLeaseStore(instance_id="imtest-heart")
        actives = store.list_active_instances(timeout=45)
        assert "imtest-heart" in actives
        assert store.is_owner_alive("imtest-heart", timeout=45) is True
        _clean()
        assert "imtest-heart" not in store.list_active_instances(timeout=45)


class TestNotifyObservability:
    def test_resolve_miss_reasons(self):
        from src.api import issues_center as ic
        # no_bm
        assert ic._resolve_user_chat_bot(None, 1) is None
        # 无会话记录 → no_session_row
        class _Bm:
            def get_all_bots(self):
                return {}
        assert ic._resolve_user_chat_bot(_Bm(), 999999) is None

    def test_resolve_bot_not_in_memory(self):
        from src.api import issues_center as ic
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=91001")
            c.execute("INSERT INTO chat_agent_session (id, user_id, im_app_id) VALUES ('imobs-s', 91001, 'cli_imobs')")
            db.commit()
        class _Bm:
            def get_all_bots(self):
                return {}  # 与 session 记录不一致 → bot_not_in_memory（#9708 场景）
        try:
            assert ic._resolve_user_chat_bot(_Bm(), 91001) is None
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM chat_agent_session WHERE user_id=91001")
                db.commit()

    def test_pending_review_notify_skip_no_ctx_counter(self):
        import asyncio as _a
        from src.api import issues_center as ic
        ic._IM_NOTIFY_COUNTERS.clear()
        _a.run(ic._run_pending_review_notify(None, None, 1, "", "", "审核点", "ref",
                                             "gitcode:org/test", 1, {}))
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1

    def test_pending_review_notify_send_ok_counter(self):
        import asyncio as _a
        from unittest.mock import AsyncMock
        from src.api import issues_center as ic
        ic._IM_NOTIFY_COUNTERS.clear()
        ctx = type("Ctx", (), {"name": "chat_bot_cli_x"})()
        api = AsyncMock()
        api.send_card_to_user = AsyncMock(return_value="mid")
        _a.run(ic._run_pending_review_notify(ctx, api, 1, "", "ou_1", "审核点", "",
                                             "gitcode:org/test", 1, {}))
        counts = ic.get_im_notify_counters()
        assert counts.get("sent_ok") == 1
        assert counts.get("skipped_no_ctx", 0) == 0
        api.send_card_to_user.assert_awaited_once()


class TestHeartbeat:
    """heartbeat() 幂等与实例存活判定（cluster-seq-11130 根因回归：UPSERT 需跨后端可用）。"""

    def _heartbeat(self):
        bl.BotLeaseStore(instance_id="hb-inst").heartbeat(hostname="h", ip="")

    def test_heartbeat_writes_instance(self):
        _clean()
        try:
            self._heartbeat()
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT instance_id, hostname, im_enabled, last_heartbeat "
                    "FROM grape_im_instance WHERE instance_id='hb-inst'").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "hb-inst" and rows[0][1] == "h"
            assert rows[0][2] == 1
            assert rows[0][3]  # last_heartbeat 非空
        finally:
            _clean()

    def test_heartbeat_idempotent_no_duplicate_row(self):
        _clean()
        try:
            self._heartbeat()
            self._heartbeat()  # 重复心跳不产生第二行
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT instance_id FROM grape_im_instance WHERE instance_id='hb-inst'").fetchall()
            assert len(rows) == 1
        finally:
            _clean()

    def test_list_active_instances_returns_heartbeated(self):
        _clean()
        try:
            self._heartbeat()
            actives = bl.BotLeaseStore(instance_id="x").list_active_instances(timeout=45)
            assert "hb-inst" in actives
        finally:
            _clean()

    def test_owner_alive_true_after_heartbeat(self):
        _clean()
        try:
            self._heartbeat()
            assert bl.BotLeaseStore(instance_id="x").is_owner_alive("hb-inst", timeout=45) is True
        finally:
            _clean()

    def test_owner_alive_false_when_no_heartbeat(self):
        _clean()
        try:
            store = bl.BotLeaseStore(instance_id="x")
            assert store.is_owner_alive("never-heartbeat", timeout=45) is False
        finally:
            _clean()