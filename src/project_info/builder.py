"""服务端 project-info 生成器（调用 codegraph CLI + GitCode API）。

职责上收落地（见 docs/new-version2/codegraph-builder-plan.md）：
- 服务端统一生成 project-graph.md / project-capabilities.md / .metadata.json；
- 触发：issue_opened / issue_updated 时后台刷新（maybe_refresh_project_info）；
- 目录约定（三目录分离）：
  * bare mirror:  {WORKSPACE_DIR}/{owner}/{repo}/codebase/   （repogit 维护）
  * 分析副本:      {WORKSPACE_DIR}/{owner}/{repo}/project-info/codegraph-repo/  （生成器专用，非 bare）
  * 产物:          {WORKSPACE_DIR}/{owner}/{repo}/project-info/
"""
import json, os, shutil, subprocess, hashlib
from src.config import log, WORKSPACE_DIR
from src.repo_key import split_owner_repo, BASELINE_NAME_MAP

_ANALYSIS_SUBDIR = "codegraph-repo"
_GRAPH_FILE = "project-graph.md"
_CAP_FILE = "project-capabilities.md"
_META_FILE = ".metadata.json"
_GRAPH_TEMPLATE = """# Project Graph: {owner}/{repo}

> Last updated: {updated} | Branches: {branches_list} | Commit: {sha}

## Architecture Overview
{architecture}

## Module Dependency Graph
{modules}

## Core Call Chains
{chains}

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
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _write_to_obs(repo_key, _META_FILE, json.dumps(meta, ensure_ascii=False))
    except Exception as e:
        log.warning(f"project-info meta write failed {repo_key}: {e}")


def _write_file(repo_key: str, name: str, content: str):
    try:
        p = os.path.join(_pi_dir(repo_key), name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
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
        from src.repo_key import knowledge_obs_prefix
        storage = get_storage()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:64]
        file_path = os.path.relpath(os.path.join(_pi_dir(repo_key), name), WORKSPACE_DIR)
        # Issue #95/#190：name 映射为中文展示名（统一常量）
        display_name = BASELINE_NAME_MAP.get(name, name)
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
                c.execute(
                    "UPDATE agent_shared_knowledge SET "
                    "status='accepted', obs_key=?, content_hash=?, created_by='system', "
                    "source='system', name=?, description=? "
                    "WHERE id=?",
                    (storage_key, content_hash, display_name,
                     "codegraph 自动生成的项目基线知识", kid)
                )
            else:
                # 不存在 → INSERT
                c.execute(
                    "INSERT INTO agent_shared_knowledge "
                    "(scope, repo_key, agent_type, issue_num, file_path, status, obs_key, content_hash, created_by, source, name, description) "
                    "VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'system', 'system', ?, ?)",
                    (repo_key, file_path, storage_key, content_hash, display_name,
                     "codegraph 自动生成的项目基线知识")
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
    # bare mirror 不存在 → 主动建立（修复仅 VOD/Issue 仓库永不建 codebase 导致 project-info 永远 degraded 的缺陷）
    if not os.path.isdir(base):
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
        # 刷新 main/master 到最新
        for br in ("origin/main", "origin/master"):
            _run(["git", "-C", work, "fetch", "origin", br], timeout=300)
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


def _codegraph_explore(work: str, query: str, max_files: int = 10) -> str:
    code, out, err = _run(["codegraph", "explore", "--max-files", str(max_files), query], cwd=work, timeout=120)
    return out or err or ""


def _codegraph_files(work: str) -> str:
    code, out, err = _run(["codegraph", "files", "--format", "tree"], cwd=work, timeout=120)
    return out or err or ""


def _codegraph_callees(work: str, symbol: str, limit: int = 10) -> str:
    code, out, err = _run(["codegraph", "callees", "-l", str(limit), symbol], cwd=work, timeout=120)
    return out or err or ""


def _build_graph_md(repo_key: str, meta: dict, arch: str, modules: str, chains: str, key_files: str) -> str:
    owner, rname = split_owner_repo(repo_key)
    return _GRAPH_TEMPLATE.format(
        owner=owner, repo=rname,
        updated=meta.get("last_updated", ""),
        branches_list=", ".join(meta.get("branches_analyzed", [])),
        sha=meta.get("last_commit_sha", ""),
        architecture=arch[:600] or "N/A",
        modules=modules[:600] or "N/A",
        chains=chains[:600] or "N/A",
        branches_desc=meta.get("branches_analyzed", []) or "N/A",
        key_files=key_files[:600] or "N/A",
    )


def _build_capabilities_md(repo_key: str, meta: dict, tech: str, features: str, api: str) -> str:
    owner, rname = split_owner_repo(repo_key)
    return _CAP_TEMPLATE.format(
        owner=owner, repo=rname,
        updated=meta.get("last_updated", ""),
        tech_stack=tech[:600] or "N/A",
        features=features[:600] or "N/A",
        api=api[:600] or "N/A",
        integrations="N/A", skills_agents="N/A", data_model="N/A", config="N/A",
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

    # 获取 main HEAD sha（用于刷新判定）
    try:
        import urllib.request
        from src.repo_key import parse_repo_key
        _provider, _, _ = parse_repo_key(repo_key)
        if _provider == "github":
            from src.user.db import get_github_token
            tok = token or get_github_token(repo_key=repo_key) or ""
            _api_base = "https://api.github.com"
        else:
            from src.user.db import get_gitcode_token
            tok = token or get_gitcode_token(repo_key=repo_key) or ""
            _api_base = "https://gitcode.com/api/v5"
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

    # codegraph 探索（Issue #103：每两步续期锁，防止大仓库 build 超时）
    arch = _codegraph_explore(work, "main entry point application startup", 10)
    modules = _codegraph_explore(work, "module import dependency", 15)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        # 只写本地 meta，不调 _write_meta（避免 _write_to_obs 在无锁状态下写 OBS+DB）
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        try:
            p = os.path.join(_pi_dir(repo_key), _META_FILE)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    chains = _codegraph_explore(work, "API routes endpoints handlers", 10)
    files = _codegraph_files(work)
    if renew_fn and not renew_fn():
        log.warning(f"project-info: lock lost during build for {repo_key}, aborting")
        meta["degraded"] = True
        meta["note"] = "build aborted: distributed lock lost"
        try:
            p = os.path.join(_pi_dir(repo_key), _META_FILE)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"ok": False, "degraded": True, "reason": "lock lost during build"}
    tech = _codegraph_explore(work, "tech stack dependencies", 8)
    features = _codegraph_explore(work, "features capabilities", 8)
    api = _codegraph_explore(work, "API endpoints", 8)

    meta["degraded"] = False
    meta["note"] = ""
    meta["branches_analyzed"] = ["main", "master"]
    _write_meta(repo_key, meta)
    _write_file(repo_key, _GRAPH_FILE, _build_graph_md(repo_key, meta, arch, modules, chains, files))
    _write_file(repo_key, _CAP_FILE, _build_capabilities_md(repo_key, meta, tech, features, api))
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
            from src.repo_key import split_owner_repo, parse_repo_key
            _provider, _, _ = parse_repo_key(repo_key)
            if _provider == "github":
                from src.user.db import get_github_token
                tok = get_github_token(repo_key=repo_key) or ""
                _api_base = "https://api.github.com"
            else:
                from src.user.db import get_gitcode_token
                tok = get_gitcode_token(repo_key=repo_key) or ""
                _api_base = "https://gitcode.com/api/v5"
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
