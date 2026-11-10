"""IM 引擎装配层测试（Issue #825 P1 接线收尾）。

覆盖验收项：
  - 开关两态（env / 覆盖文件 → 秒级切回的开关读取）；
  - Router 六注入点集成（真实 sqlite 落库：绑定/持久化+dedup 同事务/命令/agent 双轨/表情回执）；
  - 回滚路径（mode 翻转 → stop 新引擎）；
  - 租约键契约（A2 M3：键即 installation 键，无翻译层）；
  - 001 迁移幂等重跑。
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from src.im.channel.channel import CancelContext, ChannelConfig
from src.im.channel.message import InboundMessage, Source
from src.im.channel.registry import ChannelRegistry
from src.im.channel.types import ChatType, MsgType, Scope, Type
from src.im import engine_wiring
from src.im.engine_wiring import (
    ImEngine, SqliteBindingStore, SqliteDedupStore,
    engine_mode, run_startup_migration,
)


# ---------------------------------------------------------------- fixtures

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


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "engine.db")
    conn = sqlite3.connect(path)
    _legacy_tables(conn)
    conn.execute(
        "INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) "
        "VALUES (101, '', 'app_chat_1', 'sec1')")
    conn.commit()
    _backfill(conn)                      # A2 Phase B：启动不再回填，夹具显式跑一次性回填
    conn.close()
    return path


def _backfill(conn) -> None:
    """跑 001 的回填（A2 Phase B 后启动迁移不再回填，测试里显式调用一次）。"""
    _mig = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                        "migrations", "001_channel_tables.py")
    spec = importlib.util.spec_from_file_location("migrations_001_wiring", _mig)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ensure_channel_tables(conn)
    module.backfill_installations(conn)


class FakeApi:
    def __init__(self):
        self.reactions = []
        self.cards = []
        self.replies = []

    async def add_reaction(self, ctx, message_id, emoji="OK"):
        self.reactions.append((ctx.app_id, message_id, emoji))
        return True

    async def send_card_to_chat(self, ctx, chat_id, text, **kwargs):
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
        self.disconnected = False

    async def connect(self, ctx):
        await asyncio.sleep(3600)

    async def disconnect(self):
        self.disconnected = True


def _fake_ctx(inst):
    scope = inst.get("scope", "user")
    return SimpleNamespace(
        app_id=inst["config"]["app_id"],
        app_secret=inst["config"].get("app_secret", ""),
        bot_type="vod_bot" if scope == "repo" else "chat_bot",
        name="fake",
    )


def _msg(mid, text="hello", sender="ou_9", chat_type=ChatType.P2P):
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.FEISHU, chat_id="oc-1", chat_type=chat_type,
                      sender_id=sender),
        type=MsgType.TEXT, text=text, command_text=text,
    )


class Recorder:
    def __init__(self):
        self.calls = []


class NoopLease:
    """测试替身：总是抢到锁（不走 global_im_bot_config）。"""

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


def _build_engine(db_path, api, rec):
    return ImEngine(
        db_path,
        registry=ChannelRegistry(),
        lease_store=NoopLease(),
        api_factory=lambda: api,
        ctx_factory=_fake_ctx,
        process_chat=lambda ctx, api_, parsed: rec.calls.append(("chat", ctx.bot_type, parsed["text"])) or asyncio.sleep(0),
        process_vod=lambda ctx, api_, parsed: rec.calls.append(("vod", ctx.bot_type, parsed["text"])) or asyncio.sleep(0),
        process_command=_fake_process_command(rec),
        handle_selection=lambda ctx, api_, chat_id, msg_id, num, pending: rec.calls.append(("selection", num)) or asyncio.sleep(0),
        get_pending_selection=lambda chat_id: rec.pending.get(chat_id) if hasattr(rec, "pending") else None,
        channel_builder=FakeChannel,
        scan_interval=3600,
    )


def _fake_process_command(rec):
    async def _cmd(ctx, api_, text, chat_id, sender_id, db, account="",
                   msg_id="", sender_open_id="", card_value=None, action_token=""):
        rec.calls.append(("command", text))
        return "cmd-ok"
    return _cmd


async def _drive(db_path, rec, api=None, msg=None):
    """起引擎 → 取 Supervisor 已建 channel 的 inbound_handler → 处理一条消息。"""
    api = api or FakeApi()
    engine = _build_engine(db_path, api, rec)
    await engine.start()
    await asyncio.sleep(0.1)            # 让 Supervisor 首轮 scan 完成（asyncio 调度）
    try:
        channels = engine.supervisor._channels
        assert channels, "supervisor should build channel for backfilled installation"
        ch = next(iter(channels.values()))[0]
        handler = ch.cfg.inbound_handler
        exc = await handler(CancelContext(), msg or _msg("om_1"))
        assert exc is None
        return engine, api
    except Exception:
        await engine.stop()
        raise


# ---------------------------------------------------------------- 开关

def test_engine_mode_default_on(monkeypatch):
    monkeypatch.delenv("GRAPE_IM_ENGINE", raising=False)
    monkeypatch.setenv("GRAPE_IM_ENGINE_MODE_FILE", "/nonexistent/im_engine_mode")
    assert engine_mode() == "on"


def test_engine_mode_env_off(monkeypatch):
    monkeypatch.setenv("GRAPE_IM_ENGINE", "off")
    monkeypatch.setenv("GRAPE_IM_ENGINE_MODE_FILE", "/nonexistent/im_engine_mode")
    assert engine_mode() == "off"


def test_engine_mode_file_overrides_env(monkeypatch, tmp_path):
    mode_file = tmp_path / "im_engine_mode"
    monkeypatch.setenv("GRAPE_IM_ENGINE", "on")
    monkeypatch.setenv("GRAPE_IM_ENGINE_MODE_FILE", str(mode_file))
    mode_file.write_text("off")
    assert engine_mode() == "off"          # 文件覆盖 env（运行期可改 → 秒级切回）
    mode_file.write_text("on")
    assert engine_mode() == "on"
    mode_file.write_text("garbage")
    assert engine_mode() == "on"           # 非法值回落 env


# ---------------------------------------------------------------- 迁移幂等

def test_startup_migration_idempotent(db_path):
    stats1 = run_startup_migration(db_path)
    # A2 Phase B：启动只建表，回填挪到 002 一次性迁移
    assert stats1["backfill"] == "moved-to-002_drop_legacy_im"
    stats2 = run_startup_migration(db_path)
    assert stats2["backfill"] == "moved-to-002_drop_legacy_im"   # 幂等：可重复执行
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT COUNT(*) FROM grape_t_channel_installation WHERE legacy_app_id='app_chat_1'"
    ).fetchone()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert rows[0] == 1
    for t in ("grape_t_channel_installation", "grape_t_channel_chat_session_binding",
              "grape_t_channel_inbound_dedup", "grape_t_channel_sender_identity",
              "grape_t_channel_inbound_message"):
        assert t in tables


# ---------------------------------------------------------------- 注入点集成

async def test_router_injection_points_bound_chat_media(db_path):
    """绑定路径：身份/绑定落库/持久化+dedup 同事务/agent 双轨/reaction 全链路。"""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO unified_user_mapping (user_id, feishu_open_id) VALUES (101, 'ou_9')")
    conn.commit()
    conn.close()

    rec = Recorder()
    engine, api = await _drive(db_path, rec, msg=_msg("om_1", "hello"))
    try:
        await asyncio.sleep(0.7)           # debounce 0.5s 窗口
        # agent 双轨：chat 轨 → process_chat_async 桥接
        assert any(c[0] == "chat" and c[2] == "hello" for c in rec.calls)
        # OK reaction（agent 执行前受理回执）
        assert api.reactions and api.reactions[0][2] == "OK"
        # 会话绑定落库（grape_t_channel_chat_session_binding）
        store = SqliteBindingStore(db_path)
        inst_row = sqlite3.connect(db_path).execute(
            "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='app_chat_1'"
        ).fetchone()
        binding = store.get(inst_row[0], "oc-1")
        assert binding is not None and binding.agent_type in ("chat", "vod")
        # 消息持久化 + dedup marked 同事务
        c2 = sqlite3.connect(db_path)
        msg_row = c2.execute(
            "SELECT text FROM grape_t_channel_inbound_message WHERE message_id='om_1'").fetchone()
        dedup_row = c2.execute(
            "SELECT state FROM grape_t_channel_inbound_dedup WHERE message_id='om_1'").fetchone()
        c2.close()
        assert msg_row and msg_row[0] == "hello"
        assert dedup_row and dedup_row[0] == "marked"
    finally:
        await engine.stop()


async def test_router_unbound_sender_gets_binding_card(db_path):
    """未绑定发送者 → 绑定引导卡，不触发 agent。"""
    rec = Recorder()
    engine, api = await _drive(db_path, rec, msg=_msg("om_2", "hi", sender="ou_unknown"))
    try:
        await asyncio.sleep(0.7)
        assert not any(c[0] in ("chat", "vod") for c in rec.calls)
        assert api.cards and "绑定" in api.cards[0][1]
    finally:
        await engine.stop()


async def test_router_command_and_dedup(db_path):
    """命令走 process_command；重复 message_id 被去重丢弃。"""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO unified_user_mapping (user_id, feishu_open_id) VALUES (101, 'ou_9')")
    conn.commit()
    conn.close()

    rec = Recorder()
    api = FakeApi()
    engine = _build_engine(db_path, api, rec)
    await engine.start()
    await asyncio.sleep(0.1)            # 让 Supervisor 首轮 scan 完成（asyncio 调度）
    try:
        handler = next(iter(engine.supervisor._channels.values()))[0].cfg.inbound_handler
        assert await handler(CancelContext(), _msg("om_3", "/repo")) is None
        assert any(c[0] == "command" and c[1] == "/repo" for c in rec.calls)
        assert api.replies and api.replies[0][1] == "cmd-ok"
        # 重投同一条消息：dedup DROP，不再执行命令
        rec.calls.clear()
        assert await handler(CancelContext(), _msg("om_3", "/repo")) is None
        assert not rec.calls
    finally:
        await engine.stop()


async def test_router_pending_selection(db_path):
    """纯数字 + pending selection → _handle_selection。"""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO unified_user_mapping (user_id, feishu_open_id) VALUES (101, 'ou_9')")
    conn.commit()
    conn.close()

    rec = Recorder()
    rec.pending = {"oc-1": {"action": "repo", "items": []}}
    engine, api = await _drive(db_path, rec, msg=_msg("om_4", "2"))
    try:
        await asyncio.sleep(0.7)
        assert any(c[0] == "selection" and c[1] == "2" for c in rec.calls)
    finally:
        await engine.stop()


async def test_router_agent_vod_track(db_path):
    """repo 轨安装 → ctx.bot_type=vod_bot → process_vod_async 桥接。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret) "
        "VALUES ('gitcode:acme/web', 'vod', 'app_vod_1', 'secv')")
    _backfill(conn)                      # A2 Phase B：repo 轨安装行由一次性回填生成
    conn.commit()
    conn.close()

    rec = Recorder()
    api = FakeApi()
    engine = ImEngine(
        db_path, registry=ChannelRegistry(), lease_store=NoopLease(),
        api_factory=lambda: api, ctx_factory=_fake_ctx,
        process_chat=lambda ctx, api_, parsed: rec.calls.append(("chat", ctx.bot_type)) or asyncio.sleep(0),
        process_vod=lambda ctx, api_, parsed: rec.calls.append(("vod", ctx.bot_type)) or asyncio.sleep(0),
        channel_builder=FakeChannel, scan_interval=3600,
    )
    await engine.start()
    await asyncio.sleep(0.1)            # 让 Supervisor 首轮 scan 完成（asyncio 调度）
    try:
        # 额外插入 vod 轨身份绑定（grape_t_channel_sender_identity）
        assert engine.supervisor._channels
        ch = next(c for c, _ in engine.supervisor._channels.values()
                  if c.cfg.raw.get("app_id") == "app_vod_1")
        handler = ch.cfg.inbound_handler
        # 绑定发送者：写 grape_t_channel_sender_identity（installations 闭包的 installation_id）
        c = sqlite3.connect(db_path)
        app_cfg = c.execute(
            "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='app_vod_1'").fetchone()
        c.execute("INSERT INTO grape_t_channel_sender_identity (installation_id, platform_user_id, user_id) "
                  "VALUES (?, 'ou_9', '7')", (app_cfg[0],))
        c.commit()
        c.close()
        assert await handler(CancelContext(), _msg("om_5", "check ci")) is None
        await asyncio.sleep(0.7)
        assert any(c[0] == "vod" and c[1] == "vod_bot" for c in rec.calls)
    finally:
        await engine.stop()


