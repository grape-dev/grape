"""仓库凭据写入语义：空值 = 删除行，不写空占位行。

背景（2026-11-21 实测）：托管写路径 `_upsert_repo_credential` 无条件建行，
`bot_token` 为空时写进一条「值为空」的行；git-credential 的显式 bot 闸把这种行
判成 `BOT_CREDENTIAL_INVALID stage=empty` 并停止往下一档下降，6 个 gitee 仓库
因此全部 424。语义改成「空 → 删行」后，解析会继续落到平台 bot / 归属者。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.user import db as user_db


@pytest.fixture()
def recorder(monkeypatch):
    calls: list = []

    def _delete(repo_key, cred_type):
        calls.append(("delete", repo_key, cred_type))
        return 1

    def _upsert(repo_key, cred_type, value, updated_at):
        calls.append(("upsert", repo_key, cred_type, value))
        return {"action": "updated", "id": 0}

    monkeypatch.setattr(user_db._insight_public, "delete_repo_credential_value", _delete)
    monkeypatch.setattr(user_db._insight_public, "upsert_repo_credential_value", _upsert)
    monkeypatch.setattr(user_db, "_encrypt_repo_secret", lambda v: f"enc({v})")
    return calls


def test_empty_value_deletes_row(recorder):
    user_db._upsert_repo_credential(None, "gitee:yang-aiwu-new/demo", "bot", "")
    assert recorder == [("delete", "gitee:yang-aiwu-new/demo", "bot")]


def test_whitespace_value_deletes_row(recorder):
    user_db._upsert_repo_credential(None, "gitee:o/r", "bot", "   ")
    assert recorder == [("delete", "gitee:o/r", "bot")]


def test_none_value_deletes_row(recorder):
    user_db._upsert_repo_credential(None, "gitee:o/r", "webhook", None)
    assert recorder == [("delete", "gitee:o/r", "webhook")]


def test_non_empty_value_upserts_encrypted(recorder):
    user_db._upsert_repo_credential(None, "gitee:o/r", "bot", "pat-token")
    assert recorder and recorder[0][0] == "upsert"
    assert recorder[0][3] == "enc(pat-token)", recorder