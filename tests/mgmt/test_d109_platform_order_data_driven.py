"""渠道展示顺序与托管兜底顺序的「去字面量」守卫（2026-11-14 第六批）。

两处曾经写死的顺序：
  1. 登录渠道展示序 `_CATALOG_ORDER = {"gitcode":0,"github":1,...}` —— 加渠道必改代码；
     现改为 `_ui_order`（configs/oauth-providers.json 可选）→ 配置键顺序。
  2. 托管兜底序 `_DEFAULT_TOKEN_PROVIDER_ORDER = ("github","gitcode")` —— 二元枚举，
     且默认把无线索请求打到 github（gitee 仓库托管 404 的根因之一）；现改为
     `hosting_providers()` 卡目录派生。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_no_literal_channel_order_table():
    src = (ROOT / "src/domains/mgmt/auth/providers.py").read_text(encoding="utf-8")
    assert "_CATALOG_ORDER" not in src, "登录渠道展示序又回到字面表了（应为 _ui_order / 配置键序）"


def test_ui_order_drives_channel_display_order(monkeypatch, tmp_path):
    """配置文件里的 _ui_order 决定展示序；未列出的渠道排最后按名。"""
    from src.domains.mgmt.auth import providers as P

    cfg = tmp_path / "oauth-providers.json"
    cfg.write_text(json.dumps({
        "_ui_order": ["gitee", "gitcode"],
        "gitcode": {"name": "GitCode"},
        "gitee": {"name": "Gitee"},
        "github": {"name": "GitHub"},
    }), encoding="utf-8")
    monkeypatch.setattr(P, "_AUTH_CHANNELS_FILE", str(cfg))
    monkeypatch.setattr(P, "list_providers", lambda: ["github", "gitcode", "gitee"])

    idx = P._catalog_order_index()
    assert idx["gitee"] < idx["gitcode"], "_ui_order 未生效"
    assert idx["github"] > idx["gitcode"], "未列出的渠道应排在 _ui_order 之后"


def test_host_fallback_order_is_catalog_derived():
    from src.domains.mgmt.routes import repos as R
    from src.domains.platform.capabilities import hosting_providers

    src = (ROOT / "src/domains/mgmt/routes/repos.py").read_text(encoding="utf-8")
    assert "_DEFAULT_TOKEN_PROVIDER_ORDER" not in src, "兜底顺序又回到写死常量了"

    order = R._default_token_provider_order()
    assert order == tuple(sorted(hosting_providers())), f"兜底顺序应为卡目录派生，实际 {order}"
    # gitee/gitlab 实装托管全流程（HOSTING_FULL_FLOW）→ 必须在兜底候选里
    for p in ("gitcode", "github", "gitee"):
        assert p in order, f"{p} 实装托管却不在兜底顺序里"

def test_catalog_order_is_cached_not_reparsed(monkeypatch):
    """P2-7：展示序索引要缓存（键=文件 mtime+size），不能每请求 open+parse。

    证明方式：首次调用后把 json.load 换成抛错，第二次仍能返回结果 = 命中缓存。
    """
    from src.domains.mgmt.auth import providers as P

    P._order_cache_key = None
    P._order_cache_idx = None
    first = P._catalog_order_index()
    assert first, "首次调用应返回非空索引"

    def _boom(*a, **k):
        raise AssertionError("缓存未命中：再次解析了 oauth-providers.json")

    monkeypatch.setattr(P.json, "load", _boom)
    second = P._catalog_order_index()
    assert second == first, "第二次调用应命中缓存且结果一致"
