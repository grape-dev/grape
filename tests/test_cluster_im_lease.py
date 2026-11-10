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
        # A2 M3：租约在 grape_t_channel_lease（键 "feishu:imtest-*"），归属校验看 grape_t_channel_installation
        c.execute("DELETE FROM grape_t_channel_lease WHERE lease_key LIKE '%imtest-%'")
        c.execute("DELETE FROM grape_t_channel_installation WHERE id LIKE 'imtest-%'")
        db.commit()


class TestImRing:
    """一致性哈希环语义（旧 IM 下线 M1：实现从 feishu_async 的 _im_ring/_im_owner
    迁到新引擎的 src.im.engine.hashring.build_ring/owner，断言不变）。"""

    @staticmethod
    def _ring(instances):
        from src.im.engine.hashring import build_ring
        return build_ring(instances)

    @staticmethod
    def _owner(ring, key):
        from src.im.engine.hashring import owner
        return owner(ring, key)

    def test_deterministic_and_sticky(self):
        bots = ["cli_a1", "cli_b2", "cli_c3", "cli_d4", "cli_e5", "cli_f6"]
        ring = self._ring(["inst-a", "inst-b", "inst-c"])
        first = {b: self._owner(ring, b) for b in bots}
        # 确定性：同环两次计算一致
        assert first == {b: self._owner(ring, b) for b in bots}
        # 归属只能落在实例集合内
        assert set(first.values()) <= {"inst-a", "inst-b", "inst-c"}
        assert len(set(first.values())) >= 2  # 尽量覆盖多个实例

    def test_minimal_migration_on_join(self):
        bots = [f"cli_t{i}" for i in range(200)]
        ring_a = self._ring(["a", "b", "c"])
        ring_b = self._ring(["a", "b", "c", "d"])  # 新实例 d 加入
        moved = sum(1 for b in bots if self._owner(ring_a, b) != self._owner(ring_b, b))
        # 一致性哈希：新增实例只扰动一部分（多数保持粘性）
        assert 0 < moved < 120

    def test_empty_ring_owner_empty(self):
        assert self._owner([], "x") == ""


class TestLeaseTakeoverProtection:
    def _bot(self, key="feishu:imtest-1", expires_sql=None, owner=""):
        """种一条 active 安装 + 对应的 grape_t_channel_lease 行（键 = "<type>:<installation_id>"）。"""
        installation_id = key.split(":", 1)[-1]
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO grape_t_channel_installation (id, channel_type, scope, status) "
                "VALUES (?, 'feishu', 'user', 'active')", (installation_id,))
            c.execute("DELETE FROM grape_t_channel_lease WHERE lease_key=?", (key,))
            if expires_sql:
                c.execute(
                    f"INSERT INTO grape_t_channel_lease (lease_key, channel_type, installation_id, "
                    f"lease_token, lease_expires_at, lease_owner) "
                    f"VALUES (?, 'feishu', ?, 'tok', {expires_sql}, NULLIF(?,''))",
                    (key, installation_id, owner),
                )
            else:
                c.execute(
                    "INSERT INTO grape_t_channel_lease (lease_key, channel_type, installation_id, lease_token) "
                    "VALUES (?, 'feishu', ?, 'tok')",
                    (key, installation_id),
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
        assert c.try_acquire("feishu:imtest-1", steal_after=60, instance_timeout=45) is None

    def test_owner_alive_past_steal_window_forced(self):
        # lease 已过期 200s，owner instB 仍心跳存活 → 超过 steal_after 允许强制接管（防假死）
        self._bot(expires_sql="datetime('now','-200 seconds')", owner="instB")
        self._alive("instB", age_before_s=5)
        c = bl.BotLeaseStore(instance_id="instC")
        tok = c.try_acquire("feishu:imtest-1", steal_after=60, instance_timeout=45)
        assert tok  # 允许接管

    def test_owner_dead_stolen(self):
        # lease 已过期，owner instB 心跳超时 → 可接管
        self._bot(expires_sql="datetime('now','-120 seconds')", owner="instB")
        self._alive("instB", age_before_s=300)  # 5 分钟前心跳 → 失联
        c = bl.BotLeaseStore(instance_id="instC")
        assert c.try_acquire("feishu:imtest-1", steal_after=60, instance_timeout=45)

    def test_heartbeat_and_active_instances(self):
        self._alive("imtest-heart", age_before_s=5)
        store = bl.BotLeaseStore(instance_id="imtest-heart")
        actives = store.list_active_instances(timeout=45)
        assert "imtest-heart" in actives
        assert store.is_owner_alive("imtest-heart", timeout=45) is True
        _clean()
        assert "imtest-heart" not in store.list_active_instances(timeout=45)


class TestHeartbeatFutureGuard:
    """心跳"未来值"护栏 + 周期清理（2026-10-18 加固，起因 issue #365 的存量 CST 行/滚动升级窗口）。"""

    def _hb_at(self, inst, ts_str):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_im_instance WHERE instance_id=?", (inst,))
            c.execute(
                "INSERT INTO grape_im_instance (instance_id, hostname, ip, last_heartbeat) "
                "VALUES (?, 'h', '', ?)", (inst, ts_str))
            db.commit()

    def test_future_heartbeat_not_treated_as_alive(self):
        """CST 口径写入的行（UTC 视角下 +8h）不得算活 —— 否则幽灵实例继续占一致性哈希环。"""
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        self._hb_at("imtest-future", future)
        store = bl.BotLeaseStore(instance_id="imtest-me")
        assert "imtest-future" not in store.list_active_instances(timeout=45)
        assert store.is_owner_alive("imtest-future", timeout=45) is False
        _clean()

    def test_small_clock_skew_still_alive(self):
        """容差内的时钟漂移（未来 30s）仍算活，避免误杀正常实例。"""
        from datetime import datetime, timedelta, timezone
        skewed = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        self._hb_at("imtest-skew", skewed)
        store = bl.BotLeaseStore(instance_id="imtest-me")
        assert "imtest-skew" in store.list_active_instances(timeout=45)
        assert store.is_owner_alive("imtest-skew", timeout=45) is True
        _clean()

    def test_purge_stale_instances_keeps_fresh(self):
        """周期清理：删 24h 前的行、保留新鲜行（实例靠心跳 UPSERT 自愈）。"""
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        fresh = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        self._hb_at("imtest-old", old)
        self._hb_at("imtest-fresh", fresh)
        n = bl.purge_stale_instances(older_than_hours=24)
        assert n >= 1
        with db_lock:
            ids = [r[0] for r in db.cursor().execute(
                "SELECT instance_id FROM grape_im_instance").fetchall()]
        assert "imtest-old" not in ids and "imtest-fresh" in ids
        _clean()


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
        """A2 M2/M3 后凭证只认 grape_t_channel_installation：用户没有安装行 → 一律 None。

        （原用例语义是"会话行有 app_id 但 BotManager 内存里没有"，bm 参数已弃用，
        现在等价于"没有本人安装行"这条负路径。）
        """
        from src.api import issues_center as ic
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=91001")
            db.commit()
        class _Bm:
            def get_all_bots(self):
                return {}
        assert ic._resolve_user_chat_bot(_Bm(), 91001) is None

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