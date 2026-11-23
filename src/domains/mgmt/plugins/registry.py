"""插件注册表（Issue #1089 插件市场-C1）：plugin.json 清单解析、安装/卸载、查询。

挂点⑤（plugin.json bundled_skills）：安装插件时为捆绑技能建映射与技能行，
技能市场据 grape_t_plugin_skill 标注"来自插件 X"；卸载时联动解除。

挂点①（应用场景凭据协商）消费 credentials 声明：get_plugin() 供
scenes._collect_plugin_credential_requirements 查询凭据字段。
"""
from datetime import datetime, timezone
import json
import re

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str

try:
    from src.kernel.session_manager import db, db_lock
except Exception:  # pragma: no cover - worker 独立进程可能无主控 DB
    db = None
    db_lock = None

_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def parse_plugin_manifest(manifest: dict) -> tuple[dict, list[str]]:
    """解析并校验 plugin.json 清单，返回 (规范化 manifest, 错误列表)。

    plugin.json 字段：
      name(必填, 小写-连字符) / version / display_name / description / author
      credentials: [{key(必填), label, required}]（凭据声明，应用场景时协商配置）
      bundled_skills: [{skill_name(必填), display_name, description, agent_type}]
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return {}, ["plugin.json 必须是 JSON 对象"]
    name = str(manifest.get("name", "")).strip()
    if not name:
        errors.append("name 字段不能为空")
    elif not _PLUGIN_NAME_RE.match(name):
        errors.append("name 仅允许小写字母、数字、连字符，且以字母/数字开头")
    version = str(manifest.get("version", "") or "1.0.0").strip()

    credentials = []
    raw_creds = manifest.get("credentials") or []
    if not isinstance(raw_creds, list):
        errors.append("credentials 必须是数组")
    else:
        seen_keys: set[str] = set()
        for item in raw_creds:
            if not isinstance(item, dict) or not str(item.get("key", "")).strip():
                errors.append("credentials 条目缺少 key")
                continue
            key = str(item["key"]).strip()
            if key in seen_keys:
                errors.append(f"credentials key 重复: {key}")
                continue
            seen_keys.add(key)
            credentials.append({
                "key": key,
                "label": str(item.get("label", "") or key),
                "required": bool(item.get("required", False)),
            })

    bundled = []
    raw_bundled = manifest.get("bundled_skills") or []
    if not isinstance(raw_bundled, list):
        errors.append("bundled_skills 必须是数组")
    else:
        seen_skills: set[str] = set()
        for item in raw_bundled:
            if not isinstance(item, dict) or not str(item.get("skill_name", "")).strip():
                errors.append("bundled_skills 条目缺少 skill_name")
                continue
            sn = str(item["skill_name"]).strip()
            if sn in seen_skills:
                errors.append(f"bundled_skills skill_name 重复: {sn}")
                continue
            seen_skills.add(sn)
            bundled.append({
                "skill_name": sn,
                "display_name": str(item.get("display_name", "") or sn),
                "description": str(item.get("description", "") or ""),
                "agent_type": str(item.get("agent_type", "") or ""),
            })

    normalized = {
        "name": name,
        "version": version,
        "display_name": str(manifest.get("display_name", "") or name),
        "description": str(manifest.get("description", "") or ""),
        "author": str(manifest.get("author", "") or ""),
        "credentials": credentials,
        "bundled_skills": bundled,
    }
    return normalized, errors


def install_plugin(manifest: dict, source: str = "market",
                   create_skill_rows: bool = True) -> dict:
    """安装（注册）插件：upsert plugin 行 + 重建 bundled_skills 映射。

    挂点⑤联动：bundled_skills 中不存在的技能按 scope='official' 创建
    技能行（created_by_plugin=1，卸载时联动移除）；已存在的技能仅做
    来源映射（created_by_plugin=0，卸载只解除标注）。
    既有判定跨 scope（official/user/repo）：同名技能已存在于任意作用域
    时不再创建 official 重复行（避免技能市场同名冲突，QA c3）。
    """
    normalized, errors = parse_plugin_manifest(manifest)
    if errors:
        return {"ok": False, "errors": errors}
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO grape_t_plugin_registry (id, name, version, display_name, description, author, "
            "credentials_json, bundled_skills_json, status, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET version=excluded.version, "
            "display_name=excluded.display_name, description=excluded.description, "
            "author=excluded.author, credentials_json=excluded.credentials_json, "
            "bundled_skills_json=excluded.bundled_skills_json, status='installed', "
            "updated_at=excluded.updated_at",
            (uuid7_str(), normalized["name"], normalized["version"], normalized["display_name"],
             normalized["description"], normalized["author"],
             json.dumps(normalized["credentials"], ensure_ascii=False),
             json.dumps(normalized["bundled_skills"], ensure_ascii=False),
             "installed", source, now, now))
        c.execute("SELECT id FROM grape_t_plugin_registry WHERE name=?", (normalized["name"],))
        plugin_id = c.fetchone()[0]
        # 重建捆绑技能映射
        c.execute("DELETE FROM grape_t_plugin_skill WHERE plugin_id=?", (plugin_id,))
        for bs in normalized["bundled_skills"]:
            sn = bs["skill_name"]
            row = c.execute(
                "SELECT id FROM skill WHERE skill_name=? AND is_active=1 LIMIT 1",
                (sn,)).fetchone()
            created = 0
            if create_skill_rows and not row:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                    "scope, owner_id, is_active, like_count) VALUES (?,?,?,?,?,?,1,0)",
                    (sn, bs["display_name"],
                     bs["description"] or f"插件 {normalized['display_name']} 捆绑技能",
                     bs["agent_type"] or "auto", "official", 0))
                created = 1
            c.execute(
                "INSERT OR IGNORE INTO grape_t_plugin_skill (plugin_id, skill_name, created_by_plugin) "
                "VALUES (?,?,?)", (plugin_id, sn, created))
        db.commit()
    log.info(f"plugin installed: {normalized['name']} v{normalized['version']}")
    return {"ok": True, "plugin": get_plugin(normalized["name"])}


def _scene_referenced_skills(skill_names: list[str]) -> set[str]:
    """查询被 active 场景节点引用的技能名集合（卸载联动保护，QA c2）。

    场景引用权威存储：devops_pipeline_scene.nodes_json（node.skill）。
    表未就绪（旧库）等异常按无引用处理，不阻塞卸载主链路。
    """
    if not skill_names:
        return set()
    wanted = set(skill_names)
    referenced: set[str] = set()
    try:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT nodes_json FROM devops_pipeline_scene WHERE is_active=1").fetchall()
        for (nodes_json,) in rows:
            try:
                nodes = json.loads(nodes_json or "[]")
            except Exception:
                continue
            if not isinstance(nodes, list):
                continue
            for n in nodes:
                if isinstance(n, dict) and (n.get("skill") or "") in wanted:
                    referenced.add(n["skill"])
    except Exception as e:
        log.warning(f"_scene_referenced_skills failed: {e}")
    return referenced


def uninstall_plugin(name: str) -> dict:
    """卸载插件：删 plugin 行 + 映射；插件创建的技能行联动移除（挂点⑤）。

    仅移除仍处于 created_by_plugin=1 且未被其他插件标注的技能行；
    既有技能（created_by_plugin=0）只解除来源标注。
    场景引用保护（QA c2）：被 active 场景节点（node.skill）引用的
    created 技能不硬删（保留技能行避免破坏在用场景），仅解除来源标注，
    并以 kept_skill_names 返回保留名单。
    """
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM grape_t_plugin_registry WHERE name=?", (name,)).fetchone()
        if not row:
            return {"ok": False, "errors": [f"插件不存在: {name}"]}
        plugin_id = row[0]
        c.execute(
            "SELECT ps.skill_name FROM grape_t_plugin_skill ps "
            "WHERE ps.plugin_id=? AND ps.created_by_plugin=1 AND NOT EXISTS ("
            "  SELECT 1 FROM grape_t_plugin_skill ps2 "
            "  WHERE ps2.skill_name=ps.skill_name AND ps2.plugin_id!=ps.plugin_id)",
            (plugin_id,))
        created_skills = [r[0] for r in c.fetchall()]
    # 场景引用检查放锁外（只读扫描，避免长事务占写锁）
    protected = _scene_referenced_skills(created_skills)
    removable = [sn for sn in created_skills if sn not in protected]
    with db_lock:
        c = db.cursor()
        for sn in removable:
            c.execute("DELETE FROM skill WHERE skill_name=? AND scope='official'", (sn,))
        c.execute("DELETE FROM grape_t_plugin_skill WHERE plugin_id=?", (plugin_id,))
        c.execute("DELETE FROM grape_t_plugin_registry WHERE id=?", (plugin_id,))
        db.commit()
    log.info(f"plugin uninstalled: {name} (bundled skill rows removed: {len(removable)}, "
             f"kept for scene references: {sorted(protected)})")
    return {"ok": True, "removed_skill_names": removable,
            "kept_skill_names": sorted(protected)}


def get_plugin(name: str) -> dict | None:
    """按名称查插件（含解析后的 credentials / bundled_skills）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, version, display_name, description, author, "
            "credentials_json, bundled_skills_json, status, source FROM grape_t_plugin_registry WHERE name=?",
            (name,)).fetchone()
    if not row:
        return None
    return _row_to_plugin(row)


def list_plugins() -> list[dict]:
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, name, version, display_name, description, author, "
            "credentials_json, bundled_skills_json, status, source FROM grape_t_plugin_registry "
            "ORDER BY created_at DESC, id DESC").fetchall()
    return [_row_to_plugin(r) for r in rows]


def _row_to_plugin(row) -> dict:
    try:
        credentials = json.loads(row[6]) if row[6] else []
    except Exception:
        credentials = []
    try:
        bundled = json.loads(row[7]) if row[7] else []
    except Exception:
        bundled = []
    return {
        "id": row[0], "name": row[1], "version": row[2], "display_name": row[3],
        "description": row[4] or "", "author": row[5] or "",
        "credentials": credentials, "bundled_skills": bundled,
        "status": row[8] or "installed", "source": row[9] or "market",
    }


def plugin_skill_source_map() -> dict[str, str]:
    """skill_name → 插件 display_name 映射（技能市场"来自插件 X"标注）。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT ps.skill_name, p.display_name FROM grape_t_plugin_skill ps "
            "JOIN grape_t_plugin p ON p.id=ps.plugin_id WHERE p.status='installed'").fetchall()
    return {r[0]: (r[1] or "") for r in rows}
