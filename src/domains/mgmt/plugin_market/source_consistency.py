"""插件市场来源一致性模块（Issue #1798）：「上架包 == 仓库 HEAD 产物」门控。

上传链路（src/api/routes/plugins.py::upload_plugin）与 dry-run 入口
（POST /api/plugins/validate-consistency）共用的校验内核：

- ``fetch_source_repo_snapshot``：用**平台托管仓凭据**读取来源仓 HEAD
  （默认分支 tip commit sha + 文件树 bytes）。生产 seam（测试打桩点）；
  打包方无私仓凭据正是本需求背景（Spec 依赖说明）。
- ``evaluate_source_consistency``：一致性评估，返回结构化 ``consistency``
  对象（status = pass / mismatch / unverified + reasons + manifest_diff +
  canonical_hash + files_compared + warnings）。

口径（Spec 关键设计决策表 + 两处歧义收敛 + QA 复审修复，均为实现约定）：
1. source_ref 必须为 40 位 commit sha 且等于来源仓**目标分支**（声明
   source_branch，缺省默认分支——QA 修复③，release 分支打包不误拦）tip
   sha，否则 mismatch（路由层映射 409 PLUGIN_SOURCE_MISMATCH；QA 修复⑤：
   不 fail-fast，409 同时携带 manifest/hash 全明细）。快照读取 TOCTOU 防护
   （QA 修复⑦）：GitHub 归档按不可变 sha 下载（api.github.com zipball +
   平台凭据，私仓可强校验——QA 修复①）；GitCode 双读分支 tip 夹逼归档
   下载，双读一致才采信，持续移动 → 显式失败（unverified 降级）。
2. manifest 逐字段 diff：包内 manifest.json vs HEAD plugin.json，必比
   name/version/display_name/description/author/category/entry，
   差异条目 {field, expected(HEAD), actual(包)}；manifest.source.ref
   与声明的 source_ref 互校（不一致 → mismatch）。
3. canonical_content_hash：树指纹 = 相对路径排序后逐条
   sha256(path + "\\0" + bytes)，再对排序条目整体 sha256（跨打包器可复现，
   与既有 ZIP 字节 content_hash 互补，后者保留用于下载防篡改）。
   哈希输入剔除：__pycache__/、.git/、*.pyc、点开头条目（打包/上传链路同口径
   过滤）；树根的 manifest.json 与 plugin.json（清单对，走字段 diff 而非字节
   比对——manifest.json 由 plugin.json 派生时字节不可复现）。
   包内同时携带 plugin.json 时另行逐字节比对（与 HEAD 不一致 → mismatch）。
4. 缺失必备文件（Spec 决策表）：HEAD 缺 plugin.json → 拦截（无法核验来源
   manifest）；包缺 manifest.json → 上传链路既有 400 拦截（不在本模块）。
   树根 README.md / CHANGELOG.md 缺失 → 告警不拦截（Spec：CHANGELOG 缺失是
   本次事故现象，但不阻塞存量包）——单边存在时从哈希输入剔除并告警，
   双边存在时正常参与哈希（内容漂移可拦截）。
5. 平台凭据读取（QA 安全修复②）：仅当路由层授权门判定请求者对来源仓有
   成员资格/归属（user_repos 成员 / 会话 token 绑定仓 / 上传仓本身）时才
   动用平台托管仓凭据；否则仅匿名读——公开仓照常校验，私仓 → unverified
   降级 + 显式告警，不静默按通过处理（Spec 风险表口径），杜绝「用 bot
   token 探测任意私仓并回显文件树/字段值」。scope=repo 未声明来源由路由层
   400 拦截，scope=user 未声明 → unverified + 告警放行（兼容无仓库第三方
   插件）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.parse
import zipfile

# ── 口径常量 ────────────────────────────────────────────────────────────────
# 哈希输入剔除：垃圾目录/后缀 + 点开头条目（与上传链路 ZIP 过滤同口径）
HASH_EXCLUDE_DIRS = ("__pycache__", ".git")
HASH_EXCLUDE_SUFFIXES = (".pyc",)
# 树根清单对：manifest.json（包侧市场清单）与 plugin.json（HEAD 侧源清单）
# 走字段 diff / 专用字节比对，不进树哈希（派生序列化字节不可复现）
ROOT_MANIFEST_NAMES = ("manifest.json", "plugin.json")
# 树根软文档：单边缺失 → 告警不拦截（Spec 决策表：不阻塞存量包）
SOFT_DOC_FILES = ("README.md", "CHANGELOG.md")
# manifest 逐字段必比清单（Spec：逐字段 diff 且显式列出）
MANIFEST_DIFF_FIELDS = ("name", "version", "display_name", "description",
                        "author", "category", "entry")

_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def is_commit_sha40(value: str) -> bool:
    """40 位完整 commit sha 判定（upload/dry-run 声明契约与内核兜底共用，
    QA 修复⑧：单一定义点）。"""
    return bool(_SHA40_RE.match((value or "").strip()))


def _is_hash_junk(name: str) -> bool:
    """条目是否为哈希输入垃圾：点开头 / __pycache__ / .git / *.pyc。"""
    if name.startswith("."):
        return True
    if name in HASH_EXCLUDE_DIRS:
        return True
    return name.endswith(HASH_EXCLUDE_SUFFIXES)


def _is_path_junk(rel_path: str) -> bool:
    """路径中任一段或文件名为垃圾 → 剔除（路径级过滤）。"""
    parts = rel_path.split("/")
    return any(_is_hash_junk(p) for p in parts)


def collect_tree_files(root_dir: str) -> dict:
    """磁盘目录 → {相对 posix 路径: bytes}，剔除哈希垃圾条目。

    包侧（解压后的插件目录）与 HEAD 侧（归档解包目录）同口径。
    """
    files: dict[str, bytes] = {}
    if not root_dir or not os.path.isdir(root_dir):
        return files
    for _root, _dirs, _files in os.walk(root_dir):
        _dirs[:] = [d for d in _dirs if not _is_hash_junk(d)]
        for _fn in _files:
            if _is_hash_junk(_fn):
                continue
            rel = os.path.relpath(os.path.join(_root, _fn), root_dir).replace(os.sep, "/")
            if _is_path_junk(rel):
                continue
            with open(os.path.join(_root, _fn), "rb") as f:
                files[rel] = f.read()
    return files


def canonical_tree_hash(files: dict) -> str:
    """canonical 树指纹（Spec 口径，跨打包器可复现）。

    相对路径排序 → 逐条 sha256(path + "\\0" + bytes) → 整体
    sha256("path:entry_hash" 逐行拼接)。文件集合或任一内容变化均改变指纹。
    """
    lines = []
    for path in sorted(files):
        entry = hashlib.sha256(f"{path}\0".encode("utf-8") +
                               (files[path] or b"")).hexdigest()
        lines.append(f"{path}:{entry}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def diff_manifests(package_manifest: dict, head_manifest: dict) -> list:
    """manifest 逐字段 diff：HEAD = expected，包 = actual。

    差异条目 {field, expected, actual}；entry 等结构字段按 canonical JSON
    归一后比较（键序不影响判定）。仅比 MANIFEST_DIFF_FIELDS 必比字段。
    """
    diffs = []
    pkg = package_manifest if isinstance(package_manifest, dict) else {}
    head = head_manifest if isinstance(head_manifest, dict) else {}
    for field in MANIFEST_DIFF_FIELDS:
        actual = pkg.get(field)
        expected = head.get(field)
        if _canonical_value(actual) != _canonical_value(expected):
            diffs.append({"field": field, "expected": expected, "actual": actual})
    return diffs


def _canonical_value(value):
    """结构字段 canonical 化（dict/list 排序键 JSON），标量原样。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return value


