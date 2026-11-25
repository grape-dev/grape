"""官方场景定时同步模块（issue-11-scene-skill-market-tech-design.md §8.6）。

职责：
1. 每 30 分钟从 grape-skills 仓库下载 ZIP 归档包并解压 scenes/ 目录
2. 遍历每个场景目录，读 scene.json + README.md
3. 校验 nodes 合法性 + 预检 skill 引用
4. content_hash 增量比对：hash 不变跳过，变了按 version 比对更新
5. 下架通过管理员手动操作（不再自动下架）

触发方式：
- asyncio 定时任务（启动即执行，之后每 30 分钟）
- 失败不阻塞服务，打 WARNING 日志
"""
import asyncio

def _repo_root(start: str) -> str:
    """向上找仓库根（含 configs/ 或 scripts/ 的目录），模块搬迁后仍稳。"""
    cur = os.path.dirname(os.path.abspath(start))
    while cur and cur != "/":
        if os.path.isdir(os.path.join(cur, "configs")) or os.path.isdir(os.path.join(cur, "scripts")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(os.path.abspath(start))

import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile

import httpx
from src.kernel.category import canonical_category_slug
from src.kernel.config import log
from src.kernel.owner_db import db_pair as _db_pair

logger = logging.getLogger(__name__)

def _load_config():
    """加载场景同步配置：优先环境变量，回退 configs/scene-sync.json。

    R31 决策 A：SCENE_SYNC_INTERVAL 随 standalone worker 一并移除（合并后周期由
    SKILL_SYNC_INTERVAL 控制）。为兼容存量配置文件，仍容忍读取该键但仅记提示。
    """
    repo_url = os.environ.get("GRAPESKILLS_REPO_URL", "")
    repo_branch = os.environ.get("GRAPESKILLS_REPO_BRANCH", "")

    # 读 configs/scene-sync.json（环境变量优先覆盖）
    try:
        cfg_path = os.path.join(_repo_root(__file__), "configs", "scene-sync.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not repo_url:
                repo_url = cfg.get("repo_url", "")
            if not repo_branch:
                repo_branch = cfg.get("repo_branch", "")
            # 兼容提示：旧配置的 interval 键已失效（R31 合并后由 SKILL_SYNC_INTERVAL 控制）
            if cfg.get("sync_interval") or os.environ.get("SCENE_SYNC_INTERVAL"):
                log.warning("Scene sync: sync_interval/SCENE_SYNC_INTERVAL is obsolete "
                            "(R31 merged scheduler); use SKILL_SYNC_INTERVAL instead")
    except Exception:
        pass

    # 兜底默认值
    if not repo_url:
        repo_url = "https://gitcode.com/grape-dev/grape-skills"
    if not repo_branch:
        repo_branch = "main"

    return repo_url, repo_branch


_REPO_URL, _REPO_BRANCH = _load_config()
def _parse_repo_host_path(repo_url: str) -> tuple:
    """R31 开源化：解析 repo_url → (下载基址, owner/repo)。

    支持 gitcode.com 与 github.com（grape-skills 将开源到 GitHub）：
    - https://gitcode.com/{owner}/{repo}  → raw.gitcode.com
    - https://github.com/{owner}/{repo}   → codeload.github.com
    未识别的 host 回退 gitcode 语义（历史行为）。
    """
    from urllib.parse import urlparse
    p = urlparse(repo_url)
    host = (p.netloc or "").lower()
    path = p.path.strip("/")
    # D16：下载基址优先按平台卡（host 匹配卡 → 卡 download_base；新平台自动正确）
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card, list_vcs_platforms
        for _prov in list_vcs_platforms():
            _card = get_platform_card(_prov)
            if _card is not None and (host == _card.host or host.endswith("." + _card.host)):
                if _card.download_base:
                    return _card.download_base, path
                break
    except Exception:
        pass
    if host.endswith("github.com"):
        return "https://codeload.github.com", path
    if host.endswith("gitcode.com"):
        return "https://raw.gitcode.com", path
    # 回退：未知 host 走旧 gitcode raw 行为，但显式告警（不再静默回退错误平台）
    from src.kernel.config import log as _log
    _log.warning(f"scene_sync: unknown repo host {host!r}, falling back to gitcode raw semantics")
    return "https://raw.gitcode.com", repo_url.replace("https://gitcode.com/", "").rstrip("/")


_DL_BASE, _OWNER_REPO = _parse_repo_host_path(_REPO_URL)

_VALID_TAGS = {"vod", "issue", "coding", "qa", "sre", "chat"}
_VALID_MODES = {"emit"}

# category slug → id 缓存（每次同步刷新）
_cat_cache: dict[str, int] = {}
# Issue #2761：统一分类字典 slug → {id, level, parent_id}（一级/二级）
_category_dict_cache: dict[str, dict] = {}


def _refresh_category_dict_cache() -> None:
    """刷新统一分类字典缓存（category_dict 归 mgmt 域，走 owner 域 public）。"""
    _category_dict_cache.clear()
    try:
        from src.domains.mgmt import public as _mgmt_public_dict  # noqa: PLC0415
        _category_dict_cache.update(_mgmt_public_dict.category_dict_active_map("scene") or {})
    except Exception as exc:  # noqa: BLE001 - 字典不可用只降级为「不写分类」
        logger.warning(f"Scene sync: category_dict map unavailable: {exc}")


def _record_unresolved(sink: dict, slug: str, scene_name: str) -> None:
    """按同步轮聚合未命中 slug（Issue #3796）：slug → {count, scenes 样例 ≤3}。

    循环内的未命中只降 debug 进 sink，轮末由调用方发 1 条聚合 WARNING，
    替代「每场景每 tick 一条」的刷屏（原 696 条/小时）。
    """
    entry = sink.setdefault(slug, {"count": 0, "scenes": []})
    entry["count"] += 1
    if scene_name and scene_name not in entry["scenes"] and len(entry["scenes"]) < 3:
        entry["scenes"].append(scene_name)


def _resolve_scene_category(scene_data: dict, miss_sink: dict | None = None,
                            scene_name: str = "") -> tuple[int, int]:
    """按场景 JSON 的分类 slug 解析 (cat_l1_id, cat_l2_id)。

    解析口径统一在 src.kernel.category（与 skill_sync 共用）；未命中返回 (0, 0)，
    由调用方保留库内旧值（不写 0 制造脏数据）。Issue #3796：
    - legacy 别名在 lookup_slug 内生效（skill-dev/iac/general 等落收敛后 1xxx）；
    - miss_sink 提供时未命中降 debug + 进 sink（轮末聚合告警），缺省保持原 WARNING。
    """
    from src.kernel.category import resolve_category_slugs  # noqa: PLC0415

    def _on_miss(slug: str) -> None:
        _record_unresolved(miss_sink, slug, scene_name)  # type: ignore[arg-type]

    return resolve_category_slugs(
        scene_data, _category_dict_cache, logger=logger, label="Scene sync",
        on_miss=_on_miss if miss_sink is not None else None)


def _resolve_scene_category_id(scene_data: dict, miss_sink: dict | None = None,
                               scene_name: str = "") -> int:
    """按 scene.json 分类解析 `category_id`（scene_category 旧表口径）。

    与 `_resolve_scene_category` 同口径：先直查后 legacy 别名；`category` 为 path 形态
    （"l1/l2"）时**逐段解析**并取最深的命中段——原实现用整串查 `_cat_cache`，path 形态
    恒未命中、每轮进聚合告警（PR #1982 审查 Optional）。全部未命中返回 0，调用方保留
    库内旧值；`miss_sink` 提供时进轮级聚合（键取整串，与 category_dict 路径口径一致），
    缺省保持原 WARNING（与 category_dict 路径对称）。
    """
    from src.kernel.category import scene_category_slug_values  # noqa: PLC0415

    info = scene_data if isinstance(scene_data, dict) else {}
    category_id = 0
    for value in scene_category_slug_values(info) or ["general"]:
        hit = _cat_cache.get(value, 0)
        if not hit:
            aliased = canonical_category_slug(value)
            if aliased != value:
                hit = _cat_cache.get(aliased, 0)
        if hit:
            category_id = hit  # 逐段覆盖：path 形态下更具体的段优先

    if category_id:
        return category_id
    raw = str(info.get("category") or "general").strip() or "general"
    if miss_sink is not None:
        _record_unresolved(miss_sink, raw, scene_name)
        logger.debug(f"Scene sync: {scene_name} category slug '{raw}' "
                     f"unresolved in scene_category (aggregated per round)")
    else:
        logger.warning(f"Scene sync: {scene_name} category slug '{raw}' "
                       f"not found in scene_category, category_id=0 (keep existing)")
    return 0


def _compute_scene_hash(scene_dir: str) -> str:
    """计算场景目录的内容 hash（遍历所有文件，按文件名排序，取完整 SHA-256）。"""
    if not os.path.isdir(scene_dir):
        return ""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(scene_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "__MACOSX", ".git")]
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), scene_dir)
            h.update(rel.encode())
            try:
                with open(os.path.join(root, f), "rb") as fp:
                    h.update(fp.read())
            except Exception:
                pass
    return h.hexdigest()


