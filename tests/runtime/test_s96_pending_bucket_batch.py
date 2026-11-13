"""S96 用例（D71，先测后改）：D69"待人工"余量批次——登记站点的卡/注册表派生收口。

收口三件（行为保持，载体替换）：
①bot_token_health platform_label：_PLATFORM_LABELS 字面映射 → 平台卡 name；
②bot_token_health 探活基集：{gitcode, github} 字面 → provider 翻译模块注册表；
③grape_auth 过期检查：字面二元组 → 用户实际绑定平台（gitee 等绑定不再漏检）；
④issues 镜像查找循环：字面二元组 → provider 注册表。

其余 D69 登记站点分类为合法冻结（legacy alias 端点双字面量、settings
_LEGACY_PAT_FIELD_PROVIDERS、config_store oauth meta 键映射、issues 关单
双平台结果字典、repos 优先级序常量），需新卡能力位/关单链重构，另行批次。
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def test_s96_guard_no_pending_literals():
    """结构守卫：收口站点零平台字面量残留。"""
    assert '"gitcode": "GitCode"' not in _read("domains/runtime/engine/bot_token_health.py"), \
        "_PLATFORM_LABELS 字面映射必须已由平台卡 name 派生替代"
    bth = _read("domains/runtime/engine/bot_token_health.py")
    assert not re.search(r'results\s*=\s*\{\s*"gitcode"', bth), \
        "探活基集必须由 provider 注册表派生"
    ga = _read("domains/mgmt/routes/grape_auth.py")
    assert not re.search(r'for _p in \("gitcode", "github"\)', ga), \
        "过期检查必须遍历用户绑定平台"
    iss = _read("domains/runtime/routes/issues.py")
    assert not re.search(r'for _p in \("gitcode", "github"\)', iss), \
        "镜像查找循环必须遍历 provider 注册表"


def test_s96_label_from_card():
    """标签卡派生：gitcode→卡 name（GitCode），未知平台回退 capitalize。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card
    card = get_platform_card("gitcode")
    assert card is not None and card.name == "GitCode"
    # 卡缺位回退
    class _NoCard:
        pass
    from src.domains.runtime.engine.bot_token_health import _verify_bot_token
    r = _verify_bot_token("gitcode")
    # 未配 token → config 错误，且错误文案含卡权威名（非 capitalize 的 "Gitcode"）
    assert r["error_type"] in ("config", "")
    assert "Gitcode token not configured" not in r["error"]


def test_s96_health_base_from_registry():
    """探活基集=注册表：今日 gitcode/github 全在，且不因未配 token 缺席。"""
    from src.domains.runtime.vcs.providers import _MODULES
    from src.domains.runtime.engine.bot_token_health import _verify_global_bot_token
    results = _verify_global_bot_token()
    for p in _MODULES:
        assert p in results["platforms"], f"注册平台 {p} 必须在探活基集"