# ---------------------------------------------------------------- 回滚路径

async def test_engine_stop_health_and_rollback(db_path):
    """开关切回：stop 后 health 收口、Supervisor 连接释放（legacy 可接管）。"""
    rec = Recorder()
    engine = _build_engine(db_path, FakeApi(), rec)
    await engine.start()
    await asyncio.sleep(0.1)
    assert engine.health()["engine"] == "on" and engine.health()["running"] is True
    ch = next(iter(engine.supervisor._channels.values()))[0]
    await engine.stop()
    assert engine.health()["running"] is False
    assert ch.disconnected is True
    # 幂等 stop
    await engine.stop()


# ---------------------------------------------------------------- 仓储细节

def test_dedup_store_two_phase(db_path):
    run_startup_migration(db_path)
    store = SqliteDedupStore(db_path)
    assert store.try_claim("inst-1", "m-1", "tok") is True
    assert store.seen("inst-1", "m-1") is True
    assert store.try_claim("inst-1", "m-1", "tok2") is False
    assert store.mark("inst-1", "m-1") is True
    store.release("inst-1", "m-1")
    assert store.seen("inst-1", "m-1") is False


def test_binding_store_upsert_roundtrip(db_path):
    from src.im.engine.session import SessionBinding
    run_startup_migration(db_path)
    store = SqliteBindingStore(db_path)
    assert store.get("inst-1", "oc-1") is None
    b = SessionBinding(installation_id="inst-1", binding_key="oc-1",
                       chat_type="group", scope=Scope.REPO,
                       repo_key="gitcode:acme/web", agent_type="vod")
    store.upsert(b)
    got = store.get("inst-1", "oc-1")
    assert got is not None
    assert got.repo_key == "gitcode:acme/web" and got.agent_type == "vod"
    b2 = SessionBinding(installation_id="inst-1", binding_key="oc-1",
                        chat_type="group", scope=Scope.REPO,
                        repo_key="gitcode:acme/api", agent_type="vod")
    store.upsert(b2)
    assert store.get("inst-1", "oc-1").repo_key == "gitcode:acme/api"