def _refresh_cat_cache():
    """刷新 category slug → id 缓存。"""
    _cat_cache.clear()
    # scene_category / skill 都归 runtime 域：本模块被 mgmt 进程的定时器调用时，
    # 直连进程库会 1146（三库拓扑），按 owner 域解析连接（单库拓扑回落进程库）。
    db, db_lock = _db_pair("runtime")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, slug FROM scene_category WHERE is_active=1")
        for row in c.fetchall():
            if row[1]:
                _cat_cache[row[1]] = row[0]


def _validate_scene_nodes(nodes: list) -> list[str]:
    """校验 nodes 合法性，返回错误列表。空列表表示通过。"""
    errors = []
    if not nodes or len(nodes) < 2:
        return ["nodes 至少 2 个节点"]

    node_ids = set()
    for n in nodes:
        nid = n.get("id", "")
        if not nid:
            errors.append("node 缺少 id")
        if nid in node_ids:
            errors.append(f"node id 重复: {nid}")
        node_ids.add(nid)

        tag = n.get("tag", "")
        if tag not in _VALID_TAGS:
            errors.append(f"node {nid}: tag 不合法 '{tag}'，合法值: {', '.join(sorted(_VALID_TAGS))}")

        mode = n.get("mode", "")
        if mode not in _VALID_MODES:
            errors.append(f"node {nid}: mode 不合法 '{mode}'，合法值: {', '.join(sorted(_VALID_MODES))}")

        # Issue #1089 挂点①：node.plugins 多选（数组，元素为非空插件名，节点内去重）
        plugins = n.get("plugins")
        if plugins is not None:
            if not isinstance(plugins, list):
                errors.append(f"node {nid}: plugins 必须是数组")
            else:
                seen_plugins: set[str] = set()
                for p in plugins:
                    if not isinstance(p, str) or not p.strip():
                        errors.append(f"node {nid}: plugins 含非法条目（须为非空插件名）")
                        continue
                    if p in seen_plugins:
                        errors.append(f"node {nid}: plugins 重复: {p}")
                        continue
                    seen_plugins.add(p)

        handoff = n.get("handoff", "")
        nxt = n.get("next", "")
        if handoff == "complete" and nxt != "done":
            errors.append(f"node {nid}: handoff=complete 但 next='{nxt}'，应为 'done'")
        if handoff == "a2a" and nxt == "done":
            errors.append(f"node {nid}: handoff=a2a 但 next='done'，应使用 handoff=complete")

    # 链路完整性
    first = nodes[0].get("id", "")
    current = first
    visited = set()
    while current != "done":
        if current in visited:
            errors.append("next 链路存在循环")
            break
        visited.add(current)
        node = next((n for n in nodes if n.get("id") == current), None)
        if not node:
            errors.append(f"next 链路断链: 找不到节点 '{current}'")
            break
        current = node.get("next", "")
        if not current:
            errors.append(f"节点 '{node.get('id')}' 缺少 next")
            break
    else:
        if current != "done":
            errors.append("next 链路未走到 done")

    return errors


