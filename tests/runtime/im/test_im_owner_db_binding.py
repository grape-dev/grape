"""IM 模块跨域连接回归（weekly-11 三库拆分）。

事故现场（2026-11-21 生产实测）：
飞书 `/repo` 选仓库回数字 → 卡片回
`❌ 选择处理失败: (1146, "Table 'grape_mgmt.chat_conversations' doesn't exist")`。

成因：IM 引擎由 **mgmt 进程**启动并执行（`grape_server.py` → `engine_wiring.start_im_engine`），
而 `src/domains/runtime/im/*` 属于 runtime 域；`chat_conversations` / `issue` /
`chat_agent_session` / `repo_im_group_binding` / `global_im_bot_config` /
`bot_seen_messages` / `grape_im_instance` 都是 runtime 基表（`configs/table_ownership.json`），
用进程本域连接必然 1146（且多处被 except 吞成静默失败）。

契约：这些模块的 `db` / `db_lock` 必须是 `owner_db.db_pair("runtime")` 的产物
——分库拓扑打 runtime 自有连接，单库/无 DSN 拓扑自动回落 `session_manager.db`（零回归）。
"""
import asyncio
import os
import pathlib
import re
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.owner_db import _OwnerDB, owner_lock

IM_DIR = pathlib.Path(ROOT) / "src" / "domains" / "runtime" / "im"

# 渠道无关：这 6 个模块承担所有 IM 渠道（飞书/钉钉/企微/微信/Slack/Telegram）的
# 入站命令、会话绑定、通知、去重与租约；渠道 adapter 只管传输与渲染。
_IM_DB_MODULES = ("handler_async", "vod_handler_async", "notify",
                  "feishu_async", "seen_store", "bot_lease")


def _im_modules():
    import importlib
    return [importlib.import_module(f"src.domains.runtime.im.{n}") for n in _IM_DB_MODULES]


