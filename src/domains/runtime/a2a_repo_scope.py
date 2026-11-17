"""A2A 仓库作用域解析（Issue #3055 C 项，独立小模块）。

两张值对象/策略类收敛本单新增的仓库解析逻辑，`routes/webhook.py` 只做一行转发
（巨型路由文件不承载实现）：

- :class:`RepoKeyResolution` —— 裸 ``owner/repo`` → 全 provider 候选集反查
  ``managed_repos``（mgmt 域读口 ``repo_key_in_candidates``，不直连外域表）；
  未命中才回落到平台卡默认前缀，并显式 warning（此前硬编码 github/gitcode 双前缀，
  gitee 等新平台裸传必错判 gitcode）。
- :class:`IssueRepoScope` —— 越仓反查作用域：``issue`` 表属 runtime 域（本域直连合法），
  但只把**同 provider** 他仓的同编号当作「仓库不匹配」的正向证据；跨平台同编号
  （gitee #1 与 gitcode #1）是完全不同的单，不得互证。

SQL 只出现在本模块（``issue`` = runtime 域基表，归属见
docs/weekly-11/table-owner.tsv），跨域读一律走 mgmt public。
"""

from dataclasses import dataclass, field

from src.kernel.config import log

__all__ = ["RepoKeyResolution", "resolve_a2a_repo_key", "IssueRepoScope"]


@dataclass(frozen=True)
class RepoKeyResolution:
    """裸仓库名的 repo_key 解析结果（候选集 + 是否在 managed_repos 命中）。"""

    repository: str
    candidates: tuple[str, ...] = field(default_factory=tuple)
    repo_key: str = ""
    resolved: bool = False

    @classmethod
    def resolve(cls, repository: str) -> "RepoKeyResolution":
        from src.kernel.repo_key import to_repo_key, repo_key_candidates

        repo = str(repository or "").strip()
        if not repo:
            return cls(repository="", repo_key="")
        if ":" in repo:
            return cls(repository=repo, candidates=(repo,), repo_key=to_repo_key(repo), resolved=True)
        cands = tuple(repo_key_candidates(repo))
        try:
            from src.domains.mgmt import public as mgmt_public
            hit = mgmt_public.repo_key_in_candidates(list(cands))
        except Exception as exc:  # 读口异常不阻断：回落默认前缀（log 明示）
            log.warning(f"A2A repo resolution: managed_repos lookup failed for {repo!r}: {exc}")
            hit = ""
        if hit:
            return cls(repository=repo, candidates=cands, repo_key=hit, resolved=True)
        return cls(repository=repo, candidates=cands, repo_key=to_repo_key(repo), resolved=False)

    def fallback_warning(self) -> str:
        """未命中 managed_repos 时的告警文案（调用方打日志，单一文案来源）。"""
        return (f"A2A repo resolution fallback: {self.repository!r} not found in managed_repos "
                f"(candidates={list(self.candidates)}); defaulting to {self.repo_key!r}")


def resolve_a2a_repo_key(repository: str) -> str:
    """裸/带前缀仓库名 → repo_key（全 provider 候选反查，未命中回落并 warning）。"""
    resolution = RepoKeyResolution.resolve(repository)
    if not resolution.repository:
        return ""
    if not resolution.resolved and ":" not in resolution.repository:
        log.warning(resolution.fallback_warning())
    return resolution.repo_key


class IssueRepoScope:
    """单个 repo_key 的越仓反查作用域（同 provider 才互证）。"""

    def __init__(self, repo_key: str):
        from src.kernel.repo_key import parse_repo_key
        self.repo_key = str(repo_key or "")
        self.provider = parse_repo_key(self.repo_key)[0] or ""

    def other_repo_with_issue(self, issue_number) -> str:
        """本仓之外、**同 provider** 且存在该编号的 repo_key；无证据返回 ""。"""
        from src.kernel.issue_ident import is_native_issue_ident, native_issue_number

        if not self.repo_key or not is_native_issue_ident(issue_number):
            return ""
        num = native_issue_number(issue_number)
        try:
            from src.kernel.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    # 前缀相等用 substr，不用 LIKE：provider 允许下划线，而 `_` 是 LIKE
                    # 的单字符通配 → `LIKE 'a_b:%'` 会误命中 `aXb:...`（P2-6 全仓纪律）。
                    "SELECT repo_key FROM issue WHERE issue_number=? AND repo_key<>? "
                    "AND substr(repo_key, 1, ?) = ? LIMIT 1",
                    (num, self.repo_key, len(self.provider) + 1,
                     f"{self.provider}:"))
                row = c.fetchone()
                return str(row[0]) if row and row[0] else ""
        except Exception as exc:
            log.warning(f"A2A #2555: cross-repo issue lookup failed for "
                        f"repo={self.repo_key!r} issue=#{issue_number}: {exc}")
            return ""