"""help/api markdown ↔ OpenAPI 契约 一致性测试（api-docs-standardization-design.md R3）。

规则（单一事实源 = /openapi.json，由当前代码渲染，逻辑见 src/help_docs_sync.py）：
1. 每个对外契约操作（含 x-token-scope）都必须在 help/api/*.md 有 `METHOD /path` 端点块
   （内部端点走 DOC_EXEMPT 白名单）。
2. 文档中出现的对外端点必须在契约中存在（或白名单内），防止文档记录已下线接口。
3. README scope 一览须链接到全部 18 篇文档。
"""
from pathlib import Path

from src.help_docs_sync import compare, DOC_EXEMPT, _normalize, load_contract_ops, load_doc_ops

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELP_DIR = _REPO_ROOT / "help" / "api"

ALL_DOCS = [
    "00-auth.md", "01-tasks.md", "02-repos.md", "03-agents.md", "04-chat.md",
    "05-skills.md", "06-scenes.md", "07-knowledge.md", "08-events.md",
    "09-task-logs.md", "10-git-repos.md", "11-issues.md", "12-a2a.md",
    "13-settings.md", "14-runtime.md", "15-devstage.md", "16-auth-and-misc.md",
    "README.md",
]


def _exempt() -> set[tuple[str, str]]:
    return {(m.lower(), _normalize(p)) for m, p in DOC_EXEMPT}


def test_every_contract_op_is_documented():
    """契约全部对外操作都有文档端点块（白名单除外）——防新端点漏文档。"""
    contract = load_contract_ops()
    docs = load_doc_ops()
    exempt = _exempt()

    missing = sorted(
        (m, p, op_id)
        for (m, p), op_id in contract.items()
        if (m, p) not in docs and (m, p) not in exempt
    )
    assert not missing, "\n".join(
        f"契约有、文档缺: {m.upper()} {p} ({op_id})" for m, p, op_id in missing)


def test_every_documented_op_is_in_contract():
    """文档记录的所有对外端点必须在契约存在（或白名单）——防「文档写了下线接口」。"""
    contract = load_contract_ops()
    docs = load_doc_ops()
    exempt = _exempt()

    extra = sorted((m, p) for m, p in docs
                   if (m, p) not in contract and (m, p) not in exempt)
    assert not extra, "\n".join(
        f"文档有、契约无（疑似已下线）: {m.upper()} {p}" for m, p in extra)


def test_compare_helper_consistent():
    """compare() 组合结果与两个独立断言一致（防工具逻辑自身漂移）。"""
    missing, extra = compare()
    assert isinstance(missing, list) and isinstance(extra, list)


def test_readme_links_all_doc_files():
    """README scope 一览须覆盖全部内容文档，防止新增文档忘记入目录/链接。"""
    readme = (_HELP_DIR / "README.md").read_text(encoding="utf-8")
    for doc in ALL_DOCS:
        if doc == "README.md":
            continue  # 索引自身不要求出现在正文
        assert doc in readme, f"README 缺少链接: {doc}"
        assert (_HELP_DIR / doc).exists(), f"缺失文档: {doc}"