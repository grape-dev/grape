"""agent_tags 派生标签（Issue #1208：selector 标签化调度的数据底座）。

服务端在注册/心跳时派生三维度冒号标签（§2.1 agent_tags 表）：
- ``engine:<name>``  = agent_runtimes.name（引擎类型名）
- ``scope:<global|user|repo>`` = 凭据 scope
- ``scene:<tag>``    = agent_runtimes.scene_tags（场景市场名，空回退 default）

存储（D4）：capabilities 行式标签——派生行与用户手设能力行同表（engine_capabilities），
派生行只占冒号命名空间（``tag LIKE '%:%'``），用户平铺行（vod/coding/all…）永不触碰；
``get_tags``（worker 能力面）过滤冒号行，派生维度只服务端消费。
platform:/worker: 维度本期数据先行暂缓消费；label: 用户自定义维度预留（v1 不实现）。

刷新时机：注册/心跳时写（runtime.py 两链挂点）；引擎换绑/凭据 scope 切换随
#646 scope 同步事件后由下一次心跳收敛（幂等替换，无残留双值行）。
"""
import json
from datetime import datetime, timezone

from src.kernel.config import log

_KNOWN_SCOPES = ("global", "user", "repo")


def derive_agent_tags(engine_name: str, cred_scope: str, scene_tags,
                        engine_type: str = "") -> list:
    """按三维度派生 agent_tags（纯函数，大小写归一，脏值容错）。

    [云引擎标签修复 2026-11-11 用户裁定] engine: 维度用 **engine_type** 派生——
    云沙箱引擎 name 是 i5 四段全名（如 hermes:hermes-git-acp:s2:4），按 name 派生
    会产出 engine:<四段名> 标签，selector 按 engine:hermes 精确匹配时云引擎
    永不命中（实测 S2 s2:4 引擎 0 命中根因）。engine_type 缺失（本机引擎
    name==type）回退 name，零回归。
    - engine_name 为空（异常行）→ 不派生 engine: 维度；
    - cred_scope 不在三档值域 → 归一 global（与注册侧 cred_scope 兜底一致）；
    - scene_tags 空/脏 → 回退 ["default"]（与 query_engines 读侧兜底一致）。
    """
    tags = []
    name = ((engine_type or "") or engine_name or "").strip().lower()
    if name:
        tags.append(f"engine:{name}")
    scope = (cred_scope or "global").strip().lower()
    if scope not in _KNOWN_SCOPES:
        scope = "global"
    tags.append(f"scope:{scope}")
    scene_list = []
    if isinstance(scene_tags, str):
        try:
            scene_list = json.loads(scene_tags) if scene_tags else []
        except Exception:
            scene_list = []
    elif isinstance(scene_tags, list):
        scene_list = scene_tags
    if not isinstance(scene_list, list):
        scene_list = []
    for t in scene_list:
        v = str(t or "").strip().lower()
        if v:
            tags.append(f"scene:{v}")
    if not any(t.startswith("scene:") for t in tags):
        tags.append("scene:default")
    return tags


def sync_derived_agent_tags(c, rid: int, engine_name: str, cred_scope: str,
                            scene_tags, engine_uid: str = "",
                            engine_type: str = "") -> list:
    """把派生标签落 engine_capabilities 冒号行（幂等）+ 刷新 agent_tags JSON 快照。

    事务契约：调用方持有 db_lock 并负责 commit（与 runtime.py 注册/心跳既有
    cursor 用法一致；c 为已开启事务的 cursor）。
    - 先删该引擎全部冒号行（只动派生命名空间，用户手设平铺行不触碰）；
    - 再插当前派生行（engine_uid 引用同步，孤儿防护对齐注册默认 'all' 行口径）；
    - 最后回写 agent_runtimes.agent_tags JSON 快照（含平铺 + 派生，settings
      视图/前端按 tags 过滤消费）。
    返回当前派生标签列表（调用方可留痕）。
    """
    derived = derive_agent_tags(engine_name, cred_scope, scene_tags, engine_type=engine_type)
    try:
        # 心跳高频路径写放大防护：派生集与现有冒号行一致且快照已同步 → 跳过写
        # （注册/心跳幂等语义不变；变更时才落 DELETE+INSERT；快照列缺失降级继续写）
        try:
            existing = [r[0] for r in c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND tag LIKE '%:%'", (rid,)).fetchall()]
            _snap_row = c.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?",
                                  (rid,)).fetchone()
            try:
                snapshot_now = json.loads(_snap_row[0]) if (_snap_row and _snap_row[0]) else []
            except Exception:
                snapshot_now = []
            _plain = [r[0] for r in c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND tag NOT LIKE '%:%'", (rid,)).fetchall()]
            if sorted(existing) == sorted(derived) and isinstance(snapshot_now, list) \
                    and sorted(set(snapshot_now)) == sorted(set(existing + _plain)):
                return derived
        except Exception:
            pass  # 预检失败（缺列/缺表）→ 走常规写路径，行为与未优化一致
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag LIKE '%:%'", (rid,))
        now = datetime.now(timezone.utc).isoformat()
        for tag in derived:
            c.execute(
                "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, "
                "enabled, created_at, updated_at, engine_uid) VALUES (?,?,1,50,1,?,?,?)",
                (rid, tag, now, now, engine_uid or None))
        _rows = c.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,)).fetchall()
        snapshot = sorted({r[0] for r in _rows if r[0]})
        c.execute("UPDATE agent_runtimes SET agent_tags=? WHERE id=?",
                  (json.dumps(snapshot, ensure_ascii=False), rid))
    except Exception as e:
        # 未迁移旧库（缺列/缺表）降级：派生失败不阻断注册/心跳主流程
        log.warning(f"sync_derived_agent_tags failed for runtime {rid}: {e}")
        return derived
    return derived


def load_engine_tags(c, rid: int) -> set:
    """读取引擎 agent_tags 快照（含派生维度 + 平铺能力行）——selector 匹配的 engine_tags 输入。

    冒号派生行以 engine_capabilities 为权威（行式），平铺能力行同表；
    任一异常降级为空集（空集时非空 selector 必不命中——fail-closed 语义）。
    Lock contract：调用方已持有 db_lock。
    """
    try:
        rows = c.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,)).fetchall()
        return {str(r[0] or "").strip().lower() for r in rows if r[0]}
    except Exception as e:
        log.warning(f"load_engine_tags failed for runtime {rid}: {e}")
        return set()