def fetch_source_repo_snapshot(source_repo: str, branch: str = "",
                               use_platform_credentials: bool = False) -> dict:
    """用平台托管仓凭据读取来源仓 HEAD 快照（生产 seam，测试打桩点）。

    返回 {"ok": True, "head_sha", "default_branch", "files": {rel: bytes}}，
    失败返回 {"ok": False, "error": str}（调用方按 Spec 风险口径 unverified
    降级 + 告警，不静默按通过处理）。

    - head_sha：目标分支（QA Spec 修复③：声明 source_branch，缺省默认分支——
      Spec 决策表「该仓目标分支」口径，release 分支打包不误拦）tip commit sha；
    - files：HEAD 文件树（归档 zip 解包，单顶层目录剥离），相对 posix 路径；
    - use_platform_credentials（QA 安全修复②）：仅当请求者对来源仓有成员资格/
      归属（路由层授权门计算）才允许动用平台托管仓凭据；False 时仅匿名读——
      公开仓仍可校验，私仓匿名不可达 → unverified 降级，杜绝「用 bot token
      探测任意私仓并回显文件树/字段值」。
    """
    from src.kernel.repo_key import to_repo_key, parse_repo_key
    repo_key = to_repo_key((source_repo or "").strip())
    provider, owner, repo = parse_repo_key(repo_key)
    if not owner or not repo or "/" in repo:
        return {"ok": False, "error": f"无效的来源仓库标识: {source_repo!r}"}
    if provider not in ("gitcode", "github"):
        return {"ok": False, "error": f"来源仓库平台暂不支持: {provider}"}
    try:
        token = _platform_repo_token(repo_key) if use_platform_credentials else ""
        declared_branch = (branch or "").strip()
        if provider == "gitcode":
            branch_used, head_sha, archive = _gitcode_snapshot(
                owner, repo, declared_branch, token)
        else:
            branch_used, head_sha = _github_head(owner, repo, token, declared_branch)
            archive = _download_github_archive(owner, repo, head_sha, token)
        files = _archive_tree(archive)
        return {"ok": True, "head_sha": head_sha, "default_branch": branch_used,
                "files": files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _platform_repo_token(repo_key: str) -> str:
    """平台托管仓凭据：仓级 bot token（全局兜底）。打包方无私仓凭据，正是
    本需求的背景（Spec 依赖说明）；取不到 → 空串（公开仓匿名可读）。"""
    try:
        from src.domains.mgmt.user.db import get_repo_bot_token
        return (get_repo_bot_token(repo_key) or "").strip()
    except Exception:
        return ""


def _gitcode_snapshot(owner: str, repo: str, branch: str, token: str,
                      attempts: int = 2) -> tuple:
    """GitCode HEAD 快照：双读校验消除 TOCTOU（QA Spec 修复⑦）。

    raw.gitcode.com 归档仅能按分支名下载（scene_sync 同源 URL）——「读分支
    tip sha → 按分支名下归档」两步间分支推进会使 sha 与树错位。两次读 tip
    夹逼归档下载：双读一致才可信；推进 → 重试；持续移动 → 显式失败
    （调用方 unverified 降级告警，不静默按通过处理）。
    """
    _repo_path = (f"/repos/{urllib.parse.quote(owner, safe='')}"
                  f"/{urllib.parse.quote(repo, safe='')}")
    if branch:
        branch_used = branch
    else:
        info = _gitcode_v5_get(_repo_path, token)
        branch_used = str(info.get("default_branch") or "").strip() or "main"
    for _ in range(max(1, attempts)):
        sha_a = _gitcode_branch_tip(_repo_path, branch_used, token)
        archive = _download_gitcode_archive(owner, repo, branch_used, token)
        sha_b = _gitcode_branch_tip(_repo_path, branch_used, token)
        if sha_a == sha_b:
            return branch_used, sha_a, archive
    raise RuntimeError(
        f"来源仓分支 {branch_used} 在快照读取期间持续发生推进（TOCTOU 防护），"
        f"请稍后重试")


def _gitcode_branch_tip(repo_path: str, branch: str, token: str) -> str:
    """GitCode：读指定分支 tip commit sha（双读校验的读臂）。"""
    br = _gitcode_v5_get(
        f"{repo_path}/branches/{urllib.parse.quote(branch, safe='')}", token)
    head_sha = str(((br.get("commit") or {}).get("sha")) or "").strip()
    if not head_sha:
        raise RuntimeError(f"读取来源仓 HEAD sha 失败（分支 {branch}）")
    return head_sha


def _gitcode_v5_get(path: str, token: str) -> dict:
    import requests
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(f"https://gitcode.com/api/v5{path}",
                        headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitCode API GET {path} 失败（{resp.status_code}）")
    return resp.json() if resp.text.strip() else {}


def _download_gitcode_archive(owner: str, repo: str, branch: str, token: str) -> bytes:
    """GitCode 归档 zip（raw.gitcode.com，私仓用平台凭据；scene_sync 同源 URL）。"""
    import requests
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = (f"https://raw.gitcode.com/{urllib.parse.quote(owner, safe='')}"
           f"/{urllib.parse.quote(repo, safe='')}"
           f"/archive/refs/heads/{urllib.parse.quote(branch, safe='')}.zip")
    resp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"下载来源仓归档失败（{resp.status_code}）: {url}")
    if len(resp.content or b"") < 100:
        raise RuntimeError("来源仓归档为空或过小")
    return resp.content


def _github_head(owner: str, repo: str, token: str, branch: str = "") -> tuple:
    """GitHub：目标分支（声明则直接读该分支，缺省读默认分支）+ 分支 tip commit sha。"""
    import requests
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = (f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}"
            f"/{urllib.parse.quote(repo, safe='')}")
    if branch:
        _branch = branch
    else:
        resp = requests.get(base, headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API 读取仓库信息失败（{resp.status_code}）")
        _branch = str(resp.json().get("default_branch") or "").strip() or "main"
    resp = requests.get(f"{base}/commits/{urllib.parse.quote(_branch, safe='')}",
                        headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitHub API 读取 HEAD sha 失败（{resp.status_code}）")
    head_sha = str((resp.json().get("sha")) or "").strip()
    if not head_sha:
        raise RuntimeError(f"读取来源仓 HEAD sha 失败（分支 {_branch}）")
    return _branch, head_sha


def _download_github_archive(owner: str, repo: str, ref: str, token: str) -> bytes:
    """GitHub 归档 zip：api.github.com zipball + 平台凭据（QA 修复①，Spec-最差）。

    匿名 codeload 对私仓必失败（token 仅用于读 HEAD），repo scope 在 GitHub
    永远无法强校验——改走 zipball 端点携带 Bearer 凭据：私仓 302 至带签名的
    codeload 归档（跨域重定向 requests 自动去 Authorization，签名 URL 免凭据）。
    """
    import requests
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = (f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}"
           f"/{urllib.parse.quote(repo, safe='')}"
           f"/zipball/{urllib.parse.quote(ref, safe='')}")
    resp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"下载来源仓归档失败（{resp.status_code}）: {url}")
    if len(resp.content or b"") < 100:
        raise RuntimeError("来源仓归档为空或过小")
    return resp.content