@pytest.mark.parametrize("mod", _im_modules(), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_im_module_db_is_runtime_owner(mod):
    assert isinstance(mod.db, _OwnerDB), f"{mod.__name__}.db 必须是域代理，不能是裸连接"
    assert mod.db.domain == "runtime"


@pytest.mark.parametrize("mod", _im_modules(), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_im_module_lock_is_runtime_owner_lock(mod):
    assert mod.db_lock is owner_lock("runtime")


def test_every_im_module_with_module_level_db_is_runtime_bound():
    """未来新增/改动 IM 模块时兜底：模块级 db 一旦出现就必须是 runtime 域代理。

    只扫 IM 主包（channels 适配器单独由下一个用例约束），避免 import 各渠道 SDK。
    """
    import importlib

    offenders = []
    for path in sorted(IM_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        src = path.read_text(encoding="utf-8")
        if not re.search(r"^db\s*[,=]", src, re.M):
            continue
        mod = importlib.import_module(f"src.domains.runtime.im.{path.stem}")
        db = getattr(mod, "db", None)
        if not (isinstance(db, _OwnerDB) and db.domain == "runtime"):
            offenders.append(path.name)
    assert not offenders, f"这些 IM 模块的 db 未绑 runtime 域：{offenders}"


def test_channel_adapters_stay_transport_only():
    """6 个渠道 adapter 不得直连运行域表——命令/会话一律走共享 handler。

    本用例锁的是「新渠道自己写 SQL」这条回归路径：adapter 只做入站解析与渲染，
    数字选择/卡片回调必须解成 `/bind N` / `/join N` 交给 `_handle_selection`。
    """
    forbidden = re.compile(
        r"\b(?:FROM|INTO|UPDATE)\s+`?(chat_conversations|chat_messages|issue|"
        r"repo_im_group_binding|agent_runtimes|agent_tasks|chat_agent_session)\b",
        re.I)
    adapters = sorted(p for p in (IM_DIR / "adapters").iterdir() if p.is_dir())
    assert [p.name for p in adapters] == ["dingtalk", "feishu", "slack",
                                          "telegram", "wechat", "wecom"]
    offenders = []
    for ch in adapters:
        for py in ch.rglob("*.py"):
            if forbidden.search(py.read_text(encoding="utf-8")):
                offenders.append(str(py.relative_to(ROOT)))
    assert not offenders, f"渠道 adapter 出现运行域表 SQL（应走共享 handler）：{offenders}"


def test_inbound_command_entry_points_are_single_shared_impl():
    """全仓只能有一份 _handle_selection / process_chat_async / process_vod_async。

    多一份 = 某个渠道走了自己的分支实现（本次 1146 的根因正是「共享 handler 用错库」，
    若渠道再分叉，修一处漏一处）。
    """
    pats = {
        "def _handle_selection": 1,
        "def process_chat_async": 1,
        "def process_vod_async": 1,
    }
    found = {k: [] for k in pats}
    for py in (pathlib.Path(ROOT) / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for k in pats:
            if k in text:
                found[k].append(str(py.relative_to(ROOT)))
    for k, n in pats.items():
        assert len(found[k]) == n, f"{k} 出现 {len(found[k])} 次：{found[k]}"


class _Ctx:
    app_id = "app_feishu_test"
    name = "feishu-test"


class _Egress:
    def __init__(self):
        self.cards = []

    async def send_raw_card(self, ctx, msg_id, card, chat_id=""):
        self.cards.append(card)

    async def reply_text(self, ctx, msg_id, text):
        self.cards.append(text)


class _Cur:
    """游标替身：记录 SQL；mgmt 库上对 chat_conversations 的语句按真实报错抛 1146。"""

    rowcount = 1

    def __init__(self, owner: str, recorded: list):
        self.owner = owner
        self.recorded = recorded

    def execute(self, sql, params=None):
        if self.owner == "mgmt" and "chat_conversations" in sql:
            raise RuntimeError(
                "1146 Table 'grape_mgmt.chat_conversations' doesn't exist")
        self.recorded.append((self.owner, sql, params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        return None

    def rollback(self):
        return None


def test_repo_binding_pointer_reset_lands_on_runtime_connection(monkeypatch):
    """复现原始事故：进程是 mgmt 域时，绑定仓库后的会话指针 UPDATE 必须打 runtime 连接。

    改动前该语句走 `session_manager.db`（mgmt 库）→ 1146 冒泡成「选择处理失败」卡片。
    """
    from src.domains.runtime.im import engine_wiring  # noqa: F401  确保装配可用
    from src.domains.runtime.im import handler_async as ha
    from src.kernel import db_registry
    import src.kernel.session_manager as sm

    recorded = []
    rt_cur, mgmt_cur = _Cur("runtime", recorded), _Cur("mgmt", recorded)
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: _Conn(rt_cur if d == "runtime" else mgmt_cur))
    monkeypatch.setattr(db_registry, "current_domain", lambda: "mgmt")
    monkeypatch.setattr(ha, "_current_bound_repo", lambda chat_id, account, db: "")
    monkeypatch.setattr(ha, "get_user_id_by_app_id", lambda app_id, sender_id="": 1)
    monkeypatch.setattr(sm, "upsert_repo_im_binding", lambda *a, **kw: 0)
    ha._pending_selections.clear()

    pending = {"action": "repo", "repo_key": "", "page": 1, "page_size": 20,
               "items": [{"repo_key": "gitcode:example-org/demo"}],
               "expires": time.time() + 120}
    ha._pending_selections["chatTEST"] = pending
    eg = _Egress()
    try:
        asyncio.run(ha._handle_selection(_Ctx(), eg, "chatTEST", "msg1", "1", pending))
    finally:
        ha._pending_selections.clear()

    updates = [(o, s) for o, s, _ in recorded if "chat_conversations" in s]
    assert updates, "绑定仓库必须重置会话指针（清 im_channel_id）"
    assert all(o == "runtime" for o, _ in updates), f"指针写落在了非 runtime 连接: {updates}"
    # 没有把 1146 冒成失败卡片
    assert not any(isinstance(c, str) and "选择处理失败" in c for c in eg.cards)