"""服务端 project-info 生成器（调用 codegraph CLI + GitCode API）。

职责上收落地（见 docs/new-version2/codegraph-builder-plan.md）：
- 服务端统一生成 project-graph.md / project-capabilities.md / .metadata.json；
- 触发：issue_opened / issue_updated 时后台刷新（maybe_refresh_project_info）；
- 目录约定（三目录分离）：
  * bare mirror:  {WORKSPACE_DIR}/{owner}/{repo}/codebase/   （repogit 维护）
  * 分析副本:      {WORKSPACE_DIR}/{owner}/{repo}/project-info/codegraph-repo/  （生成器专用，非 bare）
  * 产物:          {WORKSPACE_DIR}/{owner}/{repo}/project-info/
"""
import json, os, shutil, subprocess, hashlib, time
from src.config import log, WORKSPACE_DIR
from src.localfs import write_text as _atomic_write
from src.repo_key import split_owner_repo, BASELINE_NAME_MAP, BASELINE_DESC_MAP, BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK, knowledge_obs_prefix

_ANALYSIS_SUBDIR = "codegraph-repo"
_GRAPH_FILE = "project-graph.md"
_CAP_FILE = "project-capabilities.md"
_META_FILE = ".metadata.json"

# 各节内容上限（env 可调，便于按库/部署调节 token 预算）
_SECTION_CAP = int(os.environ.get("PROJECT_INFO_SECTION_CAP", "3000"))
_KEY_FILES_CAP = int(os.environ.get("PROJECT_INFO_KEY_FILES_CAP", "8000"))
_KEY_FILES_MAX_DEPTH = int(os.environ.get("PROJECT_INFO_FILES_MAX_DEPTH", "5"))
_GRAPH_VERSION = 2
_GRAPH_TEMPLATE = """# Project Graph: {owner}/{repo}

> Last updated: {updated} | Branches: {branches_list} | Commit: {sha}

## Architecture Overview
{architecture}

## Module Dependency Graph
{modules}

## Core Call Chains
{chains}

## Background Work & Scheduling
{background_jobs}

## Error Handling & Observability
{error_handling}

## Auth & Security
{auth_security}

## Deployment & CI-CD
{deployment}

## Branch Structure
{branches_desc}

## Key Files
{key_files}
"""

_CAP_TEMPLATE = """# Project Capabilities: {owner}/{repo}

> Last updated: {updated}

## Tech Stack
{tech_stack}

## Supported Features
{features}

## API Endpoints
{api}

## External Integrations
{integrations}

## Skills & Agents
{skills_agents}

## Data Model
{data_model}

## Configuration
{config}
"""


def _pi_dir(repo_key: str) -> str:
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "project-info")


def _analysis_dir(repo_key: str) -> str:
    return os.path.join(_pi_dir(repo_key), _ANALYSIS_SUBDIR)


def _repo_base_dir(repo_key: str) -> str:
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "codebase")


def _codegraph_available() -> bool:
    return shutil.which("codegraph") is not None


def _read_meta(repo_key: str) -> dict:
    try:
        p = os.path.join(_pi_dir(repo_key), _META_FILE)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_meta(repo_key: str, meta: dict):
    try:
        p = os.path.join(_pi_dir(repo_key), _META_FILE)
        _atomic_write(p, json.dumps(meta, ensure_ascii=False, indent=2))
        _write_to_obs(repo_key, _META_FILE, json.dumps(meta, ensure_ascii=False))
    except Exception as e:
        log.warning(f"project-info meta write failed {repo_key}: {e}")


