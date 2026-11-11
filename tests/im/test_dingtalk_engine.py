"""钉钉工厂注册 + 双安装会话隔离测试（Issue #936 P2，设计文档 §7.7/§8-P2 验收）。

验收项：
  - engine_wiring 注册 Type.DINGTALK 工厂（im_engine 核心 src/im/engine、
    src/im/channel 一行不改——第三平台零改核心）；
  - 同一用户飞书（user 轨）+ 钉钉（user 轨）双安装，会话绑定按
    (installation_id, channel_chat_id) 隔离，binding 行 channel_type 真实。
"""
import asyncio
import importlib.util
import os
import sqlite3

import pytest

from src.im import engine_wiring
from src.im.channel.channel import CancelContext, ChannelConfig
from src.im.channel.message import InboundMessage, Source
from src.im.channel.registry import ChannelRegistry
from src.im.channel.types import ChatType, MsgType, Scope, Type
from src.im.engine_wiring import ImEngine, SqliteBindingStore, run_startup_migration


def _legacy_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_agent_session (
            id INTEGER PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE IF NOT EXISTS devops_agent_session (
            id INTEGER PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE IF NOT EXISTS unified_user_mapping (
            user_id INTEGER, gitcode_user TEXT, feishu_open_id TEXT,
            feishu_name TEXT, gitcode_nickname TEXT, app_id TEXT
        );
        """
    )
    conn.commit()


class FakeApi:
    def __init__(self):
        self.cards = []
        self.replies = []
        self.reactions = []

    async def add_reaction(self, ctx, message_id, emoji="OK"):
        self.reactions.append((message_id, emoji))

    async def send_card_to_chat(self, ctx, chat_id, text, **kw):
        self.cards.append((chat_id, text))
        return "om_card"

    async def reply_text(self, ctx, message_id, text):
        self.replies.append((message_id, text))
        return "om_reply"


class FakeChannel:
    """connect 挂起（不触网）；暴露 cfg.inbound_handler 供测试驱动 Router。"""

    def __init__(self, cfg, sender):
        self.cfg = cfg
        self.sender = sender

    async def connect(self, ctx):
        await asyncio.sleep(3600)

    async def disconnect(self):
        pass


class NoopLease:
    def try_acquire(self, key, steal_after=60, instance_timeout=45.0):
        return f"tok-{key}"

    def renew(self, key, token):
        return True

    def release(self, key, token):
        pass

    def batch_renew(self, held):
        return []

    def list_expired_or_unheld(self, cursor="", limit=100):
        return []


def _feishu_msg(mid, text="hello feishu"):
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.FEISHU, chat_id="oc-feishu-1",
                      chat_type=ChatType.P2P, sender_id="ou_9"),
        type=MsgType.TEXT, text=text, command_text=text,
    )


def _backfill_001(path: str) -> None:
    """A2 Phase B 后启动迁移不再回填：测试里显式跑一次 001 回填。"""
    _mig = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                        "migrations", "001_channel_tables.py")
    spec = importlib.util.spec_from_file_location("migrations_001_ding", _mig)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(path)
    module.ensure_channel_tables(conn)
    module.backfill_installations(conn)
    conn.close()
    run_startup_migration(path)     # 其余渠道表（消息/去重等）


def _dingtalk_msg(mid, text="hello dingtalk"):
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.DINGTALK, chat_id="cid-ding-1",
                      chat_type=ChatType.P2P, sender_id="staff_001"),
        type=MsgType.TEXT, text=text, command_text=text,
    )


@pytest.fixture
def dual_db(tmp_path):
    path = str(tmp_path / "dual.db")
    conn = sqlite3.connect(path)
    _legacy_tables(conn)
    # 同一用户 101 的飞书 user 轨安装（legacy 回填来源）
    conn.execute(
        "INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) "
        "VALUES (101, '', 'app_feishu_1', 'secf')")
    conn.commit()
    conn.close()
    # 回填飞书安装后，手工插入钉钉安装（P2：grape_t_channel_installation 主写）
    _backfill_001(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO grape_t_channel_installation (id, channel_type, scope, owner_user_id, "
        "repo_key, config, status, legacy_app_id) VALUES "
        "('inst-dt-1', 'dingtalk', 'user', '101', '', "
        "'{\"app_id\": \"ding_app_1\", \"app_secret\": \"secd\"}', "
        "'active', 'ding_app_1')")
    # 双安装身份绑定（grape_t_channel_sender_identity 视图）
    feishu_inst = conn.execute(
        "SELECT id FROM grape_t_channel_installation WHERE channel_type='feishu'").fetchone()[0]
    conn.execute(
        "INSERT INTO grape_t_channel_sender_identity "
        "(installation_id, platform_user_id, user_id) "
        "VALUES (?, 'ou_9', '101')", (feishu_inst,))
    conn.execute(
        "INSERT INTO grape_t_channel_sender_identity "
        "(installation_id, platform_user_id, user_id) "
        "VALUES ('inst-dt-1', 'staff_001', '101')")
    conn.commit()
    conn.close()
    return path


def _build_engine(db_path, rec):
    return ImEngine(
        db_path,
        registry=ChannelRegistry(),
        lease_store=NoopLease(),
        api_factory=lambda: FakeApi(),
        ctx_factory=lambda inst: type("C", (), {
            "app_id": (inst.get("config") or {}).get("app_id", ""),
            "app_secret": (inst.get("config") or {}).get("app_secret", ""),
            "bot_type": "vod_bot" if inst.get("scope") == "repo" else "chat_bot",
        })(),
        process_chat=lambda ctx, api, parsed: rec.append(
            ("chat", ctx.app_id, parsed["chat_id"],
             parsed["text"])) or asyncio.sleep(0),
        process_vod=lambda ctx, api, parsed: rec.append(
            ("vod", ctx.app_id, parsed["text"])) or asyncio.sleep(0),
        channel_builder=FakeChannel,
        scan_interval=3600,
    )


def test_dingtalk_factory_registered():
    """工厂注册：Type.DINGTALK 可 build（注册成功 = 钉钉可用）。"""
    from src.im.adapters.dingtalk.channel import DingtalkChannel
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    engine = ImEngine("unused.db", registry=ChannelRegistry(), lease_store=NoopLease(),
                      scan_interval=3600)
    cfg = ChannelConfig(type=Type.DINGTALK, raw={"app_id": "ding_x", "app_secret": "s"},
                        installation_id="inst-x", scope=Scope.USER)
    built = engine._dingtalk_factory(cfg)
    assert built is not None
    assert isinstance(built, DingtalkChannel)
    assert isinstance(built._sender, DingtalkSender) or built._sender is not None
    assert built._cfg.inbound_handler is not None


async def test_dual_install_session_isolation(dual_db):
    """双安装隔离（P2 验收）：同一用户 101 飞书 + 钉钉双安装，
    两个会话绑定行各自独立、channel_type 真实、互不串线。"""
    rec = []
    engine = _build_engine(dual_db, rec)
    await engine.start()
    await asyncio.sleep(0.2)
    try:
        channels = engine.supervisor._channels
        assert len(channels) == 2, "feishu + dingtalk 双安装都应建渠道"
        by_type = {ch.cfg.type: ch for ch, _ in channels.values()}

        exc = await by_type[Type.FEISHU].cfg.inbound_handler(
            CancelContext(), _feishu_msg("om_f1", "hello feishu"))
        assert exc is None
        exc = await by_type[Type.DINGTALK].cfg.inbound_handler(
            CancelContext(), _dingtalk_msg("mid_d1", "hello dingtalk"))
        assert exc is None
        await asyncio.sleep(0.8)      # debounce 窗口

        # 双轨派发都走 chat handler，且 app/chat 各自正确
        assert ("chat", "app_feishu_1", "oc-feishu-1", "hello feishu") in rec
        assert ("chat", "ding_app_1", "cid-ding-1", "hello dingtalk") in rec

        # 会话绑定行隔离：两条独立绑定，channel_type 与渠道一致
        conn = sqlite3.connect(dual_db)
        rows = conn.execute(
            "SELECT installation_id, channel_chat_id, channel_type "
            "FROM grape_t_channel_chat_session_binding").fetchall()
        conn.close()
        assert len(rows) == 2
        by_chat = {r[1]: r for r in rows}
        assert by_chat["oc-feishu-1"][2] == "feishu"
        assert by_chat["cid-ding-1"][2] == "dingtalk"
        assert by_chat["oc-feishu-1"][0] != by_chat["cid-ding-1"][0]
    finally:
        await engine.stop()


async def test_typed_binding_store_writes_channel_type(dual_db):
    """_TypedBindingStore：dingtalk upsert 落真实 channel_type。"""
    from src.im.engine.session import SessionBinding
    store = SqliteBindingStore(dual_db)
    typed = engine_wiring._TypedBindingStore(store, "dingtalk")
    b = SessionBinding(installation_id="inst-dt-1", binding_key="cid-ding-9",
                       chat_type="p2p", scope=Scope.USER, agent_type="chat")
    typed.upsert(b)
    got = store.get("inst-dt-1", "cid-ding-9")
    assert got is not None and got.binding_key == "cid-ding-9"
    conn = sqlite3.connect(dual_db)
    ct = conn.execute(
        "SELECT channel_type FROM grape_t_channel_chat_session_binding "
        "WHERE channel_chat_id='cid-ding-9'").fetchone()[0]
    conn.close()
    assert ct == "dingtalk"


def test_binding_store_default_stays_feishu(dual_db):
    """默认 channel_type=feishu：旧调用路径（飞书工厂）行为不变。"""
    from src.im.engine.session import SessionBinding
    store = SqliteBindingStore(dual_db)
    store.upsert(SessionBinding(installation_id="i", binding_key="oc-x"))
    conn = sqlite3.connect(dual_db)
    ct = conn.execute(
        "SELECT channel_type FROM grape_t_channel_chat_session_binding "
        "WHERE channel_chat_id='oc-x'").fetchone()[0]
    conn.close()
    assert ct == "feishu"
