"""审计日志——append-only + 哈希链。

每条日志记录前一条的哈希值，任何篡改会断链。
使用自增序列号保证链顺序，数据库行锁保证并发安全。
不记录密码、验证码等敏感信息。
"""
import hashlib
import json
import re

from src.kernel.config import log
# 审计表归 mgmt 域：worker/event 进程调用时必须走 owner 连接，否则会写进
# 进程本库（runtime）——三库下要么 1146 被吞，要么拆断审计哈希链。
from src.kernel.owner_db import db_pair

db, db_lock = db_pair("mgmt")


# 事件类型
EVENT_TYPES = frozenset({
    "register",                    # 注册成功
    "login_success",               # 登录成功
    "login_failed",                # 登录失败
    "password_reset",              # 密码重置
    "password_change",             # 密码修改
    "email_change",                # 邮箱变更
    "bind",                        # 第三方绑定
    "unbind",                      # 第三方解绑
    "transfer_bind",               # 绑定转移到另一个账号（此前不在白名单 → 转移动作没有任何审计行）
    "login_rename",                # 第三方登录名变更（Issue #497：external_id 不变、login 变）
    "account_disabled",            # 账号禁用
    "account_restored",            # 账号恢复
    "account_locked",              # 账号锁定
    "account_unlocked",            # 账号解锁
    "account_deleted",             # 账号注销
    "account_anonymized",          # 账号匿名化（冷静期到期自动执行）
    "set_password",                # 设置密码（无密码用户）
    "admin_user_created",          # admin 手动创建用户
    "admin_user_edited",           # admin 编辑用户信息
    "admin_password_reset",        # admin 重置用户密码
    "admin_whitelist_added",       # admin 添加登录白名单
    "admin_whitelist_deleted",     # admin 删除登录白名单
    "admin_whitelist_toggled",     # admin 切换白名单开关
    "admin_login_failed",          # admin 登录失败
    "admin_signup_whitelist_added",   # admin 添加注册白名单
    "admin_signup_whitelist_deleted", # admin 删除注册白名单
    "admin_allow_signup_toggled",     # admin 切换开放注册开关
    "admin_password_changed",        # admin 修改自己的密码
    "admin_config_saved",            # admin 保存平台凭据配置
    "dev_verification_code_used",   # 固定验证码使用（需求8/16）
    "repo_cascade_cleanup",         # 仓库级联清理（Issue #340：解除托管/仓库删除清空关联数据）
    # 运维类破坏性 DDL 留痕（QA C3，2026-11-21）：启动期删副本表 / 列宽 MODIFY
    "table_copy_cleanup",           # 别域副本表清理（detail: table/owner/rows/db/role/migration/phase）
    "session_token_key_widen",      # session_api_tokens.session_key 列宽对齐（同一 detail 形状）
    "issue_admin_confirmed",        # 仓库 admin 确认需求完成（Issue #393：人工终审留痕）
    "issue_reverted_to_pending_review",  # 需求属主撤销已完成需求回待审核（Issue #1408）
    "credential_reveal",            # 凭据明文按需取回（Issue #474 方案A：二次确认后当次返回，detail 含 provider/field，不落明文）
    "credential_decrypt_failed",    # 渠道表凭据密文解密失败（Issue #3054 P1-3：密钥事故显式化，detail 只记 provider/field/column/kid/error，不落密文与明文）
    "scene_secret_reveal",          # 场景自定义变量密钥明文按需取回（Issue #885：二次确认后当次返回，detail 含 scene_id/repo_key/name，不落明文）
    "scene_pin",                    # 场景置顶/取消置顶（Issue #1735：仓库共享状态变更留痕，detail 含 scene_id/repo_key/pinned）
    "im_installation_change",       # IM 渠道安装变更（凭证保存/解绑/引擎启停/卡片配置；detail 只记动作与标识，不含 secret）
    "org_repo_bind",                # 组织关联托管仓库
    "org_repo_map_analyze",         # 组织仓库地图：单仓分析
    "org_repo_map_analyze_all",     # 组织仓库地图：全量分析
    "org_repo_map_sync",            # 组织仓库地图：显式同步汇总文档到 grape-org
    "org_repo_ai_result",           # 组织仓库 AI 分析结果回写（worker / 手动）
    "org_repo_analysis_run",        # 组织 AI 分析手动触发（单仓/全量）
    "org_repo_analysis_automation_ensure",  # 组织 AI 分析每日自动任务开启
    "org_repo_unbind",              # 组织解绑普通关联仓库
    "org_repo_transfer",            # 组织间迁移普通关联仓库
    "org_provision_manual_bind",    # 组织手动绑定已有仓库为组织仓库（Issue #2258 兜底）
    "org_provision_started",        # 组织供给开始（建仓/建场景/绑定）
    "org_provision_completed",      # 组织供给完成
    "org_provision_failed",         # 组织供给失败（含租约丢失）
    "issue_pipeline_advanced",      # 流水线确定性推进（manual/patrol；detail 含任务/from/to/seq，不落正文）
    "issue_pipeline_triggered_from_created",  # 挂单首次触发入链（Issue #3621 / #3913 C3：created→vod 留痕；detail 含 repo_key/issue_id/issue_number/from_stage/to_stage/actor/seq）
    # Issue #2112：组织需求跨仓下派（6 类事件，未登记会走 Unknown 分支丢弃）
    "dispatch_created",             # 下派批次创建
    "target_created",               # 单目标子 Issue 创建成功
    "dispatch_completed",           # 下派全部目标成功收敛
    "dispatch_partial_failed",      # 下派存在失败目标（含全部失败）
    "dispatch_cancelled",           # 下派被取消
    "target_retried",               # 单目标重试（复用既有子 Issue）
    "dispatch_needs_review",        # 下派批次等待人工复核
    # 插件市场写操作（QA #1847 C3：未登记会被静默丢弃，等于没有审计）
    "plugin_upload",                # 插件上传（detail: plugin/version/scope）
    "plugin_delete",                # 插件市场条目软删（detail: plugin/version/plugin_id）
    "plugin_certify",               # 官方认证位标记/撤销（detail: plugin_id/certified）
    "plugin_rollback",              # 生效版本回滚（detail: plugin/plugin_id/target_version）
    "plugin_like",                  # 点赞开关（detail: plugin_id/liked）
    "plugin_install",               # 运行时安装（detail: plugin）
    "plugin_uninstall",             # 运行时卸载（detail: plugin）
    "plugin_credentials_saved",     # 插件凭据保存（detail 只记 key 名，不落值）
    "plugin_config_saved",          # 插件参数默认值保存（detail 只记 key 名）
    # Issue #2150：组织上下文与组织知识库（未登记会被静默丢弃）
    "org_context_create",           # 组织上下文创建（draft）
    "org_context_update",           # 组织上下文更新
    "org_context_publish",          # 组织上下文发布（自动降级旧 active）
    "org_context_deprecate",        # 组织上下文废弃
    "org_context_delete",           # 组织上下文删除
    "org_knowledge_create",         # 组织知识创建（draft）
    "org_knowledge_update",         # 组织知识更新
    "org_knowledge_publish",        # 组织知识发布（自动降级旧 active）
    "org_knowledge_deprecate",      # 组织知识废弃
    "org_knowledge_archive",        # 组织知识归档（终态，Issue #2150 契约 archived）
    "org_knowledge_delete",         # 组织知识删除
    "cross_write_apply",            # 跨域补偿写应用（/internal/xwrite/apply：action/幂等键/调用方/结果摘要，detail 不落 payload 原文）
    "sandbox_whitelist_created",    # admin 新增沙箱五维白名单规则（PR #1507 F7：detail 只记标识，不记凭据）
    "sandbox_whitelist_updated",    # admin 更新沙箱白名单规则（detail 含 rule_id/backend/domain）
    "sandbox_whitelist_deleted",    # admin 删除沙箱白名单规则（detail 含 rule_id/backend）
    "sandbox_path_created",         # admin 新增沙箱文件系统路径（detail 含 backend/path/access）
    "sandbox_path_updated",         # admin 更新沙箱文件系统路径
    "sandbox_path_deleted",         # admin 删除沙箱文件系统路径
    "sandbox_config_updated",       # admin 更新沙箱配置（detail 含 config_id/mode/degrade_policy）
    "issue_terminal_disposition",   # 终态载体单人工处置（Issue #2816：new_carrier/reassign/close，detail 含 repo_key/issue_id/action/结果，不含 secret）
    "skill_comment_posted",          # 技能详情页发表评论（Issue #3168 D11：detail 含 skill_id/comment_id/字数，不落评论原文）
    "skill_meta_patched",            # 技能元信息免传包更新（Issue #3202：detail 含 skill_id/更新字段名列表，不记值）
    "skill_version_created",         # 技能版本行写入（Issue #3168：detail 含 skill_id/version，上传/更新/重传共用）
    "engine_tag_change",            # 引擎角色标签写入（Issue #3396 F2：add/update/delete/set，detail 含 runtime_id/tag(s)/enabled/priority，不含凭据）
})


