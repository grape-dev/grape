"""一键部署闭环：server_base_url 从 Host 头自愈 — 单元测试

背景（AI 部署 / 平台托管环境）：
- GRAPE_AUTO_DETECT_URL=false（AI 为加速启动常关闭探测）→ meta.webhook_url /
  server_base_url 为空 → get_server_base_url() 各读取点兜底 http://localhost:3000
  → OAuth 回调 / A2A / 前端设备接入静默失败。
- 既有 Host 自愈（main.py 中间件）只覆盖 webhook_url，本改动补全 server_base_url。

测试覆盖（所有 DB 操作用 mock，不连真实数据库）：
- 中间件在 server_base_url 为空 / localhost / 127.x / 0.0.0.0 时写入 Host 推导地址
- server_base_url 已是有效公网地址时幂等跳过（不覆盖用户显式配置）
- 推导复用 detect_webhook_from_host 的安全约束（本地 Host 不写入）
- webhook_url 分支行为不变（回归保护）
"""
from unittest.mock import patch, MagicMock

import pytest


# 中间件里判本地基址的逻辑是内嵌函数，这里以同规格实现做行为参考，
# 结构断言（test_structure_*）直接读源码防漂移。
def _is_local_base(base: str) -> bool:
    # 与 main.py 中间件内嵌函数同规格（复核 #7 同步，2026-10-28：补 https 形态）
    b = (base or "").lower().rstrip("/")
    return (not b) or b.startswith(
        ("http://localhost", "https://localhost", "http://127.", "https://127.",
         "http://0.0.0.0", "https://0.0.0.0", "http://[::1]", "https://[::1]")
    )


class TestLocalBaseJudgement:
    """server_base_url 本地/空值判定（何时允许 Host 自愈覆盖）。"""

    def test_empty_and_none_are_local(self):
        assert _is_local_base("") is True
        assert _is_local_base(None) is True

    def test_https_local_variants_are_local(self):
        """复核 #7（2026-10-28 审查）：反代终结 TLS 后写回的 https 本地值同样可自愈。"""
        assert _is_local_base("https://localhost") is True
        assert _is_local_base("https://127.0.0.1") is True
        assert _is_local_base("https://0.0.0.0:8443") is True
        assert _is_local_base("https://[::1]:3000") is True
        # https 公网有效值不得误判为本地
        assert _is_local_base("https://example.com") is False

    def test_structure_https_prefixes_in_middleware(self):
        """结构守卫：main.py _is_local_base 须含 https://localhost 前缀（防漂移回退）。"""
        import pathlib
        main_src = pathlib.Path("src/api/main.py").read_text(encoding="utf-8")
        assert '"https://localhost"' in main_src, "复核 #7 修复不得回退：https 本地形态须在白名单"
        assert '"https://127."' in main_src

    def test_local_variants_are_local(self):
        assert _is_local_base("http://localhost:3000") is True
        assert _is_local_base("http://localhost") is True
        assert _is_local_base("http://127.0.0.1:3000") is True
        assert _is_local_base("http://0.0.0.0:8080") is True
        assert _is_local_base("http://[::1]:3000") is True

    def test_public_base_not_local(self):
        assert _is_local_base("https://workspace.example.com") is False
        assert _is_local_base("http://1.2.3.4:3000") is False  # 示例地址（原真实实测环境 IP 不随开源仓发布）

    def test_real_public_base_not_local(self):
        """真实部署过的公网地址不被判定为本地（不能被覆盖）。"""
        assert _is_local_base("http://5.6.7.8:8080") is False  # 示例地址（原为真实部署过的公网地址）


class TestMiddlewareHealsServerBaseUrl:
    """中间件 server_base_url Host 自愈行为（mock DB 验证写入序列）。"""

    def _run_middleware_logic(self, meta_values: dict):
        """按 main.py 中间件的实际代码路径执行（逐行翻译，非引用真函数，
        真函数依赖 Request 对象；此处以等价 mock 驱动验证写入决策）。"""
        from src.user.db import detect_webhook_from_host
        host = "workspace.example.com"
        fwd_host, fwd_proto = "", "https"
        writes = {}

        def fake_get(key, _m=meta_values):
            return _m.get(key, "")

        def fake_set(key, value):
            writes[key] = value

        # —— 以下与 main.py 中间件逻辑一一对应 ——
        if not fake_get("webhook_url"):
            _candidate = detect_webhook_from_host(host, fwd_host, fwd_proto)
            if _candidate:
                fake_set("webhook_url", _candidate)
        if _is_local_base(fake_get("server_base_url")):
            _base_candidate = detect_webhook_from_host(host, fwd_host, fwd_proto)
            if _base_candidate:
                fake_set("server_base_url", _base_candidate[: -len("/webhook/gitcode")])
        return writes

    def test_heals_when_server_base_url_empty(self):
        """server_base_url 为空 → 从 Host 头写入公网基址。"""
        writes = self._run_middleware_logic({})
        assert writes["webhook_url"] == "https://workspace.example.com/webhook/gitcode"
        assert writes["server_base_url"] == "https://workspace.example.com"

    def test_heals_when_server_base_url_is_localhost(self):
        """server_base_url 为 localhost 残留（探测关闭场景）→ 覆盖为公网基址。"""
        writes = self._run_middleware_logic(
            {"webhook_url": "https://x.example.com/webhook/gitcode", "server_base_url": "http://localhost:3000"}
        )
        assert writes == {"server_base_url": "https://workspace.example.com"}

    def test_no_overwrite_when_server_base_url_valid(self):
        """server_base_url 已是有效公网地址（用户显式配置/DB 既有值）→ 幂等跳过。"""
        writes = self._run_middleware_logic(
            {"webhook_url": "https://x.example.com/webhook/gitcode", "server_base_url": "https://x.example.com"}
        )
        assert "server_base_url" not in writes

    def test_local_host_not_written(self):
        """Host 头本身是本地地址（本地开发直连 127.0.0.1:3000）→ 不写入。"""
        from src.user.db import detect_webhook_from_host
        candidate = detect_webhook_from_host("127.0.0.1:3000", "", "")
        assert candidate == ""  # 现有安全约束：本地 Host 不推导


