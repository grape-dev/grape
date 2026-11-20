"""Issue #3167：`org_id` 类型归一 —— 引擎/设备页 500 修复回归。

背景：历史库 `runtime_credentials` / `agent_runtimes` 的 `org_id` 以 integer 存储，
读取侧 `_org_unassigned()` 对它调用 `.strip()` 抛
`AttributeError: 'int' object has no attribute 'strip'`，导致
`GET /api/settings/agent-runtimes` 与 `GET /api/settings/runtime-devices`
在「个人组织视图 + 历史库」条件下必现 500；同一类型缺陷的另一面是
`'2' == 2` 恒 `False`（不报错），把本应可见的引擎/设备静默过滤。

本文件覆盖：
1. `_org_unassigned` 六值锁（Spec 验收）：`0` / `"0"` / `""` / `None` → True，
   `5` / `"5"` → False；补充哨兵族与带空白脏值。
2. `_org_key` 的 int/str 等价（`5` 与 `"5"` 同权，`0` 与 `""` 同权）。
3. 端点级回归：个人组织视图 + 历史 int `org_id` 行 → 不抛异常且引擎/设备可见。
4. 端点级回归：非个人组织视图下 int 行与 str 行可见性一致（不静默过滤）。

数据种子风格对齐 tests/runtime/test_issue_473_range_filter.py。
"""
import hashlib

import pytest

from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock

init_database()

_PERSONAL_ORG = 947301      # 个人组织（is_personal=1）
_COMPANY_ORG = 947302       # 非个人组织（is_personal=0）

_USER_ID = 301
_USER_NAME = "t3167user"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't3167-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't3167-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?)", (_PERSONAL_ORG, _COMPANY_ORG))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,1)",
            (_PERSONAL_ORG, "t3167-personal", "t3167-personal", _USER_ID))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,0)",
            (_COMPANY_ORG, "t3167-company", "t3167-company", _USER_ID))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)",
                  (_USER_ID, _USER_NAME))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't3167-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't3167-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?)", (_PERSONAL_ORG, _COMPANY_ORG))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        db.commit()


def _user(org_id):
    """当前登录用户（org_id 由用例指定，可为 int 或 str）。"""
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": org_id}


def _create_cred(name, engine, scope, org_id, owner_name=_USER_NAME, repo_key=""):
    """种一条凭据 + 一条引擎行（org_id 按参数原样落库，用于模拟历史 integer 行）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, "
            "owner_id, repo_key, org_id, status) VALUES (?,?,?,?,?,?,?,?, 'active')",
            (hashlib.sha256(name.encode()).hexdigest(), name, name, scope, owner_name, "",
             repo_key, org_id))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'user','',?,'online')",
            (cid, engine, engine, org_id))
        db.commit()
        return cid


# --------------------------------------------------------------------------- 单元层


@pytest.mark.parametrize("org_id,expected", [
    (0, True),
    ("0", True),
    ("", True),
    (None, True),
    (5, False),
    ("5", False),
])
def test_org_unassigned_six_values(org_id, expected):
    """Spec 六值锁：哨兵族真值集合与真实归属值互斥。"""
    from src.domains.mgmt.routes.settings import _org_unassigned
    assert _org_unassigned({"org_id": org_id}) is expected


@pytest.mark.parametrize("org_id", [" 0 ", "None", " None ", 0.0])
def test_org_unassigned_sentinel_family(org_id):
    """历史脏值：带空白的 '0'、文本 'None'、数值 0.0 均视为未归属。"""
    from src.domains.mgmt.routes.settings import _org_unassigned
    assert _org_unassigned({"org_id": org_id}) is True


def test_org_unassigned_missing_key():
    """缺 org_id 字段的行等同未归属（不抛 KeyError）。"""
    from src.domains.mgmt.routes.settings import _org_unassigned
    assert _org_unassigned({}) is True


@pytest.mark.parametrize("org_id,expected", [
    (5, "5"),
    ("5", "5"),
    (" 5 ", "5"),
    (0, ""),
    ("0", ""),
    ("", ""),
    (None, ""),
])
def test_org_key_int_str_equivalence(org_id, expected):
    """`_org_key` 归一：真实归属 int/str 同权，哨兵族统一折叠为 ''。"""
    from src.domains.mgmt.routes.settings import _org_key
    assert _org_key({"org_id": org_id}) == expected
    assert _org_key(org_id) == expected, "裸值应可与行 dict 等价调用"


# --------------------------------------------------------------------------- 端点层


def test_personal_org_view_with_int_org_id_no_500():
    """个人组织视图 + 历史 int org_id 行：两个端点不再 500，且引擎/设备可见。

    修复前 `_org_unassigned` 对 int org_id 调 `.strip()` 抛 AttributeError；
    修复后经 `_org_key` 归一，int 行与 str 用户组织同权。
    """
    from src.domains.mgmt.routes.settings import get_agent_runtimes, get_runtime_devices
    _create_cred("t3167-personal-cred", "t3167-engine", "user", _PERSONAL_ORG)

    runtimes = get_agent_runtimes(user=_user(str(_PERSONAL_ORG)))
    assert "t3167-engine" in {rt["name"] for rt in runtimes["runtimes"]}, \
        "个人组织视图下 org_id 为 int 的引擎不应被过滤/不应 500"

    devices = get_runtime_devices(user=_user(str(_PERSONAL_ORG)), page=1, page_size=100)
    assert "t3167-personal-cred" in {d["name"] for d in devices["devices"]}, \
        "个人组织视图下 org_id 为 int 的设备不应被过滤/不应 500"


def test_personal_org_view_int_equals_str_visibility():
    """同一组织下 int 型与 str 型 org_id 的可见性判定一致。"""
    from src.domains.mgmt.routes.settings import get_agent_runtimes
    _create_cred("t3167-int-cred", "t3167-engine-int", "user", _PERSONAL_ORG)
    _create_cred("t3167-str-cred", "t3167-engine-str", "user", str(_PERSONAL_ORG))

    names = {rt["name"] for rt in get_agent_runtimes(user=_user(str(_PERSONAL_ORG)))["runtimes"]}
    assert "t3167-engine-int" in names and "t3167-engine-str" in names, \
        f"int/str 型 org_id 可见性应一致: {names}"


def test_company_org_view_int_org_id_visible():
    """非个人组织视图：org_id 为 int 的引擎/设备可见（比较口径统一，非放宽隔离）。"""
    from src.domains.mgmt.routes.settings import get_agent_runtimes, get_runtime_devices
    _create_cred("t3167-company-cred", "t3167-engine-company", "user", _COMPANY_ORG)

    runtimes = get_agent_runtimes(user=_user(str(_COMPANY_ORG)))
    assert "t3167-engine-company" in {rt["name"] for rt in runtimes["runtimes"]}

    devices = get_runtime_devices(user=_user(str(_COMPANY_ORG)), page=1, page_size=100)
    assert "t3167-company-cred" in {d["name"] for d in devices["devices"]}


def test_company_org_view_strict_isolation_kept():
    """非个人组织严格隔离不回归：其他组织的 int org_id 引擎仍不可见。"""
    from src.domains.mgmt.routes.settings import get_agent_runtimes
    _create_cred("t3167-other-cred", "t3167-engine-other", "user", _PERSONAL_ORG)

    names = {rt["name"] for rt in get_agent_runtimes(user=_user(str(_COMPANY_ORG)))["runtimes"]}
    assert "t3167-engine-other" not in names, "非个人组织不得看到其他组织引擎"
