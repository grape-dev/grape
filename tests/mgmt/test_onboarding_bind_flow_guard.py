"""引导页 / 绑定弹层的 i18n 键解析守卫（2026-11-14 实锤：绑定成功后显示原始 key）。

现象：BindDialog 的 `t()` 只查 `app.account.<key>`，而 `bind_success` 只存在于
`app.login.<key>` → 取不到就回吐 key 本身，界面显示 "bind_success" 而不是
「GitCode 绑定成功」/「GitCode linked」。

守卫：
  1. 组件里用到的每个 key，在 zh/en 两个语言包里必须至少被 `app.account.` 或
     `app.login.` 其中之一命中；
  2. 语言切换必须能触发重渲染（组件内注册 `grape:langchange` 监听），否则切语言后
     卡片/弹层文案停在旧语言；
  3. 绑定成功后必须走 goToRepos 出口（摘 ob-mode + 改路由），只调 loadRepos 会留在引导页。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = ROOT / "src/api/frontend/app/src"
SHELL = ROOT / "src/api/frontend/shell"
KEY_RE = re.compile(r"\bt\(\s*'([a-zA-Z0-9_]+)'")


def _packs(lang: str) -> dict:
    return json.loads((SHELL / f"locales/{lang}/app.json").read_text(encoding="utf-8"))


def _component_keys(path: Path) -> set[str]:
    return set(KEY_RE.findall(path.read_text(encoding="utf-8")))


def test_bind_dialog_keys_resolve_in_both_languages():
    keys = _component_keys(APP / "components/BindDialog.vue")
    assert keys, "未解析到 BindDialog 的 i18n key"
    for lang in ("zh", "en"):
        pack = _packs(lang)
        account = pack.get("account", {})
        login = pack.get("login", {})
        missing = [k for k in keys if k not in account and k not in login]
        assert not missing, f"{lang} 语言包缺 BindDialog 的键（会显示原始 key）：{missing}"


def test_onboarding_keys_resolve_in_both_languages():
    """引导页用 app.login.* / repos.* / organizations.*，按模块前缀逐个校验。"""
    src = (APP / "views/OnboardingView.vue").read_text(encoding="utf-8")
    keys = set(re.findall(r"t(?:Global)?\(\s*'(app\.[a-zA-Z0-9_.]+)'", src))
    assert keys, "未解析到 OnboardingView 的 i18n key"

    def dig(pack: dict, path: str):
        cur = pack
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    for lang in ("zh", "en"):
        pack = _packs(lang)
        missing = [k for k in keys if dig(pack, k.split(".", 1)[1]) is None]
        assert not missing, f"{lang} 语言包缺引导页的键：{missing}"


def test_bind_views_react_to_language_switch():
    for rel in ("components/BindDialog.vue", "views/OnboardingView.vue"):
        src = (APP / rel).read_text(encoding="utf-8")
        assert "grape:langchange" in src, f"{rel} 未监听 grape:langchange，切语言后文案不会重渲染"


def test_onboarding_leaves_to_repo_page_after_bind():
    src = (APP / "views/OnboardingView.vue").read_text(encoding="utf-8")
    assert "goToRepos" in src, "绑定成功必须走 goToRepos（摘 ob-mode + 路由到项目页）"
    # 仅调 loadRepos 会因 ob-mode 未摘除而停在引导页
    assert "setTimeout(function () {\n        if (G.app && typeof G.app.loadRepos === 'function') G.app.loadRepos()" not in src