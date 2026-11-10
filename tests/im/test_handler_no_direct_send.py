"""出站直连清零契约（Issue #1008 验收 ①）。

replier 模式下两 handler 的平台 API 发送直连 grep 计数必须为 0：
所有发送调用统一经 egress → OutboundMessage → Replier → Channel.send。
（下载类 download_image/download_file 属适配器入站能力，另案处理，不计入。）
"""
import re
from pathlib import Path

import pytest

_HANDLER_FILES = [
    "src/im/handler_async.py",
    "src/im/vod_handler_async.py",
]

# 发送直连模式（Spec 口径：api.reply_* / send_text_to_user / add_reaction 等）
_DIRECT_SEND_RE = re.compile(
    r"\bapi\.(reply_card|reply_text|send_text_to_user|send_card_to_user|"
    r"send_card_to_chat|send_quick_cmd_card|add_reaction|"
    r"update_card_by_token|update_message|send_raw_card)\b"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("rel", _HANDLER_FILES)
def test_handler_has_no_platform_send_direct_calls(rel: str):
    path = _repo_root() / rel
    source = path.read_text(encoding="utf-8")
    matches = _DIRECT_SEND_RE.findall(source)
    assert not matches, (
        f"{rel} 仍有平台 API 发送直连：{sorted(set(matches))}；"
        "应改为 egress.* → OutboundMessage → Replier → Channel.send"
    )


@pytest.mark.parametrize("rel", _HANDLER_FILES)
def test_handler_has_no_platform_prefix_assumption(rel: str):
    """共享代码不得出现 om_ 前缀平台假设（能力探测替代，事故根因守卫）。"""
    source = (_repo_root() / rel).read_text(encoding="utf-8")
    assert 'startswith("om_")' not in source, (
        f"{rel} 残留 om_ 前缀平台假设；应改 egress.message_id_valid 能力探测"
    )


def test_repo_card_distinguishes_empty_and_failed():
    """三态返回：没有仓库 vs 发送失败必须可区分（2026-11-05 误导文案回归）。"""
    import asyncio
    from unittest.mock import patch
    from src.im import handler_async as H

    class _Egress:
        def __init__(self, ok): self.ok = ok
        async def send_raw_card(self, ctx, msg_id, card, chat_id=""): return self.ok

    # 无仓库 → "empty"
    with patch.object(H, "get_managed_repos", create=True, return_value=[]):
        with patch("src.user.db.get_managed_repos", return_value=[]):
            assert asyncio.run(H._send_repo_card(None, _Egress(True), "cid", "mid", 1, "app")) == "empty"

    # 有仓库但发送失败 → "failed"（不得退化成 empty）
    repos = [{"repo_key": "gitcode:o/r", "is_admin": 1, "is_join": 0}]
    with patch("src.user.db.get_managed_repos", return_value=repos), \
         patch.object(H, "_current_bound_repo", return_value=""):
        assert asyncio.run(H._send_repo_card(None, _Egress(False), "cid", "mid", 1, "app")) == "failed"
        assert asyncio.run(H._send_repo_card(None, _Egress(True), "cid", "mid", 1, "app")) == "sent"
