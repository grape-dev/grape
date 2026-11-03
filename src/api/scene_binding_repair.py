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

from src.config import log


def _resolve_repo_copy(scene_value: str, repo_key: str) -> str:
    """按模板名解析仓内场景副本真实 scene_id；无法唯一定位返回空串。

    匹配链：scene_value 仓内不存在 → scene_market 同名 official 模板 →
    devops_pipeline_scene 中该 repo 的 market_scene_id 副本（updated_at 最新）。
    """
    from src.api.pipeline_config import scene_exists_in_repo
    if scene_exists_in_repo(scene_value, repo_key):
        return ""  # 正常绑定，无需修复
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id FROM scene_market WHERE name=? AND source='official' AND is_active=1 LIMIT 1",
                (scene_value,))
            row = c.fetchone()
        except Exception as _e:
            log.warning(f"repair_scene_bindings: market lookup failed: {_e}")
            return ""
    if not row:
        return ""
    market_id = row[0]
    with db_lock:
        c = db.cursor()
        try:
            # 多副本（weekly-09 每次应用生成独立 custom-id）取最近应用的一条
            c.execute(
                "SELECT scene_id FROM devops_pipeline_scene "
                "WHERE market_scene_id=? AND repo_key=? AND is_active=1 "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (market_id, repo_key))
            copy_row = c.fetchone()
        except Exception as _e:
            log.warning(f"repair_scene_bindings: copy lookup failed: {_e}")
            return ""
    return (copy_row[0] or "") if copy_row else ""


def repair_scene_bindings() -> dict:
    """扫描修复 managed_repos.dev_scene 与 issue.scene_id 的存量错误绑定。

    返回 {"repos_fixed", "issues_fixed", "repos_scanned", "issues_scanned"}。
    幂等：已正确的绑定二轮扫描直接跳过（fixed=0）。
    """
    from src.session_manager import db, db_lock
    repos_fixed = 0
    issues_fixed = 0
    repos_scanned = 0
    issues_scanned = 0
    started = time.time()

    # 1. managed_repos.dev_scene
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, dev_scene FROM managed_repos WHERE dev_scene != ''")
            repo_rows = c.fetchall()
        repos_scanned = len(repo_rows)
        for repo_key, dev_scene in repo_rows:
            copy_id = _resolve_repo_copy(dev_scene or "", repo_key)
            if not copy_id:
                continue
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=? AND dev_scene=?",
                          (copy_id, repo_key, dev_scene))
                db.commit()
                if c.rowcount:
                    repos_fixed += 1
                    log.info(f"repair_scene_bindings: repo {repo_key} dev_scene "
                             f"'{dev_scene}' -> '{copy_id}' (Issue #541)")
    except Exception as e:
        log.warning(f"repair_scene_bindings: managed_repos scan failed: {e}")

    # 2. issue.scene_id（issue 单表为权威，Phase C3）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, repo_key, issue_number, scene_id FROM issue WHERE scene_id != ''")
            issue_rows = c.fetchall()
        issues_scanned = len(issue_rows)
        for row_id, repo_key, issue_number, scene_id in issue_rows:
            copy_id = _resolve_repo_copy(scene_id or "", repo_key)
            if not copy_id:
                continue
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE issue SET scene_id=? WHERE id=? AND scene_id=?",
                          (copy_id, row_id, scene_id))
                db.commit()
                if c.rowcount:
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
