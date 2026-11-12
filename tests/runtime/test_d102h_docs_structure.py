"""D102-H：当期分支设计文档归拢——目录结构守卫。

用户拍板方案 A：分支自产文档归 docs/weekly-09/multi-platform-decouple/，
基线已有文档零触碰（合并 diff 不含基线文件 rename）。
本守卫防"新批次方案稿再散回 weekly-09 根目录"——新文档必须进分支文件夹
或显式登记白名单（基线遗留件）。
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_W9 = _REPO_ROOT / "docs" / "weekly-09"
_DIR = _W9 / "multi-platform-decouple"

# 基线遗留白名单（本分支之前已存在，不强制迁移——归属清理留给基线维护者）
import subprocess as _sp

_BASELINE_FILES = {
    "a2a-chain-patrol-design.md", "a2a-chain-patrol-requirements.md",
    "chat-remote-failure-hint-design.md", "git-repo-tree-slash-ref-design.md",
    "grape-page-perf-analysis-20261030.md", "grape-three-process-split-design.md",
    "hermes-acp-compat-matrix.md", "official-repo-config-unify-design.md",
    "oneclick-host-heal-design.md", "oneclick-host-heal-requirements.md",
    "org-dropdown-4-vs-3-analysis.md", "repo-owner-prefix-injection-fix-design.md",
    "scene-apply-independent-id-design.md", "unbind-repo-list-invisible-design.md",
    "unbind-repo-list-invisible-requirements.md",
}


def _is_baseline_doc(name: str) -> bool:
    """动态豁免：HEAD~1（单提交政策下=基线 tip）上已存在的 weekly-09 文档 = 基线产权，
    不算「分支自产」。防写死名单随基线推进再腐化（im-*.md 误报就是这么来的）。"""
    try:
        _sp.run(["git", "cat-file", "-e", f"HEAD~1:docs/weekly-09/{name}"],
                check=True, capture_output=True, cwd=_W9)
        return True
    except Exception:
        return False


def test_d102h_branch_docs_in_folder():
    """本分支自产文档必须在 multi-platform-decouple/ 内（防再散）。"""
    loose = sorted(p.name for p in _W9.glob("*.md")
                   if p.name not in _BASELINE_FILES and not _is_baseline_doc(p.name))
    assert not loose, f"分支自产文档散在 weekly-09 根目录（应入 multi-platform-decouple/）: {loose}"


def test_d102h_index_nav_complete():
    """索引页含批次方案稿导航节（打断文件文档地图闭环）。"""
    idx = (_DIR / "multi-platform-decouple-design.md").read_text(encoding="utf-8")
    assert "## 批次方案稿" in idx, "索引页缺批次方案稿导航节"
    # 关键方案稿链接在册
    for f in ("d100-credential-type-schema-design.md", "d101-task-token-snapshot-retirement-design.md",
              "cluster-config-convergence-requirements.md", "rc-redesign-design.md"):
        assert f"({f})" in idx, f"索引页缺 {f} 导航"


def test_d102h_fragment_nav_same_dir():
    """分片头索引导航指向同目录（归拢后 ../ 形态失效清理验证）。"""
    for frag in _DIR.glob("0*.md"):
        head = frag.read_text(encoding="utf-8")[:200]
        if "索引导航" in head:
            assert "../multi-platform-decouple" not in head, \
                f"{frag.name} 索引导航仍指 ../（归拢后已同目录）"


def test_d102h_uc39_doc_anchor():
    """文档消费方守卫：uc39 锚点路径指向新位置（归拢期唯一真回归防复活）。"""
    src = (_REPO_ROOT / "tests" / "test_uc39_deploy_checklist.py").read_text(encoding="utf-8")
    assert 'multi-platform-decouple' in src, "uc39 文档锚点未指向新目录（归拢回归）"
