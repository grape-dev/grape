"""S40-S42 + S43/S44 场景用例：代理能力位上翻进卡 + 卡详情页 Apple 式可用性。

S40-S42（_pflag 三态解析序）：
  ① 卡能力键显式声明（含 False）→ 优先返回卡值（卡纠正模块默认）；
  ② 卡键缺席（None，存量 json 卡全部如此）→ 回退 provider 模块开关，行为零变化；
  ③ 卡有键且模块也有 → 卡胜出。

S43/S44（详情页可用性）：
  - field_docs 词条 zh/en 键集一致且覆盖全部代理能力键（配置指导内置）；
  - admin.js 渲染层含高级折叠（details/summary）与 f-doc 字段指导；
  - admin.html 双域说明在位。
"""
import json
import os
import re

import pytest

from src.domains.runtime.vcs.platform_card import (
    build_platform_card, get_platform_card, invalidate_platform_card_cache)


@pytest.fixture()
def _card_env():
    """meta 卡覆盖 + 缓存失效 + 恢复（复用 s58 gitee_card 模式）。"""
    from src.kernel.session_manager import db, db_lock
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def _set_meta_card(provider: str, card: dict):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({provider: card}),))
        db.commit()
    invalidate_platform_card_cache()


class TestPflagCardPrecedence:
    """S40-S42：_pflag 卡优先→模块回退三态。"""

    def test_s40_card_override_beats_module(self, _card_env):
        """① 卡显式声明（True 覆盖模块 False / False 覆盖模块 True）。"""
        from src.api.git_repo_api import _pflag
        # github 模块 MILESTONE_VERIFY_READBACK=True；meta 卡覆盖为 False
        _set_meta_card("github", {"name": "GitHub", "host": "github.com",
                                   "milestone_verify_readback": False})
        assert _pflag("github", "MILESTONE_VERIFY_READBACK") is False
        # gitcode 模块无 closing_refs（False）；卡声明 True
        _set_meta_card("gitcode", {"name": "GitCode", "host": "gitcode.com",
                                   "closing_refs_auto_close_default": True})
        assert _pflag("gitcode", "CLOSING_REFS_AUTO_CLOSE_DEFAULT") is True

    def test_s40_card_absent_falls_back_to_module(self, _card_env):
        """② 卡键缺席（None）→ 模块开关，存量行为零变化。

        存量 configs/platforms/*.json 均未声明代理能力键——内置卡经 Builder
        构建后这些键必为 None（_opt_bool 键缺席语义）。
        """
        from src.api.git_repo_api import _pflag
        # gitee 模块 ISSUE_UPDATE_OWNER_PATH=True，gitee.json 未声明卡键
        assert _pflag("gitee", "ISSUE_UPDATE_OWNER_PATH") is True
        # github 模块 LABELS_ARRAY_FORM=True
        assert _pflag("github", "LABELS_ARRAY_FORM") is True
        # github 模块 MILESTONE_DUE_ON_REQUIRED 未声明（False 默认）
        assert _pflag("github", "MILESTONE_DUE_ON_REQUIRED") is False
        # gitcode 模块 MILESTONE_DUE_DATE_ONLY=True
        assert _pflag("gitcode", "MILESTONE_DUE_DATE_ONLY") is True

    def test_s40_builtin_cards_declare_no_capability_keys(self):
        """③ 内置 json 卡不声明代理能力键（键值必为 None）——存量零回归的形态锁。"""
        for p in ("gitcode", "github", "gitee", "gitlab"):
            card = get_platform_card(p)
            assert card is not None, p
            for key in ("milestone_verify_readback", "closing_refs_auto_close_default",
                        "issue_update_owner_path", "labels_array_form"):
                assert getattr(card, key) is None, f"{p}.{key} 应为 None（键缺席）"

    def test_s40_string_forms_coerced(self, _card_env):
        """④ 字符串形态归一：'true'/'1'→True，空串→None（模块回退）。"""
        _set_meta_card("gitlab", {"name": "GitLab", "host": "gitlab.com",
                                  "labels_array_form": "true"})
        card = get_platform_card("gitlab")
        assert card.labels_array_form is True
        _set_meta_card("gitlab", {"name": "GitLab", "host": "gitlab.com",
                                  "labels_array_form": ""})
        card = get_platform_card("gitlab")
        assert card.labels_array_form is None


class TestFieldDocsLocale:
    """S43：field_docs 词条双语一致 + 覆盖代理能力键（配置指导内置）。"""

    _CAP_KEYS = ("milestone_verify_readback", "milestone_id_resolve_to_number",
                 "milestone_due_on_required", "milestone_due_date_only",
                 "milestone_due_time_only", "closing_refs_auto_close_default",
                 "labels_array_form", "labels_color_prefix_hash",
                 "issue_update_owner_path", "issue_body_repo_bare",
                 "pr_comment_id_hex", "legacy_repo_token_getter",
                 "token_storage_field", "webhook_token_field",
                 "owner_identity_token_fallback", "git_tree_family")

    def test_s43_field_docs_zh_en_key_sets_match(self):
        zh = json.load(open("src/api/static/locales/zh/admin.json"))
        en = json.load(open("src/api/static/locales/en/admin.json"))
        assert set(zh["field_docs"]) == set(en["field_docs"]), "zh/en field_docs 键集漂移"
        assert len(zh["field_docs"]) >= 40

    def test_s43_field_docs_cover_all_capability_keys(self):
        zh = json.load(open("src/api/static/locales/zh/admin.json"))["field_docs"]
        missing = [k for k in self._CAP_KEYS if k not in zh]
        assert not missing, f"代理能力键缺配置指导词条: {missing}"

    def test_s43_storage_field_doc_states_f4_semantics(self):
        zh = json.load(open("src/api/static/locales/zh/admin.json"))["field_docs"]
        assert "grape_t_repo_credentials" in zh["token_storage_field"]
        assert "grape_t_repo_credentials" in zh["webhook_token_field"]


class TestAdminUiUsability:
    """S43/S44：详情页高级折叠 + 字段指导 + 双域说明（源码形态锁，对齐 D20 模式）。"""

    @staticmethod
    def _read(p):
        return open(p, encoding="utf-8").read()

    def test_s43_render_has_advanced_fold(self):
        js = self._read("src/api/static/js/admin.js")
        assert "adv-fold" in js, "高级折叠 details 渲染缺失"
        assert "summary" in js
        # 常用键白名单在位（两级曝光）
        assert "_COMMON_CARD_KEYS" in js
        for k in ("auth_url", "bot_username", "api_base"):
            assert k in js

    def test_s43_render_uses_field_docs(self):
        js = self._read("src/api/static/js/admin.js")
        assert "admin.field_docs." in js, "字段级配置指导渲染缺失"
        assert "f-doc" in js

    def test_s44_domain_notes_present(self):
        html = self._read("src/api/static/admin.html")
        assert "domain_note_channels" in html
        assert "domain_note_platforms" in html
        zh = json.load(open("src/api/static/locales/zh/admin.json"))
        assert "domain_note_channels" in zh
        assert "domain_note_platforms" in zh

    def test_s44_admin_js_version_bumped(self):
        html = self._read("src/api/static/admin.html")
        m = re.search(r"admin\.js\?v=(\d{8}-\d+)", html)
        assert m, "admin.js 版本号缺失"
        assert m.group(1) >= "20261113-1", f"admin.js 版本号未 bump: {m.group(1)}"
