"""Issue #3632：平台卡契约的**集成**层测试（路由 + 鉴权 + 阻断语义）。

与单元文件（test_issue_3632_platform_card_contract.py）的分工：
- 单元：契约客户端/服务端、装配器、来源策略——不碰 HTTP、不碰 DB；
- 本文件：把 runtime 路由真的挂到 FastAPI 上，用真实 `rta_` 凭据行过
  `_require_credential`，验证 200/401 与响应形状；并锁住 agent 侧"平台未注册 →
  任务显式阻断"这条最关键的行为改动。

为什么要这层：单元测试全绿只能证明"零件对"，而 #3632 的现场结论是"链路断在
进程之间"。评审也指出缺少"路由真挂在 worker server、rta_ 鉴权真能过、
端到端真能返回 gitee"的证据——本文件补这一层。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.domains.runtime.vcs import card_contract


GITEE_ROW = {
    "provider": "gitee", "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "webhook_dialect": "gitee_v5",
    "clone_auth": "basic_password", "issue_write_form": "owner_param",
    "branch_create_form": "form", "brand_color": "#C71D23",
}
# 假凭据：仅用于匹配路由的 `rta_` 前缀并种到测试库，不对应任何真实账号/服务。
TOKEN = "rta_issue3632_integration"


class _FakeMgmtService:
    """契约数据源桩：等价于 mgmt 域 Local 返回的行（已只含白名单列）。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_repo_platform_cards(self, enabled_only: bool = True):
        self.calls.append(enabled_only)
        return list(self.rows)


def _seed_credential() -> None:
    """在测试库种一条 active 的 worker 凭据（token 与 sha256 同路由实现口径）。"""
    from src.kernel.session_manager import db

    db.execute(
        "CREATE TABLE IF NOT EXISTS runtime_credentials ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT DEFAULT '',"
        " scope TEXT DEFAULT 'worker', owner_name TEXT DEFAULT '',"
        " owner_id INTEGER DEFAULT 0, repo_key TEXT DEFAULT '',"
        " token_hash TEXT UNIQUE, status TEXT DEFAULT 'active')")
    db.commit()
    # 表可能由 init_database 建过（列集合随迁移变化）：按真实列裁剪后插入，
    # 避免 NOT NULL 列（如 name）缺值导致 IntegrityError。
    cols = {row[1] for row in db.execute("PRAGMA table_info(runtime_credentials)").fetchall()}
    values = {
        "name": "issue3632-test",
        "scope": "worker",
        "owner_name": "",
        "owner_id": 0,
        "repo_key": "",
        "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "status": "active",
    }
    use = {k: v for k, v in values.items() if k in cols}
    marks = ", ".join("?" for _ in use)
    db.execute(
        f"INSERT OR REPLACE INTO runtime_credentials ({', '.join(use)}) VALUES ({marks})",
        tuple(use.values()))
    db.commit()


@pytest.fixture()
def app_client(monkeypatch):
    from src.domains.runtime.routes.runtime import router

    app = FastAPI()
    app.include_router(router)
    _seed_credential()
    fake = _FakeMgmtService([GITEE_ROW, {"provider": "gitcode", "host": "gitcode.com"}])
    monkeypatch.setattr("src.kernel.gsb.provider.get_service", lambda name: fake)
    client = TestClient(app)
    client._fake_mgmt = fake  # 供断言 enabled_only 透传
    yield client


def test_route_is_mounted_under_runtime_prefix():
    """/api/runtime/platform-cards 必须真挂在 runtime router 上（worker server 同源）。"""
    from src.domains.runtime.routes.runtime import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/runtime/platform-cards" in paths
    assert router.prefix == "/api/runtime"


def test_endpoint_requires_runtime_credential(app_client):
    """无 token / 非 rta_ / 未登记 token → 401（不得裸奔）。"""
    assert app_client.get("/api/runtime/platform-cards").status_code == 401
    assert app_client.get("/api/runtime/platform-cards",
                          headers={"Authorization": "Bearer not-a-runtime-token"}
                          ).status_code == 401
    assert app_client.get("/api/runtime/platform-cards",
                          headers={"Authorization": "Bearer rta_unknown"}
                          ).status_code == 401


