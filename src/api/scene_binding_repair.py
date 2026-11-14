"""Issue #541：存量场景错误绑定幂等修复（迁移）。

背景：模板建仓曾把模板场景名（如 hello-world）误写进
`managed_repos.dev_scene` / `issue.scene_id`，导致派发侧仓内查不到该
scene_id，静默回退全局模板行（无引擎）→ SRE 派发失败。

修复策略（幂等，可多实例/多轮安全重跑）：
- 仅处理「值仓内不存在 + scene_market 存在同名 official 模板 + 能按
  market_scene_id 在仓内唯一定位场景副本（取 updated_at 最新一条）」的记录；
- 其余一律跳过（正常绑定不动，无法定位的留告警日志不盲改）；
- 每次改绑前日志留痕（改哪个仓 / 旧值 → 新值）。

挂载：`src/api/main.py` lifespan 启动时执行一次（try/except 不阻断启动）。
"""
import time

from src.kernel.config import log


def _resolve_repo_copy(scene_value: str, repo_key: str) -> str:
    """按模板名解析仓内场景副本真实 scene_id；无法唯一定位返回空串。

    匹配链：scene_value 仓内不存在 → scene_market 同名 official 模板 →
    devops_pipeline_scene 中该 repo 的 market_scene_id 副本（updated_at 最新）。

    三张表都归 mgmt，SQL 只在 `src.domains.mgmt.public` 留一份（任意拓扑原语）；
    本函数只保留匹配链规则，不再持有数据库句柄。
    """
    from src.api.pipeline_config import scene_exists_in_repo
    if scene_exists_in_repo(scene_value, repo_key):
        return ""  # 正常绑定，无需修复
    from src.domains.mgmt import public as mgmt_public
    try:
        market_id = mgmt_public.scene_active_official_id_by_name_any(scene_value)
    except Exception as _e:
        log.warning(f"repair_scene_bindings: market lookup failed: {_e}")
        return ""
    if not market_id:
        return ""
    try:
        # 多副本（weekly-09 每次应用生成独立 custom-id）由 owner 侧取最近应用的一条
        return mgmt_public.scene_copy_id_by_market_any(market_id, repo_key)
    except Exception as _e:
        log.warning(f"repair_scene_bindings: copy lookup failed: {_e}")
        return ""


def repair_scene_bindings() -> dict:
    """扫描修复 managed_repos.dev_scene 与 issue.scene_id 的存量错误绑定。

    返回 {"repos_fixed", "issues_fixed", "repos_scanned", "issues_scanned"}。
    幂等：已正确的绑定二轮扫描直接跳过（fixed=0）。
    """
    from src.domains.mgmt import public as mgmt_public
    from src.domains.runtime import public as runtime_public
    repos_fixed = 0
    issues_fixed = 0
    repos_scanned = 0
    issues_scanned = 0
    started = time.time()

    # 1. managed_repos.dev_scene
    try:
        repo_rows = mgmt_public.repo_bound_scene_rows_any()
        repos_scanned = len(repo_rows)
        for repo_key, dev_scene in repo_rows:
            copy_id = _resolve_repo_copy(dev_scene or "", repo_key)
            if not copy_id:
                continue
            # CAS：旧值仍是本次读到的那条才改，与原 UPDATE ... AND dev_scene=? 同口径
            if mgmt_public.set_repo_dev_scene_cas_any(repo_key, dev_scene, copy_id):
                repos_fixed += 1
                log.info(f"repair_scene_bindings: repo {repo_key} dev_scene "
                         f"'{dev_scene}' -> '{copy_id}' (Issue #541)")
    except Exception as e:
        log.warning(f"repair_scene_bindings: managed_repos scan failed: {e}")

    # 2. issue.scene_id（issue 单表为权威，Phase C3）
    try:
        issue_rows = runtime_public.issue_bound_scene_rows_any()
        issues_scanned = len(issue_rows)
        for row_id, repo_key, issue_number, scene_id in issue_rows:
            copy_id = _resolve_repo_copy(scene_id or "", repo_key)
            if not copy_id:
                continue
            if runtime_public.set_issue_scene_cas_any(row_id, scene_id, copy_id):
                issues_fixed += 1
                log.info(f"repair_scene_bindings: issue {repo_key} #{issue_number} "
                         f"scene '{scene_id}' -> '{copy_id}' (Issue #541)")
    except Exception as e:
        log.warning(f"repair_scene_bindings: issue scan failed: {e}")

    # 修复后清场景缓存，确保派发侧立即按新绑定解析
    if repos_fixed or issues_fixed:
        try:
            from src.api.pipeline_config import invalidate_scene_cache
            invalidate_scene_cache("", "")
        except Exception as _e:
            log.warning(f"repair_scene_bindings: cache invalidate failed: {_e}")

    log.info(f"repair_scene_bindings: scanned {repos_scanned} repos / {issues_scanned} issues, "
             f"fixed {repos_fixed} repos / {issues_fixed} issues "
             f"in {time.time() - started:.2f}s (Issue #541)")
    return {"repos_fixed": repos_fixed, "issues_fixed": issues_fixed,
            "repos_scanned": repos_scanned, "issues_scanned": issues_scanned}
