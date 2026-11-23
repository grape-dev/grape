"""Issue #1616：nginx /static 与 /openapi.json 显式 location——契约入口固化不漂移。

漂移形态：无专用 location 时 /static/openapi.json、/openapi.json 落进兜底
`location /`，受上层访问控制/缓存策略影响（实测部分部署 /openapi.json 403、
/static/openapi.json 404/200 漂移）→ CLI 契约拉取不可用或拉到旧缓存。

基线说明：本分支集群入口配置为根目录 deploy/nginx-grape.conf（单进程拓扑，全部流量
反代 127.0.0.1:3000）；Spec 中 deploy/nginx-grape-cluster.conf 为上游 weekly-10
三进程拆分引入的等价物，本基线不存在（见 PR 说明）。

seam（配置契约）：deploy/nginx-grape.conf 必须含 /openapi.json 与 /static/ 专用 location，
且位于兜底 location / 之前，显式反代后端、禁缓存（契约必须实时）。
"""
import os

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _conf() -> str:
    with open(os.path.join(_REPO_ROOT, "deploy/nginx-grape.conf"), encoding="utf-8") as f:
        return f.read()


def test_openapi_json_has_dedicated_location():
    conf = _conf()
    assert "location = /openapi.json" in conf
    # 专用块必须显式反代后端（不被兜底 location 漂移接管）
    assert "proxy_pass http://127.0.0.1:38080;" in conf.split("location = /openapi.json", 1)[1].split("}", 1)[0]  # arch11: 契约由 Mgmt 进程(38080)服务


def test_static_openapi_json_has_dedicated_location():
    conf = _conf()
    assert "location /static/" in conf
    block = conf.split("location /static/", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:38080;" in block  # arch11: Mgmt(38080)


def test_dedicated_locations_precede_catch_all():
    """专用 location 必须出现在兜底 location / 之前（nginx 前缀匹配虽与顺序无关，
    显式置前可读性优先，且防止后续在 location / 内加限制时误伤契约入口）。"""
    conf = _conf()
    i_static = conf.index("location /static/")
    i_openapi = conf.index("location = /openapi.json")
    i_catch_all = conf.index("location / {")
    assert i_static < i_catch_all and i_openapi < i_catch_all


def test_contract_locations_are_no_cache():
    """契约入口禁缓存（no-cache）：CLI 拉到的一定是当前版本契约，不经 CDN/代理旧缓存。"""
    conf = _conf()
    block_static = conf.split("location /static/", 1)[1].split("}", 1)[0]
    block_openapi = conf.split("location = /openapi.json", 1)[1].split("}", 1)[0]
    assert "no-cache" in block_static
    assert "no-cache" in block_openapi
