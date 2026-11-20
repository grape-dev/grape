"""组织上下文解析——从 HTTP 请求中解析当前组织上下文。

优先级：
1. X-Org-ID header
2. X-Org-Slug header → 查 organization 表
3. ?org_id query
4. ?org_slug query → 查 organization 表
5. 未指定 → 返回用户的个人组织 ID（兼容现有行为）

校验：用户必须是该组织的成员（org_member 表），否则 403。

D105 第三批：org_id 全链路 str，哨兵 0 → ''（''=全局/未归属；MySQL TEXT 列与 0
数值比较会把非数字串强转 0 误匹配，哨兵必须用空串）。
"""
from __future__ import annotations

from fastapi import Request, HTTPException

# 组织上下文原语走 owner 域 public（原 import mgmt.user.org_db 内部模块）
from src.domains.mgmt import public as _mgmt_org
from src.kernel.config import log

get_org = _mgmt_org.org_by_id
get_org_by_slug = _mgmt_org.org_by_slug
get_member_role = _mgmt_org.member_role
get_personal_org_id = _mgmt_org.personal_org_id
is_org_member = _mgmt_org.org_member


def resolve_org_context(request: Request, user_id: str) -> str:
    """解析当前请求的组织上下文，返回 org_id（str；未归属为 ''）。

    未指定时回退到用户的个人组织（兼容现有 API 行为）。
    指定时校验成员关系，非成员 → 403。
    """
    org_id = ""

    # 1. X-Org-ID header（不透明 id 透传，不 int() 强转——uuid 形态 org_id 合法）
    org_id = (request.headers.get("X-Org-ID", "") or "").strip()

    # 2. X-Org-Slug header
    if not org_id:
        slug = request.headers.get("X-Org-Slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = str(org["id"])

    # 3. ?org_id query
    if not org_id:
        org_id = (request.query_params.get("org_id", "") or "").strip()

    # 4. ?org_slug query
    if not org_id:
        slug = request.query_params.get("org_slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = str(org["id"])

    # 5. 未指定 → 回退到个人组织
    if not org_id:
        org_id = get_personal_org_id(user_id)
    elif get_org(org_id) is None:
        # 客户端 localStorage 里可能残留已删除组织的 id（刚删组织 / 别的标签页删掉）：
        # 视作未指定并回落个人组织，否则整站请求都会 403「不是该组织的成员」。
        log.warning(f"org context: org {org_id} 不存在，回落个人组织 user={user_id}")
        org_id = get_personal_org_id(user_id)
    else:
        # 校验成员关系
        if not is_org_member(user_id, org_id):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})

    return org_id


def _org_scene_metadata(scene_id: str, repo_key: str) -> dict:
    """读取仓库场景的组织元数据；普通场景返回空组织标识。"""
    if not scene_id or not repo_key:
        return {"scene_type": "", "org_id": ""}
    try:
        from src.domains.mgmt import public as _mgmt_public

        return _mgmt_public.get_scene_org_metadata(scene_id, repo_key)
    except Exception as exc:
        log.warning(
            "_org_scene_metadata lookup failed: scene_id=%s repo_key=%s error=%s",
            scene_id,
            repo_key,
            exc,
        )
        raise HTTPException(status_code=503, detail={
            "code": 503,
            "error_code": "ORG_SCENE_LOOKUP_FAILED",
            "message": "组织场景元数据读取失败，请稍后重试",
        })


def validate_org_scene_entry(repo_key: str, scene_id: str, context_org_id: str) -> dict:
    """校验组织需求入口必须同时使用组织仓库与组织场景。

    普通仓库场景不触发组织约束；仓库或场景任一带有组织属性时，二者必须属于同一组织，
    且调用方当前组织上下文必须与该组织一致。
    """
    from src.domains.mgmt.user.org_db import get_managed_repo_binding, get_org
    from src.kernel.repo_key import to_repo_key

    normalized_repo = to_repo_key(repo_key or "")
    if not normalized_repo:
        return {"is_org": False}

    binding = get_managed_repo_binding(normalized_repo) or {}
    repo_org_id = str(binding.get("org_id") or "")
    is_org_repo = bool(binding.get("is_org_repo"))
    scene_meta = _org_scene_metadata(scene_id, normalized_repo)
    scene_org_id = str(scene_meta.get("org_id") or "")
    if scene_org_id in ("0", "0.0"):
        # D105-15 哨兵：'0' 表示无组织，不能当成组织场景（bool('0') 为 True）
        scene_org_id = ""
    is_org_scene = scene_meta.get("scene_type") == "org"
    if scene_org_id and not is_org_scene:
        # 场景行上的 org_id 并不等于「组织场景」：建场景时会写入创建者归属组织，
        # 个人组织（organization.is_personal=1）的 org_id 只是归属标记。把它当成
        # 组织场景，会让「普通仓库 + 个人流水线」被判成「组织仓库/组织场景必须成对」
        # 而直接 400（grape-dev/grape 下 10 条个人流水线全线建单失败）。
        scene_org = get_org(scene_org_id) or {}
        if not scene_org.get("is_personal"):
            is_org_scene = True
    # Issue #2118 复审：普通仓库引用组织模板（官方 market scope='org' / file
    # scene_type='org' / 其它仓库的 org 场景行）时，本仓库场景表查不到 org 属性，
    # 只看 devops_pipeline_scene 会把错配当成普通场景放行。这里补一层「模板来源」
    # 判定，纳入组织场景归属，交由下方「仓库/场景必须同属组织」规则拒绝。
    if not is_org_scene:
        try:
            from src.api.pipeline_config import scene_is_org_scoped
            if scene_is_org_scoped(scene_id):
                is_org_scene = True
        except Exception as exc:
            log.warning(
                "validate_org_scene_entry: org-scope probe failed scene_id=%s repo=%s error=%s",
                scene_id, normalized_repo, exc,
            )

    if not is_org_repo and not is_org_scene:
        return {"is_org": False}

    if not is_org_repo or not is_org_scene:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "INVALID_ORG_CONTEXT",
            "message": "组织需求必须同时使用组织仓库与组织场景",
        })

    if not repo_org_id or repo_org_id != scene_org_id:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "INVALID_ORG_CONTEXT",
            "message": "组织仓库与组织场景不属于同一组织",
        })

    org = get_org(repo_org_id) or {}
    if (
        (org.get("org_repo_key") or "") != normalized_repo
        or (org.get("org_scene_id") or "") != scene_id
    ):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "INVALID_ORG_CONTEXT",
            "message": "仓库或场景不是当前组织的组织需求入口",
        })

    if str(context_org_id or "") != repo_org_id:
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "error_code": "PERMISSION_DENIED",
            "message": "请切换到组织上下文后创建组织需求",
        })

    return {
        "is_org": True,
        "org_id": repo_org_id,
        "repo_key": normalized_repo,
        "scene_id": scene_id,
    }
