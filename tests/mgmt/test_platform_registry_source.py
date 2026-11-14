"""平台目录读源：对象模型（grape_t_repo_platforms + caps）为权威，平台卡兜底。

用户质询（2026-11-14）：「查询平台仓库是不是不应该查询这个接口？我们不是有新的
模型对象了吗」。`/settings/vcs/platforms` 原先直接遍历平台卡（configs 里的
repo-platforms.json），而 D109 已把平台提升为一等对象：方言与凭据能力（含
pat_create_url）都在 repo_platforms 表（原 repo_credential_caps 表已合表删除）。
卡是导入源，不是运行时
权威——否则「系统支持哪些平台」取决于部署目录里有没有那张卡。

本锁：
  1. registry 表优先；表=None（未 migrated）或读失败 → 回落卡，表返回空列表 = 答案
     （全部 disabled，不回落），两者都挂 → 空列表；
  2. settings 的平台目录接口与 auth 的关系推导共用同一读口（不再各自遍历卡）；
  3. 响应字段语义不变（provider/name/host/dialect/brand_color/pat_create_url/
     pr_web_path），前端不需要改。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REG = ROOT / "src/domains/platform/registry.py"
SETTINGS = ROOT / "src/domains/mgmt/routes/settings.py"
AUTH = ROOT / "src/domains/mgmt/routes/auth.py"


def test_table_first_then_cards_then_empty(monkeypatch):
    from src.domains.platform import registry

    table_row = [{"provider": "gitcode", "name": "GitCode", "host": "gitcode.com",
                  "dialect": "gitcode", "brand_color": "#2a6ae0",
                  "pat_create_url": "https://from-table/x"}]
    card_row = [{"provider": "card_only", "name": "Card", "host": "card.example",
                 "dialect": "github", "brand_color": "",
                 "pat_create_url": "https://from-card/x"}]

    monkeypatch.setattr(registry, "_descriptors_from_tables", lambda: table_row)
    monkeypatch.setattr(registry, "_descriptors_from_cards", lambda: card_row)
    assert registry.platform_descriptors() == table_row, "表有行时必须用表"
    assert registry.supported_platforms() == ["gitcode"]

    monkeypatch.setattr(registry, "_descriptors_from_tables", lambda: None)
    assert registry.platform_descriptors() == card_row, "表未就绪必须回落卡"

    # r4 裁定（2026-11-15）：空列表是**答案**（全部 disabled），只有 None（表未就绪）
    # 才回落卡 —— 否则「全平台禁用」会被卡悄悄撤销。
    monkeypatch.setattr(registry, "_descriptors_from_tables", lambda: [])
    assert registry.platform_descriptors() == [], "表有结果（哪怕是空）即权威，不回落卡"

    def boom():
        raise RuntimeError("table down")

    monkeypatch.setattr(registry, "_descriptors_from_tables", boom)
    assert registry.platform_descriptors() == card_row, "表读失败 fail-open 到卡"

    monkeypatch.setattr(registry, "_descriptors_from_cards", boom)
    assert registry.platform_descriptors() == [], "两边都挂 → 空列表，不抛"


def test_settings_catalog_uses_registry_not_cards():
    src = SETTINGS.read_text(encoding="utf-8")
    i = src.index("def list_vcs_platforms_public(")
    seg = src[i:src.index("_pr_web_path_of(provider)", i)]
    assert "from src.domains.platform.registry import platform_descriptors" in seg
    assert "for row in platform_descriptors():" in seg
    assert "list_vcs_platforms()" not in seg, "平台目录不得再遍历平台卡"
    # 字段语义保持：前端消费的键一个都不少
    for key in ("provider", "name", "host", "dialect", "pat_create_url", "brand_color", "pr_web_path"):
        assert f'"{key}"' in seg, f"响应缺字段 {key}"


def test_relation_derivation_uses_same_registry():
    src = AUTH.read_text(encoding="utf-8")
    i = src.index("def _pat_platforms(")
    seg = src[i:src.index("def _merge_pat_platforms(")]
    assert "from src.domains.platform.registry import supported_platforms" in seg
    assert "for p in supported_platforms():" in seg
    assert "list_vcs_platforms" not in seg, "关系推导不得绕过对象模型"


def test_registry_falls_back_on_unmigrated(monkeypatch):
    """migrated 前不允许纯表读（§16 影响 #1 的缓解）。"""
    from src.domains.platform import importer, registry

    monkeypatch.setattr(importer, "is_table_backed", lambda: False)
    assert registry._descriptors_from_tables() is None
    src = REG.read_text(encoding="utf-8")
    assert re.search(r"if not is_table_backed\(\):\s*\n\s*return None", src)