def _now_str() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _get_last_hash() -> str:
    """获取最后一条审计日志的哈希值。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT curr_hash FROM audit_logs ORDER BY seq DESC LIMIT 1"
            )
            row = c.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _next_seq() -> int:
    """获取下一个序列号。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT MAX(seq) FROM audit_logs")
            row = c.fetchone()
            return (row[0] + 1) if row and row[0] else 1
    except Exception:
        return 1


def log_audit(
    event_type: str,
    user_id: "str | int" = 0,   # uuid7 主键 TEXT（operator 侧 admin id 仍可为 int）
    operator_id: "str | int" = 0,
    ip: str = "",
    detail: "dict | None" = None,
) -> None:
    """记录审计日志。

    参数：
    - event_type: 事件类型（见 EVENT_TYPES）
    - user_id: 被操作的用户 ID
    - operator_id: 操作者 ID（管理员操作时与 user_id 不同）
    - ip: 请求 IP
    - detail: 附加信息（JSON，不可包含密码/验证码等敏感信息）
    """
    if event_type not in EVENT_TYPES:
        log.warning(f"Unknown audit event type: {event_type}")
        return

    try:
        for attempt in range(3):
            with db_lock:
                c = db.cursor()
                seq = _next_seq()
                prev_hash = _get_last_hash()
                created_at = _now_str()

                # 构建哈希内容
                content = json.dumps({
                    "seq": seq,
                    "event_type": event_type,
                    "user_id": user_id,
                    "operator_id": operator_id,
                    "ip": ip,
                    "detail": detail or {},
                    "prev_hash": prev_hash,
                    "created_at": created_at,
                }, sort_keys=True, ensure_ascii=False)
                curr_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

                # 插入（seq UNIQUE 约束——集群并发时冲突重试）
                try:
                    c.execute(
                        "INSERT INTO audit_logs "
                        "(seq, event_type, user_id, operator_id, ip, detail, "
                        "prev_hash, curr_hash, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (seq, event_type, user_id, operator_id, ip,
                         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                         prev_hash, curr_hash, created_at),
                    )
                    db.commit()
                    return  # 成功，退出重试循环
                except Exception as insert_err:
                    db.rollback()
                    if attempt < 2:
                        # seq 冲突，重试（重新获取 seq + prev_hash）
                        continue
                    raise insert_err  # 最后一次重试仍失败，抛出
    except Exception as e:
        log.warning(f"Audit log failed: {e}")


