"""运行时插件状态读取（Issue #1090 / 插件市场 C2）。

数据来源（契约对接 + 降级先行），按精确度从高到低：
1. B5（#1088）物化表 `installed_plugins`（repo_key/runtime_id/name/version/
   fingerprint/capabilities/hook_types/health）；表不存在 → 跳过下一来源。
2. C1（#1089 挂点④）真实契约：agent_runtimes.installed_plugins 设备注册自报
   （JSON [{name, version, status}]）。绑定引擎（runtime_id>0）读该行自报为
   事实源（空清单也权威）；行不存在/JSON 损坏 → 跳过下一来源。
3. C1（#1089 挂点⑤）插件注册表：plugin 表 status='installed' 条目
   （description 作能力说明）；广播（runtime_id=0）无引擎事实源时兜底。
4. 全部来源不可用 → 空清单降级，绝不抛异常、不阻塞任务下发（降级先行）。

另有 C1 五类挂点声明（scene.json node.plugins 节点挂点）由 get_declared_plugins
读取，与 installed 源在 collect_plugin_capabilities 合并（同名 installed 优先）。

设计约束（Spec 关键决策）：
- 以 B5 物化状态为唯一事实源，不做运行时二次探测（决策：数据来源 A）；
- 任何读取异常一律降级为空清单，不抛异常、不阻塞任务下发（降级先行）；
- 进程内 TTL 缓存减少任务下发 DB 查询；补装完成后 invalidate_plugin_cache()
  使下一次任务下发读到新能力（恢复闭环）。
"""
import json
import time

# 归一化条目上限（防 Prompt 膨胀，Spec Risks：能力清单条目设上限）
PLUGIN_LIST_MAX = 20

# (repo_key, scene_id) -> (ts, items)
_CAP_CACHE: dict = {}
_CAP_CACHE_TTL = 60


def invalidate_plugin_cache() -> None:
    """补装恢复闭环：确认单安装完成后调用，清空能力清单缓存。"""
    _CAP_CACHE.clear()


def notify_plugin_installed(repo_key: str = "", name: str = "") -> None:
    """恢复闭环触发点（契约）：确认单补装完成、B5（#1088）状态回写后由安装链路调用。

    使下一次任务下发的能力清单读取到新插件（Spec AC ④：经确认单补装后，
    Agent 同任务重试或后续任务可恢复完整能力）。

    语义别名：与 invalidate_plugin_cache 当前动作一致（清空能力清单缓存），
    委托实现；保留独立名以表达「补装回写」契约语义，将来若需按 repo_key/name
    精细失效，只需改此处。
    """
    invalidate_plugin_cache()


def _normalize_installed_row(row, columns) -> dict:
    """把 B5 installed_plugins 行归一化为插件条目（宽松字段适配）。"""
    d = dict(zip(columns, row))
    name = d.get("name") or d.get("plugin_name") or ""
    if not name:
        return {}
    hooks_raw = d.get("hook_types") or d.get("hooks") or ""
    if isinstance(hooks_raw, str):
        hooks = [h.strip() for h in hooks_raw.replace(";", ",").split(",") if h.strip()]
    elif isinstance(hooks_raw, (list, tuple)):
        hooks = [str(h).strip() for h in hooks_raw if str(h).strip()]
    else:
        hooks = []
    return {
        "name": name,
        "version": d.get("version") or "",
        "capabilities": d.get("capabilities") or "",
        "hooks": hooks or ["runtime_installed"],
        "status": "installed",
        "health": d.get("health") or "unknown",
        "fingerprint": d.get("fingerprint") or "",
        "source": "installed_plugins",
    }


def _read_self_report_plugins(runtime_id: int):
    """读取绑定引擎的 installed_plugins 自报（#1089 挂点④ / B5 真实契约）。

    返回 None = 行不存在/列缺失/JSON 损坏（继续下一来源）；
    返回 list（可空）= 该引擎自报事实源（空清单也权威，不回退）。
    """
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT installed_plugins FROM agent_runtimes WHERE id=?",
                (runtime_id,)).fetchone()
        if row is None:
            return None
        out = []
        for p in json.loads(row[0] or "[]"):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).strip()
            if not name:
                continue
            out.append({
                "name": name,
                "version": str(p.get("version", "")).strip(),
                "capabilities": "",
                "hooks": ["runtime_installed"],
                "status": str(p.get("status", "installed")).strip() or "installed",
                "health": "unknown",
                "fingerprint": "",
                "source": "runtime_self_report",
            })
        return out
    except Exception:
        return None


def _read_registry_plugins() -> list:
    """读取插件注册表 installed 条目（#1089 挂点⑤；description 作能力说明）。

    表不存在/查询失败 → []（继续降级）。
    """
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT name, display_name, version, description FROM grape_t_plugin "
                "WHERE status='installed' ORDER BY name LIMIT ?",
                (PLUGIN_LIST_MAX,)).fetchall()
        return [{
            "name": r[0],
            "version": r[2] or "",
            "capabilities": r[3] or "",
            "hooks": ["registry"],
            "status": "installed",
            "health": "unknown",
            "fingerprint": "",
            "source": "grape_t_plugin_registry",
        } for r in rows]
    except Exception:
        return []


