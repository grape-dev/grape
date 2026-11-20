"""`enabled` 在两个管理页的语义已经分叉，说明与映射必须跟着分。

- 登录渠道：enabled = 该渠道**是否允许登录**（关闭 → 登录入口 403 CHANNEL_DISABLED）。
- 代码托管平台：enabled = 该平台**是否在用户面显示**（关闭 → 目录/仓库列表/显式请求 403，
  内网与脚本、托管链路仍可用）。

2026-11-15 实锤：两页共用 `field.enabled` 一条说明，平台页还在说"该对象是否启用"，
与它现在的行为不符；两页 docRow 都按字段名取键，所以必须在页面侧做映射。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWS = ROOT / "src/api/frontend/app/src/views"
LOCALES = ROOT / "src/api/frontend/shell/locales"


def _pack(lang: str) -> dict:
    return json.loads((LOCALES / f"{lang}/admin.json").read_text(encoding="utf-8"))


def test_channel_and_platform_have_their_own_enabled_doc():
    for lang in ("zh", "en"):
        field = _pack(lang).get("field") or {}
        assert "enabled_channel" in field and "enabled_platform" in field, lang
        ch, pf = field["enabled_channel"]["what"], field["enabled_platform"]["what"]
        assert ch != pf, f"{lang}: 两页共用一条说明（语义已分叉）"


def test_platform_page_uses_platform_wording():
    text = (VIEWS / "AdminPlatforms.vue").read_text(encoding="utf-8")
    assert "enabled_platform" in text, "平台页 docRow 没有映射到 platform 口径"
    ch = (VIEWS / "AdminChannels.vue").read_text(encoding="utf-8")
    assert "enabled_channel" in ch, "渠道页 docRow 没有映射到 channel 口径"


def test_platform_wording_mentions_user_side_and_internal_availability():
    zh = (_pack("zh").get("field") or {})
    what = zh["enabled_platform"]["what"]
    note = zh["enabled_platform"]["note"]
    assert "用户面" in what, what
    assert "内网" in note and "托管" in note, note