# ---------------------------------------------------------------------------
# 脱敏工具
# ---------------------------------------------------------------------------

def mask_email(email: str) -> str:
    """邮箱脱敏：alice@example.com → a***@example.com"""
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        return f"*@{domain}"
    return f"{name[0]}***@{domain}"


def mask_identifier(identifier: str) -> str:
    """标识符脱敏：邮箱用 mask_email，用户名保留首尾字符。"""
    if "@" in identifier:
        return mask_email(identifier)
    if len(identifier) <= 2:
        return identifier
    return f"{identifier[0]}***{identifier[-1]}"


# ---------------------------------------------------------------------------
# 组织审计查询（Issue #2260：只读，不改写入链路/哈希链）
# ---------------------------------------------------------------------------

# 组织相关事件前缀（组织事件名以此开头）
_ORG_EVENT_PREFIXES = ("org_repo_", "org_context_", "org_knowledge_")

# 跨仓下派事件（Issue #2112）：实际事件名为 dispatch_*/target_*，无 org_dispatch_ 前缀
_DISPATCH_EVENT_TYPES = frozenset({
    "dispatch_created",
    "target_created",
    "dispatch_completed",
    "dispatch_partial_failed",
    "dispatch_cancelled",
    "target_retried",
    "dispatch_needs_review",
})