def _archive_tree(archive: bytes) -> dict:
    """归档 zip → {相对 posix 路径: bytes}（剥离单顶层目录，剔除哈希垃圾条目）。"""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise RuntimeError("来源仓归档中无文件")
        prefix = ""
        firsts = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(firsts) == 1 and all("/" in n for n in names):
            prefix = firsts.pop() + "/"
        for name in names:
            rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if not rel or _is_path_junk(rel):
                continue
            files[rel] = zf.read(name)
    return files


def evaluate_source_consistency(plugin_dir: str, manifest: dict,
                                 source_repo: str, source_ref: str,
                                 branch: str = "",
                                 use_platform_credentials: bool = False) -> dict:
    """一致性门控评估（同步；路由层经 threadpool 调用）。

    返回 ``consistency`` 对象：
    - status: pass（一致）/ mismatch（拦截，路由层 409）/ unverified（降级告警）
    - reasons: 拦截原因结构化清单（{code, message, ...}）
    - manifest_diff: 逐字段差异清单（{field, expected, actual}）
    - canonical_hash: {package, head} 树指纹；hash_files: 差异文件明细
    - files_compared: 参与哈希比对的文件清单（AC3：展示参与比对文件）
    - warnings: 非拦截类告警（软文档缺失 / 平台凭据降级等）

    branch：目标分支声明位（Spec 决策表「该仓目标分支」；空 = 默认分支）。
    use_platform_credentials：路由层授权门结果（请求者对来源仓的成员资格/
    归属校验，QA 安全修复②）；False 时快照读取仅匿名，私仓 → unverified。

    评估顺序（QA 修复⑤：全程无 fail-fast，原因累计后统一判定）：
    来源快照 → sha 比对 → manifest 逐字段 diff → canonical 树指纹比对；
    reasons 非空即 mismatch（409 携带全部明细）。
    """
    base = {"source_repo": (source_repo or "").strip(),
            "source_ref": (source_ref or "").strip(),
            "source_branch": (branch or "").strip(),
            "head_sha": "", "default_branch": "",
            "reasons": [], "manifest_diff": [],
            "canonical_hash": {}, "hash_files": {}, "files_compared": [],
            "warnings": []}
    snapshot = fetch_source_repo_snapshot(base["source_repo"],
                                         branch=base["source_branch"],
                                         use_platform_credentials=use_platform_credentials)
    if not snapshot.get("ok"):
        base["warnings"].append(
            f"无法读取来源仓 {base['source_repo']} HEAD（平台凭据不可用或仓库不可达）："
            f"{snapshot.get('error', '')} —— 来源一致性未验证（unverified 降级，显式告警，"
            f"不静默按通过处理）")
        return {**base, "status": "unverified"}
    base["head_sha"] = str(snapshot.get("head_sha") or "").strip()
    base["default_branch"] = str(snapshot.get("default_branch") or "")

    # AC1：source_ref 必须等于目标分支 HEAD sha（QA 修复⑤：不再 fail-fast——
    # 记录原因后继续评估，409 同时携带 manifest_diff/hash 明细，AC2/AC3 清单
    # 在非 HEAD 场景同样对发布方可读）
    if base["source_ref"].lower() != base["head_sha"].lower():
        base["reasons"].append({
            "code": "SOURCE_REF_NOT_HEAD",
            "message": (f"声明的 source_ref 不是来源仓 HEAD："
                        f"期望 {base['head_sha']}（{base['default_branch']} tip），"
                        f"实际 {base['source_ref']}"),
            "expected": base["head_sha"], "actual": base["source_ref"]})

    # source_ref 形态防御（路由层已校验 40 位 sha，此处兜底；非拦截性记录）
    if not _SHA40_RE.match(base["source_ref"]):
        base["reasons"].append({
            "code": "SOURCE_REF_NOT_HEAD",
            "message": f"source_ref 必须为 40 位完整 commit sha: {base['source_ref']!r}",
            "expected": base["head_sha"], "actual": base["source_ref"]})

    head_files = snapshot.get("files") or {}

    # AC2-互校：manifest.source.ref 与声明的 source_ref 必须一致
    m_source = (manifest or {}).get("source") if isinstance(manifest, dict) else None
    m_source_ref = str((m_source or {}).get("ref") or "").strip() if isinstance(m_source, dict) else ""
    if m_source_ref and m_source_ref.lower() != base["source_ref"].lower():
        base["reasons"].append({
            "code": "MANIFEST_SOURCE_REF_CONFLICT",
            "message": (f"manifest.source.ref 与声明的 source_ref 不一致："
                        f"manifest 声明 {m_source_ref}，上传声明 {base['source_ref']}"),
            "expected": base["source_ref"], "actual": m_source_ref})

    # AC4-拦截项：HEAD 缺 plugin.json（无法核验来源 manifest）
    head_manifest_text = head_files.get("plugin.json")
    if head_manifest_text is None:
        base["reasons"].append({
            "code": "HEAD_PLUGIN_JSON_MISSING",
            "message": (f"来源仓 HEAD（{base['default_branch']} @ {base['head_sha'][:12]}）"
                        f"缺少必备文件 plugin.json，无法核验来源 manifest")})
        return {**base, "status": "mismatch"}

    # AC2：manifest 逐字段 diff（HEAD=expected，包=actual），差异显式列出
    try:
        head_manifest = json.loads(
            head_manifest_text.decode("utf-8-sig") if isinstance(head_manifest_text, bytes)
            else head_manifest_text)
        if not isinstance(head_manifest, dict):
            raise ValueError("plugin.json 必须是 JSON 对象")
    except Exception as e:
        base["reasons"].append({
            "code": "HEAD_PLUGIN_JSON_INVALID",
            "message": f"来源仓 HEAD plugin.json 不是合法 JSON: {e}"})
        return {**base, "status": "mismatch"}
    base["manifest_diff"] = diff_manifests(manifest, head_manifest)
    if base["manifest_diff"]:
        fields = ", ".join(d["field"] for d in base["manifest_diff"])
        base["reasons"].append({
            "code": "MANIFEST_FIELD_MISMATCH",
            "message": (f"包内 manifest.json 与来源仓 HEAD plugin.json "
                        f"逐字段比对不一致：{fields}"),
            "diff": base["manifest_diff"]})

    # AC3：canonical 树指纹比对（清单对剔除/软文档策略见 _gate_hash_maps 与模块 docstring）
    pkg_files = collect_tree_files(plugin_dir)
    pkg_map, head_map, soft_warnings, hash_detail = _gate_hash_maps(pkg_files, head_files)
    base["warnings"].extend(soft_warnings)
    base["files_compared"] = sorted(set(pkg_map) | set(head_map))
    pkg_hash = canonical_tree_hash(pkg_map)
    head_hash = canonical_tree_hash(head_map)
    base["canonical_hash"] = {"package": pkg_hash, "head": head_hash}
    base["hash_files"] = hash_detail
    # 包内同时携带 plugin.json 时逐字节比对（与 HEAD 不一致 = 非 HEAD 产物）
    if "plugin.json" in pkg_files and pkg_files["plugin.json"] != head_files.get("plugin.json"):
        base["reasons"].append({
            "code": "PLUGIN_JSON_CONTENT_MISMATCH",
            "message": "包内 plugin.json 与来源仓 HEAD plugin.json 内容不一致（逐字节比对）"})
    if pkg_hash != head_hash:
        base["reasons"].append({
            "code": "CANONICAL_HASH_MISMATCH",
            "message": (f"canonical_content_hash 不一致：包 {pkg_hash[:12]}… ≠ "
                        f"HEAD {head_hash[:12]}…（参与比对文件 {len(base['files_compared'])} 个）"),
            "hash": base["canonical_hash"], "hash_files": hash_detail,
            "files_compared": base["files_compared"]})

    if base["reasons"]:
        return {**base, "status": "mismatch"}
    return {**base, "status": "pass"}


