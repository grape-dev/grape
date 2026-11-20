"""守卫：`configs/platforms/*.json` 必须能被平台对象导入器整条接受。

2026-11-20 事故（线上现象：托管仓库"不见了"，仓库页整页空，库里一行没少）：
上游把能力位 `create_repo_options` 同时写进了 `configs/platforms/*.json` 和
`src/domains/runtime/vcs/platform_card.py`，但 `RepoPlatformModel`（表模型）没有这列。
`importer._apply_fields` 遇到模型里没有的键会抛「未知配置键 …整条拒绝」
→ 平台行停在 `source='orphaned'` / `enabled=0` → `is_platform_visible()` 为假 →
`/api/repos/member` 把该平台的仓库全部剔除 → 用户看到的是"仓库没了"。

这类"配置键先于模型列落地"的漂移只在**启动导入**里报错，业务请求全 200，排查成本极高。
所以这里直接跑导入器自己的校验路径（preview，不写库），并单独钉住已知能力位落表。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def test_platform_configs_are_accepted_by_the_importer():
    """导入器 preview 必须 0 failed —— 有 failed 即整条平台配置被拒、平台会被判 orphaned。"""
    from sqlalchemy.orm import Session
    from src.kernel.persistence.engine import get_engine
    from src.domains.platform import importer

    with Session(get_engine()) as s:
        report = importer.scan_and_apply(s, apply=False)
    assert report.failed == [], (
        "平台配置被整条拒绝（该平台会 orphaned/enabled=0，用户面仓库列表整页为空）："
        + "; ".join(report.failed)
    )
    # 三张平台卡都应在场（少一张即配置目录被改坏）
    assert {"gitcode", "github"} <= set(report.platforms and [c.provider for c in report.platforms])


@pytest.mark.parametrize(
    "column",
    ["create_repo_options", "branch_create_form", "issue_write_form", "issue_search",
     "pr_comment_endpoint", "pr_labels_via_issues", "workflows_write", "webhook_dialect"],
)
def test_card_capability_flags_are_persisted_columns(column):
    """卡构建器读的能力位必须在表模型里，否则导入器会「未知配置键」整条拒绝。"""
    from src.kernel.persistence.models_platform_object import RepoPlatformModel

    cols = {c.name for c in RepoPlatformModel.__table__.columns}
    assert column in cols, f"卡能力位 {column} 未落表（importer 会整条拒绝平台配置）"

# ── F1 回归：能力位必须贯通「表 → 跨域读 → 卡构建」整条读链路 ──────────────────
_CARD_SRC = os.path.join(ROOT, "src", "domains", "runtime", "vcs", "platform_card.py")
_MGMT_SRC = os.path.join(ROOT, "src", "domains", "mgmt", "public", "__init__.py")
# raw 键 → 表列 的历史别名（importer 侧做同名语义映射）
_RAW_ALIAS = {"owner_identity_token_fallback": "owner_fallback"}


def _raw_read_keys() -> set:
    """`build_platform_card` 实际从 raw 里读的键。"""
    import re

    src = open(_CARD_SRC, encoding="utf-8").read()
    body = src[src.index("def build_platform_card("):]
    return set(re.findall(r'raw\.get\(\s*"([a-z0-9_]+)"', body))


def _allowlists() -> tuple:
    import re

    out = []
    for path in (_CARD_SRC, _MGMT_SRC):
        src = open(path, encoding="utf-8").read()
        block = re.search(r"_PLATFORM_CARD_COLUMNS = \(([^)]*)\)", src, re.S).group(1)
        out.append({c.strip().strip('"\'') for c in block.split(",") if c.strip()})
    return tuple(out)


def _cross_domain_selects() -> tuple:
    import re

    src = open(_MGMT_SRC, encoding="utf-8").read()
    one = re.search(r'"repo_platform_cards": \((.*?)\),\n', src, re.S).group(1)
    every = re.search(r'"repo_platform_cards_all": \((.*?)\),\n', src, re.S).group(1)
    return one, every


def test_card_capabilities_reach_the_table_read_path():
    """表真源模式下卡能力位必须能读到 —— 列建了但白名单/SELECT 没带上，能力位恒 False。

    事故（PR #1878 review F1）：`create_repo_options` 写进了表与卡构建器，却没进两侧
    `_PLATFORM_CARD_COLUMNS` 与两条跨域具名 SELECT，于是 `_raws_from_table()` 整卡替换后
    该键天然缺失 → `build_platform_card` 走默认 False → 建仓的 auto_init/default_branch
    被静默丢弃。
    """
    from src.kernel.persistence.models_platform_object import RepoPlatformModel

    cols = {c.name for c in RepoPlatformModel.__table__.columns}
    required = {_RAW_ALIAS.get(k, k) for k in _raw_read_keys()} & cols
    runtime_cols, mgmt_cols = _allowlists()
    one, every = _cross_domain_selects()

    problems = []
    for col in sorted(required):
        if col not in runtime_cols:
            problems.append(f"runtime._PLATFORM_CARD_COLUMNS 缺 {col}")
        if col not in mgmt_cols:
            problems.append(f"mgmt._PLATFORM_CARD_COLUMNS 缺 {col}")
        if col not in one:
            problems.append(f"repo_platform_cards SELECT 缺 {col}")
        if col not in every:
            problems.append(f"repo_platform_cards_all SELECT 缺 {col}")
    assert not problems, "卡能力位未贯通表读链路：\n  " + "\n  ".join(problems)


def test_allowlists_stay_in_sync():
    """runtime 与 mgmt 两份白名单必须同序同集合（一方改了一方没改 = 跨域读错列）。"""
    runtime_cols, mgmt_cols = _allowlists()
    assert runtime_cols == mgmt_cols, (
        f"两份白名单不一致：runtime-only={sorted(runtime_cols - mgmt_cols)} "
        f"mgmt-only={sorted(mgmt_cols - runtime_cols)}")

def test_every_platform_config_builds_a_card_from_table_truth():
    """配置 → 导入器字段 → 卡构建必须能走通（表真源模式下这是唯一路径）。

    事故（PR #1878 复现）：`configs/platforms/gitcode.json` 没写 `branch_create_form` /
    `issue_write_form`，导入器按 '' 落库，于是表行整卡替换后 `build_platform_card` 直接抛
    `branch_create_form 必须是 form/refs_sha: ''` —— 读链路退回文件卡，能力位（含
    create_repo_options）等于白建。缺键必须显式声明，不能靠卡构建器的默认值蒙混。
    """
    from src.domains.platform import importer
    from src.domains.runtime.vcs.platform_card import build_platform_card

    fields_by_provider, errors = importer.load_repo_platforms()
    assert not errors, f"平台配置解析有错: {errors}"
    assert set(fields_by_provider) >= {"gitcode", "github"}, "三张主平台卡必须在场"

    from src.kernel.persistence.models_platform_object import RepoPlatformModel

    # 关键：模拟**落库后的行**，不是配置文件本身 —— 导入器只写配置里出现的键，
    # 缺键的列保持模型默认值（Text→''），表真源读到的就是 ''（不是卡构建器的 form/repo_path 兜底）。
    model_defaults = {}
    for col in RepoPlatformModel.__table__.columns:
        default = getattr(col.default, "arg", "") if col.default is not None else ""
        model_defaults[col.name] = "" if default is None else default

    problems = []
    for provider, fields in sorted(fields_by_provider.items()):
        row = dict(model_defaults)
        row.update(fields)
        row["provider"] = provider
        try:
            card = build_platform_card(provider, row)
        except Exception as e:  # noqa: BLE001 - 任何构建失败都算配置缺键
            problems.append(f"{provider}: {e}")
            continue
        if not getattr(card, "branch_create_form", ""):
            problems.append(f"{provider}: branch_create_form 为空")
        if not getattr(card, "issue_write_form", ""):
            problems.append(f"{provider}: issue_write_form 为空")
    assert not problems, "平台配置缺键，表真源卡构建会失败：\n  " + "\n  ".join(problems)
