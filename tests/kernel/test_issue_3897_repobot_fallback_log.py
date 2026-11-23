"""Issue #3897 D 组（A）：RepoBotToken 历史列回退日志降噪，但回退能力不变。

现场：`RepoBotToken: historical column fallback hit (rc miss)` 约 3.9 万条/小时
（worker 1.4 万 + mgmt 1.4 万量级）——token 解析是高频率路径，每条历史数据命中都
打 WARNING，真正的告警被淹掉。

契约（验收 D-1：0 条/小时 **或** debug）：
  1. 回退仍然生效（返回值不变，**不能**为了降噪把回退路径关掉）；
  2. 默认降为 DEBUG；
  3. 同一 `(repo, field)` 只保留 1 条 WARNING / 小时（观测与下线判据 grep 仍可用）；
  4. 不同 repo 各自有 1 条 WARNING（不是全局只报一次，避免掩盖面）。

全部走 port 桩（`_repos` / `_tokens`）：不依赖测试库表名（schema-mode 用例会把
`managed_repos` 改名，造成顺序相关的假红），也不触发真实解密。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import pytest

from src.kernel import token_resolver as TR

_REPO_A = "gitcode:issue3897a/repo"
_REPO_B = "gitcode:issue3897b/repo"
_COL = "bot_gitcode_token"
_TOKEN = "tok-3897-legacy"


class _FakeProvider:
    """最小 provider：只声明历史列（本用例锁的是回退分支自身的行为）。"""

    def token_storage_field(self) -> str:
        return _COL


@pytest.fixture
def strategy(monkeypatch):
    """把 port 层打桩：rc 恒 miss、历史列恒命中、解密恒等。"""
    monkeypatch.setattr(TR._tokens, "get_repo_credential_meta", lambda rk, kind: None)
    monkeypatch.setattr(TR._tokens, "get_host_user_id", lambda rk: "")
    monkeypatch.setattr(TR._tokens, "repo_secret_decrypt", lambda raw: raw)
    monkeypatch.setattr(TR._repos, "repo_bot_token_from_legacy_column_any",
                        lambda rk, col: _TOKEN if col == _COL else "")
    TR._fallback_warn_at.clear()

    s = TR.RepoBotToken.__new__(TR.RepoBotToken)
    s.provider = _FakeProvider()
    s.bypass_path_b = False
    return s


def test_fallback_still_returns_token_and_warns_once_per_hour(strategy, caplog):
    caplog.set_level("DEBUG")

    for _ in range(5):
        assert strategy._fetch(_REPO_A) == _TOKEN, "降噪不得影响回退取值"

    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "historical column fallback hit" in r.getMessage()]
    debugs = [r for r in caplog.records
              if r.levelname == "DEBUG" and "historical column fallback hit" in r.getMessage()]
    assert len(warns) == 1
    assert len(debugs) == 4
    assert _REPO_A in warns[0].getMessage() and _COL in warns[0].getMessage()


def test_rate_limit_is_per_repo(strategy, caplog):
    caplog.set_level("DEBUG")

    strategy._fetch(_REPO_A)
    strategy._fetch(_REPO_B)

    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "historical column fallback hit" in r.getMessage()]
    assert len(warns) == 2, "不同 repo 各自保留一条 WARNING（不掩盖受影响面）"