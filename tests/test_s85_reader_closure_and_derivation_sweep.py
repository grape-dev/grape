"""S85 用例（D60 批次 2 扩展，先测后改）：S82 读者收口补漏 + 全仓派生清零。

三块：
1. 真回归修复锁：get_bot_token_by_repo（12 调用点读者）rc 感知——
   S82 冻结 managed_repos 凭据列后漏切本读者，新托管仓 bot token 只进
   grape_t_repo_credentials，本读者读列返回 ''（merge-base 过/S82 挂，worktree
   实证）。修法=S82 同款：rc 优先 + 列回落（历史行），路径 B 守卫保持；
2. 结构守卫扩展：全 src/ 三元式内联派生清零（S84 只罩 user/db/
   token_resolver，本批扩全仓——events/scenes/event_pipeline/platform_card
   同款 ~10 处收口后守卫转绿）；
3. 行为锁：source 派生（events/scenes）gitee 前缀不折叠 gitcode。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_owner LIKE 's85o%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 'gitcode:s85o%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_owner LIKE 's85o%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 'gitcode:s85o%'")
        db.commit()


# ---------- 1. get_bot_token_by_repo rc 感知（S82 补漏） ----------

def test_s85_bot_token_reader_hits_rc_for_new_repo():
    """S82 后新托管仓凭据只在 rc：读者必须感知（12 调用点继承）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitcode:s85o1/r1"
    assert udb.add_managed_repo(rk, "s85o1", "r1", webhook_id=1,
                                bot_gitcode_token="btok-rc") is True
    assert udb.get_bot_token_by_repo("s85o1", "r1") == "btok-rc"


def test_s85_bot_token_reader_legacy_column_fallback():
    """历史行（列有值、rc 无行——backfill 前的旧库形态）：列回落不破坏。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitcode:s85o2/r2"
    with db_lock:
        db.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, "
            "full_name, bot_gitcode_token) VALUES (?,?,?,?,?)",
            (rk, "s85o2", "r2", "s85o2/r2", "btok-col"))
        db.commit()
    assert udb.get_bot_token_by_repo("s85o2", "r2") == "btok-col"


def test_s85_bot_token_reader_path_b_guard_kept():
    """路径 B（host_user_id>0）跳过语义保持（R12.12，不随收口弱化）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitcode:s85o3/r3"
    assert udb.add_managed_repo(rk, "s85o3", "r3", webhook_id=1,
                                bot_gitcode_token="btok-b",
                                host_user_id=7) is True
    assert udb.get_bot_token_by_repo("s85o3", "r3") == ""


# ---------- 2. 全仓结构守卫：三元式内联派生清零 ----------

_INLINE = '.split(":", 1)[0] if ":" in'


def test_s85_repo_wide_no_inline_provider_derivation():
    """全 src（repo_key.py 自身除外）三元式 provider 派生清零。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "repo_key.py":
            continue
        text = py.read_text(encoding="utf-8")
        if _INLINE in text:
            for i, line in enumerate(text.splitlines(), 1):
                if _INLINE in line:
                    offenders.append(f"{py.relative_to(root.parent)}:{i}")
    assert not offenders, (
        "内联 provider 派生残留（统一走 src.kernel.repo_key.parse_repo_key，"
        f"D60③/S85 全仓清零）：{offenders}")


# ---------- 3. 行为锁：source 派生 gitee 不折叠 ----------

def test_s85_source_derivation_gitee_not_folded():
    """events/scenes source 派生：gitee: 前缀 → 'gitee'（不折叠 gitcode）。"""
    from src.kernel.repo_key import parse_repo_key
    assert parse_repo_key("gitee:o/r")[0] == "gitee"
    assert parse_repo_key("gitcode:o/r")[0] == "gitcode"
    assert parse_repo_key("o/r")[0] == "gitcode"
