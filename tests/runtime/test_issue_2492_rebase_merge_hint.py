"""Issue #2492：rebase 合入失败 → 中文结构化提示（上游原文保留在 detail/日志）。

三分支：
- rebase 冲突命中：中文结构化提示（原因=变基冲突 / 冲突提交 sha / 处理建议），
  原文进 ``detail.upstream_message`` 与日志，状态码保持上游语义（AC1/AC2）；
- rebase 非冲突失败：兜底中文提示，原文同样保留（AC3）；
- merge/squash 回归：上游原文照旧透传，不出现中文归类文案（AC4）。
"""
import logging
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import src.api.merge_gate as mg
from src.api import git_repo_api as gra

OWNER = "acme"
REPO = "widgets"
NUMBER = 7

SHA = "abc1234def5678901234567890123456789abcd"

CONFLICT_MSG = (
    "Something went wrong during merge: rebase failed: conflict cannot be "
    f'resolved automatically, commit "{SHA}" failed to apply'
)
NON_CONFLICT_MSG = (
    "Something went wrong during merge: rebase failed: branch protection "
    "hook declined the update"
)
PLAIN_MSG = "Something went wrong during merge: not mergeable"


def _merge(merge_method, upstream_status=400, upstream_message=PLAIN_MSG):
    """以 mock 上游失败跑一次 merge_pull，返回抛出的 HTTPException。"""

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "PUT" and str(url).endswith("/merge"):
            raise HTTPException(status_code=upstream_status,
                                detail={"code": upstream_status,
                                        "message": upstream_message})
        return {}

    body = gra.PRMergeBody(merge_method=merge_method) if merge_method else None
    with patch.object(gra, "_vcs_req", side_effect=_fake_vcs_req), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"), \
         patch.object(gra, "_pre_merge_qa_gate", return_value=None), \
         patch.object(mg, "run_merge_gate", return_value={"decision": "passed"}), \
         patch.object(mg, "run_post_merge_gate",
                      return_value={"decision": "passed"}), \
         patch.object(mg, "record_gate_log", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            gra.merge_pull(OWNER, REPO, NUMBER, body,
                           user={"username": "u"}, p="gitcode")
    return exc_info.value


def test_rebase_conflict_returns_chinese_structured_hint(caplog):
    """AC1/AC2：rebase 冲突命中 → 中文结构化提示 + 原文保留 + 状态码透传。"""
    with caplog.at_level(logging.WARNING):
        exc = _merge("rebase", upstream_status=400, upstream_message=CONFLICT_MSG)

    assert exc.status_code == 400
    detail = exc.detail
    # 原因归类 + 处理建议 + 冲突提交 sha 都在用户可见 message 里
    assert "变基冲突" in detail["message"]
    assert SHA in detail["message"]
    assert "建议" in detail["message"] and "merge_method=merge" in detail["message"]
    # 不再裸透传上游英文串
    assert "Something went wrong during merge" not in detail["message"]
    # 结构化子字段
    assert detail["reason"] == "rebase_conflict"
    assert detail["conflict_commits"] == [SHA]
    assert "merge_method=merge" in detail["suggestion"]
    # 原文保留在 detail 子字段
    assert detail["upstream_message"] == CONFLICT_MSG
    # 原文保留在日志
    assert CONFLICT_MSG in caplog.text


def test_rebase_non_conflict_falls_back_with_original_message(caplog):
    """AC3：非冲突类 rebase 失败 → 兜底中文提示，原文仍可追溯。"""
    with caplog.at_level(logging.WARNING):
        exc = _merge("rebase", upstream_status=422,
                     upstream_message=NON_CONFLICT_MSG)

    assert exc.status_code == 422
    detail = exc.detail
    assert "变基失败" in detail["message"]
    assert "变基冲突" not in detail["message"]
    assert "Something went wrong during merge" not in detail["message"]
    assert detail["reason"] == "rebase_failed"
    assert detail["conflict_commits"] == []
    assert detail["upstream_message"] == NON_CONFLICT_MSG
    assert NON_CONFLICT_MSG in caplog.text


def test_merge_method_squash_keeps_upstream_message_untouched():
    """AC4：squash（与 merge 同路径）失败 → 原文原样透传，不出现中文归类文案。"""
    body = gra.PRMergeBody(merge_method="squash")
    exc = _merge("squash", upstream_status=405, upstream_message=PLAIN_MSG)

    assert exc.status_code == 405
    assert exc.detail["message"] == PLAIN_MSG
    assert "变基" not in exc.detail["message"]
    assert "reason" not in exc.detail
    assert body.merge_method == "squash"


def test_default_merge_method_keeps_upstream_message_untouched():
    """AC4 回归：缺省 merge_method（body 省略）失败 → 原文原样透传。"""
    exc = _merge(None, upstream_status=405, upstream_message=PLAIN_MSG)

    assert exc.status_code == 405
    assert exc.detail == {"code": 405, "message": PLAIN_MSG}