def get_installed_plugins(repo_key: str = "", runtime_id: int = 0) -> list:
    """读取已安装插件清单（源链：B5 物化表 → 绑定引擎自报 → 注册表 → 降级空）。

    - B5 物化表存在且有匹配行 → 直接采用（最精确）；
    - 绑定引擎（runtime_id>0）自报可读 → 该行即事实源（空清单也权威）；
    - 其余（广播/行缺失/自报损坏）→ plugin 注册表 installed 兜底；
    - 全部不可用 → []，不抛异常不阻塞。
    """
    # ① B5 物化表（契约形态一）：表存在且有匹配行即采用；表存在但无行 → 继续源链
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT * FROM installed_plugins LIMIT 0")
            columns = [desc[0] for desc in c.description]
            _where, _params = [], []
            if repo_key:
                _where.append("(repo_key=? OR repo_key='')")
                _params.append(repo_key)
            if runtime_id:
                _where.append("runtime_id=?")
                _params.append(runtime_id)
            sql = "SELECT * FROM installed_plugins"
            if _where:
                sql += " WHERE " + " AND ".join(_where)
            sql += " ORDER BY id LIMIT ?"
            _params.append(PLUGIN_LIST_MAX)
            rows = c.execute(sql, tuple(_params)).fetchall()
        items = [x for x in (_normalize_installed_row(r, columns) for r in rows) if x]
        if items:
            return items[:PLUGIN_LIST_MAX]
    except Exception:
        pass  # B5 未合入（表不存在）/ schema 漂移 → 继续下一来源
    # ② 绑定引擎自报（#1089 挂点④ 真实契约）
    if runtime_id and runtime_id > 0:
        self_report = _read_self_report_plugins(runtime_id)
        # 本机直连行（credential_id=0，无 worker 心跳上报）自报恒空 → 设备级清单在
        # 同引擎类型的 worker 行上（设备目录共享，装到 worker 行对直连行同样生效）——
        # 空自报回退同名引擎的 worker 行读取，避免直连会话误判"未安装"。
        if self_report is not None and not self_report:
            try:
                from src.kernel.session_manager import db, db_lock
                with db_lock:
                    _row = db.cursor().execute(
                        "SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,)).fetchone()
                    _eng_name = (_row[0] or "") if _row else ""
                if _eng_name:
                    with db_lock:
                        _wrow = db.cursor().execute(
                            "SELECT id FROM agent_runtimes WHERE name=? AND credential_id>0 "
                            "ORDER BY last_seen_at DESC LIMIT 1", (_eng_name,)).fetchone()
                    if _wrow:
                        _w_report = _read_self_report_plugins(_wrow[0])
                        if _w_report:
                            return _w_report[:PLUGIN_LIST_MAX]
            except Exception:
                pass
        if self_report is not None:
            return self_report[:PLUGIN_LIST_MAX]
    # ③ 注册表兜底（#1089 挂点⑤）/ ④ 全部不可用 → []
    return _read_registry_plugins()


def get_declared_plugins(scene_id: str = "", repo_key: str = "") -> list:
    """读取 C1 五类挂点声明（本期消费 scene.json node.plugins 节点挂点）。

    C1 未合入/节点未声明 plugins → 空清单；读取失败不抛异常。
    """
    if not scene_id:
        return []
    try:
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(scene_id, repo_key=repo_key) or {}
        nodes = cfg.get("nodes") or []
    except Exception:
        return []
    items, seen = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        plugins = node.get("plugins") or []
        if isinstance(plugins, str):
            plugins = [plugins]
        for p in plugins or []:
            if not isinstance(p, str) or not p.strip():
                continue
            name = p.strip()
            if name in seen:
                continue
            seen.add(name)
            items.append({
                "name": name,
                "version": "",
                "capabilities": "",
                "hooks": ["scene_node"],
                "status": "declared",
                "health": "unknown",
                "fingerprint": "",
                "source": "node_plugins",
            })
            if len(items) >= PLUGIN_LIST_MAX:
                break
        if len(items) >= PLUGIN_LIST_MAX:
            break
    return items


def collect_plugin_capabilities(repo_key: str = "", runtime_id: int = 0,
                                scene_id: str = "") -> list:
    """汇总插件能力清单（installed + declared 合并去重，同名 installed 优先）。

    任何异常降级为空清单；结果按 (repo_key, scene_id) 进程内 TTL 缓存。
    """
    cache_key = (repo_key, scene_id, runtime_id)
    now = time.time()
    cached = _CAP_CACHE.get(cache_key)
    if cached and now - cached[0] < _CAP_CACHE_TTL:
        return list(cached[1])
    try:
        installed = get_installed_plugins(repo_key, runtime_id)
    except Exception:
        installed = []
    try:
        declared = get_declared_plugins(scene_id, repo_key)
    except Exception:
        declared = []
    merged, seen = [], set()
    for item in installed + declared:
        name = item.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        merged.append(item)
        if len(merged) >= PLUGIN_LIST_MAX:
            break
    if merged:  # 空结果不缓存（B5 未合入/表空/自报未落时，下次重查而非把空钉死 TTL）
        _CAP_CACHE[cache_key] = (now, list(merged))
    return list(merged)