class TestStructureGuards:
    """S7 源码结构断言：防止后续重构把自愈逻辑弄丢。"""

    def test_s7_structure_server_base_url_heal_present(self):
        """S7: set/get_meta_value("server_base_url") 调用存在，自愈逻辑弄丢即失败。"""
        import inspect
        import src.api.main as main_mod
        src = inspect.getsource(main_mod.request_id_and_log_middleware)
        assert 'set_meta_value("server_base_url"' in src, "中间件缺少 server_base_url Host 自愈写入"
        assert 'get_meta_value("server_base_url")' in src, "中间件缺少 server_base_url 读取判定"

    def test_s7_structure_webhook_heal_unchanged(self):
        """S7b: webhook_url 既有自愈分支保留（回归保护）。"""
        import inspect
        import src.api.main as main_mod
        src = inspect.getsource(main_mod.request_id_and_log_middleware)
        assert 'set_meta_value("webhook_url"' in src

    def test_s6_regression_file_exists_and_runs(self):
        """S6: 既有 Issue #372 webhook 检测回归文件存在且含用例（回归保护入口）。

        S6 的完整执行 = CI 全量跑 tests/test_issue_372_webhook_url_detect.py
        （28 用例）；本用例保证该文件不被误删/清空，回归链不静默断掉。
        """
        import pathlib
        p = pathlib.Path(__file__).parent / "test_issue_372_webhook_url_detect.py"
        assert p.is_file(), "Issue #372 webhook 回归文件丢失"
        content = p.read_text(encoding="utf-8")
        assert "def test_" in content, "Issue #372 webhook 回归文件无用例"
        assert "detect_webhook_from_host" in content


# ===========================================================================
# 场景集成用例（spec.md S1-S5）：真实中间件函数 + mock DB + TestClient
# ===========================================================================

from fastapi import FastAPI
from fastapi.testclient import TestClient

_WORKSPACE_HOST = "workspace.example.com"


def _build_client(meta_values: dict):
    """最小应用挂真实中间件，patch DB 与 JWT 解析，返回 (client, writes)。"""
    import src.user.db as db_mod
    import src.api.jwt_utils as jwt_mod
    import src.api.main as main_mod

    writes = {}
    app = FastAPI()

    @app.get("/api/anything")
    def _anything():
        return {"ok": True}

    app.middleware("http")(main_mod.request_id_and_log_middleware)

    with patch.object(db_mod, "get_meta_value", side_effect=lambda k, _m=meta_values: _m.get(k, "")), \
         patch.object(db_mod, "set_meta_value", side_effect=lambda k, v, _w=writes: _w.__setitem__(k, v)), \
         patch.object(jwt_mod, "verify_access_token", return_value={"sub": 1}):
        yield TestClient(app), writes


class TestScenarioIntegration:
    """spec.md 场景 S1-S5 的端到端（中间件真实执行路径）。"""

    def _request(self, client, host):
        """模拟平台反代链路：Host 头 + X-Forwarded-Proto（平台 https 终端）。"""
        return client.get(
            "/api/anything",
            headers={"Host": host, "X-Forwarded-Proto": "https", "Authorization": "Bearer t"},
        )

    def test_s1_integration(self):
        """S1 全新一键部署首访：meta 空 → 两个 URL 一次性写对。"""
        for client, writes in _build_client({}):
            r = self._request(client, _WORKSPACE_HOST)
            assert r.status_code == 200
            assert writes["webhook_url"] == f"https://{_WORKSPACE_HOST}/webhook/gitcode"
            assert writes["server_base_url"] == f"https://{_WORKSPACE_HOST}"

    def test_s2_integration(self):
        """S2 localhost 残留：server_base_url 被覆盖，webhook_url 不动。"""
        meta = {"webhook_url": f"https://{_WORKSPACE_HOST}/webhook/gitcode", "server_base_url": "http://localhost:3000"}
        for client, writes in _build_client(meta):
            r = self._request(client, _WORKSPACE_HOST)
            assert r.status_code == 200
            assert writes == {"server_base_url": f"https://{_WORKSPACE_HOST}"}

    def test_s3_integration(self):
        """S3 有效值幂等：不写库。"""
        meta = {"webhook_url": "https://x.example.com/webhook/gitcode", "server_base_url": "https://x.example.com"}
        for client, writes in _build_client(meta):
            r = self._request(client, _WORKSPACE_HOST)
            assert r.status_code == 200
            assert writes == {}

    def test_s4_integration(self):
        """S4 本地开发直连（Host=127.0.0.1:3000）：不写任何值。"""
        for client, writes in _build_client({}):
            r = self._request(client, "127.0.0.1:3000")
            assert r.status_code == 200
            assert writes == {}

    def test_s5_integration(self):
        """S5 匿名请求：_uid 守卫拦下，不触发推导。"""
        for client, writes in _build_client({}):
            r = client.get("/api/anything", headers={"Host": _WORKSPACE_HOST})
            assert r.status_code == 200
            assert writes == {}