def _check_skills_exist(nodes: list, scene_name: str) -> list[str]:
    """预检 skill 引用是否在 skill 表中存在。返回不存在的 skill 名列表。

    P46.2：与 _rebuild_scene_skill_ref 统一口径（is_active=1 且 scope ∈ official/repo），
    消除"预检通过但 skill_id=0 写入"的口径不一致。
    """
    missing = []
    skill_names = set(n.get("skill", "") for n in nodes if n.get("skill", ""))
    if not skill_names:
        return missing
    existing = _query_active_skill_names(skill_names)
    for sn in skill_names:
        if sn and sn not in existing:
            missing.append(sn)
            logger.warning(f"场景 {scene_name} 引用了不存在的技能: {sn}")
    return missing


def _query_active_skill_names(skill_names: set) -> set:
    """P46.2 共享查询：is_active=1 且 scope ∈ ('official','repo') 的 skill 名集合。

    _check_skills_exist 与 _rebuild_scene_skill_ref 共用，保证口径一致。
    """
    db, db_lock = _db_pair("runtime")
    if not skill_names:
        return set()
    with db_lock:
        c = db.cursor()
        placeholders = ",".join("?" * len(skill_names))
        c.execute(
            f"SELECT DISTINCT skill_name FROM skill "
            f"WHERE skill_name IN ({placeholders}) AND is_active=1 "
            f"AND scope IN ('official','repo')",
            list(skill_names))
        return set(r[0] for r in c.fetchall())


def _parse_version(v: str) -> tuple:
    """将语义化版本号转为可比较的元组（补齐到 3 位）。"""
    try:
        parts = [int(x) for x in v.split(".")]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except Exception:
        return (0, 0, 0)


