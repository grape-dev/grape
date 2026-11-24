"""契约版本号单一来源 + 版线护栏（Issue #2784 S2）。

护栏守护两件事：
1. **对外契约的 info.version 就是单一来源的值**——不再由各进程入口各自
   硬编码（本单修复前仓库同时存在 2.2.0 五处与 2.2.2 两处）；
2. **测试线契约版本对现网合法**——是现网发布快照的合法递进步且为预发布，
   避免再次出现「2.2.0 -> 2.5.0」跳版（跳版号从未上现网，却触发 CLI 升级提醒）。
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import contract_versioning as cv
from src.api.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "help" / "api" / "openapi-baseline.json"

#: `version="2.3.0a1"` / `"version": "2.3.0a1"` 这类硬编码契约版本字面量；
#: 左词边界避免把 `sdk_version=`、`"mock_version":` 之类的相邻字段误判成契约版本。
_VERSION_LITERAL_RE = re.compile(
    r'(?<![a-zA-Z_])(?:"version"\s*:\s*|version\s*=\s*)"2\.[0-9]+\.[0-9]+[a-z0-9]*"'
)


@pytest.fixture(scope="module")
def contract():
    return TestClient(app).get("/openapi.json").json()


def test_contract_root_version_is_single_source(contract):
    assert contract["info"]["version"] == cv.CONTRACT_VERSION


def _released_snapshots() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (REPO_ROOT / "contracts" / "released").glob("*.json")
    ]


def _latest_released_snapshot() -> dict:
    snapshots = _released_snapshots()
    assert snapshots, "contracts/released/ 至少要有一次发布冻结快照（现网版本来源）"
    return max(snapshots, key=lambda s: s["release_index"])


def test_contract_version_is_legal_prerelease_of_production_snapshot(contract):
    # Issue #4254：现网版本的单一来源是发布冻结快照（contracts/released/，按 release_index
    # 取最新），不是 help/api/openapi-baseline.json —— 后者由 `--update-baseline` 从当前契约
    # 写盘，必然带着测试线的预发布版本；把它当"现网"会让本用例恒红（2026-11-20~24 在
    # arch-weekly-11 上红了 4 天，期间 #4254 直接合入）。删侧观察期门禁早已改用发布快照
    # （src/domains/contracts/deprecation.py），这里与之对齐，保证"现网是什么"只有一个答案。
    production = _latest_released_snapshot()["version"]
    errors = cv.check_successor(production, contract["info"]["version"])
    assert errors == [], "测试线契约版本必须是从现网版本合法递进的预发布版：" + "; ".join(errors)
    assert cv.is_prerelease(contract["info"]["version"]), (
        "测试线契约必须带 a/b/rc 预发布后缀，否则 CLI 无法区分测试契约与现网契约"
    )


def test_contract_aggregator_shares_the_same_constant():
    from src.api import openapi_merge

    assert openapi_merge.CONTRACT_VERSION == cv.CONTRACT_VERSION


def test_no_contract_version_literal_anywhere_in_api_package():
    """扫全包（含子目录）：进程入口、契约聚合、健康探针等落点都不得复制版本字面量。"""
    offenders = []
    api_root = REPO_ROOT / "src" / "api"
    for path in sorted(api_root.rglob("*.py")):
        hits = _VERSION_LITERAL_RE.findall(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path.relative_to(api_root)}: {hits}")
    assert offenders == [], (
        "契约版本号必须取自 src/api/contract_versioning.CONTRACT_VERSION，"
        "不得在入口/聚合/健康探针等落点硬编码：" + "; ".join(offenders)
    )


# --------------------------------------------------------------------------
# 文档层护栏：规则与测试契约通道必须写在仓库里，否则下一次 bump 又回到随手改
# --------------------------------------------------------------------------

def test_contributing_documents_version_policy():
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "契约版本号策略" in text, "CONTRIBUTING 必须含「契约版本号策略」章节"
    assert "禁跳版" in text
    assert "contract_versioning" in text, "策略章节必须指向实现位置（单一事实源）"
    assert "x-changelog" in text, "策略章节必须说明变更通知入口"


def test_deploy_readme_pins_the_test_contract_channel():
    """部署清单必须写明测试契约通道承载预发布契约。

    通道域名按部署环境注入（本仓为公开仓，按 §1-11/§1-12 不落真实内网域名），
    因此这里只校验「有通道口径 + 承载预发布」两条事实，不钉具体域名。
    """
    text = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "测试契约通道" in text, "部署清单必须写明测试契约通道"
    assert "预发布" in text, "部署清单必须写明测试通道承载预发布契约（a/b/rc）"


def test_version_policy_design_note_exists():
    """版本策略正文必须入仓：正文落在 `CONTRIBUTING.md` §9.1（公开、可审）。

    此前正文放在 `docs/issue-2784/version-policy.md`，该文件属内部规划文档，
    已按开源合规 §1-11/§3-7 移出仓库；门禁改成校验公开落点，避免两条规范互相拉扯。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "### 9.1 契约版本号策略" in text, "版本策略正文必须落在 CONTRIBUTING.md §9.1"
    assert "CONTRACT_VERSION" in text, "策略正文必须指向单一事实源实现"


# --------------------------------------------------------------------------
# 现网版本来源护栏（Issue #4254 合并后补）。
# 回归背景：护栏原先从 help/api/openapi-baseline.json 读"现网版本"，而该文件由
# `scripts/regenerate_openapi.py --update-baseline` 从当前契约写盘——测试线每重定基
# 一次就把预发布版本写进去，于是「候选 2.3.0a1 与现网 2.3.0a1 相同 → 未递进」，
# 版本护栏从 2026-11-20 起恒红 4 天，期间 #4254 直接合入。现改用发布冻结快照作唯一
# 来源，并在这里钉死快照本身的两条性质，避免"现网版本"再次漂移。

def test_released_snapshots_are_formal_versions_and_monotone():
    """发布冻结快照只装正式版，release_index 唯一且单调（现网版本的判定基准）。"""
    snapshots = _released_snapshots()
    assert snapshots, "contracts/released/ 至少要有一次发布冻结快照（现网版本来源）"
    for s in snapshots:
        assert not cv.is_prerelease(s["version"]), (
            f"发布冻结快照 {s.get('version')} 带预发布后缀——现网只装正式版，"
            f"预发布请走 CONTRACT_VERSION（测试线），冻结只在转正后做"
        )
        assert s.get("paths"), f"发布冻结快照 {s.get('version')} 缺 paths"
    indexes = [s["release_index"] for s in sorted(snapshots, key=lambda s: s["release_index"])]
    assert indexes == sorted(set(indexes)), (
        f"release_index 必须唯一且单调递增，实际={indexes}"
    )
