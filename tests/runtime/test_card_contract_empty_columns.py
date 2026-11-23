"""契约通道取卡时空字符串列必须按"未配置"处理。

回归背景：表路径（card_projection）会省略空值列，契约路径原样透传 →
webhook_verify="" / branch_create_form="" 被 Builder 判成非法枚举值，
整张卡被拒（自建实例只写 dialect+host 时 worker 整卡丢失）。
"""
from __future__ import annotations

import json

from src.domains.runtime.vcs import card_contract, platform_card


class _FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(cards: dict):
    payload = json.dumps({"cards": cards})
    return card_contract.PlatformCardContractClient(
        base_url="http://127.0.0.1:8080", token="rta_fake_test_token", timeout=1.0,
        opener=lambda req, timeout=None: _FakeResp(payload))


def test_empty_string_columns_are_dropped():
    raw = {
        "dialect": "gitlab",
        "host": "gitlab.example.com",
        "webhook_verify": "",
        "branch_create_form": "",
        "issue_write_form": "",
        "webhook_path": "",
    }
    got = _client({"gitlab-corp": dict(raw)}).fetch_cards()
    assert got == {"gitlab-corp": {"dialect": "gitlab", "host": "gitlab.example.com"}}


def test_self_hosted_card_with_empty_columns_still_builds():
    """最小自建实例卡（只写 dialect+host，其余列留空）必须能建出卡。"""
    raw = {
        "dialect": "gitlab", "host": "gitlab.example.com",
        "webhook_verify": "", "branch_create_form": "", "issue_write_form": "",
    }
    got = _client({"gitlab-corp": dict(raw)}).fetch_cards()
    card = platform_card.build_platform_card("gitlab-corp", got["gitlab-corp"])
    assert card.dialect == "gitlab"
    assert card.host == "gitlab.example.com"
    # 空值列回落到 Builder 默认值，不是非法枚举
    assert card.webhook_verify in platform_card._VALID_WEBHOOK_VERIFY
    assert card.branch_create_form in platform_card._VALID_BRANCH_CREATE_FORM


def test_non_string_falsy_values_are_kept():
    """False / 0 / [] 是有效配置（如 create_repo_options=false），不能被当空值剥掉。"""
    raw = {"dialect": "gitlab", "host": "h.example.com",
           "workflows_write": False, "pr_labels_via_issues": False}
    got = _client({"gitlab-corp": dict(raw)}).fetch_cards()
    assert got["gitlab-corp"]["workflows_write"] is False
    assert got["gitlab-corp"]["pr_labels_via_issues"] is False