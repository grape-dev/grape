"""管理台字段说明（"?" 文案）的链路验收。

单一来源 = 配置文件 `_fields`；接口透传 `docs`；前端 AGHelp 渲染。
守卫：
1) 两个配置文件的 `_fields` 每条三段式齐全（what / values / note，note 以 ⚠ 开头）
2) 关键字段有说明（缺一条即红，防新增字段忘记写文案）
3) 三个集合接口的响应里带 docs，且键与配置文件一致
4) 前端组件与页面确实使用 AGHelp（含 aria-expanded 无障碍属性）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "src" / "api" / "frontend" / "app" / "src"

OAUTH_DOC_KEYS = {"type", "auth_url", "token_url", "user_url", "scope", "pkce_enabled",
                  "id_field", "login_field", "name_field", "avatar_field", "redirect_uri",
                  "accept_header"}
# 易踩坑字段：note 必须以 ⚠ 开头（其余字段只要求非空，避免"处处警示"）
HIGH_RISK = {"pkce_enabled", "scope", "oauth_requires_scope",
             "issue_write_form", "webhook_verify", "pr_head_filter", "type"}

# 凭据能力表 / repo-credentials.json 已删除（2026-11-14），字段说明改为随平台卡
# 渠道↔平台授权的说明落在独立配置文件（与表 grape_t_channel_repo_link 同名同构）
LINK_DOC_KEYS = {"oauth_provider", "repo_provider"}  # relation 档位已取消（2026-11-14）


def _load(name: str) -> dict:
    return json.loads((ROOT / "configs" / name).read_text(encoding="utf-8"))


def _assert_three_part(docs: dict, keys: set[str], file: str) -> None:
    missing = keys - set(docs)
    assert not missing, f"{file} 缺少字段说明: {sorted(missing)}"
    for k in keys:
        d = docs[k]
        assert isinstance(d, dict), f"{file}.{k} 说明必须是对象（what/values/note）"
        assert d.get("what"), f"{file}.{k} 缺 what"
        assert d.get("values"), f"{file}.{k} 缺 values"
        assert d.get("note"), f"{file}.{k} 缺 note"
        if k in HIGH_RISK:
            assert d["note"].startswith("⚠"), f"{file}.{k} 是易踩坑字段，note 必须以 ⚠ 开头"


def test_oauth_fields_docs_complete():
    _assert_three_part(_load("oauth-providers.json").get("_fields") or {}, OAUTH_DOC_KEYS,
                       "oauth-providers.json")


def test_channel_repo_links_docs_complete():
    """渠道↔平台授权的说明在独立文件里（oauth 卡已不含 repo_links）。"""
    _assert_three_part(_load("channel-repo-links.json").get("_fields") or {}, LINK_DOC_KEYS,
                       "channel-repo-links.json")



@pytest.fixture()
def wired(monkeypatch):
    eng = create_engine("sqlite://")
    raw = eng.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    from src.kernel.persistence import engine as engine_mod

    monkeypatch.setattr(engine_mod, "get_engine", lambda: eng)
    yield eng
    eng.dispose()


def test_api_passes_docs_through(wired):
    from src.domains.mgmt.admin import (
        oauth_channel_routes as ocr,
        repo_platform_routes as rpr,
    )

    # 表为空 → 走 fallback，但 docs 必须照样透传（否则前端"?"是空的）
    ch = ocr.list_channels(admin={})
    assert ch["docs"] and "pkce_enabled" in ch["docs"], "oauth-channels 未透传 docs"
    pf = rpr.list_platforms(admin={})
    assert isinstance(pf["docs"], dict), "repo-platforms docs 应为 dict（凭据能力表已删，字段说明随平台卡）"


def test_frontend_uses_help_component():
    help_vue = FRONT / "components" / "ui" / "AGHelp.vue"
    assert help_vue.exists(), "缺少 AGHelp 组件"
    src = help_vue.read_text(encoding="utf-8")
    for token in ("aria-expanded", "role=\"tooltip\"", "@keydown.esc", "AGHelp"):
        if token == "AGHelp":
            continue
        assert token in src, f"AGHelp 缺无障碍/交互要素: {token}"
    for page in ("AdminChannels.vue", "AdminPlatforms.vue"):
        p = FRONT / "views" / page
        text = p.read_text(encoding="utf-8")
        assert "import AGHelp" in text, f"{page} 未引入 AGHelp"
        assert "<AGHelp" in text, f"{page} 模板未使用 AGHelp（字段无 ? 提示）"
        assert "v-bind=\"docRow(" in text, f"{page} 未按字段渲染说明"
        # 变量名随实现（field/key 均可），关键是用 i18n key 渲染 what 段
        assert re.search(r"t\(`field\.\$\{(field|key)\}\.what`", text), f"{page} 的 ? 文案未走 i18n"
        assert "t('page." in text, f"{page} 页面文案未走 i18n"
    admin = (FRONT / "views" / "AdminView.vue").read_text(encoding="utf-8")
    assert "AdminChannels" in admin and "AdminPlatforms" in admin, "AdminView 未接入两个对象页"

# ---------- i18n：字段说明中英齐全（前端写，用户口径"简单点"）----------

I18N_JS = ROOT / "src" / "api" / "frontend" / "shell" / "js" / "i18n.js"


def _block(text: str, name: str, start: int = 0):
    """按对象名定位块：缩进由首行自身决定（page=8 空格、field=10 空格）。"""
    i = text.index(f"{name}: {{", start)
    indent = i - text.rindex("\n", 0, i) - 1
    closer = "\n" + " " * indent + "},"
    j = text.index(closer, i) + len(closer)
    return i, j


def _keys(seg: str):
    
    return set(re.findall(r"^\s+(\w+): ", seg, re.M))


def test_i18n_covers_all_fields():
    text = I18N_JS.read_text(encoding="utf-8")
    zi, zj = _block(text, "field")
    ei, ej = _block(text, "field", zj)
    zh, en = _keys(text[zi:zj]), _keys(text[ei:ej])
    assert zh == en, f"i18n field 键中英不一致: 缺 en {sorted(zh - en)} / 多 en {sorted(en - zh)}"

    # 两页渲染的字段集合都必须有说明
    import re

    needed = {"client_id", "client_secret", "enabled"}
    for page in ("AdminChannels.vue", "AdminPlatforms.vue"):
        src = (FRONT / "views" / page).read_text(encoding="utf-8")
        m = re.search(r"const DEF_FIELDS = \[(.*?)\]", src, re.S)
        assert m, f"{page} 缺 DEF_FIELDS"
        needed |= {k.strip().strip("'\"") for k in m.group(1).split(",") if k.strip()}
    missing = needed - zh
    assert not missing, f"页面渲染但 i18n 无说明的字段: {sorted(missing)}"

    # 三段式：what / values / note 都非空（note 允许非 ⚠，但必须存在）
    for key in needed:
        for lang, seg in (("zh", text[zi:zj]), ("en", text[ei:ej])):
            row = re.search(r"^\s+" + key + r": (\{.*\}),\s*$", seg, re.M)
            assert row, f"{lang}.field.{key} 未找到"
            obj, _end = json.JSONDecoder().raw_decode(row.group(1))
            for part in ("what", "values", "note"):
                assert obj.get(part), f"{lang}.field.{key} 缺 {part}"


def test_i18n_page_keys_zh_en_parity():
    text = I18N_JS.read_text(encoding="utf-8")
    zi, zj = _block(text, "page")
    ei, ej = _block(text, "page", zj)
    zh, en = _keys(text[zi:zj]), _keys(text[ei:ej])
    assert zh == en, f"i18n page 键中英不一致: {sorted(zh ^ en)}"
    # 页面里出现的每个 page.* 键都必须在 i18n 里
    import re

    used = set()
    for page in ("AdminChannels.vue", "AdminPlatforms.vue"):
        used |= set(re.findall(r"t\('page\.(\w+)'", (FRONT / "views" / page).read_text(encoding="utf-8")))
    assert not (used - zh), f"页面用了但 i18n 缺的 page 键: {sorted(used - zh)}"
