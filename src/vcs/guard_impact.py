"""守卫脚本影响预检（Issue #996）。

场景（PR #526 / 提交 558219a3）：产物清理提交删除 docs/issue-N/* 后，仓库里
已提交的守卫脚本（scripts/check-*.sh/.py、tests/check-*.sh/.py）仍引用这些
路径 → 守卫由绿转红 → QA 暂停合入并回 coding，产生 1 轮返工。

本模块在清理前扫描守卫脚本对「待删路径」的引用：
- 命中 → breaking=True，清理应跳过删除，并在事件 payload 标注
  guard_impact{scripts:[...], breaking:true}；
- 未命中 → breaking=False，清理照常执行，不产生 guard_impact 字段。
"""
from __future__ import annotations

import base64
import fnmatch
import re

from src.config import log
from src.repo_key import parse_repo_key

# 守卫脚本目录与文件名模式（只扫脚本目录，避免整仓遍历）
GUARD_DIRS = ("scripts", "tests")
GUARD_PATTERNS = ("check-*.sh", "check-*.py")

# 一次读取守卫文件的大小上限（防止超大脚本拖垮内存）
_MAX_SCRIPT_BYTES = 1024 * 1024
_MAX_PAGES = 10


def is_guard_script(path: str) -> bool:
    """相对路径是否属于守卫脚本（scripts/check-* 或 tests/check-*）。"""
    p = (path or "").replace("\\", "/").removeprefix("./")
    parts = p.split("/")
    if len(parts) < 2:
        return False
    directory = parts[-2]
    name = parts[-1]
    if directory not in GUARD_DIRS:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in GUARD_PATTERNS)


def _reference_needles(target_paths) -> list[str]:
    needles = []
    for raw in target_paths or []:
        path = str(raw or "").replace("\\", "/").removeprefix("./")
        if not path:
            continue
        if path not in needles:
            needles.append(path)
        # 目录引用（docs/issue-996/ 或 docs/issue-996）覆盖整目录删除
        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            for cand in (parent + "/", parent, parent + "/*"):
                if cand not in needles:
                    needles.append(cand)
    return needles


def _path_pattern(needle: str):
    """路径引用匹配：两侧加边界，避免 docs/issue-996 误命中 docs/issue-9960。"""
    return re.compile(
        r"(?<![A-Za-z0-9_./-])" + re.escape(needle) + r"(?![A-Za-z0-9_-])")


def scripts_referencing(target_paths, scripts: dict) -> list[str]:
    """返回引用了待删路径的守卫脚本路径列表（scripts: {path: text}）。"""
    needles = _reference_needles(target_paths)
    if not needles:
        return []
    patterns = [_path_pattern(n) for n in needles]
    hits = []
    for path, text in (scripts or {}).items():
        if not is_guard_script(path):
            continue
        body = text or ""
        if any(pat.search(body) for pat in patterns):
            hits.append(path)
    return sorted(set(hits))


def scan_guard_impact(target_paths, scripts: dict) -> dict:
    """纯函数：按守卫脚本文本评估影响，返回 {scripts, breaking}。"""
    hits = scripts_referencing(target_paths, scripts)
    return {"scripts": hits, "breaking": bool(hits)}


def _decode_content(item: dict, path: str = "") -> str:
    content = item.get("content")
    if not isinstance(content, str) or not content:
        return ""
    try:
        raw = base64.b64decode(content)
    except Exception as e:
        log.warning(f"guard impact: decode content failed path={path}: {e}")
        return ""
    if len(raw) > _MAX_SCRIPT_BYTES:
        raw = raw[:_MAX_SCRIPT_BYTES]
    return raw.decode("utf-8", errors="replace")


def fetch_guard_scripts(repo_key: str, ref: str, token: str = "",
                        provider: str = "") -> dict:
    """拉取仓库已提交守卫脚本文本 {path: text}（contents API，目录预筛）。

    失败 fail-open 返回已取到的部分（空 dict 表示无守卫脚本/不可用）。
    """
    from src.api.git_repo_api import _vcs_req, _vcs_url

    p, owner, repo = parse_repo_key(repo_key)
    p = provider or p
    if p not in ("gitcode", "github"):
        return {}
    if not owner or not repo:
        return {}

    scripts: dict = {}
    for directory in GUARD_DIRS:
        for page in range(1, _MAX_PAGES + 1):
            try:
                items = _vcs_req(p, "GET",
                                 _vcs_url(owner, repo, f"/contents/{directory}",
                                          {"ref": ref, "per_page": 100, "page": page}, p),
                                 token)
            except Exception as e:
                log.warning(f"guard impact: list {directory} failed repo={repo_key} ref={ref}: {e}")
                break
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                path = item.get("path") or ""
                if not is_guard_script(path):
                    continue
                text = _decode_content(item, path)
                if not text:
                    try:
                        detail = _vcs_req(
                            p, "GET",
                            _vcs_url(owner, repo, f"/contents/{path}", {"ref": ref}, p),
                            token)
                        if isinstance(detail, dict):
                            text = _decode_content(detail, path)
                    except Exception as e:
                        log.warning(f"guard impact: fetch content failed path={path} "
                                    f"repo={repo_key} ref={ref}: {e}")
                        text = ""
                if text:
                    scripts[path] = text
            if len(items) < 100:
                break
    return scripts


def assess_guard_impact(repo_key: str, ref: str, target_paths,
                        token: str = "", provider: str = "",
                        scripts: dict | None = None) -> dict:
    """清理前守卫影响评估（生产入口；scripts 注入用于测试/本地复用）。"""
    if scripts is None:
        try:
            scripts = fetch_guard_scripts(repo_key, ref, token=token, provider=provider)
        except Exception as e:
            log.warning(f"guard impact: fetch failed repo={repo_key} ref={ref}: {e}")
            scripts = {}
    return scan_guard_impact(target_paths, scripts)


def list_issue_artifacts(repo_key: str, ref: str, issue_number, token: str = "",
                         provider: str = "") -> list[dict]:
    """列出 docs/issue-N 下的产物文件（[{path, sha}]）；目录不存在返回 []。"""
    from src.api.git_repo_api import _vcs_req, _vcs_url

    p, owner, repo = parse_repo_key(repo_key)
    p = provider or p
    if p not in ("gitcode", "github") or not owner or not repo:
        return []
    directory = f"docs/issue-{str(issue_number).lstrip('#')}"
    try:
        items = _vcs_req(p, "GET",
                         _vcs_url(owner, repo, f"/contents/{directory}",
                                  {"ref": ref, "per_page": 100}, p),
                         token)
    except Exception as e:
        log.warning(f"guard impact: list artifacts failed repo={repo_key} dir={directory}: {e}")
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "file":
            path = item.get("path") or ""
            if path:
                entry = {"path": path}
                if item.get("sha"):
                    entry["sha"] = item["sha"]
                out.append(entry)
    return out