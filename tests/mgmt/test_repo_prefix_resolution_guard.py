"""仓库维度读路径的「真实前缀」守卫（2026-11-14 第八批，gitee 实弹后收口）。

三条都是同一形状：路径参数只有 owner/repo，前端 URL 不带平台前缀，
`to_repo_key(owner/repo)` 默认产出 `gitcode:` —— 于是 gitee/github 托管仓在这些
路径上读/写错行（project-info 返回空基线、events 列表恒空、agent 落错 runtime 行、
引擎 scope 匹配不到 repo 级凭据）。

守卫：
  1. `_engine_scope_extra()` 的 SQL 占位符数量必须随候选集变化（含 gitee/gitlab 前缀）；
     （2026-11-16 rebase 后该能力并入基线同名函数，scope 过滤仍由平台卡候选集派生）
  2. events 路由不得再写死 `[gitcode:, github:]` 候选对；
  3. project-info / agents 两处必须走 `resolve_managed_repo_key()`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROUTES = ROOT / "src/domains/runtime/routes"
MGMT = ROOT / "src/domains/mgmt/routes"


def test_repo_scope_clause_follows_candidate_set():
    from src.domains.runtime.routes.events import _engine_scope_extra
    from src.kernel.repo_key import repo_key_candidates

    repo_key = "gitee:yang-aiwu-new/livefire-tpl-verify-1010"
    bare = repo_key.split(":", 1)[1]
    sql, params = _engine_scope_extra(bare, "33")
    cands = repo_key_candidates(bare)

    assert sql.count("rc.owner_id = ?") == len(cands), (
        f"占位符数({sql.count('rc.owner_id = ?')}) 应等于候选键数({len(cands)})")
    assert params[:len(cands)] == cands, "前 N 个参数 = 候选键"
    # 候选集必须覆盖 gitee 自身前缀（原写死 gitcode/github 两家时这里是空的）
    assert "gitee:yang-aiwu-new/livefire-tpl-verify-1010" in params


def test_events_route_has_no_hardcoded_prefix_pair():
    src = (ROUTES / "events.py").read_text(encoding="utf-8")
    assert 'f"gitcode:{' not in src, "events 路由又写死 gitcode 前缀候选"
    assert 'f"github:{' not in src, "events 路由又写死 github 前缀候选"
    assert "repo_key_candidates" in src, "events 候选键应由平台卡派生"


def test_project_info_and_agents_resolve_real_prefix():
    repos_src = (MGMT / "repos.py").read_text(encoding="utf-8")
    # project-info 段落
    i = repos_src.index("def get_repo_project_info")
    seg = repos_src[i:i + 1200]
    assert "resolve_managed_repo_key" in seg, "project-info 必须解析 managed_repos 真实前缀"

    agents_src = (MGMT / "agents.py").read_text(encoding="utf-8")
    n = agents_src.count("resolve_managed_repo_key")
    assert n >= 2, f"agents 两处应按真实前缀解析（实际 {n}）"
    assert not re.search(r"repo_key = to_repo_key\(f\"\{owner\}/\{repo\}\"\)", agents_src), \
        "agents 仍存在裸 to_repo_key（默认 gitcode 前缀）"