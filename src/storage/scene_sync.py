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
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile

import httpx
from src.config import log
from src.session_manager import db, db_lock

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

def _load_config():
    """加载场景同步配置：优先环境变量，回退 configs/scene-sync.json。"""
    repo_url = os.environ.get("GRAPESKILLS_REPO_URL", "")
    repo_branch = os.environ.get("GRAPESKILLS_REPO_BRANCH", "")
    sync_interval = os.environ.get("SCENE_SYNC_INTERVAL", "")

    # 读 configs/scene-sync.json（环境变量优先覆盖）
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "scene-sync.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not repo_url:
                repo_url = cfg.get("repo_url", "")
            if not repo_branch:
                repo_branch = cfg.get("repo_branch", "")
            if not sync_interval:
                sync_interval = str(cfg.get("sync_interval", ""))
    except Exception:
        pass

    # 兜底默认值
    if not repo_url:
        repo_url = "https://gitcode.com/grape-dev/grape-skills"
    if not repo_branch:
        repo_branch = "main"

    return repo_url, repo_branch, float(sync_interval) if sync_interval else 1800.0


_REPO_URL, _REPO_BRANCH, _SYNC_INTERVAL = _load_config()
_OWNER_REPO = _REPO_URL.replace("https://gitcode.com/", "").rstrip("/")

_VALID_TAGS = {"vod", "issue", "coding", "qa", "sre", "chat"}
_VALID_MODES = {"emit"}

# category slug → id 缓存（每次同步刷新）
_cat_cache: dict[str, int] = {}


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
    """预检 skill 引用是否在 skill 表中存在。返回不存在的 skill 名列表。"""
    missing = []
    skill_names = set(n.get("skill", "") for n in nodes if n.get("skill", ""))
    if not skill_names:
        return missing
    with db_lock:
        c = db.cursor()
        placeholders = ",".join("?" * len(skill_names))
        c.execute(f"SELECT DISTINCT skill_name FROM skill WHERE skill_name IN ({placeholders})", list(skill_names))
        existing = set(r[0] for r in c.fetchall())
    for sn in skill_names:
        if sn and sn not in existing:
            missing.append(sn)
            logger.warning(f"场景 {scene_name} 引用了不存在的技能: {sn}")
    return missing


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
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "scene-sync.json")
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

    URL: https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
    认证: Authorization: Bearer {token}
    ZIP 内顶层目录名: {repo}-{branch}
    """
    url = f"https://raw.gitcode.com/{_OWNER_REPO}/archive/refs/heads/{_REPO_BRANCH}.zip"
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


def _rebuild_scene_skill_ref(market_id: int, nodes: list, scene_name: str = ""):
    """重建 scene_skill_ref：DELETE + INSERT，用 is_active=1 查 skill_id（优先 official，其次 repo）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_skill_ref WHERE scene_id=?", (market_id,))
        for n in nodes:
            skill_name = n.get("skill", "")
            if skill_name:
                # 优先查 official，其次 repo（仓库场景可能引用 repo scope 技能）
                c.execute(
                    "SELECT id FROM skill WHERE skill_name=? AND is_active=1 AND scope='official' LIMIT 1",
                    (skill_name,),
                )
                sk_row = c.fetchone()
                if not sk_row:
                    c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 AND scope='repo' LIMIT 1",
                        (skill_name,),
                    )
                    sk_row = c.fetchone()
                skill_id = sk_row[0] if sk_row else 0
                if skill_id == 0:
                    logger.warning(f"Scene sync: {scene_name} skill '{skill_name}' not found in active skills, skill_id=0")
                c.execute(
                    "INSERT OR IGNORE INTO scene_skill_ref (scene_id, skill_id, skill_name) VALUES (?,?,?)",
                    (market_id, skill_id, skill_name),
                )
        db.commit()