def test_endpoint_returns_platform_cards_end_to_end(app_client):
    """带真实 rta_ 凭据 → 200，响应含 gitee，且经 mgmt 契约取数（enabled_only 透传）。"""
    resp = app_client.get("/api/runtime/platform-cards",
                          headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    cards = resp.json()["cards"]
    assert set(cards) == {"gitee", "gitcode"}
    assert cards["gitee"]["host"] == "gitee.com"
    assert app_client._fake_mgmt.calls == [True], "必须按契约取数，enabled_only=True 透传"


def test_endpoint_degrades_instead_of_500(monkeypatch):
    """mgmt 契约不可用 → 200 + 空 cards + error 标记（worker 侧据此 WARNING 并回落）。"""
    from src.domains.runtime.routes.runtime import router

    class _Boom:
        def get_repo_platform_cards(self, enabled_only: bool = True):
            raise RuntimeError("1146 grape_t_repo_platforms doesn't exist")

    app = FastAPI()
    app.include_router(router)
    _seed_credential()
    monkeypatch.setattr("src.kernel.gsb.provider.get_service", lambda name: _Boom())
    client = TestClient(app)

    resp = client.get("/api/runtime/platform-cards",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cards": {}, "error": "platform_cards_unavailable"}


def test_worker_platform_card_block_is_a_task_failure(caplog):
    """平台未注册 → 任务以 failed 结束并带稳定 code，不再"假完成"。"""
    from src.domains.runtime.engine.agent import platform_card_block_result
    from src.domains.runtime.vcs.platform_card import PlatformCardMissingError

    exc = PlatformCardMissingError("gitee")
    with caplog.at_level("ERROR"):
        result = platform_card_block_result("task-1", "gitee:acme/demo", exc)

    assert result["_started"] is False, "必须阻断，不进入 Agent 执行"
    assert result["_error"].startswith("platform_not_registered:")
    assert "gitee" in result["_error"]
    assert any("blocked by platform card" in r.getMessage() for r in caplog.records)


def test_contract_server_uses_kernel_provider_seam():
    """服务端取数入口必须是 get_service("mgmt") 契约，而不是直读表/直连 DSN。"""
    fake = _FakeMgmtService([GITEE_ROW])
    server = card_contract.PlatformCardContractServer(mgmt_service=fake)
    assert server.snapshot()["cards"]["gitee"]["host"] == "gitee.com"
    assert fake.calls == [True]


class _FakeHttpResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _worker_env(monkeypatch, base="http://127.0.0.1:8080"):
    monkeypatch.setenv("RUNTIME_SERVER_URL", base)
    monkeypatch.setenv("RUNTIME_TOKEN", "rta_fake_test_token")
    # 引擎进程的真实特征：没有 mgmt DSN → 表不可读。
    # 打 platform.public（轻量 wrapper，调用期解析），不 import importer
    # ——后者顶层依赖 sqlalchemy，会让本文件在缺该依赖的机器上无法收集。
    monkeypatch.setattr("src.domains.platform.public.is_table_backed", lambda: False)
    from src.domains.platform.public import is_table_backed as _probe
    assert _probe() is False, "判据桩必须生效（否则表源会真的去读本进程库）"


def test_worker_without_dsn_gets_gitee_via_contract(monkeypatch, tmp_path):
    """端到端行为（issue 验收 3）：无 mgmt DSN 的引擎进程靠契约拿到 gitee 卡。"""
    from src.domains.runtime.vcs import platform_card as pc

    _worker_env(monkeypatch)
    monkeypatch.setattr(pc, "_CONFIGS_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeHttpResp(json.dumps({"cards": {"gitee": GITEE_ROW}})))

    cards = pc._build_cards()

    assert "gitee" in cards, "契约通道必须把 gitee 卡带进引擎进程"
    assert cards["gitee"].host == "gitee.com"
    assert cards["gitee"].clone_auth == "basic_password"


def test_worker_without_dsn_and_unreachable_contract_falls_back_loudly(monkeypatch, tmp_path, caplog):
    """端到端行为（issue 验收 4）：契约不可达 → 回落文件/代码默认，且 WARNING 响亮。"""
    from src.domains.runtime.vcs import platform_card as pc

    _worker_env(monkeypatch, base="http://127.0.0.1:9")  # 不可达
    monkeypatch.setattr(pc, "_CONFIGS_DIR", str(tmp_path / "absent"))

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    with caplog.at_level("WARNING"):
        cards = pc._build_cards()

    assert "gitee" not in cards, "契约与文件卡都不可得时确实没有 gitee（原缺陷前置条件）"
    assert {"gitcode", "github", "gitlab"} <= set(cards), "代码默认卡仍生效"
    assert any("服务端平台卡契约取卡失败" in r.getMessage() for r in caplog.records)
