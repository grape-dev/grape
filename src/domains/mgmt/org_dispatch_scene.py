"""组织下派前的场景目录与目标场景解析（只读）。

职责边界：
- 给组织下派 AI 提供每个关联仓库的“可执行场景目录”（不含 advanced_options /
  自定义变量值等运行期敏感配置）；
- 服务端在建子 Issue 前按「显式推荐 → 仓库默认场景 → 唯一场景」解析目标场景，
  显式推荐不可执行时回退，避免 AI 选到 SRE 未绑引擎/技能不可解析的场景后建单失败。

不新增表：最终场景仍写 ``grape_t_org_dispatch_target.scene_id``；选择理由保存在
dispatch 的 ``plan_json`` 中。
"""
from __future__ import annotations

import json
from typing import Any

from src.kernel.config import log
from src.kernel.repo_key import to_repo_key
from src.domains.runtime import public as runtime_public
from src.domains.mgmt.pipeline_config import (
    get_scenes,
    get_scenes_for_repos,
    scene_exists_in_repo,
)
from src.domains.mgmt.user.db import get_repo_scene
from src.domains.mgmt.user.org_db import list_org_repos

_MAX_PROBLEMS = 8
_MAX_RULES = 20
_MAX_CONDITION = 500


def _scene_skill_chain(scene: dict) -> list[str]:
    """场景节点 → 紧凑技能链（跳过 disabled 节点，不暴露引擎/自定义变量）。"""
    chain: list[str] = []
    for node in scene.get("nodes") or []:
        if not isinstance(node, dict) or node.get("enabled") is False:
            continue
        node_id = str(node.get("id") or node.get("tag") or "").strip()
        skill = str(node.get("skill") or "").strip()
        if node_id and skill:
            chain.append(f"{node_id}:{skill}")
        elif node_id or skill:
            chain.append(node_id or skill)
    return chain


def _problem_payload(problems: list) -> list[dict]:
    out: list[dict] = []
    for item in (problems or [])[:_MAX_PROBLEMS]:
        if not isinstance(item, dict):
            continue
        out.append({
            "code": str(item.get("reason_code") or item.get("code") or ""),
            "message": str(item.get("message") or item.get("reason") or "")[:300],
        })
    return out


def _precheck(scene_id: str, repo_key: str, user: dict | None,
              cache: dict[tuple, dict] | None = None) -> dict:
    """可执行性预校验；异常按 fail-open（与建单侧 precheck 同一语义）。

    缓存键必须含 repo_key：预校验结果依赖仓库级引擎/技能绑定，同一个 scene_id
    在不同仓库的结论可能不同（场景副本 / 关联仓共用模板时）。
    """
    _key = (str(repo_key or ""), str(scene_id or ""))
    if cache is not None and _key in cache:
        return cache[_key]
    try:
        result = runtime_public.precheck_scene(
            scene_id, repo_key, user=user, explicit=True)
    except Exception as exc:  # noqa: BLE001 - 预校验异常不阻断目录/解析
        log.warning(
            f"org_dispatch_scene: precheck failed scene={scene_id} repo={repo_key}: {exc}")
        result = {"ok": True, "problems": [], "warnings": [f"precheck failed: {exc}"]}
    if cache is not None:
        cache[_key] = result
    return result