def _write_local_meta(repo_key: str, meta: dict):
    """仅写本地 meta（锁丢失/中断时用，不写 OBS+DB 防止无锁状态污染共享）。"""
    try:
        p = os.path.join(_pi_dir(repo_key), _META_FILE)
        _atomic_write(p, json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _write_file(repo_key: str, name: str, content: str):
    try:
        p = os.path.join(_pi_dir(repo_key), name)
        _atomic_write(p, content)
        _write_to_obs(repo_key, name, content)
    except Exception as e:
        log.warning(f"project-info write failed {repo_key}/{name}: {e}")


def _write_to_obs(repo_key: str, name: str, content: str):
    """写 OBS + 落库 baseline 记录。OBS 写失败时只 log warning（降级到本地）。

    Issue #95：补 source='system' / name / description / created_by / OBS key 格式统一。
    """
    import hashlib
    try:
        from src.storage import get_storage
        storage = get_storage()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:64]
        file_path = os.path.relpath(os.path.join(_pi_dir(repo_key), name), WORKSPACE_DIR)
        # Issue #95/#190：name 映射为中文展示名（统一常量）
        display_name = BASELINE_NAME_MAP.get(name, BASELINE_NAME_FALLBACK)
        storage_key = ""
        if storage:
            # Issue #190：OBS key 含 provider 前缀，避免跨 provider 同名仓库冲突
            storage_key = f"{knowledge_obs_prefix(repo_key)}/project-info/{name}"
            storage.save_bytes(storage_key, content.encode("utf-8"))
        # 无论 OBS 是否启用，都写 DB
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # Issue #190：查重改用 name（不受 file_path 格式变化影响）
            existing = c.execute(
                "SELECT id FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='baseline' AND name=? AND archived=0",
                (repo_key, display_name)
            ).fetchone()
            if existing:
                # 已存在 → UPDATE（保留原 id，ref 不失效）
                kid = existing[0]
                _desc = BASELINE_DESC_MAP.get(name, BASELINE_DESC_FALLBACK)
                c.execute(
                    "UPDATE agent_shared_knowledge SET "
                    "status='accepted', obs_key=?, content_hash=?, created_by='system', "
                    "source='system', name=?, description=? "
                    "WHERE id=?",
                    (storage_key, content_hash, display_name,
                     _desc, kid)
                )
            else:
                # 不存在 → INSERT
                _desc = BASELINE_DESC_MAP.get(name, BASELINE_DESC_FALLBACK)
                c.execute(
                    "INSERT INTO agent_shared_knowledge "
                    "(scope, repo_key, agent_type, issue_num, file_path, status, obs_key, content_hash, created_by, source, name, description) "
                    "VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'system', 'system', ?, ?)",
                    (repo_key, file_path, storage_key, content_hash, display_name,
                     _desc)
                )
                kid = c.lastrowid
            db.commit()
            # Issue #95：自动创建 ref（baseline → 全局关联，幂等）
            if kid:
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO scene_knowledge_ref "
                        "(knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
                        "VALUES (?, '', '', 0, 1, 'system')",
                        (kid,)
                    )
                    db.commit()
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"project-info OBS write failed {repo_key}/{name}: {e}")


def _run(cmd: list, cwd: str = None, timeout: int = 300) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def _ensure_analysis_worktree(repo_key: str, token: str = "") -> str | None:
    """确保分析副本为非 bare 工作目录（从 bare mirror checkout 最新 main/master）。"""
    base = _repo_base_dir(repo_key)
    # Issue #200: 每次都刷新 bare mirror refs（ensure_repo_base 内部幂等：已存在只 fetch）
    try:
        from src.runtime.repogit import ensure_repo_base
        base = ensure_repo_base(repo_key, token or "")
    except Exception as e:
        log.warning(f"project-info ensure_repo_base failed {repo_key}: {e}")
    if not base or not os.path.isdir(base):
        return None
    work = _analysis_dir(repo_key)
    try:
        _git_marker = os.path.join(work, ".git")
        if not (os.path.isfile(_git_marker) or os.path.isdir(_git_marker)):
            os.makedirs(work, exist_ok=True)
            # 从 bare mirror 检出（checkout 到分析副本，不 clone 远程，避免重复拉取）
            code, out, err = _run(["git", "--git-dir", base, "worktree", "add", "--force", work, "origin/HEAD"], timeout=900)
            if code != 0:
                # origin/HEAD 可能不存在，尝试 origin/main / origin/master
                for br in ("origin/main", "origin/master"):
                    code, out, err = _run(["git", "--git-dir", base, "worktree", "add", "--force", work, br], timeout=900)
                    if code == 0:
                        break
            if code != 0:
                log.warning(f"project-info analysis worktree add failed {repo_key}: {err[:200]}")
                return None
        # 刷新 main/master 到最新（私有仓库需 token 认证；Issue #200）
        from src.runtime.repogit import _run_git
        for br in ("origin/main", "origin/master"):
            _r = _run_git(["-C", work, "fetch", "origin", br], token=token, timeout=300)
            if _r and _r.returncode != 0:
                log.warning(f"project-info worktree fetch {br} failed {repo_key}: {(_r.stderr or '')[:200]}")
        # codegraph 索引初始化（幂等：已存在则跳过；索引持久化于分析副本 .codegraph/）
        if not os.path.isdir(os.path.join(work, ".codegraph")):
            code, out, err = _run(["codegraph", "init"], cwd=work, timeout=600)
            if code != 0:
                log.warning(f"project-info codegraph init failed {repo_key}: {err[:200]}")
                return None
        return work
    except Exception as e:
        log.warning(f"project-info analysis worktree failed {repo_key}: {e}")
        return None