def test_start_im_engine_supports_mysql_backend(monkeypatch, db_path):
    """MySQL 后端同样启动新引擎（2026-11 适配：仓储走 session_manager 池化连接，
    方言由 src.mysql_compat 翻译）——不再像 P0/P1 那样直接跳过。"""
    class _FakeEngine:
        async def start(self):
            self.started = True

    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    engine_wiring._reset_backend_cache()
    monkeypatch.setattr(engine_wiring, "ImEngine", lambda path: _FakeEngine())
    try:
        engine = asyncio.run(engine_wiring.start_im_engine(db_path))
        assert engine is not None
        assert engine.started is True
        assert engine_wiring._is_mysql_backend() is True
    finally:
        engine_wiring._reset_backend_cache()


def test_inbound_to_parsed_contract():
    from src.im.engine_wiring import _inbound_to_parsed
    msg = InboundMessage(
        event_id="e1", message_id="om_9",
        source=Source(channel_type=Type.FEISHU, chat_id="oc-2",
                      chat_type=ChatType.GROUP, sender_id="ou_1"),
        type=MsgType.TEXT, text="/repo", command_text="/repo",
        media_refs=[],
    )
    parsed = _inbound_to_parsed(msg)
    assert parsed["chat_id"] == "oc-2" and parsed["chat_type"] == "group"
    assert parsed["sender_open_id"] == "ou_1" and parsed["message_id"] == "om_9"
    assert parsed["type"] == "message" and parsed["text"] == "/repo"


