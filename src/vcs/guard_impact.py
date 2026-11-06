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
_MAX_DIR_DEPTH = 5


def is_guard_script(path: str) -> bool:
    """相对路径是否属于守卫脚本（scripts/**/check-* 或 tests/**/check-*）。

    顶层目录必须是守卫目录，文件名可在任意嵌套层级（如 scripts/ci/check-*.sh）。
    """
    p = (path or "").replace("\\", "/").removeprefix("./")
    parts = [part for part in p.split("/") if part]
    if len(parts) < 2:
        return False
    if parts[0] not in GUARD_DIRS:
        return False
    name = parts[-1]
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
    """路径引用匹配（保守优先）。

    - 前置边界允许 `/`：覆盖 `./docs/...`、`$ROOT/docs/...`、绝对路径等写法；
    - 非目录 needle 的后置边界排除 `.`：避免 `docs/issue-996` 误命中
      `docs/issue-996.md` / `docs/issue-996.tar.gz` 这类无关文件；
    - 目录 needle（以 `/` 结尾）不加后置边界，允许其下任意文件引用。
    """
    escaped = re.escape(needle)
    prefix = r"(?<![A-Za-z0-9_.-])"
    suffix = "" if needle.endswith(("/", "*")) else r"(?![A-Za-z0-9_.-])"
    return re.compile(prefix + escaped + suffix)


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
        body = (text or "").replace("\\", "/")
        if any(pat.search(body) for pat in patterns):
            hits.append(path)
    return sorted(set(hits))


def scan_guard_impact(target_paths, scripts: dict) -> dict:
    """纯函数：按守卫脚本文本评估影响，返回 {scripts, breaking}。

    性能（Issue #996 复审）：旧实现是「每个待删路径一条正则 × 每个脚本全文」，
    10 个路径 × 300 个 100KB 脚本要做 300MB 正则匹配（实测 15.5s）。这里两点优化：
    - 字面量预筛：needle 已被 re.escape 转义，能被正则命中的脚本必然包含该字面量，
      不含任何 needle 文本的脚本直接跳过正则（绝大多数脚本走这条快路径）；
    - 单条交替正则：所有 needle 合成一条，全文只扫一遍，而不是 N 条各扫一遍。
    命中集合与旧实现完全一致（同一批 needle、同一组边界断言）。
    """
    needles = _reference_needles(target_paths)
    if not needles:
        return {"scripts": [], "breaking": False}
    combined = re.compile("|".join(_path_pattern(n).pattern for n in needles))
    hits = []
    for path, text in (scripts or {}).items():
        if not is_guard_script(path):
            continue
        body = (text or "").replace("\\", "/")
        if not any(n in body for n in needles):
            continue
        if combined.search(body):
            hits.append(path)
    return {"scripts": sorted(set(hits)), "breaking": bool(hits)}


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

    def _fetch_content(path: str) -> str:
        try:
            detail = _vcs_req(
                p, "GET",
                _vcs_url(owner, repo, f"/contents/{path}", {"ref": ref}, p),
                token)
        except Exception as e:
            log.warning(f"guard impact: fetch content failed path={path} "
                        f"repo={repo_key} ref={ref}: {e}")
            return ""
        if not isinstance(detail, dict):
            return ""
        return _decode_content(detail, path)

    def _walk(directory: str, depth: int) -> None:
        if depth > _MAX_DIR_DEPTH:
            return
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
                if item.get("type") == "dir":
                    if path:
                        _walk(path, depth + 1)
                    continue
                if not is_guard_script(path):
                    continue
                text = _decode_content(item, path) or _fetch_content(path)
                if text:
                    scripts[path] = text
            if len(items) < 100:
                break

    for directory in GUARD_DIRS:
        _walk(directory, 0)
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