# 组织审计事件全集（唯一来源：EVENT_TYPES 白名单 + 下派事件），查询只允许命中此集合
ORG_AUDIT_EVENT_TYPES = frozenset(
    e for e in EVENT_TYPES if e.startswith(_ORG_EVENT_PREFIXES)
) | _DISPATCH_EVENT_TYPES

# 前缀别名：用户可传的语义前缀 → 实际事件集合
#   org_dispatch_ : 实际无此前缀，映射到 6 类下派事件（否则按字面过滤查不到下派记录）
#   org_provision_: 组织供给 started/completed/failed（Issue #1820 C3 整改）
_EVENT_TYPE_ALIASES = {
    "org_dispatch_": _DISPATCH_EVENT_TYPES,
    "org_provision_": frozenset({
        "org_provision_started", "org_provision_completed", "org_provision_failed",
    }),
}

# detail 脱敏键（归一化分词后按 token 精确/后缀匹配，覆盖 *_token/*_secret/camelCase/Header 式键名）
#   只放语义明确的片段，避免误伤业务字段（如 repo_key / context_id / target_count）。
_SENSITIVE_KEY_TOKENS = frozenset({
    "token", "secret", "password", "passwd", "credential", "credentials",
    "authorization", "cookie",
})
# 连写形态的后缀（camelCase/下划线/连字符归一后：apiKey→apikey、private_key→privatekey）
_SENSITIVE_KEY_SUFFIXES = (
    "token", "secret", "password", "passwd", "credential", "credentials",
    "apikey", "privatekey", "accesskey", "signingkey", "secretkey", "sshkey",
    "authorization", "cookie",
)
_CONTENT_KEY_TOKENS = frozenset({"content", "body", "text", "markdown", "prompt"})
# 连写形态后缀：不含 text（"context" 以 text 结尾，误伤组织上下文键）
_CONTENT_KEY_SUFFIXES = ("content", "body", "markdown", "prompt")
_MASK_DETAIL_MAX_DEPTH = 3
_REDACTED = "[REDACTED]"

# 组织审计查询：行流式读取块大小（避免全量 fetchall 进内存）
_ORG_AUDIT_FETCH_CHUNK = 200

# 键名分词：驼峰边界先拆，再按非字母数字切分
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SPLIT = re.compile(r"[^a-z0-9]+")
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$")


def _resolve_event_types(event_type: str) -> set:
    """把查询输入解析为白名单内的事件集合（禁止把用户输入拼进 LIKE/IN）。

    解析顺序：空 → 全集；精确命中；前缀别名（org_dispatch_/org_provision_）；
    否则在 EVENT_TYPES 白名单内做前缀展开。未知前缀返回空集。
    """
    et = (event_type or "").strip()
    if not et:
        return set(ORG_AUDIT_EVENT_TYPES)
    if et in ORG_AUDIT_EVENT_TYPES:
        return {et}
    alias_key = et if et.endswith("_") else et + "_"
    if alias_key in _EVENT_TYPE_ALIASES:
        return set(_EVENT_TYPE_ALIASES[alias_key])
    return {e for e in ORG_AUDIT_EVENT_TYPES if e.startswith(et)}


def _normalize_since(since: str) -> str:
    """规范化 since：仅接受无时区的 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS。"""
    s = (since or "").strip()
    if not s:
        return ""
    if not _SINCE_RE.fullmatch(s):
        raise ValueError("since 仅支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，不支持时区")
    if len(s) == 10:
        return s + " 00:00:00"
    return s.replace("T", " ")


