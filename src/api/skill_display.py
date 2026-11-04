"""Shared skill display-name resolution (Issue #980).

场景列表（scenes.py with-pipeline）与 dashboard 此前各自构建
`scope='official'` 的 skill_name → display_name 映射并直接覆盖节点显示名，
导致三档同名技能（official/repo/user，唯一键 UNIQUE(skill_name, scope, owner_id)）
无法区分：节点实际绑定 user/repo 档却显示官方名。

本模块提供共用解析：
- resolve_skill_display_name(skill_name, skill_id, scope, owner_id, repo_key, user_id)
  解析优先级：skill_id 精确命中 → (scope, skill_name) 显式档位 →
  user(owner=user_id) → repo(owner=repo_key) → official 兜底 → skill_name 本身。
  失活技能（is_active=0）视为未命中，沿回退链继续。
  该优先级与派发侧 resolve_skill_id（src/user/db.py，Issue #367 个人优先）一致，
  保证展示名与实际绑定档位一致。
- build_skill_display_map(repo_key, user_id)
  批量版：一次查询全部 active 技能，按上述优先级为每个 skill_name 选定
  display_name，供列表类接口按名批量 enrich（避免 N+1）。
"""
from src.session_manager import db, db_lock


def _norm_user_id(user_id) -> str:
    if user_id is None:
        return ""
    return str(user_id)


def _pick_display(display_name: str, skill_name: str) -> str:
    return display_name or skill_name


def resolve_skill_display_name(skill_name: str, skill_id: int = 0,
                               scope: str = "", owner_id: str = "",
                               repo_key: str = "", user_id=None) -> str:
    """按回退链解析技能显示名（Issue #980）。

    优先级：skill_id → (scope, owner_id) 显式档 → user → repo → official → skill_name。
    任一层只认 is_active=1 的行（失活=未命中，继续回退）。
    skill_name 为空直接返回空串（与既有节点语义一致）。
    """
    if not skill_name:
        return ""
    uid = _norm_user_id(user_id)
    if scope and not owner_id:
        owner_id = ""
    try:
        with db_lock:
            c = db.cursor()
            if skill_id:
                c.execute(
                    "SELECT display_name FROM skill WHERE id=? AND is_active=1",
                    (int(skill_id),),
                )
                row = c.fetchone()
                if row:
                    return _pick_display(row[0], skill_name)
            if scope:
                c.execute(
                    "SELECT display_name FROM skill "
                    "WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1 LIMIT 1",
                    (skill_name, scope, owner_id),
                )
                row = c.fetchone()
                if row:
                    return _pick_display(row[0], skill_name)
            if uid:
                c.execute(
                    "SELECT display_name FROM skill "
                    "WHERE skill_name=? AND scope='user' AND owner_id=? AND is_active=1 LIMIT 1",
                    (skill_name, uid),
                )
                row = c.fetchone()
                if row:
                    return _pick_display(row[0], skill_name)
            if repo_key:
                c.execute(
                    "SELECT display_name FROM skill "
                    "WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 LIMIT 1",
                    (skill_name, repo_key),
                )
                row = c.fetchone()
                if row:
                    return _pick_display(row[0], skill_name)
            c.execute(
                "SELECT display_name FROM skill "
                "WHERE skill_name=? AND scope='official' AND is_active=1 LIMIT 1",
                (skill_name,),
            )
            row = c.fetchone()
            if row:
                return _pick_display(row[0], skill_name)
    except Exception:
        pass
    return skill_name


def build_skill_display_map(repo_key: str = "", user_id=None) -> dict:
    """批量构建 skill_name → display_name 映射（与 resolve 同一回退链）。

    单次查询全部 active 技能，Python 侧按 user(匹配 user_id) > repo(匹配 repo_key)
    > official 的优先级为每个 skill_name 选定显示名；display_name 为空回退
    skill_name。DB 异常时返回空 dict（调用方按旧语义回退 skill_name）。
    """
    uid = _norm_user_id(user_id)
    skill_map: dict = {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT skill_name, display_name, scope, owner_id FROM skill WHERE is_active=1"
            )
            rows = c.fetchall()
    except Exception:
        return {}
    # 优先级打分：3=user(本上下文) 2=repo(本上下文) 1=official 0=不相关档
    best: dict = {}
    for skill_name, display_name, scope, owner_id in rows:
        if scope == "user" and uid and owner_id == uid:
            prio = 3
        elif scope == "repo" and repo_key and owner_id == repo_key:
            prio = 2
        elif scope == "official":
            prio = 1
        else:
            prio = 0
        if skill_name not in best or prio > best[skill_name][0]:
            best[skill_name] = (prio, _pick_display(display_name, skill_name))
    return {name: val[1] for name, val in best.items()}
