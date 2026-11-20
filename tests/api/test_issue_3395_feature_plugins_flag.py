"""Issue #3395：前端功能开关 GRAPE_FEATURE_PLUGINS 双态测试。

Seam（公开接口，不测内部实现）：
1. 服务端单一下发源 `src.api.frontend_flags`：默认开启；仅 0/false/no/off 关闭。
2. shell HTML 路由 `/`：
   - flag=1（默认）：`window.__FEATURES__.plugins === true`，侧边栏 nav-plugins 保留；
   - flag=0：`window.__FEATURES__.plugins === false`，`<a id="nav-plugins">` 整块不输出，
     根 `<html>` 带 `data-plugins-off="1"`。
3. 后端 API 保留：两种 flag 下 `GET /api/plugins` 均 200（不因开关下线）。
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


_SAMPLE_SHELL = (
    '<!DOCTYPE html><html lang="zh-CN"><head><title>x</title></head><body>'
    '<a id="nav-plugins" onclick="app.nav(\'plugins\')"><span class="ic">🧩</span>'
    '<span data-i18n="app.nav.plugins">插件市场</span></a>'
    '<a id="nav-scene-market" onclick="app.nav(\'scene-market\')">场景·技能</a>'
    '</body></html>'
)


def _frontend_app():
    """最小前端 + 插件路由应用（catch-all 最后注册，避免拦截 API）。"""
    from src.api import spa
    from src.api.deps import require_org_member
    from src.domains.mgmt.routes import plugins

    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_org_member] = lambda: {"id": 1, "org_id": 0, "username": "t"}
    spa.install_spa_routes(app)
    return app


@pytest.mark.parametrize("raw, expected", [
    ("1", True),
    ("0", False),
    ("true", True),
    ("false", False),
    ("OFF", False),
    ("", True),
])
def test_plugins_enabled_semantics(monkeypatch, raw, expected):
    """只接受 0/false/no/off 为关闭；空值/缺省按默认 1 开启。"""
    from src.api import frontend_flags as ff

    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", raw)
    assert ff.plugins_enabled() is expected


def test_plugins_enabled_defaults_to_on(monkeypatch):
    from src.api import frontend_flags as ff

    monkeypatch.delenv("GRAPE_FEATURE_PLUGINS", raising=False)
    assert ff.plugins_enabled() is True


def test_apply_feature_flags_on_keeps_nav(monkeypatch):
    from src.api import frontend_flags as ff

    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", "1")
    out = ff.apply_feature_flags(_SAMPLE_SHELL)

    assert "window.__FEATURES__" in out
    assert '"plugins": true' in out or '"plugins":true' in out
    assert 'id="nav-plugins"' in out
    assert "data-plugins-off" not in out


def test_apply_feature_flags_off_strips_nav_and_marks_html(monkeypatch):
    from src.api import frontend_flags as ff

    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", "0")
    out = ff.apply_feature_flags(_SAMPLE_SHELL)

    assert "window.__FEATURES__" in out
    assert '"plugins": false' in out or '"plugins":false' in out
    assert 'id="nav-plugins"' not in out, "flag=0 时侧边栏插件入口必须整块不输出"
    assert 'id="nav-scene-market"' in out, "只移除插件入口，其它导航不受影响"
    assert 'data-plugins-off="1"' in out


def test_apply_feature_flags_is_idempotent(monkeypatch):
    from src.api import frontend_flags as ff

    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", "0")
    once = ff.apply_feature_flags(_SAMPLE_SHELL)
    twice = ff.apply_feature_flags(once)

    assert twice.count("window.__FEATURES__") == 1
    assert twice.count('data-plugins-off="1"') == 1


def test_shell_route_flag_on_keeps_nav_plugin(monkeypatch):
    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", "1")
    client = TestClient(_frontend_app())
    r = client.get("/")

    assert r.status_code == 200
    assert 'id="nav-plugins"' in r.text
    assert '"plugins": true' in r.text or '"plugins":true' in r.text


def test_shell_route_flag_off_hides_nav_plugin(monkeypatch):
    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", "0")
    client = TestClient(_frontend_app())
    r = client.get("/")

    assert r.status_code == 200
    assert 'id="nav-plugins"' not in r.text
    assert '"plugins": false' in r.text or '"plugins":false' in r.text
    assert 'data-plugins-off="1"' in r.text


@pytest.mark.parametrize("flag", ["1", "0"])
def test_plugins_api_available_regardless_of_flag(monkeypatch, flag):
    monkeypatch.setenv("GRAPE_FEATURE_PLUGINS", flag)
    client = TestClient(_frontend_app())
    r = client.get("/api/plugins")

    assert r.status_code == 200, f"flag={flag} 时后端 /api/plugins 必须保留（200）"
    assert "plugins" in r.json()