def _get_sync_token() -> str:
    """获取同步用 GitCode token，优先级：GRAPE_DEV_TOKEN 环境变量 → configs/scene-sync.json 的 gitcode_api_token。"""
    # 1. 环境变量
    token = os.environ.get("GRAPE_DEV_TOKEN", "")
    if token:
        return token
    # 2. configs/scene-sync.json
    try:
        cfg_path = os.path.join(_repo_root(__file__), "configs", "scene-sync.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                token = json.load(f).get("gitcode_api_token", "")
            if token:
                return token
    except Exception:
        pass
    return ""


async def _download_repo_zip(client: httpx.AsyncClient, token: str) -> str:
    """下载仓库 ZIP 归档并解压到临时目录，返回解压后的本地根路径。

    URL: gitcode: https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
         github:  https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}
    （R31 开源化：grape-skills 同步源支持 GitHub；两个平台 ZIP 顶层目录均为 {repo}-{branch}）
    认证: Authorization: Bearer ***（gitcode 私仓用；GitHub 公开仓库匿名即可）
    """
    if _DL_BASE.endswith("codeload.github.com"):
        url = f"{_DL_BASE}/{_OWNER_REPO}/zip/refs/heads/{_REPO_BRANCH}"
        # GitHub codeload 对任何 Authorization Bearer header 一律返回 404（实测），
        # 公开仓库必须匿名下载；GitCode PAT 对 GitHub 无效且致命。S16/UC16 修复。
        headers = {}
    else:
        url = f"{_DL_BASE}/{_OWNER_REPO}/archive/refs/heads/{_REPO_BRANCH}.zip"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
    resp = await client.get(url, headers=headers, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    data = resp.content
    if not data or len(data) < 100:
        raise ValueError(f"ZIP download returned empty or too small ({len(data)} bytes)")

    tmp_dir = tempfile.mkdtemp(prefix="scene_sync_zip_")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(tmp_dir)

    # ZIP 顶层目录名: {repo}-{branch}
    repo_name = _OWNER_REPO.split("/")[-1]
    top_dir = f"{repo_name}-{_REPO_BRANCH}"
    repo_root = os.path.join(tmp_dir, top_dir)
    if not os.path.isdir(repo_root):
        # 兜底：找解压目录下第一个子目录
        entries = os.listdir(tmp_dir)
        if entries and len(entries) == 1:
            repo_root = os.path.join(tmp_dir, entries[0])
        else:
            raise FileNotFoundError(f"ZIP top-level directory '{top_dir}' not found in extracted files")

    return repo_root


def _declares_skill_nodes(nodes: list) -> bool:
    """模板是否声明了技能节点（用于区分「解析失败」与「本来就没有技能」两种空集）。

    `refs` 为空时语义分两种：模板从未声明技能 → 清空引用正确；声明了但解析不出来
    （技能同步未就绪）→ 必须保留既有绑定，等下一轮自愈。
    """
    return any(str(n.get("skill") or "").strip() for n in (nodes or []))


def _rebuild_scene_skill_ref(market_id: int, nodes: list, scene_name: str = "",
                             repo_key: str = ""):
    """重建 scene_skill_ref（DELETE + INSERT）。

    解析顺序（2026-11-13 修，三档同名不再错配）：
      1. 节点带 skill_id 且 is_active=1 → **直接用它**（Issue #979 权威语义：
         三档同名时锁定用户实际选中的那一条）；
      2. 否则按名字回落：repo_key 给定先查本仓 repo 档（owner_id=repo_key），再 official，
         再任意 repo，最后 user（user 场景/市场模板才可能命中）。

    与原实现的两处差异：
      · 不再无条件按名字重解析——那会把用户选的 repo/user 档覆盖成 official 档；
      · repo 档查询带 owner_id=repo_key 过滤——避免跨仓同名技能被本仓场景认领。
    """
    db, db_lock = _db_pair("runtime")
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_ssr
    from src.domains.mgmt import public as _mgmt_public_ssr
    _mg_ssr = _mgmt_lib_ssr(foreign_only=True)
    refs: list = []
    with db_lock:
        c = db.cursor()
        for n in nodes:
            skill_name = (n.get("skill", "") or "").strip()
            try:
                skill_id = int(n.get("skill_id") or 0)
            except (TypeError, ValueError):
                skill_id = 0
            if not skill_name and not skill_id:
                continue

            resolved = 0
            if skill_id:
                row = c.execute(
                    "SELECT id FROM skill WHERE id=? AND is_active=1", (skill_id,)).fetchone()
                if row:
                    resolved = skill_id
                else:
                    logger.warning(
                        f"Scene sync: {scene_name} node skill_id={skill_id} inactive/missing, "
                        f"fallback to name '{skill_name}'")
            if not resolved and skill_name:
                if repo_key:
                    row = c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 "
                        "AND scope='repo' AND owner_id=? LIMIT 1",
                        (skill_name, repo_key)).fetchone()
                    resolved = row[0] if row else 0
                if not resolved:
                    row = c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 "
                        "AND scope='official' LIMIT 1", (skill_name,)).fetchone()
                    resolved = row[0] if row else 0
                if not resolved:
                    row = c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 "
                        "AND scope='repo' LIMIT 1", (skill_name,)).fetchone()
                    resolved = row[0] if row else 0
                if not resolved:
                    row = c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 "
                        "AND scope='user' LIMIT 1", (skill_name,)).fetchone()
                    resolved = row[0] if row else 0
            if resolved == 0:
                # 不写 0：读取侧会把 skill_id=0 当成「技能已删除」，且这条绑定永远不会回填。
                # 宁可本轮少一条引用（下一轮同步会补齐），也不要落一条无效绑定。
                logger.warning(
                    f"Scene sync: {scene_name} skill '{skill_name}' not found in active skills, ref skipped")
                continue
            refs.append({"skill_id": int(resolved), "skill_name": skill_name})
    if not refs:
        # 两种「空集」必须分开：
        #   ① 模板本来就没有技能节点 → 清空引用是正确语义（继续往下走 replace）；
        #   ② 声明了技能但一个都没解析出来 → 清空会让场景彻底失去引用，保留原状等下一轮自愈。
        if _declares_skill_nodes(nodes):
            logger.warning(
                f"Scene sync: {scene_name} none of the declared skill(s) resolved, "
                f"existing bindings left untouched")
            return
        logger.info(f"Scene sync: {scene_name} declares no skill nodes, clearing scene_skill_ref")
    if _mg_ssr is not None:
        # 三库拓扑：一条幂等意图进 outbox（远端失败可重放），本进程不直写外域表
        _write_scene_skill_ref_via_lib(_mg_ssr, market_id, refs, scene_name)
    else:
        # 单库/测试拓扑：DELETE + INSERT 由 mgmt public 在同一事务里完成
        # （解析完成后再重建，避免先删后远端失败留下空引用——与原 lib 路径同口径）
        _mgmt_public_ssr.replace_scene_skill_ref_any(market_id, refs)


