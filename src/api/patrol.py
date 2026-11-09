"""A2A 链路巡检器（拉模型兜底；Phase 1 只读检测 + Phase 2 补偿执行者见 patrol_compensate.py）。

docs/weekly-09/a2a-chain-patrol-design.md / -requirements.md 为本模块的权威规格：
- 三层判定 L1→L2→L3（D11）：L1 事件表全状态反查、L2 下游关联任务（任意状态）、
  L3 才是等待窗（confirm 窗，meta patrol:confirm_minutes，默认 10 分钟）。
- 僵尸证据化（D12）：running 两档（stream_ended=1 确定性 / 2h 无信号低置信）；
  queued 确定性归类——引擎健康分形态（远端 credential_id>0 看
  status=online+last_seen_at 新鲜；本机 cid=0 看 installed+healthy，
  无心跳语义，engine_select.py:103-118 同款规则）。
- 停摆链（D13）：非独立扫描，由断链结果按 issue 聚合派生。
- 判定范围跟随链路模式（D17）：PR 订阅链（issue_id `#pr_` 前缀）被 a2a 端点
  设计性拦截（events.py:1240），本无交棒语义，排除出断链候选。
- 终态化唯一通道（D18）：本模块零 `UPDATE agent_tasks SET status` 写路径，
  终态化只属 scheduler 的 PENDING_SELF_REPORT_TIMEOUT 通道。
- 集群选主（D3）：meta 谓词 UPDATE 原子抢占（rowcount=1 才是主），
  仿 event_store.py:189 的 event_active_guard 抢占模式。
- 本 Phase 只读：仅 INSERT grape_t_patrol_findings（唯一约束去重），不改既有表。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from src.config import CONFIG_DIR, log
from src.session_manager import db, db_lock

# ====== 配置面（决策变更 2026-11-02 负责人拍板）：全部走 configs/patrol-config.json
# （example 入库、正式文件 gitignored，缺省即全默认=能力全开），不再读任何 env。
# 运行期紧急控制走 meta 键（每周期重读，无需重启）：
#   patrol:compensate_enabled   —— 补偿总开关（一键全关）
#   patrol:compensate_blocklist —— 补偿黑名单（逗号分隔 repo_key；*=全拦）
# 坏值一律回落默认+告警（fail-fast 不静默，对齐 GateHandler 对 meta 非法值的口径）。======
PATROL_CONFIG_PATH = os.path.join(CONFIG_DIR, "patrol-config.json")

_DEFAULTS = {
    "interval_seconds": 300,     # 巡检周期（D9 决策：默认 5min）
    "confirm_minutes": 10,       # 断链确认窗（D4 决策：默认 10min）
    "lookback_mode": "runtime",   # D17 两档 LOOKBACK：audit=存量核对（全历史）/runtime=常规（7 天窗）
    "compensate_enabled": True,  # 补偿默认开（决策变更：能力默认生效，紧急关走 meta）
}

_INT_KEYS = ("interval_seconds", "confirm_minutes")


def _load_patrol_config(path: str = "") -> dict:
    """读 configs/patrol-config.json：缺省/坏 JSON/坏类型 → 逐键回落默认+告警。"""
    cfg = dict(_DEFAULTS)
    path = path or PATROL_CONFIG_PATH
    try:
        if not os.path.exists(path):
            return cfg
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"顶层须为对象，得到 {type(data).__name__}")
    except Exception as e:
        log.warning(f"[patrol] {os.path.basename(path)} 读取失败（{e}），全部走默认")
        return cfg
    for k, default in _DEFAULTS.items():
        v = data.get(k)
        if v is None:
            continue
        try:
            if k in _INT_KEYS:
                if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                    raise ValueError(f"{k}={v!r} 须为正整数")
            elif k == "lookback_mode" and v not in ("audit", "runtime"):
                raise ValueError(f"lookback_mode={v!r} 须为 audit/runtime")
            elif k == "compensate_enabled" and not isinstance(v, bool):
                raise ValueError(f"compensate_enabled={v!r} 须为布尔")
            cfg[k] = v
        except ValueError as e:
            log.warning(f"[patrol] {os.path.basename(path)} 配置非法（{e}），该键走默认 {default!r}")
    return cfg


_cfg = _load_patrol_config()   # 文件值仅作首次 seed 与缺省回退；运行真源是 DB meta（集群共享）

# ====== 运行真源：DB meta 键（每周期重读，改即生效，双机主从共享一份；决策变更 2026-11-02） ======
_INTERVAL_META = "patrol:interval_seconds"
_CONFIRM_META = "patrol:confirm_minutes"
_LOOKBACK_META = "patrol:lookback_mode"
# 运行期紧急控制键（同属 meta 真源）：
#   patrol:compensate_enabled   —— 补偿总开关（一键全关）
#   patrol:compensate_blocklist —— 补偿黑名单（逗号分隔 repo_key；*=全拦）
_COMPENSATE_ENABLED_META = "patrol:compensate_enabled"
_COMPENSATE_BLOCKLIST_META = "patrol:compensate_blocklist"

_META_KEY_OF = {
    "interval_seconds": _INTERVAL_META,
    "confirm_minutes": _CONFIRM_META,
    "lookback_mode": _LOOKBACK_META,
    "compensate_enabled": _COMPENSATE_ENABLED_META,
}


def _meta_int(key: str, default: int, minimum: int = 1) -> int:
    raw = _meta_value(key)
    if not raw:
        return default
    try:
        v = int(raw)
        if v < minimum:
            raise ValueError(f"< {minimum}")
        return v
    except ValueError as e:
        log.warning(f"[patrol] {key}={raw!r} 非法（{e}），按默认 {default} 处理（fail-fast 不静默）")
        return default


def _meta_mode(default: str) -> str:
    raw = _meta_value(_LOOKBACK_META)
    if not raw:
        return default
    if raw in ("audit", "runtime"):
        return raw
    log.warning(f"[patrol] {_LOOKBACK_META}={raw!r} 须为 audit/runtime，按默认 {default!r} 处理")
    return default


def patrol_runtime_config() -> dict:
    """每周期读取的运行配置（DB meta 真源，集群共享，改即生效无需重启）。

    优先级：meta > configs/patrol-config.json（seed/回退） > 代码默认。
    坏值回落并告警（fail-fast 口径与 GateHandler 对非法 meta 值一致）。
    """
    return {
        "interval_seconds": _meta_int(_INTERVAL_META, _cfg["interval_seconds"]),
        "confirm_minutes": _meta_int(_CONFIRM_META, _cfg["confirm_minutes"]),
        "lookback_mode": _meta_mode(_cfg["lookback_mode"]),
        "compensate_enabled": compensate_enabled(),
    }


def _seed_meta_from_file() -> int:
    """首次启动：文件配置 seed 进 meta 表（表里已有键不覆盖——login-whitelist 先例，
    db_setup._migrate_whitelist_from_file 同款语义：此后以表为准，文件不再参与）。"""
    seeded = []
    try:
        from src.user.db import get_meta_value, set_meta_value
        for k, mkey in _META_KEY_OF.items():
            v = _cfg.get(k)
            if v is None:
                continue
            if (get_meta_value(mkey) or "").strip():
                continue
            sval = str(v).lower() if isinstance(v, bool) else str(v)
            set_meta_value(mkey, sval)
            seeded.append(mkey)
        if seeded:
            log.info(f"[patrol] 配置文件 seed 进 meta（此后以表为准）: {seeded}")
    except Exception as e:
        log.warning(f"[patrol] 配置 seed 失败（继续以代码默认+meta 为准）: {e}")
    return len(seeded)

# ====== 阈值常量（无调参决策的值不进配置文件，少一项就少一处坏值故障面） ======
_LOOKBACK_RUNTIME_DAYS = 7    # runtime 回看窗（设计定值；audit 模式覆盖全历史不受此影响）
_RUNNING_ZOMBIE_HOURS = 2     # running 无信号僵尸窗（D12 判例定值）
_QUEUED_ZOMBIE_HOURS = 24     # queued 引擎在线观察兜底窗（D12 判例定值）
# 引擎心跳新鲜度不设 patrol 副本：_engine_unreachable 内单源引用
# src.runtime.engine_select._RUNTIME_STALE_SECONDS（平台既有 RUNTIME_STALE_SECONDS 配置）。
_LEADER_TTL_MULTIPLIER = 2  # 心跳窗 = 2×周期（集群时钟漂移余量，设计 §八）

_LEADER_KEY = "patrol:leader"

# ====== 交棒幂等键规范（决策 A，2026-11-02 负责人拍板）======
# 平台规范交棒键 handoff:{task_id}:{target_agent}：patrol 与 skill 守门件按同一
# 派生式生成（本函数为单源，严禁调用方自行拼接——护栏①）。
# 效果：迟到原交棒与补偿撞同一键 → 端点幂等（idx_events_idem 唯一索引 + 三层
# 防重）命中返回已有事件 → 残余竞态降级为良性先到先得（无论谁先到，恰好一次
# 同构交棒）。skill 侧落地前 patrol 单侧使用，键空间与原生交棒不相撞，
# 行为与旧 patrol-comp 键完全等价（单调不劣）；skill 侧渐进覆盖后窗口闭合。
HANDOFF_KEY_TEMPLATE = "handoff:{task_id}:{target_agent}"


def handoff_idempotency_key(task_id: str, target_agent: str) -> str:
    """规范交棒键派生（单源）。task_id/target_agent 缺一即拒——端点幂等键
    非空是唯一索引语义的前提（SQLite/MySQL 唯一索引对 NULL/空互不相等，
    空键=永不幂等，护栏②）。"""
    if not task_id or not str(task_id).strip() or not target_agent or not str(target_agent).strip():
        raise ValueError(f"handoff key requires non-empty task_id and target_agent "
                         f"(got task_id={task_id!r}, target_agent={target_agent!r})")
    return HANDOFF_KEY_TEMPLATE.format(task_id=str(task_id).strip(),
                                       target_agent=str(target_agent).strip())

# ====== 时间规范（repo 统一口径：UTC aware ISO） ======


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str):
    """健壮解析库内两种时间格式，返回 aware datetime（None=不可解析）。

    - agent_tasks.completed_at：aware ISO（+00:00）
    - events.created_at：naive 空格格式 'YYYY-MM-DD HH:MM:SS'
    naive 归一与存储层口径一致（Issue #512）：sqlite=UTC；mysql=CST(+08:00)。
    注意：src.runtime.health.parse_dt 返回 float 时间戳而非 datetime，勿混用。
    """
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        try:
            from src.api.time_utils import _db_backend
            if _db_backend() == "mysql":
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            else:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _minutes_since(dt) -> float:
    if dt is None:
        return -1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


# ====== D3：谓词 UPDATE 原子选主（rowcount=1 才是主） ======


def _meta_value(key: str) -> str:
    """读 meta 键（动态配置源，异常回退空串=未配置）。"""
    try:
        from src.user.db import get_meta_value
        return (get_meta_value(key) or "").strip()
    except Exception:
        return ""


def compensate_enabled() -> bool:
    """补偿总开关（决策变更 2026-11-02：默认开，能力默认生效）。

    优先级：meta 键 patrol:compensate_enabled（运行期紧急控制，每周期重读）
    > configs/patrol-config.json 的 compensate_enabled > 默认 True。
    meta='false'/'0'/'no' → 关；非法值按关处理并告警（fail-fast：配置错误
    时不自动派发，但告警可见）。
    """
    raw = _meta_value(_COMPENSATE_ENABLED_META)
    if not raw:
        return bool(_cfg["compensate_enabled"])
    val = raw.strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    log.warning(f"[patrol] invalid {_COMPENSATE_ENABLED_META}={raw!r}, treated as false")
    return False


def compensate_repo_blocked(repo_key: str) -> bool:
    """补偿黑名单（决策变更：白名单删除，改为异常才配置的黑名单）。

    meta 键 patrol:compensate_blocklist 逗号分隔 repo_key；默认/空=不拦任何仓
    （补偿全仓生效）；列表命中 → 该仓不补；'*' → 全拦（紧急刹车）。
    """
    raw = _meta_value(_COMPENSATE_BLOCKLIST_META)
    if not raw:
        return False
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if "*" in items:
        return True
    return repo_key in items


def _leader_expired(cur: str, cutoff: str) -> bool:
    """leader 值过期判定：新格式 `holder|iso` 取时间戳段比较；兼容历史纯 ISO 值。"""
    if not cur:
        return True
    ts = cur.split("|", 1)[1] if "|" in cur else cur
    return ts < cutoff


def claim_patrol_leader(holder: str, interval_seconds: int) -> bool:
    """抢占/续约巡检主（评审三轮修正：自续约必须真实生效）。

    value 格式 `holder|iso_ts`（持有者+时间戳）。流程：
    1. 自续约：value 前缀精确等于 `holder|` → 无条件续（原子，自家钥匙开自家锁）；
       评审四轮修正：前缀比较用 substr 精确匹配，不用 LIKE——holder 含 `_`/`%` 时
       LIKE 通配符会让仅差一个字符的主机名互开对方的锁（双主扫描）。
    2. 读现值：非空且未过期 → 非主返回 False；
    3. CAS 接管：UPDATE ... WHERE value=<读到的旧值>（双主并发只有一方 rowcount=1）；
    4. 引导期：行不存在/空值 → INSERT OR IGNORE（良性竞态，db_setup.py:4158 先例同款）。
    cutoff 与时间戳段统一 aware ISO（Python 预计算，禁 DB 方言时间函数）。
    """
    cutoff = datetime.fromtimestamp(
        time.time() - interval_seconds * _LEADER_TTL_MULTIPLIER,
        tz=timezone.utc).isoformat()
    new_val = f"{holder}|{_now_iso()}"
    with db_lock:
        c = db.cursor()
        try:
            # 1) 自续约（holder 前缀精确匹配 = 自家持有；substr 精确前缀，
            #    防 LIKE 单字通配符 `_` 让 host_a 续掉 host-a 的锁——评审四轮修正）
            c.execute(
                "UPDATE meta SET value=? WHERE key=? AND substr(value,1,?)=?",
                (new_val, _LEADER_KEY, len(holder) + 1, holder + "|"))
            db.commit()
            if c.rowcount == 1:
                return True
            # 2) 读现值判断可否接管
            c.execute("SELECT value FROM meta WHERE key=?", (_LEADER_KEY,))
            row = c.fetchone()
            cur = (row[0] or "") if row else ""
            if cur and not _leader_expired(cur, cutoff):
                return False   # 他主持有且新鲜
            # 3) CAS 接管：仅当值仍是读到的那份（空值行同样精确匹配 ''）
            c.execute(
                "UPDATE meta SET value=? WHERE key=? AND value=?",
                (new_val, _LEADER_KEY, cur))
            db.commit()
            if c.rowcount == 1:
                return True
            # 4) 行不存在（UPDATE 撞不到）→ 引导期 INSERT
            if not cur:
                c.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?,?)",
                    (_LEADER_KEY, new_val))
                db.commit()
                return c.rowcount == 1
            return False   # CAS 失败：并发被他人改写，本轮让位
        except Exception as e:
            log.warning(f"[patrol] leader claim failed: {e}")
            return False


# ====== D17：断链候选提取与链路模式过滤 ======

_PR_SUB_PREFIX = "#pr_"
_NEXT_STEP_RE = re.compile(r"^a2a[-:]\s*([a-zA-Z0-9_\-]+)", re.IGNORECASE)


def extract_next_step(result_text: str) -> str:
    """从 result_text 结构化 JSON 提取 next_step（D6：解析 JSON，不加列）。"""
    if not result_text:
        return ""
    try:
        data = json.loads(result_text)
        if isinstance(data, dict):
            return str(data.get("next_step") or "").strip()
    except Exception:
        pass
    return ""


def parse_target_agent(next_step: str) -> str:
    """`a2a-qa` / `a2a:qa` → `qa`；解析不出返回空（双源判定退化，仅 L1+窗口）。"""
    m = _NEXT_STEP_RE.match((next_step or "").strip())
    return m.group(1).strip().lower() if m else ""


def _is_pr_subscription_chain(issue_id: str) -> bool:
    """PR 订阅链伪 ID（`#pr_` 前缀）：a2a 端点设计性拦截、本无交棒语义（D17）。"""
    return (issue_id or "").startswith(_PR_SUB_PREFIX)


# ====== L1 / L2 反查（D11：全状态口径 + 关联性约束） ======


def issue_terminal(repo_key: str, issue_id: str) -> str:
    """issue 终态判定（评审五轮修正①：人已处置证据——终检/检测双守卫）。

    返回终态值（'completed' / 'pending_review' / 'cancelled'），非终态/查不到返回 ''。
    依据：用户不走修链而直接收敛 issue（confirm_issue_done 置 completed，
    issues.py:676；审阅流可置 pending_review）。此时 L1/L2 找不到"交棒证据"
    是正常的——链已被人关闭，补偿会把已收敛 issue 复活（派发下游 agent、
    评论、回推 stage），属行为伤害。a2a 端点只拦 cancelled（webhook.py:1010
    附近），completed/pending_review 不拦——巡检必须自守。
    """
    if not repo_key or not issue_id:
        return ""
    num = str(issue_id).lstrip("#")
    if not num.isdigit():
        return ""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
            (repo_key, int(num)))
        row = c.fetchone()
    status = (row[0] or "").strip() if row else ""
    if status in ("completed", "pending_review", "cancelled"):
        return status
    return ""


def l1_event_exists(repo_key: str, issue_id: str, completed_at: str,
                    target_agent: str = "") -> bool:
    """L1：issue 在任务完成后是否出现过交棒 a2a_event（全状态，不只 running）。

    评审十六轮修正④/S32 棒次过滤：target_agent 已知时只认 agent_type 匹配该棒的
    a2a_event——同 issue 的其他棒次事件（人工补早前链、别的链路交棒）不再构成
    本棒的"已交棒"证据（原口径会把真断链误判为已交接，漏报且无痕）。口径与端点
    active-guard（webhook.py:86 按 agent_type 过滤）对齐；target 不可解析时保持
    宽口径（任意 a2a_event 即证据，误报=0 优先）。
    events.created_at 为 naive 空格格式、agent_tasks.completed_at 为 aware ISO——
    跨格式必须 Python 侧解析比较，禁止字符串比较。
    """
    done_dt = _parse_dt(completed_at)
    if done_dt is None:
        # 完成时间不可解析 → 无法关联，保守视为无证据（交给 L2/窗口）
        return False
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT created_at, agent_type FROM events WHERE repo_key=? AND issue_id=? AND event_type='a2a_event'",
            (repo_key, issue_id))
        rows = c.fetchall()
    for row in rows:
        created = row[0]
        evt_agent = str(row[1] or "").strip().lower() if len(row) > 1 else ""
        evt_dt = _parse_dt(created or "")
        if evt_dt is None or evt_dt < done_dt:
            continue
        if target_agent and evt_agent != str(target_agent).strip().lower():
            continue  # S32：其他棒次的交棒不算本棒证据
        return True
    return False


def l2_downstream_exists(repo_key: str, issue_id: str, target_agent: str,
                         completed_at: str, parent_issue: str = "") -> bool:
    """L2：下游关联任务是否存在（任意状态，含 completed/failed）。

    关联性约束（D11①）：同 issue + 目标节点，且 created_at > 本任务 completed_at。
    子任务链（D17）：关联键换 parent_issue + agent_type（主链同 issue 任务不构成证据）。
    """
    done_dt = _parse_dt(completed_at)
    if done_dt is None:
        return False
    if parent_issue:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_at FROM agent_tasks WHERE repo_key=? AND issue_id=? AND agent_type=?",
                (repo_key, parent_issue, target_agent))
            rows = c.fetchall()
    else:
        if not target_agent:
            return False
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_at FROM agent_tasks WHERE repo_key=? AND issue_id=? AND agent_type=?",
                (repo_key, issue_id, target_agent))
            rows = c.fetchall()
    for (created,) in rows:
        dt = _parse_dt(created or "")
        if dt is not None and dt > done_dt:
            return True
    return False


def _subtask_parent(repo_key: str, issue_id: str) -> str:
    """D17：子任务链的 L2 关联键（评审六轮修正②接线；七轮修正③补双源）。

    双源读取与 ModeHandler/get_issue_stage 同款：issue 单表为权威
    （Phase C3 已收编并停写 issue_state），issue_state 仅停写过渡期兜底——
    只读 issue_state 会漏掉 C3 新建的子任务链（L2 退化主链键，丢 D17
    防误配保护）。标记 is_subtask_mode=1 且 parent_issue_id 非空 → 返回
    parent_issue（L2 改按 parent_issue+agent_type 找证据，主链同 issue
    任务不构成子任务链正向证据，UC1-12）；否则 ''（主链口径）。
    """
    if not repo_key or not issue_id:
        return ""
    try:
        from src.api.session_store import get_issue_stage
        st = get_issue_stage(repo_key, issue_id) or {}
    except Exception:
        st = {}
    try:
        sub = int(st.get("is_subtask_mode") or 0)
    except (TypeError, ValueError):
        sub = 0
    parent = (st.get("parent_issue_id") or "").strip()
    return parent if (sub == 1 and parent) else ""


def _resolve_owner(repo_key: str) -> int:
    """D16：归属人维度，与 D14 托管模式同源。

    路径 A 兜底（评审十四轮修正①，UC8-2 补口）：host_user_id 无值时回落
    user_repos 管理员映射（与 IdentityHandler.ManagedUserFallbackStrategy 同款
    口径）——bot token 托管仓的 finding 不再误标 unowned（误置顶=信号过载，
    且后续通知路由漏派）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT host_user_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 "
                "ORDER BY created_at LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
    return 0