def test_app_dedup_releases_when_channel_is_gone(db_path):
    """回归：同一 app 只建一条 WS 连接，但去重必须跟着"活渠道"走。

    旧实现建渠道即把 app_id 记账、永不移除；一旦工厂建完渠道后抢租约失败
    （如 global_im_bot_config 还没这行），该 app_id 就被永久拉黑，之后每轮扫描
    registry 静默拒绝 —— 表现为安装删了再加回来不重连，必须重启进程。现以
    Supervisor._channels 为准，失败/让渡后自动释放。
    """
    class _Sup:
        def __init__(self):
            self._channels = {}

    engine = ImEngine(db_path, registry=ChannelRegistry(), lease_store=NoopLease(),
                      scan_interval=3600)
    engine.supervisor = _Sup()
    cfg = ChannelConfig(type=Type.FEISHU, raw={"app_id": "cli_dedup", "app_secret": "s"},
                        installation_id="inst-a", scope=Scope.USER)

    assert engine._feishu_factory(cfg) is not None
    # 渠道还没进 Supervisor（模拟抢租约失败 / 已让渡）→ 不能记成已建
    assert engine._feishu_factory(cfg) is not None

    live = engine._feishu_factory(cfg)
    engine.supervisor._channels["inst-a"] = (live, CancelContext())
    assert engine._feishu_factory(cfg) is None      # 活渠道在 → 去重生效

    engine.supervisor._channels.pop("inst-a")       # 渠道让渡 / 断开
    assert engine._feishu_factory(cfg) is not None   # 同 app 可重新建连