def _recommend_config(raw: str, valid_scene_ids: set[str]) -> dict:
    """智能推荐配置：只保留仍在该仓库场景集合内的规则/兜底（输入为原始 JSON 串）。"""
    try:
        cfg = json.loads(raw or "{}") or {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as exc:  # noqa: BLE001 - 配置损坏不阻断目录
        log.warning(f"org_dispatch_scene: recommend config parse failed: {exc}")
        cfg = {}
    rules = []
    for rule in (cfg.get("rules") or [])[:_MAX_RULES]:
        if not isinstance(rule, dict):
            continue
        scene_id = str(rule.get("scene_id") or "")
        if scene_id not in valid_scene_ids:
            continue
        rules.append({
            "scene_id": scene_id,
            "condition": str(rule.get("condition") or "")[:_MAX_CONDITION],
        })
    fallback = str(cfg.get("fallback_scene") or "")
    return {
        "fallback_scene": fallback if fallback in valid_scene_ids else "",
        "rules": rules,
    }


def _scene_payload(scene: dict, *, repo_key: str, dev_scene: str,
                   include_executable: bool, user: dict | None,
                   precheck_cache: dict[tuple, dict]) -> dict:
    scene_id = str(scene.get("id") or "")
    item = {
        "scene_id": scene_id,
        "name": str(scene.get("name") or ""),
        "display_name": str(scene.get("display_name") or scene.get("name") or ""),
        "description": str(scene.get("description") or ""),
        "scene_type": str(scene.get("scene_type") or "standard"),
        "skill_chain": _scene_skill_chain(scene),
        "is_default": bool(dev_scene and scene_id == dev_scene),
    }
    if include_executable:
        pre = _precheck(scene_id, repo_key, user, precheck_cache)
        item["executable"] = bool(pre.get("ok"))
        item["problems"] = _problem_payload(pre.get("problems") or [])
        warnings = pre.get("warnings") or []
        if warnings:
            item["warnings"] = [str(w)[:300] for w in warnings[:4]]
    return item


def scene_catalog(org_id: str, *, repo_key: str = "",
                  include_executable: bool = False,
                  user: dict | None = None) -> dict:
    """返回组织关联仓库的场景目录。

    ``repo_key`` 为空返回全部关联仓库（排除组织自动仓库）；指定时只返回该仓库，
    且必须已关联当前组织，否则抛 ``ValueError``（路由层转 400 INVALID_TARGET）。
    """
    wanted = to_repo_key(str(repo_key or "").strip()) if str(repo_key or "").strip() else ""
    rows = []
    for row in list_org_repos(str(org_id)) or []:
        if not row.get("repo_key") or row.get("is_org_repo"):
            continue
        key = str(row.get("repo_key") or "")
        if wanted and key != wanted:
            continue
        rows.append(row)

    if wanted and not rows:
        raise ValueError(f"target repository is not linked to this organization: {wanted}")

    repo_keys = [str(row.get("repo_key") or "") for row in rows]
    scenes_by_repo = get_scenes_for_repos(repo_keys)
    precheck_cache: dict[tuple, dict] = {}
    repos: list[dict] = []
    for row in sorted(rows, key=lambda r: str(r.get("full_name") or r.get("repo_key") or "")):
        key = str(row.get("repo_key") or "")
        effective_default = str(row.get("dev_scene") or "")
        scenes = scenes_by_repo.get(key) or []
        valid_ids = {str(s.get("id") or "") for s in scenes if s.get("id")}
        repos.append({
            "repo_key": key,
            "full_name": str(row.get("full_name") or ""),
            "dev_scene": effective_default,
            "scene_recommend_config": _recommend_config(
                str(row.get("scene_recommend_config") or ""), valid_ids),
            "scenes": [
                _scene_payload(
                    scene, repo_key=key, dev_scene=effective_default,
                    include_executable=include_executable, user=user,
                    precheck_cache=precheck_cache)
                for scene in scenes
            ],
        })
    return {"org_id": str(org_id), "repos": repos}


def resolve_target_scene_detail(repo_key: str, requested_scene_id: str = "",
                                *, user: dict | None = None) -> dict:
    """解析目标仓库场景。

    顺序：显式推荐（须在仓内且可执行）→ 仓库 dev_scene（须可执行）→ 唯一场景
    （须可执行）。解析不到返回 ``scene_id`` 为空，并带 warnings 供 target.error。
    """
    repo_key = to_repo_key(str(repo_key or "").strip())
    requested = str(requested_scene_id or "").strip()
    warnings: list[str] = []
    problems: list[dict] = []
    precheck_cache: dict[tuple, dict] = {}

    def _usable(scene_id: str) -> bool:
        nonlocal problems
        if not scene_exists_in_repo(scene_id, repo_key):
            warnings.append(f"scene not found in repo: {scene_id}")
            return False
        pre = _precheck(scene_id, repo_key, user, precheck_cache)
        if pre.get("ok"):
            return True
        problems = _problem_payload(pre.get("problems") or [])
        _detail = "；".join(
            f"{p.get('code') or 'problem'}: {p.get('message') or ''}" for p in problems
        ) or "precheck failed"
        warnings.append(f"scene not executable: {scene_id}（{_detail}）")
        return False

    if requested and _usable(requested):
        return {"scene_id": requested, "source": "explicit",
                "requested_scene_id": requested, "warnings": warnings,
                "problems": problems}

    try:
        dev_scene = get_repo_scene(repo_key) or ""
    except Exception as exc:  # noqa: BLE001 - 读取失败按未配置处理
        log.warning(f"org_dispatch_scene: get_repo_scene failed repo={repo_key}: {exc}")
        dev_scene = ""
    if dev_scene and _usable(dev_scene):
        return {"scene_id": dev_scene, "source": "repo_default",
                "requested_scene_id": requested, "warnings": warnings,
                "problems": problems}

    try:
        scenes = get_scenes(repo_key=repo_key) or []
    except Exception as exc:  # noqa: BLE001 - 查询失败按无场景处理
        log.warning(f"org_dispatch_scene: get_scenes failed repo={repo_key}: {exc}")
        scenes = []
    if len(scenes) == 1:
        only_id = str(scenes[0].get("id") or "")
        if only_id and _usable(only_id):
            return {"scene_id": only_id, "source": "single",
                    "requested_scene_id": requested, "warnings": warnings,
                    "problems": problems}
    elif len(scenes) > 1:
        warnings.append(
            f"ambiguous scene: repo has {len(scenes)} active scenes and no usable default")

    if not scenes:
        warnings.append("no active scene in repo")
    return {"scene_id": "", "source": "none", "requested_scene_id": requested,
            "warnings": warnings, "problems": problems}