"""登录渠道 / 平台卡展示层契约（D110 §14）：品牌图标与命名。

三类回归都在这里锁住，且都来自真实故障/用户裁定，不是形式守卫：

1. 图标资产必须能被浏览器当**独立文件**渲染——`oauth-buttons.js` 里的图标是内联 SVG，
   缺 `xmlns` 在 HTML 里无碍，一旦拷成 .svg 文件由 `<img src>` 加载就是破图（2026-11-15 实锤）。
2. 卡片名称必须走语言包（`provider_name.*`），配置/meta 里的 `name` 只作兜底；
   详情页头曾直接用 `current.name`，中文名会漏进英文界面。
3. 登录渠道页不提供"渠道↔平台配对"设置（用户 2026-11-15 拍板：口径讲不清就不显示、
   也不要用户设置）；管理面 vue 不得再出现 channel-repo-links 写入调用。
"""
import re
import xml.dom.minidom
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "src/api/frontend/shell"
VIEWS = ROOT / "src/api/frontend/app/src/views"
SVG_DIR = SHELL / "img"

CHANNEL_VIEW = VIEWS / "AdminChannels.vue"
PLATFORM_VIEW = VIEWS / "AdminPlatforms.vue"

_SVG_NS = "http://www.w3.org/2000/svg"


def _logo_known(view: Path) -> set[str]:
    """从视图源码里取出 `const LOGO_KNOWN = new Set([...])` 的 provider 清单。"""
    text = view.read_text(encoding="utf-8")
    m = re.search(r"const LOGO_KNOWN = new Set\(\[(.*?)\]\)", text, re.S)
    assert m, f"{view.name} 找不到 LOGO_KNOWN 集合（图标契约被改形？）"
    return {p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()}


@pytest.mark.parametrize("view", [CHANNEL_VIEW, PLATFORM_VIEW], ids=["channels", "platforms"])
def test_every_logo_provider_has_renderable_svg(view):
    """LOGO_KNOWN 里的每个 provider 都要有能独立渲染的 svg（xmlns + viewBox）。"""
    providers = _logo_known(view)
    assert providers, "LOGO_KNOWN 为空"
    for provider in sorted(providers):
        path = SVG_DIR / f"{provider}.svg"
        assert path.exists(), f"{provider} 在 LOGO_KNOWN 里但没有 {path.name}（会渲染成破图）"
        root = xml.dom.minidom.parse(str(path)).documentElement
        assert root is not None, f"{path.name} 解析后无根节点"
        assert root.tagName == "svg", f"{path.name} 根节点不是 svg"
        # 缺 xmlns 时浏览器拒绝把文件当图片解码 = 破图占位符
        assert root.getAttribute("xmlns") == _SVG_NS, (
            f"{path.name} 缺 xmlns（内联 SVG 拷成文件必踩）")
        assert root.getAttribute("viewBox"), f"{path.name} 缺 viewBox"


def test_icon_urls_carry_cache_version():
    """图标改过内容必须带 ?v=，否则浏览器用旧缓存（破图版会被一直复用）。"""
    for view in (CHANNEL_VIEW, PLATFORM_VIEW):
        text = view.read_text(encoding="utf-8")
        assert re.search(r"/static/img/\$\{provider\}\.svg\?v=\d+", text), (
            f"{view.name} 的图标 URL 没带版本查询串")


def test_channel_detail_header_uses_localized_name():
    """详情页头不得直接用配置原名（current.name），否则中文名漏进英文界面。"""
    text = CHANNEL_VIEW.read_text(encoding="utf-8")
    header = re.search(r'<div class="ap-detail-hd">(.*?)</div>', text, re.S)
    assert header, "找不到详情页头结构"
    block = header.group(1)
    assert "channelName(current)" in block, "详情页头未走语言包"
    assert "current.name" not in block, "详情页头仍在用配置原名（绕过 i18n）"


def test_channel_page_offers_no_pairing_settings():
    """用户裁定：渠道页不提供"渠道↔平台配对"设置（关系只来自配置/同域隐式）。

    反向守卫——写入端点一旦回潮（谁再挂个"保存/删除配对"按钮）这里就红。
    """
    for view in (CHANNEL_VIEW, PLATFORM_VIEW):
        text = view.read_text(encoding="utf-8")
        assert "channel-repo-links" not in text, (
            f"{view.name} 又出现了 channel-repo-links 写入调用（配对设置已裁定移除）")
    channel_text = CHANNEL_VIEW.read_text(encoding="utf-8")
    for gone in ("page.can_drive", "page.save_link", "page.pick_platform"):
        assert gone not in channel_text, f"配对分节的文案键 {gone} 仍在模板里"


def test_provider_names_i18n_covers_every_configured_channel():
    """每个登录渠道都要在 zh/en 语言包里有专有名词（华为=华为/Huawei，不是"华为云"）。"""
    import json

    cfg = json.loads((ROOT / "configs/oauth-providers.json").read_text(encoding="utf-8"))
    providers = [k for k in cfg if not k.startswith("_")]
    for lang in ("zh", "en"):
        pack = json.loads((SHELL / f"locales/{lang}/admin.json").read_text(encoding="utf-8"))
        names = pack.get("provider_name") or {}
        missing = [p for p in providers if not names.get(p)]
        assert not missing, f"{lang} 语言包缺 provider_name：{missing}"
    zh = json.loads((SHELL / "locales/zh/admin.json").read_text(encoding="utf-8"))["provider_name"]
    en = json.loads((SHELL / "locales/en/admin.json").read_text(encoding="utf-8"))["provider_name"]
    assert zh["huawei"] == "华为", zh["huawei"]
    assert en["huawei"] == "Huawei", en["huawei"]