def test_ui_write_through_sync_and_revoke(db_path, monkeypatch):
    """写穿桥：UI 走 legacy 表，新引擎只认 grape_t_channel_installation —— 保存/解绑必须同步。"""
    # 写穿桥走 default_db_path()（= DEVOPS_DB_PATH），测试里把它指到本用例的库
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    run_startup_migration(db_path)
    assert engine_wiring.sync_installation(
        "repo", repo_key="gitcode:acme/api", app_id="cli_ui_1", app_secret="sec1") is True

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT status, config FROM grape_t_channel_installation "
                        "WHERE legacy_app_id='cli_ui_1' AND scope='repo'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "active"
    assert json.loads(rows[0][1])["app_secret"] == "sec1"

    # 幂等 + 前端回填 '********'（不带 secret）时沿用库里旧 secret
    assert engine_wiring.sync_installation(
        "repo", repo_key="gitcode:acme/api", app_id="cli_ui_1") is True
    rows = conn.execute("SELECT status, config FROM grape_t_channel_installation "
                        "WHERE legacy_app_id='cli_ui_1' AND scope='repo'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][1])["app_secret"] == "sec1"

    # 解绑 → revoked（不硬删，留审计）；引擎只枚举 active → 下一轮扫描自动让渡
    assert engine_wiring.revoke_installation(
        "repo", repo_key="gitcode:acme/api", app_id="cli_ui_1") is True
    assert conn.execute("SELECT status FROM grape_t_channel_installation WHERE legacy_app_id='cli_ui_1'"
                        ).fetchone()[0] == "revoked"

    # 重新绑定 → 回到 active
    assert engine_wiring.sync_installation(
        "repo", repo_key="gitcode:acme/api", app_id="cli_ui_1", app_secret="sec1") is True
    assert conn.execute("SELECT status FROM grape_t_channel_installation WHERE legacy_app_id='cli_ui_1'"
                        ).fetchone()[0] == "active"
    conn.close()

def test_card_action_translation_keeps_open_message_id_and_context():
    """回归：卡片按钮回调必须带上真实 open_message_id（回复目标）、action_value、token。

    旧实现把 event_id 当 message_id 用 → 回复报 99992354（invalid open_message_id），
    用户观感就是"点按钮没反应"。
    """
    from src.im.adapters.feishu.inbound import parse_card_action, parsed_to_inbound

    raw = {
        "header": {"event_id": "evt_hash_32"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "context": {"open_chat_id": "oc_1", "open_message_id": "om_card_1"},
            "action": {"value": {"cmd": "/bind 1", "scene": "s1"}},
            "token": "tok_abc",
        },
    }
    parsed = parse_card_action(raw)
    assert parsed["_reply_id"] == "om_card_1" and parsed["message_id"] == "evt_hash_32"

    msg = parsed_to_inbound(parsed, raw)
    assert msg.reply_to is not None and msg.reply_to.message_id == "om_card_1"

    back = engine_wiring._inbound_to_parsed(msg)
    assert back["_reply_id"] == "om_card_1"                    # 回复目标
    assert back["message_id"] == "evt_hash_32"                 # 去重键保持 event_id
    assert back["_card_value"] == {"cmd": "/bind 1", "scene": "s1"}
    assert back["_action_token"] == "tok_abc"
    assert engine_wiring._reply_msg_id(msg) == "om_card_1"


def test_plain_message_reply_id_is_message_id():
    from src.im.adapters.feishu.inbound import parsed_to_inbound
    parsed = {"type": "message", "text": "hi", "chat_id": "oc_1", "chat_type": "p2p",
              "sender_open_id": "ou_1", "message_id": "om_normal_1", "msg_type": "text"}
    msg = parsed_to_inbound(parsed, {})
    assert engine_wiring._reply_msg_id(msg) == "om_normal_1"
    assert engine_wiring._inbound_to_parsed(msg).get("_reply_id") in (None, "om_normal_1")


# ---------------------------------------------------------------- 身份绑定（钉钉急救）

def test_lookup_user_sync_dingtalk_column(tmp_path):
    """统一映射按渠道取列：钉钉读 dingtalk_user_id，默认仍读 feishu_open_id。"""
    path = str(tmp_path / "identity.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unified_user_mapping "
                 "(user_id INTEGER, feishu_open_id TEXT, dingtalk_user_id TEXT)")
    conn.execute("INSERT INTO unified_user_mapping "
                 "(user_id, feishu_open_id, dingtalk_user_id) VALUES (101, 'ou_x', 'staff-1')")
    conn.commit()
    conn.close()

    assert engine_wiring._lookup_user_sync(path, "inst-1", "staff-1", "dingtalk") == "101"
    assert engine_wiring._lookup_user_sync(path, "inst-1", "ou_x", "dingtalk") == ""
    assert engine_wiring._lookup_user_sync(path, "inst-1", "ou_x") == "101"


async def test_router_unbound_bind_command_writes_identity(db_path):
    """未绑定发送者发 /绑定 → 落 grape_t_channel_sender_identity，下一条正常进 agent。"""
    rec = Recorder()
    api = FakeApi()
    engine, api = await _drive(db_path, rec, api,
                              msg=_msg("om_b1", "/绑定", sender="staff-1"))
    try:
        await asyncio.sleep(0.3)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT user_id, platform_stable_user_id FROM grape_t_channel_sender_identity "
            "WHERE platform_user_id='staff-1'").fetchone()
        conn.close()
        assert row and str(row[0]) == "101"
        assert api.cards and "已绑定" in api.cards[-1][1]

        handler = next(iter(engine.supervisor._channels.values()))[0].cfg.inbound_handler
        await handler(CancelContext(), _msg("om_b2", "你好", sender="staff-1"))
        await asyncio.sleep(0.8)
        assert any(c[0] == "chat" for c in rec.calls)
    finally:
        await engine.stop()


def test_bind_identity_rejected_for_repo_scope(db_path):
    """repo 轨群装不做个人身份绑定：不落行。"""
    run_startup_migration(db_path)
    assert engine_wiring._bind_identity_sync(db_path, "inst-repo", "repo", "101",
                                             "staff-9") == ""
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM grape_t_channel_sender_identity").fetchone()[0]
    conn.close()
    assert n == 0