def _gate_hash_maps(package_files: dict, head_files: dict) -> tuple:
    """哈希输入两侧准备（口径见模块 docstring）：
    - 剔除树根清单对（manifest.json / plugin.json，走字段 diff / 专用字节比对）；
    - 树根软文档（README.md / CHANGELOG.md）单边缺失 → 告警并双侧剔除（Spec
      决策表：CHANGELOG/README 缺失不阻塞存量包）；双边存在 → 正常参与哈希
      （内容漂移仍可拦截）；
    - 其余文件集合/内容差异全部计入 hash_files 明细（差异即拦截）。
    返回 (pkg_map, head_map, warnings, hash_detail)。
    """
    pkg_map = {k: v for k, v in package_files.items() if k not in ROOT_MANIFEST_NAMES}
    head_map = {k: v for k, v in head_files.items() if k not in ROOT_MANIFEST_NAMES}
    warnings = []
    for doc in SOFT_DOC_FILES:
        in_pkg, in_head = doc in pkg_map, doc in head_map
        if in_pkg != in_head:
            where = "来源仓 HEAD" if in_head else "包内"
            warnings.append(
                f"缺失 HEAD 必备文件告警：{doc} 仅存在于{where}（软文档缺失告警不拦截，"
                f"Spec：CHANGELOG/README 缺失不阻塞存量包，请补齐后再发布）")
            pkg_map.pop(doc, None)
            head_map.pop(doc, None)
    hash_detail = {
        "missing_in_package": sorted(set(head_map) - set(pkg_map)),
        "extra_in_package": sorted(set(pkg_map) - set(head_map)),
        "content_mismatch": sorted(
            k for k in set(pkg_map) & set(head_map) if pkg_map[k] != head_map[k]),
    }
    return pkg_map, head_map, warnings, hash_detail
