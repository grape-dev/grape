"""S125 场景用例（D90 消费端批步 3）：PAT 优先解析序（D10 拍板，单序无 flag）。

依据（2026-11-07 用户修正）：
- D10 原文（design.md:190）："PAT 优先（显式配置=明确意图）；oauth_fill 在
  PAT 缺席或失效时兜底"——只有一个正确序，无灰度 flag
- 评审稿 P-3 方向 A 原文："单点调序，UC23④/UC24 守卫保证双链一次生效"
- 此前实现的 feature flag 是过度设计（双序分支=维护者需理解两个序），已删除；
  P-3A 修正 = 单序直接生效

UC↔用例映射（requirements UC29 修正版）：
- UC29① PAT 存在 → PAT 胜出（显式意图优先）
- UC29② PAT 缺失 → OAuth 回落（不空手）
- UC29③ 两源皆空 → 空串（UC23③ 同口径）
- UC29④ 分轨监控：命中 level（pat/oauth）落 log（P-8 归因信号）
"""
import os
import uuid

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as udb

from src.kernel.token_resolver import GenericVcsProvider
from src.domains.runtime.vcs.platform_card import get_platform_card


def _mk_user_with_pat():
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    uname = "s125_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
    uid = u["id"]
    udb.set_credential(uid, "gitee_pat", {"token": "pat_tok_s125"})
    return uid, uname


def _cleanup(uid, uname):
    with db_lock:
        db.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


def _provider():
    card = get_platform_card("gitee")
    assert card is not None, "gitee 内置卡缺失（测试前置）"
    return GenericVcsProvider(card)


def _patch_oauth(monkeypatch, token: str):
    monkeypatch.setattr(
        "src.api.deps.get_user_token_for",
        lambda username, user_id=0, provider="gitee": token)


class TestPatFirstOrder:
    def test_uc29_1_pat_wins_when_present(self, monkeypatch):
        """PAT 存在 → PAT 胜出（D10 显式意图优先）。"""
        uid, uname = _mk_user_with_pat()
        try:
            _patch_oauth(monkeypatch, "oauth_tok")
            tok = _provider().fetch_user_token("u", uid)
            assert tok == "pat_tok_s125"
        finally:
            _cleanup(uid, uname)

    def test_uc29_2_pat_miss_falls_back_oauth(self, monkeypatch):
        """PAT 缺失 → OAuth 兜底（不空手）。"""
        uid, uname = _mk_user_with_pat()
        try:
            with db_lock:
                db.execute("DELETE FROM user_credentials WHERE user_id=? AND provider='gitee_pat'", (uid,))
                db.commit()
            _patch_oauth(monkeypatch, "oauth_tok")
            tok = _provider().fetch_user_token("u", uid)
            assert tok == "oauth_tok"
        finally:
            _cleanup(uid, uname)

    def test_uc29_3_both_empty_returns_empty(self, monkeypatch):
        """两源皆空 → 空串（UC23③ 同口径）。"""
        uid, uname = _mk_user_with_pat()
        try:
            with db_lock:
                db.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
                db.commit()
            _patch_oauth(monkeypatch, "")
            tok = _provider().fetch_user_token("u", uid)
            assert tok == ""
        finally:
            _cleanup(uid, uname)

    def test_uc29_4_hit_level_logged(self, monkeypatch, caplog):
        """分轨监控：命中 level 落 log（P-8 归因信号）。"""
        import logging
        uid, uname = _mk_user_with_pat()
        try:
            _patch_oauth(monkeypatch, "oauth_tok")
            with caplog.at_level(logging.INFO):
                _provider().fetch_user_token("u", uid)
            assert any("level=pat" in m for m in caplog.messages)
        finally:
            _cleanup(uid, uname)