def _write_scene_skill_ref_via_lib(_mg, market_id: int, refs: list, scene_name: str = "") -> None:
    """lib 模式重建 scene_skill_ref：一条幂等意图，payload 带完整 refs（可重放）。"""
    from src.kernel import cross_write as _xw
    _ordered = sorted(refs, key=lambda r: (int(r.get("skill_id") or 0), str(r.get("skill_name") or "")))
    _sig = hashlib.sha1(json.dumps(_ordered, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    _out: dict = {}
    try:
        _res = _xw.submit(
            "mgmt", "scene_skill_ref_replace",
            _xw.unique_key(f"scene-skill-ref:{int(market_id)}:{_sig}"),
            {"scene_id": int(market_id), "refs": refs},
            lambda: _out.update(_mg.replace_scene_skill_ref(int(market_id), refs)))
        if _res.get("status") == "failed":
            logger.warning(f"Scene sync: {scene_name} scene_skill_ref queued for retry: "
                           f"{_res.get('error')}")
        else:
            logger.info(f"Scene sync: {scene_name} scene_skill_ref rebuilt via lib "
                        f"(deleted={_out.get('deleted')}, inserted={_out.get('inserted')})")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Scene sync: {scene_name} scene_skill_ref lib path failed: {e}")


def _rebuild_scene_skill_ref_by_name(scene_name: str, nodes: list, repo_key: str = ""):
    """通过场景名查 market_id（official 模板）后重建 scene_skill_ref。

    id 查询走 mgmt public 的任意拓扑具名查询：有自有 DSN 走 mgmt 连接，单库/测试
    回落进程主库，同一份 SQL 两条拓扑都能执行，调用方不再自带兜底 SQL。
    """
    from src.domains.mgmt import public as _mgmt_public
    _scene_id = _mgmt_public.official_scene_id_any(scene_name)
    if _scene_id:
        _rebuild_scene_skill_ref(int(_scene_id), nodes, scene_name, repo_key)


def _repair_scene_skill_ref_if_unresolved(scene_name: str, market_id: int, nodes: list,
                                          repo_key: str = "") -> None:
    """仅当 scene_skill_ref 里存在未解析引用（skill_id <= 0）时重建绑定。

    场景定义没变时同步会 skipped 并跳过重建，于是**首次同步写入的 skill_id=0 会永久留存**
    （典型成因：容器首次启动时技能库还没就绪，或跨境拉取技能源失败）。
    这里每轮做一次零成本检查，让存量坏数据在下一轮同步自愈；没有坏数据时不写库。
    """
    if not market_id:
        return
    # scene_skill_ref 归属 mgmt 域（configs/table_ownership.json），runtime **不得直读**：
    # 三库拓扑走 mgmt lib，单库/测试拓扑经 mgmt public 原语（此时本进程即 mgmt 持有者，
    # 不是跨域读）。直读会触发跨域读棘轮门禁，且三库实例无跨域视图时 SELECT 会 1146 被吞。
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_rep
    from src.domains.mgmt import public as _mgmt_public_rep
    _mg_rep = _mgmt_lib_rep(foreign_only=True)
    try:
        if _mg_rep is not None:
            unresolved = int(_mg_rep.count_unresolved_scene_skill_ref(int(market_id)) or 0)
        else:
            unresolved = int(_mgmt_public_rep.count_unresolved_scene_skill_ref(int(market_id)) or 0)
    except Exception as exc:  # noqa: BLE001 - 检查失败不影响同步主流程
        logger.warning(f"Scene sync: {scene_name} skill ref check failed: {exc}")
        return
    if unresolved:
        logger.info(f"Scene sync: {scene_name} has {unresolved} unresolved skill ref(s), rebuilding bindings")
        _rebuild_scene_skill_ref(int(market_id), nodes, scene_name, repo_key)


def _init_featured_scenes_if_empty():
    """精选场景初始化：如果 official 场景没有任何 is_featured=1 的，设置默认精选。

    独立于远端同步——即使没有 token 无法拉远端场景包，本地已有 official 场景也需要精选标记。
    """
    default_featured = ("default", "cloud-native", "iac")
    from src.kernel.domain_read import mgmt_lib as _mg_lib_feat
    from src.domains.mgmt import public as _mgmt_public_feat
    _mg_feat = _mg_lib_feat()
    # 计数走任意拓扑具名查询：不再为"单库拓扑"另留一份 COUNT/UPDATE 兜底 SQL
    featured_count = _mgmt_public_feat.count_featured_official_scenes_any()
    if featured_count:
        log.info(f"Scene sync: {featured_count} featured scenes already exist, skip init")
        return
    if _mg_feat is not None:
        from src.kernel import cross_write as _xw_feat
        _xw_feat.submit("mgmt", "scene_featured_init", "scene-featured-init:v1",
                        {"names": list(default_featured)},
                        lambda: _mg_feat.mark_default_featured(list(default_featured)))
        log.info(f"Scene sync: initialized featured scenes (lib): {', '.join(default_featured)}")
        return
    _mgmt_public_feat.mark_default_featured_any(list(default_featured))
    log.info(f"Scene sync: initialized featured scenes: {', '.join(default_featured)}")


async def _download_repo_zip_with_retry(client: httpx.AsyncClient, token: str) -> str:
    """R31 §8.7 网络重试：3 次指数退避（5/15/45s），仅网络类异常重试。

    4xx（认证/配置类）快速失败——重试坏 token 只产生噪音。
    幂等前提：ZIP 下载是只读操作，重试无副作用。
    """
    delays = [5, 15, 45]
    last_exc = None
    for attempt in range(len(delays) + 1):
        try:
            return await _download_repo_zip(client, token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # 4xx 快速失败
            last_exc = e
        except (httpx.TransportError, httpx.TimeoutException, OSError) as e:
            last_exc = e
        if attempt < len(delays):
            delay = delays[attempt]
            log.warning(f"Scene sync: download attempt {attempt + 1} failed ({last_exc}), retrying in {delay}s")
            await asyncio.sleep(delay)
    raise last_exc


def _detect_stale_official_scenes(repo_scene_names: set, traversal_complete: bool) -> int:
    """P46.1 stale 检测（只读，铁律 1.0：零 DB 写）。

    DB 中 source='official' 但仓库已不存在的行 → warning + 计数，供管理员发现；
    下架/清理由管理台管理员手工执行。完成守卫仅护报告准确性：遍历不完整时
    跳过本轮（不出具可能误报的名单），最坏后果是"本轮没有报告"。
    """
    if not traversal_complete:
        logger.warning("Scene sync: traversal incomplete, skipping stale detection this cycle")
        return 0
    try:
        from src.domains.mgmt import public as _mgmt_public_stale
        # 原实现在「无 mgmt_lib 但有 foreign_only 句柄」这条分支上把 fetchall() 的结果
        # 赋给了游标变量、却没给 _rows_stale 赋值 → NameError 被外层 except 吞掉，
        # 表现为 stale 检测永久静默归零。改成单一具名查询后该分支不存在了。
        _rows_stale = _mgmt_public_stale.list_official_scene_ids_names_any()
        stale = [name for (_id, name) in _rows_stale if name not in repo_scene_names]
    except Exception:
        logger.exception("Scene sync: stale detection query failed")
        return 0
    if stale:
        logger.warning(
            f"Scene sync: stale official scenes (removed from repo, "
            f"admin should deactivate manually): {', '.join(stale)}")
    return len(stale)


async def sync_official_scenes(repo_root: str | None = None):
    """同步官方场景从 grape-skills 仓库到 scene_market 表。

    R31 合并路径：skill_sync 串行调用时传入 repo_root（已下载解压的仓库根目录），
    跳过下载、复用文件，临时目录由 caller 负责清理。
    standalone 路径：repo_root=None，自行下载并清理自己的临时目录。

    铁律 1.0（需求 §1.0）：本函数对生产数据只做幂等 INSERT/UPDATE，无任何
    删除/停用语义；stale 官方场景只读检测+报告，处置权在管理台管理员。
    """
    log.info(f"Scene sync started: {_REPO_URL} branch={_REPO_BRANCH} (ZIP archive, repo_root={'provided' if repo_root else 'standalone'})")

    tmp_dir = None  # 仅 standalone 模式（本函数自行下载）时记录，用于 finally 清理
    try:
        if repo_root is None:
            # ---- standalone 模式：自行下载（R31 §8.7 网络重试包裹）----
            bot_token = _get_sync_token()
            # R25.6: 公开仓库匿名即可下载，token 非硬前提。空 token 仍发起下载（headers 为空），
            # 下载返回 401/403 时才降级到 _init_featured_scenes_if_empty() 兜底。
            async with httpx.AsyncClient(timeout=60.0) as client:
                # 1. 下载 ZIP 归档并解压（3 次指数退避，网络类异常重试）
                try:
                    repo_root = await _download_repo_zip_with_retry(client, bot_token)
                    tmp_dir = repo_root  # P45 修复：标记自建临时目录，finally 才会清理
                except Exception as e:
                    log.warning(f"Scene sync: download repo zip failed: {e}")
                    _init_featured_scenes_if_empty()
                    return

        # caller 提供的 repo_root 由 caller 清理，本函数不动它

        # 2. 定位 scenes/ 目录（P46.3：早退也走兜底，与下载失败路径行为一致）
        scenes_dir = os.path.join(repo_root, "scenes")
        if not os.path.isdir(scenes_dir):
            log.warning("Scene sync: scenes/ directory not found in zip")
            _init_featured_scenes_if_empty()
            return

        # 3. 刷新 category 缓存
        _refresh_cat_cache()
        _refresh_category_dict_cache()
        if not _cat_cache:
            log.warning("Scene sync: scene_category slug cache is empty, category_id will be 0 (unclassified)")

        stats = {"added": 0, "updated": 0, "skipped": 0, "failed": 0, "stale_official": 0,
                 "backfilled": 0}

        # Issue #3796：分类 slug 未命中按同步轮聚合（slug → {count, scenes 样例}），
        # 轮末有未命中才发 1 条 WARNING；category_dict 与 scene_category 两条路径共用。
        unresolved: dict[str, dict] = {}

        # P46.1 完成守卫 + 仓库场景集收集（与遍历同一份 listing）
        traversal_complete = True
        repo_scene_names = set()

        # 4. 遍历场景目录
        for entry in sorted(os.listdir(scenes_dir)):
            if entry in ("_template", "third-party", "__MACOSX", ".DS_Store", ".git"):
                continue
            scene_path = os.path.join(scenes_dir, entry)
            if not os.path.isdir(scene_path):
                continue
            repo_scene_names.add(entry)

            try:
                # a. 读 scene.json
                json_path = os.path.join(scene_path, "scene.json")
                if not os.path.exists(json_path):
                    logger.warning(f"Scene sync: {entry} missing scene.json, skip")
                    stats["failed"] += 1
                    continue
                with open(json_path, encoding="utf-8") as f:
                    scene_data = json.load(f)

                name = scene_data.get("name", "")
                if not name or name != entry:
                    logger.warning(f"Scene sync: {entry} name mismatch or empty, skip")
                    stats["failed"] += 1
                    continue

                nodes = scene_data.get("nodes", [])
                errors = _validate_scene_nodes(nodes)
                if errors:
                    logger.warning(f"Scene sync: {name} validation failed: {errors}")
                    stats["failed"] += 1
                    continue

                # 预检 skill 引用
                missing = _check_skills_exist(nodes, name)
                if missing:
                    logger.warning(f"Scene sync: {name} has missing skills: {missing}, skip")
                    stats["failed"] += 1
                    continue

                # b. 读 README.md
                readme_path = os.path.join(scene_path, "README.md")
                if os.path.exists(readme_path):
                    with open(readme_path, encoding="utf-8") as f:
                        market_desc = f.read()
                else:
                    market_desc = scene_data.get("description", "")

                # c. icon 先读文件数据，OBS key 等 INSERT/UPDATE 后用 market_id 生成
                icon_file_data = None
                icon_file_ext = ""
                for ext in (".png", ".svg", ".jpg"):
                    icon_file = os.path.join(scene_path, "icon" + ext)
                    if os.path.exists(icon_file):
                        try:
                            with open(icon_file, "rb") as f:
                                icon_file_data = f.read()
                            icon_file_ext = ext
                        except Exception as e:
                            logger.warning(f"Scene sync: {name} icon read failed: {e}")
                        break

                # d. content_hash
                content_hash = _compute_scene_hash(scene_path)

                # e. category slug → id（category_id 走 scene_category 表口径）。
                # Issue #3796：先直查后 legacy 别名 + path 形态逐段解析（与 category_dict
                # 路径同口径），未命中只进轮级 sink（循环内 debug），轮末聚合 1 条 WARNING。
                category_id = _resolve_scene_category_id(scene_data, unresolved, name)

                # f. 查 DB
                version = scene_data.get("version", "1.0.0")
                display_name = scene_data.get("display_name", "")
                description = scene_data.get("description", "")
                recommended_template = scene_data.get("recommended_template", "")
                onboarding = scene_data.get("onboarding", "repo-onboarding")
                # Issue #2761：标签统一 JSON（来源 auto）；分类 slug 命中才写新字段
                from src.kernel.tags import MAX_TAGS, encode_tags  # noqa: PLC0415
                _raw_tags = scene_data.get("tags", []) or []
                if isinstance(_raw_tags, str):
                    _raw_tags = [_raw_tags]
                _tag_items = [{"name": str(t).strip(), "source": "auto"}
                              for t in _raw_tags if str(t).strip()]
                tags_json = encode_tags(_tag_items, limit=MAX_TAGS)
                cat_l1_id, cat_l2_id = _resolve_scene_category(scene_data, unresolved, name)
                author = scene_data.get("author", "")
                nodes_json = json.dumps(nodes, ensure_ascii=False)
                preset_custom_fields = json.dumps(scene_data.get("custom_fields", []), ensure_ascii=False)

                # 返回口径是 (scene_id, action)（与 docstring 一致）；这里曾按 (action, market_id)
                # 解包 → action 恒为整数，skipped 判断失效、icon 以 "<action>" 当 id 回写 0 行。
                _content_fields = {
                    "display_name": display_name, "version": version, "description": description,
                    "market_desc": market_desc, "category_id": category_id,
                    "recommended_template": recommended_template, "nodes_json": nodes_json,
                    "onboarding": onboarding, "tags": tags_json, "author": author,
                    "content_hash": content_hash, "preset_custom_fields": preset_custom_fields,
                }
                if cat_l1_id:
                    _content_fields["cat_l1_id"] = cat_l1_id
                    # 一级命中：二级一并写入（0 表示重置旧二级，避免跨父级脏数据）
                    _content_fields["cat_l2_id"] = cat_l2_id
                market_id, action = _persist_scene_row(name, _content_fields, stats)
                if action in ("skipped", "backfilled"):
                    # 内容没变不重建（backfilled 只动了分类列），但绑定可能是坏的
                    # （历史 skill_id=0）→ 检查并自愈
                    _repair_scene_skill_ref_if_unresolved(name, market_id, nodes)
                    continue
                _apply_scene_icon(name, market_id, icon_file_data, icon_file_ext)

                # g. 重建 scene_skill_ref（含 skill_id）
                _rebuild_scene_skill_ref_by_name(name, nodes)

            except Exception as e:
                logger.warning(f"Scene sync: {entry} failed: {e}", exc_info=True)
                stats["failed"] += 1
                # P46.1 完成守卫：中途异常 → 仓库场景集不完整，跳过本轮 stale 检测
                traversal_complete = False

        # Issue #3796：轮末聚合未命中（每轮 ≤1 条 WARNING，slug→次数 + 样例场景；
        # 文案避开「not found in category_dict/scene_category」字面量，防 LogQL 误计）。
        if unresolved:
            _summary = ", ".join(
                f"'{slug}'×{v['count']} hits (scenes: {', '.join(v['scenes'])})"
                for slug, v in sorted(unresolved.items()))
            logger.warning(f"Scene sync: unresolved category slugs this round "
                           f"(kept existing categories): {_summary}")

        # P46.1 stale 检测（只读，铁律 1.0：零 DB 写；守卫失败最坏=本轮没有报告）
        stats["stale_official"] = _detect_stale_official_scenes(repo_scene_names, traversal_complete)

        log.info(
            f"Scene sync done: added={stats['added']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, backfilled={stats['backfilled']}, "
            f"failed={stats['failed']}, stale_official={stats['stale_official']}"
        )

        # 7. 初始化精选场景（提取为独立函数，token 缺失时也能调用）
        _init_featured_scenes_if_empty()

    finally:
        if tmp_dir:
            # P45/§4.5 统一清理形式：_download_repo_zip 返回 mkdtemp/<top_dir>，
            # rmtree(repo_root) 只删子目录会残留空父目录 scene_sync_zip_XXXX
            # （与 skill_sync 原有泄漏同款）。删除父目录。
            shutil.rmtree(os.path.dirname(tmp_dir), ignore_errors=True)


def _scene_upsert_key(name: str, fields: dict) -> str:
    """跨域写幂等键：内容哈希 + 分类目标（Issue #3796，PR #1982 审查 Required）。

    只含哈希的键是**永久去重**的：存量场景 `scene.json` 不变时每轮都返回 duplicate，
    mgmt 侧 `upsert_scene_content` 根本不执行 → 幂等层里的分类回填永远跑不到，
    Spec 验收「同步一轮后旧 slug 场景落 1xxx」在线上三库拓扑不成立。

    因此把分类目标（`category_id`/`cat_l1_id`/`cat_l2_id`）并入键：目标变化 → 新键 →
    重新执行一次（mgmt 侧自行比对库内值，无需变更时返回 skipped，不写内容列）；
    同内容同分类目标仍保持去重，跨域写日志行不会随每轮同步增长。
    """
    return (f"scene-upsert:{name}:{fields.get('content_hash') or ''}"
            f":{int(fields.get('category_id') or 0)}"
            f":{int(fields.get('cat_l1_id') or 0)}"
            f":{int(fields.get('cat_l2_id') or 0)}")


def _persist_scene_row(name: str, fields: dict, stats: dict) -> tuple[int, str]:
    """写入/更新场景内容，返回 (scene_id, action)。

    action ∈ {"skipped", "updated", "added", "backfilled"}；skipped 时 scene_id 为
    已存在的行 id；backfilled 表示内容未变、只有分类列被回填（Issue #3796）。
    只更新内容列，`is_active` 保留库内既有值（admin 设置不被覆盖）。
    """
    content_hash = fields["content_hash"]

    # lib 模式（三库三机）：跨域写走 mgmt lib + 幂等键；hash 未变时 upsert 内部直接 skipped
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_scene
    _mg_scene = _mgmt_lib_scene(foreign_only=True)
    if _mg_scene is not None:
        from src.kernel import cross_write as _xw_scene
        _out: dict = {}

        def _apply_scene() -> None:
            _out.update(_mg_scene.upsert_scene_content(name, fields))

        # payload 必须带完整 fields：失败意图要能被 drain 按幂等内容重放
        # （只带 name/hash 时补偿端无内容可写，等于登记了一条永远补不了的意图）。
        # Issue #3796：键含分类目标，否则存量场景跨域写被永久去重、分类回填不执行。
        _res = _xw_scene.submit(
            "mgmt", "scene_upsert", _scene_upsert_key(name, fields),
            {"name": name, "content_hash": content_hash, "fields": fields}, _apply_scene)
        if _res.get("status") == "failed":
            # 与原路径失败语义一致：抛出去由调用方计 failed 并跳过 icon/ref 重建
            raise RuntimeError(f"scene upsert via lib failed: {_res.get('error')}")
        if _res.get("status") == "duplicate":
            stats["skipped"] += 1
            return int(_out.get("id") or 0) or int(_mg_scene.get_official_scene_id(name) or 0), "skipped"
        _action = {"inserted": "added", "updated": "updated", "skipped": "skipped",
                   "backfilled": "backfilled"}.get(str(_out.get("action") or ""), "updated")
        stats[_action] += 1
        _sid = int(_out.get("id") or 0) or int(_mg_scene.get_official_scene_id(name) or 0)
        if _action == "backfilled":
            log.info(f"Scene sync: backfilled category '{name}' "
                     f"({', '.join(_out.get('backfilled') or [])}) (lib)")
            return _sid, "backfilled"
        log.info(f"Scene sync: {_action} '{name}' v{fields['version']} (lib)")
        return _sid, _action

    # 单库/测试拓扑：写入交给 mgmt public 的任意拓扑原语（自有 DSN → mgmt 连接，
    # 否则进程主库），幂等口径不变：hash 相同直接 skipped，UPDATE 保留 is_active。
    from src.domains.mgmt import public as _mgmt_public_scene
    _res_write = _mgmt_public_scene.upsert_scene_content_any(name, fields)
    action = str(_res_write.get("action") or "updated")
    scene_id = int(_res_write.get("id") or 0)
    if action == "skipped":
        stats["skipped"] += 1
        return scene_id, "skipped"
    if action == "backfilled":
        # Issue #3796：内容未变、只有分类列被回填——不写 version 告警、不上 icon、不重建 ref
        stats["backfilled"] = int(stats.get("backfilled") or 0) + 1
        log.info(f"Scene sync: backfilled category '{name}' "
                 f"({', '.join(_res_write.get('backfilled') or [])})")
        return scene_id, "backfilled"
    # owner 侧动作名（inserted/updated）→ 本模块对外口径（added/updated），与 lib 路径一致
    action = {"inserted": "added", "updated": "updated"}.get(action, "updated")
    if action == "updated":
        old_version = _res_write.get("old_version")
        if _parse_version(fields["version"]) <= _parse_version(old_version or "0.0.0"):
            logger.warning(
                f"Scene sync: {name} content changed but version not updated "
                f"(old={old_version}, new={fields['version']}), updating anyway"
            )
    stats[action] += 1
    log.info(f"Scene sync: {action} '{name}' v{fields['version']}")
    return scene_id, action


def _apply_scene_icon(name: str, market_id: int, icon_file_data, icon_file_ext: str) -> None:
    """上传图标并回写 icon 列（OBS key 以 market_id 生成，故必须在落库之后调用）。"""
    if not icon_file_data:
        return
    try:
        from src.domains.runtime.storage import get_storage
        storage = get_storage()
        icon_key = f"scene-icons/{market_id}{icon_file_ext}"
        if storage:
            storage.save_bytes(icon_key, icon_file_data)
            icon_val = storage.temporary_url(icon_key) if hasattr(storage, 'temporary_url') else ""
            obs_icon_key = icon_key
        else:
            static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                      "api", "frontend", "shell", "img", "scenes")
            os.makedirs(static_dir, exist_ok=True)
            dest = os.path.join(static_dir, f"{market_id}{icon_file_ext}")
            with open(dest, "wb") as f:
                f.write(icon_file_data)
            icon_val = f"/static/img/scenes/{market_id}{icon_file_ext}"
            obs_icon_key = ""
        from src.domains.mgmt import public as _mgmt_public_icon
        _mgmt_public_icon.set_scene_icon_any(market_id, icon_val, obs_icon_key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Scene sync: {name} icon storage failed: {e}")
