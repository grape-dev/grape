"""Issue #3897 D 组（A）：RepoBotToken 历史列回退日志降噪，但回退能力不变。

现场：`RepoBotToken: historical column fallback hit (rc miss)` 约 3.9 万条/小时
（worker 1.4 万 + mgmt 1.4 万量级）——token 解析是高频率路径，每条历史数据命中都
打 WARNING，真正的告警被淹掉。

契约（验收 D-1：0 条/小时 **或** debug）：
  1. 回退仍然生效（返回值不变，**不能**为了降噪把回退路径关掉）；
  2. 默认降为 DEBUG；
  3. 同一 `(repo, field)` 只保留 1 条 WARNING / 小时（观测与下线判据 grep 仍可用）；
  4. 不同 repo 各自有 1 条 WARNING（不是全局只报一次，避免掩盖面）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock
from src.kernel import token_resolver as TR

_REPO_A = "gitcode:issue3897a/repo"
_REPO_B = "gitcode:issue3897b/repo"
_COL = "bot_gitcode_token"
_TOKEN = "tok-3897-legacy"


def _setup_repo(repo_key: str, value: str) -> None:
    _o, _r = repo_key.split(":", 1)[1].split("/", 1)
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
        if _COL not in cols:
            c.execute(f"ALTER TABLE managed_repos ADD COLUMN {_COL} TEXT DEFAULT ''")
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            f"{_COL}) VALUES (?, ?, ?, ?, ?)",
            (repo_key, _o, _r, f"{_o}/{_r}", value))
        db.commit()


def _clear_rate_limit():
    TR._fallback_warn_at.clear()


class _FakeProvider:
    """最小 provider：只声明历史列（真 provider 的 LEGACY 列已随平台卡退役，
    但存量生产部署仍有卡片声明该列——本用例锁的是回退分支自身的行为）。"""

    def token_storage_field(self) -> str:
        return _COL


def _strategy() -> "TR.RepoBotToken":
    s = TR.RepoBotToken.__new__(TR.RepoBotToken)
    s.provider = _FakeProvider()
    s.bypass_path_b = False
    return s


def test_fallback_still_returns_token_and_warns_once_per_hour(caplog):
    _setup_repo(_REPO_A, _TOKEN)
    _clear_rate_limit()
    caplog.set_level("DEBUG")

    strat = _strategy()
    for _ in range(5):
        assert strat._fetch(_REPO_A) == _TOKEN, "降噪不得影响回退取值"

    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "historical column fallback hit" in r.getMessage()]
    debugs = [r for r in caplog.records
              if r.levelname == "DEBUG" and "historical column fallback hit" in r.getMessage()]
    assert len(warns) == 1
    assert len(debugs) == 4
    assert _REPO_A in warns[0].getMessage() and _COL in warns[0].getMessage()


def test_rate_limit_is_per_repo(caplog):
    _setup_repo(_REPO_A, _TOKEN)
    _setup_repo(_REPO_B, _TOKEN)
    _clear_rate_limit()
    caplog.set_level("DEBUG")

    strat = _strategy()
    strat._fetch(_REPO_A)
    strat._fetch(_REPO_B)

    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "historical column fallback hit" in r.getMessage()]
    assert len(warns) == 2, "不同 repo 各自保留一条 WARNING（不掩盖受影响面）"