def _cap(text: str, limit: int) -> str:
    """截断 helper：空串→N/A，超限→带标记（提示用 grape knowledge 拉全量）。"""
    text = (text or "").strip()
    if not text:
        return "N/A"
    if len(text) > limit:
        return text[:limit] + "\n...(已截断，完整内容用 `grape knowledge detail` 获取)"
    return text


def _codegraph_explore(work: str, query: str, max_files: int = 10) -> str:
    code, out, err = _run(["codegraph", "explore", "--max-files", str(max_files), query], cwd=work, timeout=120)
    return out or err or ""


def _codegraph_files(work: str) -> str:
    code, out, err = _run(
        ["codegraph", "files", "--format", "tree", "--max-depth", str(_KEY_FILES_MAX_DEPTH)],
        cwd=work, timeout=120)
    return out or err or ""


def _codegraph_files_section(work: str, directory: str, max_depth: int = 3) -> str:
    """目录过滤子树：探测 skills/ agents/ workflows/ 等目录用（命中才填，无命中返回空）。"""
    if not os.path.isdir(work):
        return ""
    code, out, err = _run(
        ["codegraph", "files", "--format", "tree", "--filter", directory, "--max-depth", str(max_depth)],
        cwd=work, timeout=120)
    return (out or "").strip() if code == 0 and (out or "").strip() else ""


def _codegraph_engine_version() -> str:
    code, out, err = _run(["codegraph", "--version"], timeout=30)
    return (out or err or "").strip()


def _codegraph_callees(work: str, symbol: str, limit: int = 10) -> str:
    code, out, err = _run(["codegraph", "callees", "-l", str(limit), symbol], cwd=work, timeout=120)
    return out or err or ""