# ====== findings 落库（D7：新表只增；唯一约束 + INSERT OR IGNORE 去重） ======


def _known_task_ids() -> set:
    """评审十六轮修正③/S31：已落库 finding 的 (kind, task_id) 集合。

    audit 模式每周期全历史重扫，唯一约束只防重插不防重扫——已知名
    （含 dismissed/false_positive/compensated/needs_human 各终态）短路掉
    逐任务反查（L1/L2/引擎探活），重扫成本从 O(任务数×反查) 收敛到
    O(任务数)+1 条集合查询；audit 模式忘切回 runtime 也不再是慢性病。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT kind, task_id FROM grape_t_patrol_findings WHERE task_id != ''")
            return {(r[0], r[1]) for r in c.fetchall()}
    except Exception:
        return set()


def insert_findings_batch(findings: list, chunk: int = 500) -> int:
    """批量落库（S48/R20-A）：攒批 executemany + 每块单次 commit。

    逐条 INSERT+commit 的成本：SQLite=逐条 fsync、MySQL=逐条事务网络往返，
    100 条 finding=100 次事务提交且全程串行持锁——与"重扫收敛到 O(任务数)+1
    条查询"的检测端设计不对称。INSERT OR IGNORE 天然幂等，批内合并安全；
    owner 归属按 repo_key 先去重解析（原逐条 _resolve_owner 也是 N+1，且
    其内部持锁不可在批量锁内调用）；单块失败吞掉（该块丢失其余块照常——
    S5 口径，同 _fetch_result_texts 的单块容错模式）。返回实际插入数
    （executemany rowcount 只计真实插入，OR IGNORE 被吞行不计）。
    """
    if not findings:
        return 0
    owner_cache = {rk: _resolve_owner(rk) for rk in
                   {f.get("repo_key") or "" for f in findings}}   # 锁外逐仓去重解析
    now = _now_iso()
    inserted = 0
    with db_lock:
        c = db.cursor()
        for i in range(0, len(findings), chunk):
            batch = findings[i:i + chunk]
            rows = []
            for f in batch:
                import uuid as _uuid
                rows.append((str(_uuid.uuid4()), f.get("kind") or "",
                             f.get("task_id") or "", int(f.get("event_seq") or 0),
                             f.get("repo_key") or "", f.get("issue_id") or "",
                             owner_cache.get(f.get("repo_key") or "", 0),
                             f.get("detail") or "", f.get("suggestion") or "",
                             "open", "", now, now))
            try:
                c.executemany(
                    "INSERT OR IGNORE INTO grape_t_patrol_findings "
                    "(id, kind, task_id, event_seq, repo_key, issue_id, owner, detail, suggestion, status, action_meta, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                db.commit()
                inserted += c.rowcount if (c.rowcount or 0) > 0 else 0
            except Exception as e:
                log.warning(f"[patrol] batch insert findings failed "
                            f"(chunk {i // chunk}, {len(rows)} rows): {e}")
    return inserted


def insert_finding(kind: str, task_id: str = "", event_seq: int = 0,
                   repo_key: str = "", issue_id: str = "",
                   detail: str = "", suggestion: str = "") -> bool:
    """落库一条 finding。id 一律 UUID（硬性要求：新表主键 UUID，禁自增）；
    task_id 为 agent_tasks 的 UUID 主键（空串=非任务维度 finding）；
    UNIQUE(kind, task_id, issue_id, event_seq) + INSERT OR IGNORE = 双主同轮静默去重（S18：issue_id 进键）。"""
    import uuid as _uuid
    owner = _resolve_owner(repo_key)
    now = _now_iso()
    fid = str(_uuid.uuid4())
    with db_lock:
        c = None
        try:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO grape_t_patrol_findings "
                "(id, kind, task_id, event_seq, repo_key, issue_id, owner, detail, suggestion, status, action_meta, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, kind, task_id, event_seq, repo_key, issue_id, owner, detail,
                 suggestion, "open", "", now, now))
            db.commit()
            return c.rowcount == 1
        except Exception as e:
            # S5：落库失败吞掉返回 False（S43-3 实测抓出的缺陷修复——cursor()
            # 原在 try 外，DB 异常外抛会把整轮扫描打进退避，单条失败不该伤循环）
            log.warning(f"[patrol] insert finding failed ({kind}): {e}")
            return False


# ====== 断链检测（L1→L2→L3） ======


def _fetch_result_texts(task_ids: list, chunk: int = 500) -> dict:
    """按 id 批量回捞 result_text（P1/S45 两段式拉取的阶段 2）。

    阶段 1 只拉轻量列——已知/终态/未超窗任务的 result_text（MEDIUMTEXT，
    2KB+/行）不再随候选拉取；本函数只对幸存候选分块（500/批）回捞。
    单块失败吞掉（S5：缺文本该候选判 "" 走不报口径，不炸整轮）。
    """
    out = {}
    if not task_ids:
        return out
    with db_lock:
        c = db.cursor()
        for i in range(0, len(task_ids), chunk):
            ids = list(task_ids[i:i + chunk])
            sql = ("SELECT id, result_text FROM agent_tasks WHERE id IN "
                   f"({','.join('?' * len(ids))})")
            try:
                c.execute(sql, tuple(ids))
                for tid, rt in c.fetchall():
                    out[tid] = rt or ""
            except Exception as e:
                log.warning(f"[patrol] result_text batch fetch failed "
                            f"(chunk {i // chunk}): {e}")
    return out


def _iter_completed_tasks(cutoff: str, chunk: int = 500):
    """keyset 分页流式拉取候选（S49/R20-C）。

    audit 全历史一次 fetchall 的峰值内存=O(全历史行)（万级任务下 Python
    元组对象开销 ~500B/行）；改为按 id > 游标 ORDER BY id LIMIT chunk 分页：
    每页独立完整查询（锁内取页、锁外处理——cursor 不跨锁存活，规避共享
    连接上并发执行语句的迭代语义风险），峰值内存 O(500 行)。
    分页期间并发插入的新任务由 INSERT OR IGNORE 幂等兜底（同发现不重插）。
    id 为 TEXT 主键（自动索引），id > '' 匹配全部非空 id。
    """
    last_id = ""
    while True:
        with db_lock:
            c = db.cursor()
            if cutoff:
                c.execute(
                    "SELECT id, repo_key, issue_id, agent_type, completed_at "
                    "FROM agent_tasks WHERE status='completed' AND completed_at > ? "
                    "AND id > ? ORDER BY id LIMIT ?",
                    (cutoff, last_id, chunk))
            else:  # D17 存量核对模式：全历史（分页同样适用）
                c.execute(
                    "SELECT id, repo_key, issue_id, agent_type, completed_at "
                    "FROM agent_tasks WHERE status='completed' AND id > ? "
                    "ORDER BY id LIMIT ?",
                    (last_id, chunk))
            rows = c.fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last_id = rows[-1][0]


def detect_broken_chains(lookback_cutoff_iso: str = "", known: set = None) -> list[dict]:
    """三层判定断链（Phase 1 只读）。返回 finding 描述列表（不落库，由调用方聚合）。

    known：S31 已知集共享参数（S51/R22——轮内一次拉取、双检测共享引用；
    None=自行拉取，兼容独立调用/旧签名）。语义等价：落库发生在两检测之后
    （run_patrol_once 批量落库），轮内两检测看到同一快照恒成立。
    """
    confirm_minutes = patrol_runtime_config()["confirm_minutes"]
    findings = []
    if known is None:
        known = _known_task_ids()   # S31：已落库 finding 短路逐任务反查
    # S49/R20-C：keyset 分页流式（每页独立查询、锁内取页锁外处理）——
    # P1/S45 的轻量列语义不变（result_text 不随候选拉取，阶段 2 幸存者回捞）
    rows = _iter_completed_tasks(lookback_cutoff_iso)
    # 阶段 1 短路（全部不依赖 result_text）：已知→PR 订阅→缺键→未超窗→issue 终态
    # （S47/R19 谓词顺序修正：缺键/超窗是零成本纯谓词，issue_terminal 每候选一条
    # issue 表查询——未超窗任务占候选大头时先查库再被超窗丢弃=查询白做；
    # 纯谓词交换不变性，结果集与产出顺序零变化）
    prefiltered = []
    for (tid, repo_key, issue_id, agent_type, completed_at) in rows:
        try:
            if ("broken_chain", tid) in known:
                continue  # S31：已落库（含各终态），重扫不再反查
            if _is_pr_subscription_chain(issue_id or ""):
                continue  # D17：PR 订阅链无交棒语义
            if not repo_key or not issue_id:
                continue
            done_dt = _parse_dt(completed_at or "")
            age_min = _minutes_since(done_dt)
            if age_min < 0 or age_min < confirm_minutes:
                continue  # L3：未超窗静默
            if issue_terminal(repo_key, issue_id or ""):
                continue  # 评审五轮修正①：issue 已被人收敛，链已处置，非断链
            prefiltered.append((tid, repo_key, issue_id, agent_type,
                                completed_at, age_min))
        except Exception as e:
            log.warning(f"[patrol] broken-chain prefilter failed task={tid}: {e}")
    # 阶段 2：只对幸存候选批量回捞 result_text（短路语义与顺序不变——
    # 全部谓词为纯函数，重排不改变结果集与产出顺序）
    texts = _fetch_result_texts([t[0] for t in prefiltered])
    for (tid, repo_key, issue_id, agent_type, completed_at, age_min) in prefiltered:
        try:
            next_step = extract_next_step(texts.get(tid) or "")
            if not next_step.lower().startswith("a2a"):
                continue
            target_agent = parse_target_agent(next_step)
            if l1_event_exists(repo_key, issue_id, completed_at or "", target_agent):
                continue  # L1 命中：交棒已发生
            if l2_downstream_exists(repo_key, issue_id, target_agent, completed_at or "",
                                    _subtask_parent(repo_key, issue_id or "")):
                continue  # L2 命中：下游任务已出现
            findings.append({
                "kind": "broken_chain",
                "task_id": tid,
                "event_seq": 0,
                "repo_key": repo_key,
                "issue_id": issue_id,
                "detail": json.dumps({
                    "agent_type": agent_type or "",
                    "next_step": next_step,
                    "target_agent": target_agent,
                    "completed_at": completed_at or "",
                    "age_minutes": round(age_min, 1),
                    "l1": "miss", "l2": "miss",
                }, ensure_ascii=False),
                "suggestion": (f"补发交棒 → {target_agent}" if target_agent
                               else "下一棒意图不可解析（next_step 无 agent 名），人工核对"),
            })
        except Exception as e:
            log.warning(f"[patrol] broken-chain judge failed task={tid}: {e}")
    return findings


# ====== 僵尸检测（D12：两档 / 确定性归类） ======


def _engine_unreachable(runtime_id: int) -> bool:
    """分形态引擎不可达（engine_select.py:103-118 同款规则）：
    远端 credential_id>0 = status!=online 或 last_seen_at 过期；
    本机 cid=0 = 非(installed+healthy)（无心跳语义，禁用远端口径）。
    runtime_id<=0（未绑定）不判死 → 交给时间窗档。
    """
    if not runtime_id or runtime_id <= 0:
        return False
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT credential_id, status, last_seen_at, installed, healthy FROM agent_runtimes WHERE id=?",
            (runtime_id,))
        row = c.fetchone()
    if not row:
        return True  # 引擎行都没了 = 确定性不可达
    cred_id, status, last_seen, installed, healthy = row
    if (cred_id or 0) > 0:
        if (status or "") != "online":
            return True
        if last_seen:
            dt = _parse_dt(last_seen)
            if dt is not None:
                # 评审四轮：心跳新鲜度单源引用平台口径（engine_select，env RUNTIME_STALE_SECONDS）
                from src.runtime.engine_select import _RUNTIME_STALE_SECONDS
                if (datetime.now(timezone.utc) - dt).total_seconds() > _RUNTIME_STALE_SECONDS:
                    return True
        return False
    return not (bool(installed) and bool(healthy))


def _scene_missing(repo_key: str, issue_id: str) -> bool:
    """D12 场景存在性：issue 绑定的场景已不存在 → 确定性"永不调度"。

    scene_id 取 issue_state（get_issue_stage 兼容 issue 表/issue_state 双源）；
    scene_id 为空 → False（不判死）；判定复用 get_pipeline_config
    （四源解析：仓库副本/全局行/市场官方/pipelines.json），**结果 fallback=true
    即"未解析到真实场景"（get_pipeline_config 文档约定：调用方按 fallback 拒绝
    静默回退）——场景已删除/不存在时四源不中、回退 default，正是要抓的形态**。
    """
    if not repo_key or not issue_id:
        return False
    try:
        from src.api.session_store import get_issue_stage
        st = get_issue_stage(repo_key, issue_id) or {}
        scene_id = (st.get("scene_id") or "").strip()
        if not scene_id:
            return False
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
        if cfg is None:
            return True
        return bool(cfg.get("fallback"))
    except Exception:
        return False


def detect_zombies(known: set = None) -> list[dict]:
    """running 两档 + queued 确定性归类（Phase 1 只登记/告警，零终态化写——D18）。

    轮内探活缓存（S47/R19）：引擎探活按 runtime_id 去重、场景缺失按
    (repo_key, issue_id) 去重——同 runtime/同 issue 的多任务共享一次查询，
    探活/解析成本从 O(queued 任务数) 收敛到 O(去重键数)。缓存生命周期=
    本轮函数调用（每周期重建，engine 心跳/场景变更下周期自然重判）。
    """
    findings = []
    running_hours = _RUNNING_ZOMBIE_HOURS
    queued_hours = _QUEUED_ZOMBIE_HOURS
    if known is None:
        known = _known_task_ids()   # S31：已落库僵尸 finding 短路引擎/场景探活
    # （S51/R22：run_patrol_once 轮内共享引用，同一集合不再二次全表拉取）
    _rt_probe_cache = {}        # runtime_id -> unreachable
    _scene_probe_cache = {}     # (repo_key, issue_id) -> scene_missing
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, issue_id, agent_type, runtime_id, started_at, created_at, stream_ended "
            "FROM agent_tasks WHERE status='running'")
        running_rows = c.fetchall()
        c.execute(
            "SELECT id, repo_key, issue_id, agent_type, runtime_id, created_at "
            "FROM agent_tasks WHERE status='queued'")
        queued_rows = c.fetchall()
    for (tid, repo_key, issue_id, agent_type, runtime_id, started_at, created_at, stream_ended) in running_rows:
        try:
            if ("zombie_running_signal", tid) in known and int(stream_ended or 0) == 1:
                continue  # S31：同档已落库，重扫不再登记
            if ("zombie_running_window", tid) in known and int(stream_ended or 0) != 1:
                continue  # S31：同档已落库（档位翻转时键不同，仍可重新判定）
            age_min = _minutes_since(_parse_dt((started_at or created_at) or ""))
            if int(stream_ended or 0) == 1:
                findings.append({
                    "kind": "zombie_running_signal", "task_id": tid, "event_seq": 0,
                    "repo_key": repo_key or "", "issue_id": issue_id or "",
                    "detail": json.dumps({"tier": "deterministic", "stream_ended": 1,
                                          "age_minutes": round(max(age_min, 0), 1)}, ensure_ascii=False),
                    "suggestion": "终信号已到状态未流转：Phase 3 终态化候选（走 scheduler 通道，勿在 patrol 写终态）",
                })
            elif age_min >= running_hours * 60:
                findings.append({
                    "kind": "zombie_running_window", "task_id": tid, "event_seq": 0,
                    "repo_key": repo_key or "", "issue_id": issue_id or "",
                    "detail": json.dumps({"tier": "low-confidence", "stream_ended": 0,
                                          "age_minutes": round(age_min, 1)}, ensure_ascii=False),
                    "suggestion": "无信号超窗：低置信登记，供 Phase 3 时长分布分析",
                })
        except Exception as e:
            log.warning(f"[patrol] zombie-running judge failed task={tid}: {e}")
    for (tid, repo_key, issue_id, agent_type, runtime_id, created_at) in queued_rows:
        try:
            if ("zombie_queued_dead", tid) in known or ("zombie_queued_window", tid) in known:
                continue  # S31：queued 两档均已落库（引擎/场景探活每周期省一次）
            age_min = _minutes_since(_parse_dt((created_at) or ""))
            # 引擎不可达只判一次（同一条件复用，避免双倍查询）；
            # 轮内字典缓存（S47/R19）：同 runtime 多任务 / 同 (repo,issue) 多任务
            # 共享一次探活与场景解析——跨任务 N+1 收敛为按去重键 O(distinct)
            rt_key = int(runtime_id or 0)
            if rt_key not in _rt_probe_cache:
                _rt_probe_cache[rt_key] = _engine_unreachable(rt_key)
            _unreachable = _rt_probe_cache[rt_key]
            scene_key = (repo_key or "", issue_id or "")
            if scene_key not in _scene_probe_cache:
                _scene_probe_cache[scene_key] = _scene_missing(*scene_key)
            _scene_gone = _scene_probe_cache[scene_key]
            if _unreachable or _scene_gone:
                findings.append({
                    "kind": "zombie_queued_dead", "task_id": tid, "event_seq": 0,
                    "repo_key": repo_key or "", "issue_id": issue_id or "",
                    "detail": json.dumps({"tier": "deterministic", "runtime_id": runtime_id or 0,
                                          "cause": "engine_unreachable" if _unreachable else "scene_missing",
                                          "age_minutes": round(max(age_min, 0), 1)}, ensure_ascii=False),
                    "suggestion": "引擎不可达或场景已不存在：永不调度，人工处置（重绑/取消）",
                })
            elif age_min >= queued_hours * 60:
                findings.append({
                    "kind": "zombie_queued_window", "task_id": tid, "event_seq": 0,
                    "repo_key": repo_key or "", "issue_id": issue_id or "",
                    "detail": json.dumps({"tier": "window", "age_minutes": round(age_min, 1)}, ensure_ascii=False),
                    "suggestion": "引擎在线但超 24h 未调度：登记观察",
                })
        except Exception as e:
            log.warning(f"[patrol] zombie-queued judge failed task={tid}: {e}")
    return findings


# ====== D13：停摆链 = 断链的 issue 视图（聚合派生，无独立时间窗） ======


def derive_stalled_issues(broken: list[dict]) -> list[dict]:
    by_issue: dict[tuple, list[dict]] = {}
    for f in broken:
        by_issue.setdefault((f["repo_key"], f["issue_id"]), []).append(f)
    stalled = []
    for (repo_key, issue_id), items in by_issue.items():
        stalled.append({
            "kind": "stalled_issue", "task_id": "", "event_seq": 0,
            "repo_key": repo_key, "issue_id": issue_id,
            "detail": json.dumps({"broken_task_ids": [i["task_id"] for i in items],
                                  "view": "issue-aggregate of broken_chain"}, ensure_ascii=False),
            "suggestion": items[0].get("suggestion") or "",
        })
    return stalled


# ====== findings 保留策略（R11/D21，实现后审计落地：终态行 TTL，防表无界增长） ======

_RETENTION_META = "patrol:findings_retention_days"
_RETENTION_DEFAULT_DAYS = 90
# 只清理终态且不再驱动任何动作的状态。open/claiming 是活性工作队列；
# needs_human 是未决人工信号（删了=丢线索，零危害口径），均永不清理。
_TERMINAL_RETENTION_STATUSES = ("dismissed", "false_positive", "compensated",
                                "permanent_failure")

# S40：慢扫描告警阈值（ms）——模块常量（无调参决策依据前不进 meta，
# 与 _LOOKBACK_RUNTIME_DAYS 同款纪律）；命中=告警不熔断。
_SLOW_SCAN_WARN_MS = 30000


def cleanup_terminal_findings(retention_days: int = -1) -> int:
    """删除超过保留期的终态 findings 行（R11/D21）。

    - retention_days<0 时读 meta（patrol:findings_retention_days，默认 90；
      0=显式永不清理；坏值回落默认并告警，口径同 _meta_int）
    - 只删 _TERMINAL_RETENTION_STATUSES 状态、updated_at 非空且早于 cutoff 的行
    - 返回删除行数（入 run_patrol_once 回执与日志，审计可对账）
    """
    if retention_days < 0:
        retention_days = _meta_int(_RETENTION_META, _RETENTION_DEFAULT_DAYS,
                                   minimum=0)
    if retention_days == 0:
        return 0
    cutoff = datetime.fromtimestamp(
        time.time() - retention_days * 86400, tz=timezone.utc).isoformat()
    sql = ("DELETE FROM grape_t_patrol_findings WHERE status IN "
           f"({','.join('?' * len(_TERMINAL_RETENTION_STATUSES))}) "
           "AND updated_at != '' AND updated_at < ?")
    params = _TERMINAL_RETENTION_STATUSES + (cutoff,)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(sql, params)
            db.commit()
            return c.rowcount if (c.rowcount or 0) > 0 else 0
        except Exception as e:
            log.warning(f"[patrol] findings retention cleanup failed: {e}")
            return 0


# ====== 主循环 ======


class PatrolScanInProgress(RuntimeError):
    """single-flight 闸（S41/R16，全入口）：一轮扫描已在执行（自动轮/手动轮任一入口）。"""


_scan_gate_lock = threading.Lock()
_scan_gate_busy = False


def run_patrol_once(mode: str = "") -> dict:
    """单轮巡检（Phase 1 只读）。mode 覆盖全局 LOOKBACK 档（audit=全历史）。

    全入口 single-flight 闸（S41/R16）：路由层 _scan_in_flight 只挡手动轮对手动轮，
    自动轮与手动轮并发时两轮全表扫描互踩 db_lock——闸下沉到本函数（线程安全），
    所有入口共用；在跑时抛 PatrolScanInProgress（调用方区分语义：自动轮记
    skip 日志不退避，手动轮 409）。
    """
    global _scan_gate_busy
    with _scan_gate_lock:
        if _scan_gate_busy:
            raise PatrolScanInProgress("patrol scan already in flight")
        _scan_gate_busy = True
    try:
        return _run_patrol_once_inner(mode)
    finally:
        with _scan_gate_lock:
            _scan_gate_busy = False


def _run_patrol_once_inner(mode: str) -> dict:
    t0 = time.time()
    mode = mode or patrol_runtime_config()["lookback_mode"]
    if mode == "audit":
        cutoff = ""
    else:
        days = _LOOKBACK_RUNTIME_DAYS
        cutoff = datetime.fromtimestamp(
            time.time() - days * 86400, tz=timezone.utc).isoformat()
    known = _known_task_ids()   # S51/R22：轮内一次拉取，双检测共享引用（原两检测各自全表拉同一集合）
    broken = detect_broken_chains(cutoff, known)
    zombies = detect_zombies(known)
    stalled = derive_stalled_issues(broken)
    inserted = insert_findings_batch(broken + zombies + stalled)   # S48/R20-A：批量攒批+单块单 commit
    purged = cleanup_terminal_findings()   # R11/D21：终态行 TTL 随轮执行（0=关闭）
    duration_ms = int((time.time() - t0) * 1000)
    log.info(f"[patrol] scan done: broken={len(broken)} zombie={len(zombies)} "
             f"stalled={len(stalled)} inserted={inserted} purged={purged} "
             f"duration={duration_ms}ms")
    # S40/R15：慢扫描告警（F 项落地——检测+报告零危害口径）：duration 超阈值
    # 即告警不熔断（阈值拍太紧=告警噪音，太松=无感；30s=约 60× 当前实测上界，
    # 命中即说明量级变化需复核索引/短路健康度）。
    if duration_ms > _SLOW_SCAN_WARN_MS:
        log.warning(f"[patrol] slow scan: duration={duration_ms}ms > "
                    f"{_SLOW_SCAN_WARN_MS}ms threshold (candidates={len(broken) + len(zombies)}); "
                    f"check S31 known-set short-circuit and event/task indexes")
    return {"broken": len(broken), "zombie": len(zombies), "stalled": len(stalled),
            "inserted": inserted, "purged": purged, "duration_ms": duration_ms}


async def patrol_loop():
    """巡检主循环（仿 scheduler：全程 try/except + 退避，异常绝不外溢）。

    巡检功能本身默认启用；参数运行真源=DB meta（集群共享，每周期重读，改即生效）；
    文件配置仅首次启动 seed 进 meta（此后以表为准）。
    """
    _seed_meta_from_file()
    rc = patrol_runtime_config()
    log.info(f"Patrol loop started (interval={rc['interval_seconds']}s, "
             f"confirm={rc['confirm_minutes']}min, mode={rc['lookback_mode']}, "
             f"compensate={rc['compensate_enabled']}; 真源=DB meta，每周期重读)")
    backoff = 10
    while True:
        try:
            rc = patrol_runtime_config()   # 每周期重读：meta 改值即时生效（含 interval）
            # 首轮立即执行（先干活后睡）：启动后不等首个 interval 才出第一份清单
            # 评审十四轮修正②（S27）：选主是总闸——非主实例直接 skip 整轮动作
            # （reclaim/compensate/scan），与设计 §二"谓词选主 → 非主 skip"对齐。
            # 原实现 reclaim/compensate 在选主判定之前执行，多机部署下每台
            # 都跑补偿批处理（ClaimHandler rowcount + 幂等键兜住不双发，但重复
            # 扫描/日志/DB 压力与设计意图不符）。
            if claim_patrol_leader(_hostname(), rc["interval_seconds"]):
                # claiming 孤儿回收是无条件卫生操作（补偿总闸关闭也要回收，
                # 否则孤儿滞留 claiming——开关重开后前无人捞）。原实现埋在
                # compensate_open_findings 里被开关门控，实测开关关闭时孤儿不回收。
                try:
                    from src.api.patrol_compensate import reclaim_stale_claimings
                    # D23 收尾（S42/R16）：reclaim 也是持 db_lock 的表扫描同步段，
                    # 与 run_patrol_once 同款经 to_thread 卸载出事件循环。
                    await asyncio.to_thread(reclaim_stale_claimings)
                except Exception as e:
                    log.warning(f"[patrol] claiming recycle failed: {e}")
                if rc["compensate_enabled"]:
                    # Phase 2（D20 责任链）：补偿执行者独立模块，懒加载（开关关=零 import 开销）
                    try:
                        from src.api.patrol_compensate import compensate_open_findings
                        counts = await compensate_open_findings()
                        if counts:
                            log.info(f"[patrol] compensate round: {counts}")
                    except Exception as e:
                        log.warning(f"[patrol] compensate round failed: {e}")
                # D23（实现后审计）：run_patrol_once 是持 db_lock 的同步全表扫描，
                # 裸调会卡住整个事件循环——经 asyncio.to_thread 卸载（DB 车道竞争
                # 仍在，但 API 事件循环不再被整轮冻结）。
                # Issue #1667：排队 A2A 滞留兜底对账（终态旁路/竞态窗口的最终防线）。
                # 无 repo 谓词 = 全仓 queued 扫描，但只派发「前序已不活跃」的行，
                # 正常在途排队不受影响；持 db_lock 的同步段经 to_thread 卸载。
                try:
                    from src.api.event_pipeline import reconcile_queued_a2a
                    _drained = await asyncio.to_thread(reconcile_queued_a2a)
                    if _drained:
                        log.info(f"[patrol] a2a queue reconcile: dispatched {len(_drained)}")
                except Exception as e:
                    log.warning(f"[patrol] a2a queue reconcile failed: {e}")
                try:
                    await asyncio.to_thread(run_patrol_once)
                except PatrolScanInProgress:
                    # S41/R16：手动轮正在扫描——良性 skip，不进退避
                    # （退避会把 5 分钟周期人为拉长）。
                    log.info("[patrol] auto scan skipped: manual scan in flight")
            # 周期等待放轮末：首轮已即时执行，此后每 interval 一轮（与 scheduler 惰性对齐）
            await asyncio.sleep(rc["interval_seconds"])
            backoff = 10
        except asyncio.CancelledError:
            log.info("Patrol loop cancelled")
            return
        except Exception as e:
            log.warning(f"[patrol] loop error, backoff {backoff}s: {e}")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2, 300)


def _hostname() -> str:
    """持有者标识（评审十五轮修正③/S28）：hostname#pid。

    同一主机多 uvicorn worker 共享 hostname——若只用 hostname 做持有者标识，
    所有 worker 都能通过 substr 前缀自续约（双主扫描）。pid 进标识后每进程
    唯一（live 进程内 pid 不重复，跨机靠 hostname 区分），选主恢复每 worker
    一主。# 分隔符不与 holder|iso 的 | 冲突；进程崩溃后 pid 被复用时新持有者
    续旧锁是安全的（旧持有者已死不会再动）。
    """
    import socket
    try:
        return f"{socket.gethostname()}#{os.getpid()}"
    except Exception:
        return f"unknown#{os.getpid()}"
