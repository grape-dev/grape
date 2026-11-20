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