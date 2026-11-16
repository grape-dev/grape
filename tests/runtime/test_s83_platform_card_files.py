"""S83 用例（D59，先测后改）：平台卡文件外置——每平台一个配置文件 =
能力聚合点（吸收目录包"聚合"优点，保持卡片方案的配置接入语义）。

优先级链：meta（admin 运行时覆盖）> configs/platforms/*.json（内置默认
载体）> _DEFAULT_CARDS_RAW（代码兜底，目录缺失时才用）。
守卫：文件键集合 ⊆ PlatformCard dataclass 字段（未知键整卡拒绝、error
响亮——S70 漏字段事故的结构性免疫）。
"""
import json
import os

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import (PlatformCard, PlatformCardError,
                                   _DEFAULT_CARDS_RAW, builtin_cards_from_dir,
                                   invalidate_platform_card_cache)
import src.domains.runtime.vcs.platform_card as pc


@pytest.fixture(autouse=True)
def _restore_platform_card_cache():
    """隔离守卫：本文件多个用例改 _CONFIGS_DIR 后主动 _load_cards() 重建全局
    缓存，monkeypatch 只还原模块属性、不还原缓存——teardown 必须 invalidate，
    否则同进程后续用例拿到"目录缺失兜底卡"集合（gitee 缺失 → get_platform_card
    返回 None），表现为跨文件组合跑红、单跑绿。"""
    yield
    pc.invalidate_platform_card_cache()


def test_s83_builtin_files_load_parity():
    """configs/platforms/ 四卡加载与代码内置默认逐字段等价（gitlab 的
    download_base 等），gitee 文件卡含能力位。"""
    cards = builtin_cards_from_dir(pc._CONFIGS_DIR)
    assert {"gitcode", "github", "gitlab", "gitee"} <= set(cards)
    for provider, legacy_raw in _DEFAULT_CARDS_RAW.items():
        m = cards[provider]
        for k, v in legacy_raw.items():
            assert getattr(m, k) == v, f"{provider}.{k}: {getattr(m, k)!r} != {v!r}"
    assert cards["gitee"].issue_write_form == "owner_param"
    assert cards["gitee"].branch_create_form == "form"


def test_s83_precedence_meta_over_file(monkeypatch):
    """meta（admin 覆盖）> 文件：meta 里的 gitee 卡覆盖文件默认。

    语义：meta 覆盖=整卡替换（admin 面经 S70 全字段持久化，天然完整）；
    部分覆盖（字段级 merge）语义不提供——D59 登记。

    固定「D109 表未就绪」态：表就绪时读路径是 table-first，meta 覆盖不再参与，
    本用例测的是回落语义，不能依赖跑测试的环境里表里有没有行。
    """
    from src.domains.platform import importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: False, raising=False)
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": {"name": "GiteeX", "dialect": "github",
                                   "host": "gitee.com",
                                   "webhook_path": "/webhook/gitee",
                                   "event_header": "X-Gitee-Event",
                                   "api_base": "https://gitee.com/api/v5",
                                   "api_auth": "query_token",
                                   "webhook_verify": "token_compare",
                                   "clone_auth": "basic_password",
                                   "issue_write_form": "owner_param",
                                   "branch_create_form": "form"}}),))
        db.commit()
    invalidate_platform_card_cache()
    try:
        pc._load_cards()
        assert pc._CACHE["gitee"].name == "GiteeX"
        # 覆盖只到被覆盖字段，其余字段仍来自文件（issue_write_form 保留）
        assert pc._CACHE["gitee"].issue_write_form == "owner_param"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()


def test_s83_unknown_key_rejects_card_loudly(tmp_path):
    """文件含未知键（Builder 不认识的字段）→ 整卡拒绝（响亮，非静默丢键）。"""
    bad = dict(json.load(open(os.path.join(pc._CONFIGS_DIR, "gitee.json"))))
    bad["issue_write_form_typo"] = "owner_param"
    (tmp_path / "gitee.json").write_text(json.dumps(bad), encoding="utf-8")
    cards = builtin_cards_from_dir(str(tmp_path))
    assert "gitee" not in cards, "未知键应整卡拒绝"


def test_s83_file_keys_subset_of_dataclass():
    """守卫：repo 内全部平台配置文件的键 ⊆ PlatformCard dataclass 字段。

    例外：下划线前缀键是文档键（JSON 无注释，用 _readme/_fields/_note 承载说明），
    加载器会剥离，故不参与"未知键"判定。
    """
    import dataclasses as _dc
    valid = {f.name for f in __import__("dataclasses").fields(PlatformCard)}
    configs_dir = pc._CONFIGS_DIR
    for fn in os.listdir(configs_dir):
        if not fn.endswith(".json"):
            continue
        raw = json.load(open(os.path.join(configs_dir, fn), encoding="utf-8"))
        unknown = {k for k in raw if not str(k).startswith("_")} - valid
        assert not unknown, f"{fn} 未知键: {unknown}"


def test_s83_fallback_when_dir_missing(tmp_path, monkeypatch):
    """目录缺失 → 回退 _DEFAULT_CARDS_RAW（代码兜底语义保持）。

    同上：固定表未就绪态，否则 D109 表一旦有行，读路径 table-first 会盖掉代码兜底。
    """
    from src.domains.platform import importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: False, raising=False)
    monkeypatch.setattr(pc, "_CONFIGS_DIR", str(tmp_path / "nonexistent"))
    pc._load_cards()
    assert {"gitcode", "github", "gitlab"} <= set(pc._CACHE)
