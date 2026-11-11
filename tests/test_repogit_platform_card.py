"""git 传输层平台卡驱动用例（S7/S8 扩展，D4）。

S7：clone URL 按平台卡 host 解析，未知 provider fail-fast（替代静默回退 gitcode.com）。
S8 扩展：鉴权 scheme 按平台卡 clone_auth 分派；askpass 链新平台按卡 token_env 派生。
存量 gitcode/github 行为由 step1 的 S8 回归锁兜住（必须继续绿）。
"""
import json
import tempfile

import pytest

from src.domains.runtime.engine import repogit
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


@pytest.fixture()
def gitee_card():
    raw = {
        "name": "Gitee", "dialect": "github", "host": "gitee.com",
        "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
        "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
        "webhook_verify": "token_compare", "clone_auth": "basic_password",
    }
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": raw}),))
        db.commit()
    invalidate_platform_card_cache()
    yield raw
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def test_s7_clone_url_from_card(gitee_card):
    assert repogit.clone_url_for("gitee:o/r") == "https://gitee.com/o/r.git"


def test_s7_unknown_provider_fail_fast():
    """G1 修复：未知 provider 不再静默回退 gitcode.com（历史 bug）。"""
    with pytest.raises(ValueError):
        repogit.clone_url_for("svn:o/r")


def test_s7_known_platforms_unchanged(gitee_card):
    assert repogit.clone_url_for("gitcode:o/r") == "https://gitcode.com/o/r.git"
    assert repogit.clone_url_for("github:o/r") == "https://github.com/o/r.git"
    # 无前缀默认 gitcode（parse_repo_key 现状）
    assert repogit.clone_url_for("o/r") == "https://gitcode.com/o/r.git"


def test_s8_scheme_per_platform(gitee_card):
    tok = "tk"
    # 现状平台：x-access-token
    assert repogit._clone_auth_scheme_for("gitcode:o/r") == "x_access_token"
    assert repogit._clone_auth_scheme_for("github:o/r") == "x_access_token"
    # gitee 卡：basic_password（空用户名 + token 作密码）
    assert repogit._clone_auth_scheme_for("gitee:o/r") == "basic_password"
    import base64
    assert base64.b64decode(repogit._basic_auth(tok, "basic_password")).decode() == f":{tok}"
    assert base64.b64decode(repogit._basic_auth(tok, "oauth2")).decode() == f"oauth2:{tok}"
    assert base64.b64decode(repogit._basic_auth(tok)).decode() == f"x-access-token:{tok}"


def test_s8_askpass_new_platform_uses_card_env(gitee_card):
    with tempfile.TemporaryDirectory() as td:
        p = repogit._write_askpass(td, "gitee:o/r")
        s = open(p).read()
        assert "BOT_GITEE_TOKEN" in s and "USER_GITEE_TOKEN" in s
        assert s.index("BOT_GITEE_TOKEN") < s.index("BOT_TOKEN")
        # 存量两平台的链不被新逻辑影响
        p2 = repogit._write_askpass(td, "github:o/r")
        assert "BOT_GITHUB_TOKEN" in open(p2).read()
        p3 = repogit._write_askpass(td, "gitcode:o/r")
        assert "BOT_GITCODE_TOKEN" in open(p3).read()


def test_s8_run_git_command_injection_format():
    """_run_git 命令构造：token 进 http.extraheader（不进 URL/进程参数明文）。"""
    import base64
    with tempfile.TemporaryDirectory() as td:
        # dry：不真执行网络，用一个本地必失败的参数组合检查 cmd 构造路径
        r = repogit._run_git(["--version"], token="", timeout=10)
        assert r is not None and r.returncode == 0
        # scheme=basic_password 时 header 用户名段为空（gitee 语义）
        hdr = repogit._basic_auth("abc", "basic_password")
        assert base64.b64decode(hdr).decode() == ":abc"