def _fetch_branches(repo_key: str, owner: str, rname: str) -> tuple[list, str]:
    """GitCode/GitHub 拉真实分支列表 + 默认分支（best-effort，失败回落空）。"""
    try:
        from src.repo import Repo
        _repo = Repo(repo_key)
        tok = _repo.bot_token
        if not tok:
            return [], ""
        import urllib.request, json as _json
        _api = _repo.api_base
        _headers = {"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(f"{_api}/repos/{owner}/{rname}/branches?per_page=100", headers=_headers)
        data = _json.load(urllib.request.urlopen(req, timeout=20))
        if isinstance(data, dict):
            data = data.get("branches") or []
        branches = []
        for b in data or []:
            if isinstance(b, dict) and b.get("name"):
                branches.append(b["name"])
            elif isinstance(b, str) and b:
                branches.append(b)
        default_branch = ""
        try:
            req2 = urllib.request.Request(f"{_api}/repos/{owner}/{rname}", headers=_headers)
            info = _json.load(urllib.request.urlopen(req2, timeout=20))
            if isinstance(info, dict):
                default_branch = info.get("default_branch") or ""
        except Exception:
            pass
        return branches, default_branch
    except Exception:
        return [], ""


def _build_graph_md(repo_key: str, meta: dict, arch: str, modules: str, chains: str, key_files: str,
                    extra: dict | None = None) -> str:
    owner, rname = split_owner_repo(repo_key)
    extra = extra or {}
    _brs = meta.get("branches_analyzed", []) or []
    branches_desc = "\n".join(f"- {b}" for b in _brs[:30]) if _brs else "N/A"
    if len(_brs) > 30:
        branches_desc += f"\n- ...（共 {len(_brs)} 个分支，仅列前 30）"
    return _GRAPH_TEMPLATE.format(
        owner=owner, repo=rname,
        updated=meta.get("last_updated", ""),
        branches_list=", ".join(_brs),
        sha=meta.get("last_commit_sha", ""),
        architecture=_cap(arch, _SECTION_CAP),
        modules=_cap(modules, _SECTION_CAP),
        chains=_cap(chains, _SECTION_CAP),
        background_jobs=_cap(extra.get("background_jobs"), _SECTION_CAP),
        error_handling=_cap(extra.get("error_handling"), _SECTION_CAP),
        auth_security=_cap(extra.get("auth_security"), _SECTION_CAP),
        deployment=_cap(extra.get("deployment"), _SECTION_CAP),
        branches_desc=branches_desc,
        key_files=_cap(key_files, _KEY_FILES_CAP),
    )


def _build_capabilities_md(repo_key: str, meta: dict, tech: str, features: str, api: str,
                           extra: dict | None = None) -> str:
    owner, rname = split_owner_repo(repo_key)
    extra = extra or {}
    return _CAP_TEMPLATE.format(
        owner=owner, repo=rname,
        updated=meta.get("last_updated", ""),
        tech_stack=_cap(tech, _SECTION_CAP),
        features=_cap(features, _SECTION_CAP),
        api=_cap(api, _SECTION_CAP),
        integrations=_cap(extra.get("integrations"), _SECTION_CAP),
        skills_agents=_cap(extra.get("skills_agents"), _SECTION_CAP),
        data_model=_cap(extra.get("data_model"), _SECTION_CAP),
        config=_cap(extra.get("config"), _SECTION_CAP),
    )


def build_project_info(repo_key: str, token: str = "", lock_key: str = "", renew_fn=None) -> dict:
    """生成/更新某 repo 的 project-info，返回 {ok, degraded, reason}。

    codegraph 可用 → 分析副本 + codegraph explore 生成（degraded=false）；
    codegraph 不可用 → 降级：仅基于 README + GitCode API 的基础信息（degraded=true）。

    Issue #103：lock_key + renew_fn 支持构建过程中锁续期，防止大仓库 build 超时锁过期。
    """
    owner, rname = split_owner_repo(repo_key)
    meta = _read_meta(repo_key)
    now = __import__("datetime").datetime.now().astimezone().isoformat()
    meta["last_updated"] = now
    _build_start = time.perf_counter()
    meta["graph_version"] = _GRAPH_VERSION
    if not meta.get("engine_version"):
        meta["engine_version"] = _codegraph_engine_version()

    # 获取 main HEAD sha（用于刷新判定）
    try:
        import urllib.request
        from src.repo import Repo
        _repo = Repo(repo_key)
        tok = token or _repo.bot_token
        _api_base = _repo.api_base
        if tok:
            req = urllib.request.Request(
                f"{_api_base}/repos/{owner}/{rname}/commits?per_page=1",
                headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
            import json as _json
            commits = _json.load(urllib.request.urlopen(req, timeout=20))
            if commits:
                meta["last_commit_sha"] = commits[0].get("sha", meta.get("last_commit_sha", ""))
    except Exception:
        pass

    if not _codegraph_available():
        log.warning(f"project-info: codegraph unavailable for {repo_key}, degraded")
        meta["degraded"] = True
        meta["note"] = "codegraph unavailable, generated from README + GitCode API"
        _write_meta(repo_key, meta)
        _write_file(repo_key, _GRAPH_FILE, _build_graph_md(repo_key, meta, "codegraph 不可用，降级生成", "N/A", "N/A", "N/A"))
        _write_file(repo_key, _CAP_FILE, _build_capabilities_md(repo_key, meta, "N/A", "N/A", "N/A"))
        return {"ok": True, "degraded": True, "reason": "codegraph unavailable"}

    work = _ensure_analysis_worktree(repo_key, token)
    if not work:
        meta["degraded"] = True
        meta["note"] = "analysis worktree prepare failed"
        _write_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "analysis worktree failed"}

    # codegraph 探索（Issue #103：每几步续期锁，防止大仓库 build 超时）
    arch = _codegraph_explore(work, "main entry point application startup", 10)
    modules = _codegraph_explore(work, "module import dependency", 15)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        # 只写本地 meta，不调 _write_meta（避免 _write_to_obs 在无锁状态下写 OBS+DB）
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        _write_local_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    chains = _codegraph_explore(work, "API routes endpoints handlers", 10)
    files = _codegraph_files(work)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        _write_local_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    bg = _codegraph_explore(work, "background jobs scheduled tasks cron workers queue", 8)
    errh = _codegraph_explore(work, "error handling logging observability middleware", 8)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        _write_local_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    auth = _codegraph_explore(work, "authentication authorization security guard middleware", 8)
    deploy = _codegraph_explore(work, "deployment build ci cd dockerfile container k8s", 6)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        _write_local_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    tech = _codegraph_explore(work, "tech stack dependencies", 8)
    features = _codegraph_explore(work, "features capabilities", 8)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        _write_local_meta(repo_key, meta)
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    api = _codegraph_explore(work, "API endpoints", 8)
    integ = _codegraph_explore(work, "external integrations third-party sdk api webhook oauth sso", 8)
    dmodel = _codegraph_explore(work, "data model entities database tables orm schema migration models", 8)
    cfg = _codegraph_explore(work, "configuration settings env vars feature flags config", 8)

    # Skills & Agents：目录探测（命中才填，无命中 N/A）
    sk_bits = []
    for _d in ("skills/", "agents/", "workflows/"):
        _sub = _codegraph_files_section(work, _d)
        if _sub:
            sk_bits.append(f"### {_d}\n{_sub}")
    skagents = "\n\n".join(sk_bits)

    branches, default_branch = _fetch_branches(repo_key, owner, rname)
    meta["branches_analyzed"] = branches or ["main", "master"]
    if default_branch:
        meta["default_branch"] = default_branch

    meta["degraded"] = False
    meta["note"] = ""
    meta["build_ms"] = int((time.perf_counter() - _build_start) * 1000)
    meta["sections_char_counts"] = {
        "graph_arch": len(arch), "graph_modules": len(modules), "graph_chains": len(chains),
        "graph_bg": len(bg), "graph_err": len(errh), "graph_auth": len(auth), "graph_deploy": len(deploy),
        "graph_files": len(files),
        "caps_tech": len(tech), "caps_features": len(features), "caps_api": len(api),
        "caps_integ": len(integ), "caps_dmodel": len(dmodel), "caps_cfg": len(cfg),
    }
    _write_meta(repo_key, meta)
    graph_extra = {"background_jobs": bg, "error_handling": errh, "auth_security": auth, "deployment": deploy}
    caps_extra = {"integrations": integ, "skills_agents": skagents, "data_model": dmodel, "config": cfg}
    _write_file(repo_key, _GRAPH_FILE, _build_graph_md(repo_key, meta, arch, modules, chains, files, graph_extra))
    _write_file(repo_key, _CAP_FILE, _build_capabilities_md(repo_key, meta, tech, features, api, caps_extra))
    log.info(f"project-info regenerated for {repo_key} (codegraph)")
    return {"ok": True, "degraded": False, "reason": "codegraph"}


def should_refresh(repo_key: str) -> bool:
    """更新条件：文件缺失 / commit 变化 / 超 7 天 / degraded 标记（且 codegraph 现在可用）。"""
    meta = _read_meta(repo_key)
    pi = _pi_dir(repo_key)
    if not os.path.isfile(os.path.join(pi, _GRAPH_FILE)) or not os.path.isfile(os.path.join(pi, _CAP_FILE)):
        return True
    if meta.get("degraded") and _codegraph_available():
        return True
    # 检查远程 commit 是否变化（比 7 天过期更及时）
    last_sha = meta.get("last_commit_sha", "")
    if last_sha:
        try:
            import urllib.request, json as _json
            from src.repo_key import split_owner_repo
            from src.repo import Repo
            _repo = Repo(repo_key)
            tok = _repo.bot_token
            _api_base = _repo.api_base
            if tok:
                owner, rname = split_owner_repo(repo_key)
                req = urllib.request.Request(
                    f"{_api_base}/repos/{owner}/{rname}/commits?per_page=1",
                    headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
                commits = _json.load(urllib.request.urlopen(req, timeout=20))
                if commits and commits[0].get("sha", "") != last_sha:
                    return True
        except Exception:
            pass
    last = meta.get("last_updated", "")
    if last:
        try:
            import datetime
            lu = datetime.datetime.fromisoformat(last)
            if (datetime.datetime.now(lu.tzinfo if lu.tzinfo else None) - lu).days > 7:
                return True
        except Exception:
            pass
    return False


def maybe_refresh_project_info(repo_key: str, token: str = ""):
    """webhook 触发的后台刷新（不阻塞调用方）。分布式锁跨实例互斥（Issue #103）。"""
    if not repo_key:
        return
    from src.lock import acquire_lock, release_lock, renew_lock
    lock_key = f"project_info_build:{hashlib.md5(repo_key.encode()).hexdigest()[:16]}"
    if not acquire_lock(lock_key, ttl=300):
        log.info(f"project-info: lock contention for {repo_key}, skipped")
        return
    try:
        if should_refresh(repo_key):
            log.info(f"project-info refresh triggered for {repo_key}")
            build_project_info(repo_key, token, lock_key=lock_key, renew_fn=lambda: renew_lock(lock_key, ttl=300))
    except Exception as e:
        log.warning(f"project-info refresh failed {repo_key}: {e}")
    finally:
        release_lock(lock_key)