def _parse_detail(raw) -> dict:
    """detail 文本 → dict（非法/非对象 → 空 dict）。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _org_matches(detail: dict, org_id: str) -> bool:
    """组织归属匹配：主键 org_id；org_repo_transfer 只写 source/target 也需纳入。"""
    for key in ("org_id", "source_org_id", "target_org_id"):
        try:
            if str(detail.get(key) or "") == str(org_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _key_tokens(key) -> list:
    """键名归一化分词：camelCase / 连字符 / 下划线 / 空格统一拆成小写 token。

    例：`X-Auth-Token` → [x, auth, token]；`clientSecret` → [client, secret]；
    `repo_key` → [repo, key]（key 不在敏感集合，保留业务标识）。
    """
    spaced = _CAMEL_BOUNDARY.sub("_", str(key))
    return [t for t in _KEY_SPLIT.split(spaced.lower()) if t]


def _key_kind(key) -> str:
    """键名分类：'content'（正文，剔除）/ 'sensitive'（敏感，替换）/ ''（保留）。"""
    tokens = _key_tokens(key)
    compact = "".join(tokens)
    if any(t in _SENSITIVE_KEY_TOKENS for t in tokens) or compact.endswith(
            _SENSITIVE_KEY_SUFFIXES):
        return "sensitive"
    if any(t in _CONTENT_KEY_TOKENS for t in tokens) or compact.endswith(
            _CONTENT_KEY_SUFFIXES):
        return "content"
    return ""


def mask_detail(value, _depth: int = 0):
    """递归脱敏 detail（最深 3 层）：敏感键 → [REDACTED]，正文键直接剔除。

    超出深度上限按 fail-closed 处理：整棵子树替换为 [REDACTED]，
    不原样返回（否则深层嵌套的敏感数据会绕过脱敏直接外泄）。
    """
    if _depth > _MASK_DETAIL_MAX_DEPTH:
        return _REDACTED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kind = _key_kind(k)
            if kind == "content":
                continue
            if kind == "sensitive":
                out[k] = _REDACTED
                continue
            out[k] = mask_detail(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [mask_detail(v, _depth + 1) for v in value]
    return value


def query_org_audit(org_id: str, event_type: str = "", limit: int = 50,
                    offset: int = 0, since: str = "") -> dict:
    """组织审计只读查询（Issue #2260）。

    - 事件集合以 EVENT_TYPES 白名单展开（前缀/别名；未知前缀 → 空页），不拼接 LIKE。
    - 组织隔离在 Python 解析 detail（匹配 org_id / source_org_id / target_org_id），
      兼容 SQLite/MySQL 双后端（不用各家不一致的 JSON 函数）。
    - 排序 `id DESC`；`total` 为匹配总数（与分页一致）；`limit` 夹取 1..200（默认 50），
      `offset` 夹取 >=0；`since` 与 created_at 文本比较。
    - 内存/CPU 有界：行按固定块流式读取（不全量 fetchall），`mask_detail` 只作用于
      当前分页窗口内的记录（先切片后脱敏），审计量增长不会带来线性内存/脱敏开销。
      `total` 仍需扫描候选行（detail 无派生列/索引，见后续 Issue）。

    注：`org_provision_` 前缀对应组织供给 started/completed/failed 审计。
    按空页返回，本次不新增写入。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    event_types = _resolve_event_types(event_type)
    if not event_types:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    placeholders = ",".join("?" for _ in event_types)
    sql = (
        "SELECT id, seq, event_type, user_id, operator_id, ip, detail, created_at "
        f"FROM audit_logs WHERE event_type IN ({placeholders})"
    )
    params: list = sorted(event_types)
    since_norm = _normalize_since(since)
    if since_norm:
        sql += " AND created_at >= ?"
        params.append(since_norm)
    sql += " ORDER BY id DESC"

    matched_total = 0
    page: list = []
    with db_lock:
        c = db.cursor()
        c.execute(sql, params)
        cols = [d[0] for d in c.description]
        detail_idx = cols.index("detail")
        while True:
            chunk = c.fetchmany(_ORG_AUDIT_FETCH_CHUNK)
            if not chunk:
                break
            for row in chunk:
                detail = _parse_detail(row[detail_idx])
                if not _org_matches(detail, org_id):
                    continue
                matched_total += 1
                # 先切片后脱敏：窗口外的行只计数，不构造记录、不做递归脱敏
                if matched_total <= offset or len(page) >= limit:
                    continue
                rec = dict(zip(cols, row))
                rec["detail"] = mask_detail(detail)
                page.append(rec)

    return {
        "items": page,
        "total": matched_total,
        "limit": limit,
        "offset": offset,
    }
