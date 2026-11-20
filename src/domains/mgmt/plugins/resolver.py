"""插件配置继承链（Issue #1089 Slice A；A4 #1082 契约 seam）。

契约（按 Spec #1089）：配置解析统一走继承链，作用域优先级
节点(node) > Agent(agent) > 仓库(repo) > 个人(personal)。

- merge_plugin_scopes：插件名按四作用域合并去重，保留每个插件的最高优先级来源。
- resolve_plugin_credentials：插件凭据按键级覆盖——高优先级作用域中
  非空（非 ""/None）的值覆盖低优先级；空值视为未配置、不遮蔽。

A4 #1082 未合入期间由本模块承接口径；A4 落地后应将内部实现切换到
A4 统一继承链入口，本文件签名与语义保持不变（公开 seam）。
"""

try:
    from src.kernel.session_manager import db, db_lock
except Exception:  # pragma: no cover - worker 独立进程可能无主控 DB
    db = None
    db_lock = None

# 作用域优先级：index 越小优先级越高（节点最具体）
_SCOPE_ORDER = ["node", "agent", "repo", "personal"]


def _norm_names(names) -> list[str]:
    """规范化插件名列表：转 str、去空白、去空项、保序去重。"""
    result: list[str] = []
    seen: set[str] = set()
    if not names:
        return result
    if isinstance(names, str):
        names = [names]
    for n in names:
        # 插件名仅接受字符串（数字/None 等非法条目忽略）；
        # 逗号串（YAML 单行写法 plugins: a, b）按逗号拆分
        if not isinstance(n, str):
            continue
        for part in n.split(","):
            s = part.strip()
            if not s or s in seen:
                continue
            seen.add(s)
            result.append(s)
    return result


def merge_plugin_scopes(personal=None, repo=None, agent=None, node=None) -> list[dict]:
    """按作用域合并插件名（个人→仓库→Agent→节点，高优先级覆盖）。

    返回 [{"name": 插件名, "scope": 最高优先级作用域}]，
    顺序：越靠近节点（越具体）的越靠前，同级保持声明顺序。
    """
    merged: dict[str, str] = {}
    # 从低优先级到高优先级写入：后写覆盖先写 → 记录的是最高优先级来源
    for scope in reversed(_SCOPE_ORDER):
        names = {"personal": personal, "repo": repo, "agent": agent, "node": node}[scope]
        for name in _norm_names(names):
            merged[name] = scope
    # 输出按作用域优先级排序（node 在前），同级保持 dict 插入序（声明顺序）
    out: list[dict] = []
    for scope in _SCOPE_ORDER:
        for name, s in merged.items():
            if s == scope:
                out.append({"name": name, "scope": scope})
    return out


def normalize_plugin_names(names) -> list[str]:
    """公开的插件名规范化（列表/逗号串 → 非空去重 str 列表）。

    供 SKILL.md front matter plugins: 软引用解析（挂点②）等场景复用。
    """
    return _norm_names(names)


def _load_agent_description() -> dict:
    """读取 configs/agent-description.json（挂点③ agent.plugins 的声明源）。"""
    import json
    import os
    try:
        from src.kernel.config import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "agent-description.json")
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_agent_plugins(agent_type: str) -> list[str]:
    """Agent 类型默认插件（挂点③：agent-description.json agent.plugins）。

    Agent 类型种子默认安装的声明源；未声明/未知类型返回 []。
    """
    meta = _load_agent_description().get(agent_type) or {}
    if not isinstance(meta, dict):
        return []
    return _norm_names(meta.get("plugins"))


def desired_plugins_for_tags(tags) -> list[str]:
    """引擎角色标签 → Agent 类型种子默认插件并集（挂点③④共享口径）。

    runtime.py 注册响应下发与 settings.py 健康自检自检口径共用本入口，
    避免两处各自解析 role_tags 产生口径漂移。

    - 显式角色标签（非 'all'）→ 各类型 agent.plugins 并集去重排序；
    - 'all' / 无标签 → 不注入种子默认：'all' 引擎可认领任意角色任务，
      device 级 installed_plugins 难以装齐全类型种子并集（健康自检易长期标黄），
      种子期望改由显式标签或控制台远程安装（desired_plugins）收敛。
    """
    norm: list[str] = []
    for t in tags or []:
        if not isinstance(t, str):
            continue
        s = t.strip().lower()
        if s:
            norm.append(s)
    if not norm or "all" in norm:
        return []
    out: set[str] = set()
    for t in norm:
        out.update(get_agent_plugins(t))
    return sorted(out)


def installed_plugin_names(runtime_id: int = 0) -> list[str]:
    """查询 Runtime 已上报的 installed_plugins 名单（挂点④，Slice E 落库）。

    runtime_id<=0 或列/表尚未就绪（旧库未升级、worker 独立进程）时返回 []，
    不阻塞调用方——软引用口径下"未知即缺失"，由运行时协商补装。
    """
    if not runtime_id or runtime_id <= 0 or db is None:
        return []
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT installed_plugins FROM agent_runtimes WHERE id=?", (runtime_id,)).fetchone()
        if not row or not row[0]:
            return []
        import json as _json
        data = _json.loads(row[0])
        return _norm_names([
            (p.get("name") if isinstance(p, dict) else p) for p in data
        ])
    except Exception:
        return []


def resolve_plugin_credentials(plugin_name: str, personal=None, repo=None,
                               agent=None, node=None) -> dict:
    """按作用域合并单个插件的凭据配置（节点 > Agent > 仓库 > 个人）。

    每个作用域是 {凭据键: 值} dict；高优先级作用域的非空值
    （""/None 视为未配置）覆盖低优先级。返回合并后的 dict。
    """
    resolved: dict = {}
    for scope in reversed(_SCOPE_ORDER):  # personal → repo → agent → node
        cfg = {"personal": personal, "repo": repo, "agent": agent, "node": node}[scope]
        if not isinstance(cfg, dict):
            continue
        for k, v in cfg.items():
            if not k:
                continue
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            resolved[str(k)] = v
    return resolved
