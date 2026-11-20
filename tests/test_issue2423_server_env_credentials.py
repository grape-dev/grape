"""Issue #2423 M0/M1-a：服务端凭据上行停用（OV/secret 凭据不进 env_extra）。

C3 根因：enqueue_task 经 _collect_server_env 把 user_credentials 任意键
（OV_API_KEY/OV_BASE_URL/OAuth token 等）明文上抬进 env_extra 落库。
Spec M1：执行期凭据获取改走 tk_ 兑换（exec 端点），env/DB 无 OV 明文。

Seam（公开接口）：src.domains.runtime.engine.service._collect_server_env(user_id) 返回值。
- 凭据类键（命名约定 secret 后缀 + plugin:*/OAuth 身份 provider）不再上行；
- huawei_cloud / codearts 白名单 AK/SK 仍上行（SRE hcloud CLI 依赖，落库加密由 M1-b 闭环）；
- 非 secret 键仍上行（普通配置项行为不变）。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.auth import grape_account_db
from src.domains.runtime.engine.service import _collect_server_env
from src.domains.runtime.engine.env_extra_security import detect_static_ov_conf


def _mk_user(prefix: str) -> dict:
    import uuid
    return grape_account_db.create_grape_account(f"{prefix}_{uuid.uuid4().hex[:8]}",
                                                 email=None, password="pw")


@pytest.fixture(autouse=True)
def _clean():
    _uids = []
    yield _uids
    with db_lock:
        c = db.cursor()
        for uid in _uids:
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        db.commit()


def test_ov_plugin_credentials_not_lifted(_clean):
    """OV 插件凭据（ov_api_key/ov_base_url）不再上抬进 env。"""
    user = _mk_user("ovlift")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "plugin:hd-openviking",
                           {"ov_api_key": "ov-secret-1", "ov_base_url": "http://ov.test"})
    env = _collect_server_env(user["id"], "")
    assert "OV_API_KEY" not in env
    assert "OV_BASE_URL" not in env
    assert "ov-secret-1" not in env.values()


def test_ov_provider_credentials_not_lifted(_clean):
    """openviking 直配 provider 同样不上行（凭据走 tk_ 兑换）。"""
    user = _mk_user("ovprov")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "openviking",
                           {"ov_api_key": "ov-secret-2", "note": "x"})
    env = _collect_server_env(user["id"], "")
    assert "OV_API_KEY" not in env


def test_secret_named_keys_not_lifted(_clean):
    """通用 provider：secret 命名键（*_TOKEN/*_KEY/*_SECRET）不上行；普通键保留。"""
    user = _mk_user("seckey")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "custom-tool",
                           {"deploy_hook": "https://hook.example/x",
                            "deploy_token": "tok-1", "api_key": "k-1",
                            "app_secret": "s-1", "password": "p-1"})
    env = _collect_server_env(user["id"], "")
    assert env.get("DEPLOY_HOOK") == "https://hook.example/x"
    assert "DEPLOY_TOKEN" not in env
    assert "API_KEY" not in env
    assert "APP_SECRET" not in env
    assert "PASSWORD" not in env


def test_oauth_identity_provider_not_lifted(_clean):
    """OAuth 身份 provider（gitcode/github，token 字段）不上行 env。"""
    user = _mk_user("oauthprov")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "gitcode", {"token": "pat-abc123"})
    env = _collect_server_env(user["id"], "")
    assert "TOKEN" not in env
    assert "pat-abc123" not in env.values()


def test_whitelisted_cloud_ak_sk_visible_to_sre_node(_clean):
    """huawei_cloud / codearts 白名单 AK/SK 仅 sre 节点任务可见（SRE 部署依赖）。

    评论 200629016 补充验收：迁移后 AK/SK 上行收敛为「仅 sre 节点可见」——
    非 sre 节点任务 env 不得出现用户凭据库任何明文键值。
    """
    user = _mk_user("cloudak")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "huawei_cloud", {"ak": "ak-1", "sk": "sk-1"})
    user_db.set_credential(user["id"], "codearts", {"ak": "ca-1", "sk": "cs-1"})
    env = _collect_server_env(user["id"], "", agent_type="sre")
    assert env.get("HUAWEICLOUD_SDK_AK") == "ak-1"
    assert env.get("HUAWEICLOUD_SDK_SK") == "sk-1"
    assert env.get("CODEARTS_CLI_AK") == "ca-1"
    assert env.get("CODEARTS_CLI_SK") == "cs-1"


@pytest.mark.parametrize("agent_type", ["vod", "issue", "coding", "qa", ""])
def test_cloud_ak_sk_not_visible_to_non_sre_nodes(_clean, agent_type):
    """非 sre 节点（含空 agent_type 历史调用方）任务 env 无 AK/SK 明文。"""
    user = _mk_user("cloudhide")
    _clean.append(user["id"])
    user_db.set_credential(user["id"], "huawei_cloud", {"ak": "ak-2", "sk": "sk-2"})
    user_db.set_credential(user["id"], "codearts", {"ak": "ca-2", "sk": "cs-2"})
    env = _collect_server_env(user["id"], "", agent_type=agent_type)
    for k in ("HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK",
              "CODEARTS_CLI_AK", "CODEARTS_CLI_SK"):
        assert k not in env, f"{k} 不应对 agent_type={agent_type!r} 上行"
    assert "ak-2" not in env.values() and "cs-2" not in env.values()


# ---------- M0：机器级静态 OV conf 检测（共享引擎停用静态路径的可见性） ----------

def test_detect_static_ov_conf_present(monkeypatch, tmp_path):
    """引擎主机存在 ~/.openviking/ovcli.conf → 检测返回路径（C2 混账号风险可见）。"""
    (tmp_path / ".openviking").mkdir()
    (tmp_path / ".openviking" / "ovcli.conf").write_text("api_key=shared-static")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_static_ov_conf() == os.path.join(str(tmp_path), ".openviking", "ovcli.conf")


def test_detect_static_ov_conf_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_static_ov_conf() == ""