def _rebuild_scene_skill_ref_by_name(scene_name: str, nodes: list):
    """通过场景名查 market_id 后重建 scene_skill_ref。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM scene_market WHERE name=? AND source='official' LIMIT 1", (scene_name,))
        row = c.fetchone()
    if row:
        _rebuild_scene_skill_ref(row[0], nodes, scene_name)


def _init_featured_scenes_if_empty():
    """精选场景初始化：如果 official 场景没有任何 is_featured=1 的，设置默认精选。

    独立于远端同步——即使没有 token 无法拉远端场景包，本地已有 official 场景也需要精选标记。
    """
    default_featured = ("default", "cloud-native", "iac")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM scene_market WHERE source='official' AND is_featured=1 AND is_active=1")
        featured_count = c.fetchone()[0]
        if featured_count == 0:
            placeholders = ",".join("?" * len(default_featured))
            c.execute(
                f"UPDATE scene_market SET is_featured=1, updated_at=datetime('now') "
                f"WHERE name IN ({placeholders}) AND source='official' AND is_active=1",
                default_featured,
            )
            db.commit()
            log.info(f"Scene sync: initialized featured scenes: {', '.join(default_featured)}")
        else:
            log.info(f"Scene sync: {featured_count} featured scenes already exist, skip init")


async def sync_official_scenes():
    """定时同步官方场景从 grape-skills 仓库到 scene_market 表。"""
    log.info(f"Scene sync started: {_REPO_URL} branch={_REPO_BRANCH} (ZIP archive)")

    tmp_dir = None
    try:
        bot_token = _get_sync_token()
        if not bot_token:
            log.warning("Scene sync: no GitCode token configured (GRAPE_DEV_TOKEN env / configs/scene-sync.json), skip remote sync")
            _init_featured_scenes_if_empty()
            return

        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. 下载 ZIP 归档并解压
            try:
                repo_root = await _download_repo_zip(client, bot_token)
            except Exception as e:
                log.warning(f"Scene sync: download repo zip failed: {e}")
                return

        # 2. 定位 scenes/ 目录
        scenes_dir = os.path.join(repo_root, "scenes")
        if not os.path.isdir(scenes_dir):
            log.warning("Scene sync: scenes/ directory not found in zip")
            return

        # 3. 刷新 category 缓存
        _refresh_cat_cache()
        if not _cat_cache:
            log.warning("Scene sync: scene_category slug cache is empty, category_id will be 0 (unclassified)")

        stats = {"added": 0, "updated": 0, "skipped": 0, "failed": 0}

        # 4. 遍历场景目录
        for entry in sorted(os.listdir(scenes_dir)):
            if entry in ("_template", "third-party", "__MACOSX", ".DS_Store", ".git"):
                continue
            scene_path = os.path.join(scenes_dir, entry)
            if not os.path.isdir(scene_path):
                continue

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

                # e. category slug → id
                category_slug = scene_data.get("category", "general")
                category_id = _cat_cache.get(category_slug, 0)
                if category_id == 0:
                    logger.warning(f"Scene sync: {name} category slug '{category_slug}' not found in scene_category, using 0 (unclassified)")

                # f. 查 DB
                version = scene_data.get("version", "1.0.0")
                display_name = scene_data.get("display_name", "")
                description = scene_data.get("description", "")
                recommended_template = scene_data.get("recommended_template", "")
                onboarding = scene_data.get("onboarding", "repo-onboarding")
                tags_str = ",".join(scene_data.get("tags", []))
                author = scene_data.get("author", "")
                nodes_json = json.dumps(nodes, ensure_ascii=False)
                preset_custom_fields = json.dumps(scene_data.get("custom_fields", []), ensure_ascii=False)

                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "SELECT id, version, content_hash FROM scene_market "
                        "WHERE name=? AND source='official' LIMIT 1",
                        (name,),
                    )
                    row = c.fetchone()

                    if row:
                        scene_id, old_version, old_hash = row
                        if old_hash == content_hash:
                            stats["skipped"] += 1
                            continue

                        # hash 变了
                        if _parse_version(version) <= _parse_version(old_version or "0.0.0"):
                            logger.warning(
                                f"Scene sync: {name} content changed but version not updated "
                                f"(old={old_version}, new={version}), updating anyway"
                            )

                        # 先 UPDATE 不带 icon（icon 等 OBS 上传后单独 UPDATE）
                        c.execute(
                            "UPDATE scene_market SET "
                            "display_name=?, version=?, description=?, market_description=?, "
                            "category_id=?, recommended_template=?, nodes_json=?, "
                            "onboarding=?, tags=?, author=?, content_hash=?, preset_custom_fields=?, "
                            "is_active=1, updated_at=datetime('now') "
                            "WHERE id=?",
                            (display_name, version, description, market_desc,
                             category_id, recommended_template, nodes_json,
                             onboarding, tags_str, author, content_hash,
                             preset_custom_fields, scene_id),
                        )
                        db.commit()
                        market_id = scene_id
                        stats["updated"] += 1
                        log.info(f"Scene sync: updated '{name}' v{version}")
                    else:
                        c.execute(
                            "INSERT INTO scene_market "
                            "(name, display_name, version, description, market_description, icon, obs_icon_key, "
                            "category_id, is_featured, like_count, source, source_user_id, "
                            "recommended_template, nodes_json, onboarding, tags, author, "
                            "content_hash, preset_custom_fields, is_active, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,0,0,'official',0,?,?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
                            (name, display_name, version, description, market_desc,
                             "", "", category_id, recommended_template, nodes_json,
                             onboarding, tags_str, author, content_hash,
                             preset_custom_fields),
                        )
                        db.commit()
                        market_id = c.lastrowid
                        stats["added"] += 1
                        log.info(f"Scene sync: added '{name}' v{version}")

                    # icon 上传：用 market_id 作为 OBS key（全局唯一）
                    if icon_file_data:
                        try:
                            from src.storage import get_storage
                            storage = get_storage()
                            icon_key = f"scene-icons/{market_id}{icon_file_ext}"
                            if storage:
                                storage.save_bytes(icon_key, icon_file_data)
                                icon_val = storage.temporary_url(icon_key) if hasattr(storage, 'temporary_url') else ""
                                obs_icon_key = icon_key
                            else:
                                static_dir = os.path.join(os.path.dirname(__file__), "..", "api", "static", "img", "scenes")
                                os.makedirs(static_dir, exist_ok=True)
                                dest = os.path.join(static_dir, f"{market_id}{icon_file_ext}")
                                with open(dest, "wb") as f:
                                    f.write(icon_file_data)
                                icon_val = f"/static/img/scenes/{market_id}{icon_file_ext}"
                                obs_icon_key = ""
                            # UPDATE icon 字段
                            with db_lock:
                                cur = db.cursor()
                                cur.execute("UPDATE scene_market SET icon=?, obs_icon_key=? WHERE id=?", (icon_val, obs_icon_key, market_id))
                                db.commit()
                        except Exception as e:
                            logger.warning(f"Scene sync: {name} icon storage failed: {e}")

                # g. 重建 scene_skill_ref（含 skill_id）
                _rebuild_scene_skill_ref_by_name(name, nodes)

            except Exception as e:
                logger.warning(f"Scene sync: {entry} failed: {e}", exc_info=True)
                stats["failed"] += 1

        log.info(
            f"Scene sync done: added={stats['added']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, failed={stats['failed']}"
        )

        # 7. 初始化精选场景（提取为独立函数，token 缺失时也能调用）
        _init_featured_scenes_if_empty()

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _scene_sync_worker(interval: float = 1800.0):
    """定时同步 worker：启动即执行，之后每 interval 秒。"""
    while True:
        try:
            await sync_official_scenes()
        except asyncio.CancelledError:
            logger.info("scene sync worker cancelled")
            raise
        except Exception:
            logger.exception("scene sync run failed")
        await asyncio.sleep(interval)


def start_scene_sync_scheduler():
    """启动定时同步调度。"""
    t = asyncio.create_task(_scene_sync_worker(_SYNC_INTERVAL), name="scene-sync")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"scene sync scheduler started (interval={_SYNC_INTERVAL}s, repo={_REPO_URL}, branch={_REPO_BRANCH})")


async def stop_scene_sync_scheduler():
    """优雅关闭：cancel 所有定时任务并等待退出。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("scene sync scheduler stopped")
