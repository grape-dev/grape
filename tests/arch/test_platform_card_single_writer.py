"""平台卡单一写入口守卫（A 方案：表是运行时唯一真源）。

锁四件事：
1. 卡定义只能经 `RepoPlatformRepo.upsert_admin` 落表（管理面仓储是唯一调用方）；
2. 不允许再往 meta 写 `vcs_providers_config`（卡定义）——该键只允许在装配降级路径读；
3. 不允许再写 `vcs_platform_overrides`（覆盖层退役，历史数据只读）；
4. `upsert_manual` 不再被管理面保存路径调用（手建行语义保留给导入器/迁移）。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def _py_files(root: pathlib.Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_admin_repository_is_the_only_card_writer():
    hits = []
    for p in _py_files(SRC):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "upsert_admin(" in text:
            hits.append(str(p.relative_to(REPO)))
    assert hits == ["src/kernel/persistence/repositories_platform_object.py",
                    "src/domains/mgmt/admin/platform_cards/repository.py"], (
        f"upsert_admin 调用方应只有定义处 + 管理面仓储，实际: {hits}"
    )


def test_admin_save_does_not_use_upsert_manual():
    p = SRC / "domains/mgmt/admin/platform_cards/repository.py"
    text = p.read_text(encoding="utf-8")
    assert "upsert_manual(" not in text, "管理面保存必须走 upsert_admin（避免 config_owned 拦写）"


def test_no_meta_write_for_card_definition():
    """mgmt 域内禁止写卡定义/覆盖层两个 meta 键（写 = 双真源回潮）。"""
    offenders = []
    for p in _py_files(SRC / "domains" / "mgmt"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for key in ("vcs_providers_config", "vcs_platform_overrides"):
            # set_meta_values?([... (key, value) ...]) 里出现该键 → 写入
            if re.search(rf"\(\s*[\"']{key}[\"']\s*,", text):
                offenders.append(f"{p.relative_to(REPO)}: {key}")
    assert not offenders, f"mgmt 域仍有卡定义 meta 写入: {offenders}"


def test_overrides_layer_only_in_degraded_assembly():
    """覆盖层只允许出现在装配降级路径与常量定义处，不得进入写路径。"""
    allowed = {
        "src/domains/runtime/vcs/card_overrides.py",       # META_KEY 常量定义
        "src/domains/runtime/vcs/card_sources.py",          # 降级装配（表缺该平台时）
        "src/domains/runtime/vcs/platform_card.py",         # 装配入口（把降级源接上）
        "src/domains/runtime/public/__init__.py",           # 跨域门面（读 key / 算 diff）
        "src/domains/mgmt/admin/platform_cards/service.py",  # 仅计算展示 diff（不落库）
    }
    offenders = []
    for p in _py_files(SRC):
        rel = str(p.relative_to(REPO))
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "vcs_platform_overrides" in text and rel not in allowed:
            offenders.append(rel)
    assert not offenders, f"覆盖层 meta 键出现未授权位置: {offenders}"