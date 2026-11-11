"""编号选择副作用回归测试（2026-11-05 钉钉 /repo 后回 1 "没生效" 事故）。

事故结论：链路正常，选中的 `1` 恰好是当前已绑定仓库，upsert 前后值相同；
旧实现照样 upsert + 清 im_channel_id + 发"✅已绑定"卡，看起来像没生效却丢会话位置。

契约：
  1. 选中项 == 当前绑定仓库 → 只发"已是当前仓库"提示卡，**不写库、不重置会话指针**；
  2. 选中项 != 当前绑定仓库 → 正常 upsert + 成功卡；
  3. 两种情况都清 pending（避免 120s 内重复触发）。
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im import handler_async as ha


class _Ctx:
    app_id = "dinglrjpdjel7oxksyuu"
    name = "dingtalk-test"


class _Egress:
    """接住 send_raw_card（egress 的 replier 路径），记录卡片 IR。"""

    def __init__(self):
        self.cards = []

    async def send_raw_card(self, ctx, msg_id, card, chat_id=""):
        self.cards.append(card)
        return "sent"


@pytest.fixture(autouse=True)
def _clean_pending():
    ha._pending_selections.clear()
    yield
    ha._pending_selections.clear()


def _pending(repo_keys, current=""):
    return {
        "action": "repo",
        "repo_key": current,
        "items": [{"repo_key": k} for k in repo_keys],
        "page": 1,
        "page_size": 20,
        "expires": __import__("time").time() + 120,
    }


def _run(num, pending, monkeypatch, bound, upserts):
    monkeypatch.setattr(ha, "_current_bound_repo", lambda chat_id, account, db: bound)
    monkeypatch.setattr(ha, "get_user_id_by_app_id", lambda app_id, sender_id="": 1)

    def _fake_upsert(chat_id, repo_key, source, bind_by, app_id):
        upserts.append(repo_key)

    class _Cursor:
        def execute(self, *a, **kw):
            return self

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import src.session_manager as sm
    monkeypatch.setattr(sm, "upsert_repo_im_binding", _fake_upsert)
    monkeypatch.setattr(sm, "db_lock", _Lock())
    monkeypatch.setattr(ha, "db", type("DB", (), {"cursor": staticmethod(lambda: _Cursor()), "commit": staticmethod(lambda: None)})(), raising=False)

    ha._pending_selections["cidTEST"] = pending
    eg = _Egress()
    asyncio.run(ha._handle_selection(_Ctx(), eg, "cidTEST", "msg1", num, pending))
    return eg


def test_same_repo_is_noop_with_explicit_hint(monkeypatch):
    """选中当前已绑定仓库：不写库、不清会话指针、给出"已是当前仓库"提示。"""
    upserts = []
    eg = _run("1", _pending(["github:AKAKAKAKAtes/111", "gitcode:XOcto/deepseek-harness"],
                            current="github:AKAKAKAKAtes/111"),
              monkeypatch, bound="github:AKAKAKAKAtes/111", upserts=upserts)
    assert upserts == [], "同仓库不应重复 upsert（会顺带清 im_channel_id）"
    assert len(eg.cards) == 1
    card = eg.cards[0]
    assert "已是当前" in card.title
    assert not card.actions, "提示卡不应带按钮（钉钉会降级）"
    assert "cidTEST" not in ha._pending_selections, "pending 必须清掉"


def test_different_repo_binds_and_reports(monkeypatch):
    """选中另一仓库：upsert 真实落值 + 成功卡。"""
    upserts = []
    eg = _run("2", _pending(["github:AKAKAKAKAtes/111", "gitcode:XOcto/deepseek-harness"],
                            current="github:AKAKAKAKAtes/111"),
              monkeypatch, bound="github:AKAKAKAKAtes/111", upserts=upserts)
    assert upserts == ["gitcode:XOcto/deepseek-harness"]
    assert len(eg.cards) == 1
    assert "绑定成功" in eg.cards[0].title
    assert "cidTEST" not in ha._pending_selections