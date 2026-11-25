"""需求中心（Issues Center）领域逻辑（issues_center）。

从 src/domains/runtime/routes/tasks.py 拆分（tasks-module-refactor-and-task-id-disambiguation-design.md）：
- issue 建档/状态流转/VCS 创建/附件/任务对话/平台映射/需求列表查询等「需求中心」领域函数集中于此，
- routes/tasks.py 只保留「执行实例」动作（complete/current），其他路由与业务模块均从本模块导入，
- 本模块为叶子：**不得 import routes.tasks / routes.issues**（防环形依赖）。

P1 模块归位：issue / events / agent_tasks 属 runtime 域数据，实现自 src/api/ 迁入
src/domains/runtime/；旧路径 src/api/issues_center.py 保留 sys.modules 别名
（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""

import asyncio

import os

from datetime import datetime

from fastapi import Depends, HTTPException, Query, Request

from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key

from src.kernel.time_utils import normalize_event_time

from src.kernel.issue_ident import is_actionable_issue_number, native_issue_number

from src.kernel.config import log, WORKSPACE_DIR

from src.kernel import task_conversation_metrics


class _OwnedCursor:
    """自带连接的游标：close 时把连接一并关掉（每次查询独立建连的收尾）。"""

    def __init__(self, conn, cursor):
        self._conn = conn
        self._cur = cursor

    def __getattr__(self, item):
        return getattr(self._cur, item)

    def close(self):
        try:
            self._cur.close()
        finally:
            try:
                self._conn.close()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")


class _DomainDbProxy:
    """把 `db.cursor()` / `db.commit()` 代理到 runtime 域连接。

    需求中心的 issue / issue_state / events / agent_tasks 都在 runtime 域，但该路由由
    mgmt 进程服务。三库模式下 mgmt 进程的主库是本域库，直连进程库会撞「表不存在」；
    这里按域取连接（own DSN 优先，单库回落进程库），写路径仍应走 runtime public 原语。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        # 本进程主库已经是 runtime 库时直接用主库：session_manager.db 是成熟的
        # MySQL 兼容层（方言翻译 + 连接池），换轻量连接只会引入方言差异。
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.cursor(*a, **kw)
        if db_registry.uses_own_dsn("runtime"):
            # 每次取一条独立连接：共享连接绑线程，跨线程复用会 InterfaceError(0, '')
            _conn = db_registry.new_connection("runtime")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
        # 必须与 cursor() 的取连接分支一致：runtime 域进程走 session_manager.db 的
        # autocommit=False 线程级连接，这里若 return None 会让写事务一直悬着——行锁不释放、
        # 新写入的行（如 enqueue 的任务）对其它连接不可见，随后任何 rollback 直接丢掉。
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.commit()
        if db_registry.uses_own_dsn("runtime"):
            return None
        from src.kernel.session_manager import db as _main
        return _main.commit()

    def rollback(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.rollback()
        if db_registry.uses_own_dsn("runtime"):
            try:
                db_registry.domain_db("runtime").rollback()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")
            return None
        from src.kernel.session_manager import db as _main
        return _main.rollback()

    def __getattr__(self, item):
        from src.kernel.session_manager import db as _main
        return getattr(_main, item)


_rt_db = _DomainDbProxy()


from src.kernel.session_manager import db, db_lock, get_chat_config, create_conversation, add_message

def _require_session_repo_token(request: Request, repo_key: str, *, scene=None) -> dict:
    """notify 等 agent 动作端点鉴权：仅接受 `tk_` 会话 token；**作用域按场景判定**。

    2026-11-22（#3713/#1946 收口）：原实现要求 `token.repo_key == 目标仓` 严格相等，
    于是 chat 的伪作用域（`chat:<user_id>`）与跨仓下派永远 403 —— 现场表现为
    「tasks notify 会话 token 仓库作用域不匹配」。现在改走 `scene_policy`：
      · TASK_NOTIFY（默认）：task_plane —— 跨仓/伪作用域需「会话用户对目标仓有可见权」；
      · 其它场景（如自省类）仍 repo_bound，同仓才能过。
    不在本函数里再写作用域规则：场景 → 规则全部在策略表。
    """
    from src.kernel.auth_sdk.auth_token import verify_session_token
    from src.kernel.scene_policy import Scene, decide

    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "需会话 token（tk_）"})
    info = verify_session_token(token)
    if not info:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 无效"})

    def _visible(target: str) -> bool:
        """会话用户对目标仓是否有可见权（跨仓/伪作用域的唯一放行依据）。

        能力由宿主域提供：`mgmt.public.user_can_access_repo`。runtime 不自己连托管仓库表
        —— 3库4进程下 mgmt 库不在本进程，契约内部按
        GRAPE_DSN_MGMT 路由（db_registry），这样单库/三库拓扑都不用改调用点。
        """
        from src.domains.mgmt.public import user_can_access_repo

        return user_can_access_repo(info.get("user_id"), target)

    _decision = decide(scene or Scene.TASK_NOTIFY, info.get("repo_key") or "", repo_key,
                       access_check=_visible)
    if not _decision.ok:
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": _decision.code,
            "message": f"会话 token 无权执行该操作（{_decision.message}）"})
    return info

class TaskNotifyBody(BaseModel):
    assignee: str = ""
    repo_key: str = ""
    issue_number: int | str = 0
    task_description: str = ""

class TaskAwaitingInputBody(BaseModel):
    questions: str = ""
    reason: str = ""
    # Issue #667：卡片 id 缺行时的兜底解析——GitCode 侧 Issue number（会话仓库作用域内），
    # 服务端据此复用/补建内部需求卡行，避免同步缺口导致 404 阻断澄清流程。
    # （S75：字符串 ident 平台合法，int | str 原生保形）
    issue_number: int | str = 0

class TaskPendingReviewBody(BaseModel):
    review_points: str = ""
    references: str = ""

class TaskCloseBody(BaseModel):
    repo_key: str = ""
    issue_number: int | str = 0

def _get_user_id_by_gitcode(gitcode_user: str, repo_key: str = "") -> int | None:
    """按平台用户名查 user_id（unified_user_mapping.user_id）。

    查找失败（未注册或异常）时记录 warning 日志，便于追溯（Issue #178）。
    优先返回「登录账号（grape_accounts）id」——unified_user_mapping 可能存在新旧双 id 映射
    （低 id 遗留 + grape_accounts id），若取到非登录 id 会导致 issue.user_id 与请求用户不符、
    「我的需求」按 user_id 过滤为空（issue-244 关联事故）。无登录账号时回退任意映射。

    FR-11.2: 按 repo_key 的 provider 分查对应列（gitcode_user / github_login），
    避免 OR 扫两列导致空串交叉命中。repo_key 为空时降级为 gitcode_user 查询（向后兼容）。
    M5/S51（D31）：身份优先——先按 external_identities（平台登录名权威域，
    协议无关）反查，命中即返回；未命中回退既有 um 名字域链（零危害：不迁
    数据不删表，注册名 ≠ 平台登录名错位在身份域天然消解）。
    """
    if not gitcode_user:
        return None
    # S51 身份优先归属：external_identities 命中即权威
    try:
        from src.kernel.repo_key import parse_repo_key
        from src.domains.mgmt.public import resolve_user_id_by_platform_login
        _provider, _, _ = parse_repo_key(repo_key or "")
        _uid = resolve_user_id_by_platform_login(_provider, gitcode_user)
        if _uid:
            return _uid
    except Exception as _e:
        # D33/S53：不吞——resolver 自身 SQL 失败有内部 log，但 import 级失败
        # 原先永久静默回退；留痕供排查（回退 um 兜底行为不变）
        log.warning(f"_get_user_id_by_gitcode: identity-first resolution failed "
                    f"(user={gitcode_user!r}, repo={repo_key!r}), falling back to um: {_e}")
    with db_lock:
        try:
            # FR-CQ-1: 提取为 um_column_for_repo 工具函数
            from src.kernel.repo_key import um_column_for_repo
            from src.domains.mgmt import public as _mgmt_public_um
            _col = um_column_for_repo(repo_key)
            # D39/S59：无 um 列平台（gitee 等）跳过兜底（不折叠查 gitcode_user，
            # 跨平台同名误命中消除；身份优先已在前，此处兜底 miss）
            if not _col:
                log.warning(f"_get_user_id_by_gitcode: provider has no um column, skip um fallback "
                            f"(user={gitcode_user!r}, repo={repo_key!r})")
                return None
            # 优先链两跳都由 mgmt 域执行（列名经 owner 侧白名单校验）：
            # ① 登录账号存在且启用 ② 任意映射行；谓词/列序与原 SQL 逐字一致。
            _uid = _mgmt_public_um.mapping_user_id_preferring_account(_col, gitcode_user)
            if _uid is None:
                _uid = _mgmt_public_um.mapping_user_id_by_column(_col, gitcode_user)
            if _uid is not None:
                return _uid
            log.warning(f"_get_user_id_by_gitcode: gitcode_user='{gitcode_user}' not found in unified_user_mapping (user_id will be 0)")
            return None
        except Exception as e:
            log.warning(f"_get_user_id_by_gitcode: query failed for '{gitcode_user}': {e}")
            return None

def _get_feishu_open_id(gitcode_user: str, repo_key: str = "") -> str:
    """按平台用户名查飞书 open_id。

    FR-17.2: 有 repo_key 时按 provider 分查对应列；无则只查 gitcode_user（调用方从 grape_accounts.username 取值）。
    S54（D34）：身份优先——先经 external_identities 解析 user_id 再按 user_id 查
    open_id（um 平台列空/错位时通知不再静默丢失）；未命中回退 um 名字域（原路径）。
    """
    # S99/D74：S54 解析链（身份优先 → um 兜底）迁入 channel_directory，
    # 本函数保留薄包装（存量消费者不感知）
    from src.domains.runtime.im.channel_directory import resolve_open_id_for_login
    return resolve_open_id_for_login(gitcode_user, repo_key)


def _get_feishu_open_id_by_user_id(user_id: str) -> str:
    """按平台 user_id 查飞书 open_id（等用户状态通知的 open_id 定位）。

    A2A/流水线产生的需求卡 `assignee` 常为空（归属人由 user_id 承载），飞书 open_id 应随 user_id 查询，
    而非 GitCode 用户名（assignee），否则 assignee 为空时通知静默丢失（feishu-openid-by-user-id-design.md）。
    返回 open_id（空串=未配置/缺失）。
    """
    # S99/D74：实现迁入 channel_directory（薄包装）
    from src.domains.runtime.im.channel_directory import resolve_open_id_by_user
    return resolve_open_id_by_user(user_id)

def _get_gitcode_user_by_user_id(user_id) -> str:
    """按 user_id 反查平台用户名（身份域优先：external_identities，按 provider 挑选）。

    Issue #1337: 建 issue 卡片时回填 created_by 用；查不到返回空串（归入「未归属」）。
    引擎侧 routes/tasks.py 仍调用本函数；D105 TEXT 化后哨兵为字符串口径
    （铁律⑬）：`user_id>0` 数值比较对 uuid 字符串会 TypeError/MySQL 恒假。

    P1-16 收口：原实现 `COALESCE(gitcode_user, github_login)` 把身份折叠成
    存量两列，gitee/gitlab/华为 得空串。现身份优先（external_identities.
    external_login），provider 优先序 = grape 账号 login_provider →
    平台卡目录顺序 → 其余绑定。um 平台列仅作无身份行时的兜底。
    """
    uid = str(user_id or "")
    if uid in ("", "0", "None"):
        return ""
    # ① 身份域（权威）
    try:
        from src.domains.mgmt.public import list_platform_links, get_grape_account_by_id
        # A 批：平台登录名解析取"身份行 ∪ PAT 凭据域"（PAT 的 login 同样有效）
        _idents = [i for i in (list_platform_links(uid) or [])
                   if i.get("provider") and (i.get("external_login") or "").strip()]
        if _idents:
            try:
                _login_provider = (get_grape_account_by_id(uid) or {}).get("login_provider") or ""
            except Exception:
                _login_provider = ""
            try:
                from src.domains.runtime.vcs.platform_card import list_vcs_platforms
                _order = {p: n for n, p in enumerate(list_vcs_platforms())}
            except Exception:
                _order = {}
            _idents.sort(key=lambda i: (
                0 if i["provider"] == _login_provider else 1,
                _order.get(i["provider"], len(_order)),
            ))
            return (_idents[0].get("external_login") or "").strip()
    except Exception as e:
        log.warning(f"_get_gitcode_user_by_user_id({uid}) identity lookup failed: {e}")
    # ② um 平台列兜底（存量行；新平台无列）
    try:
        from src.domains.mgmt import public as _mgmt_public_login
        return _mgmt_public_login.platform_login_by_user_id_any(uid)
    except Exception as e:
        log.warning(f"_get_gitcode_user_by_user_id({uid}) failed: {e}")
        return ""

def _ensure_assignee_task_card(user_id: str, assignee: str, repo_key: str, issue_number: int,
                               title: str, task_description: str) -> int | None:
    """为「指定 assignee + Issue」建/复用任务中心卡片（active）；区别于移除的 create，仅服务 notify 分配语义。

    2026-10-06：按 (repo_key, issue_number) 去重复用（任何状态），避免 awaiting/completed 卡导致 notify 另建新卡；
    复用卡时同步 assignee/user_id。
    """
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT id, assignee, user_id FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            card_id, _existing_asg, _existing_uid = row[0], row[1] or "", row[2] or 0
            if _existing_asg != assignee or (_existing_uid or 0) != (user_id or 0):
                c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (assignee, user_id, card_id))
                _rt_db.commit()
            return card_id
        # Issue #1337-D：created_by 回填（多平台解耦口径——账号域 username，
        # 平台无关；weekly-10 版曾用 gitcode_user 反查，解耦后废弃）
        try:
            from src.domains.mgmt import public as _mgmt_public_cb
            _uid_cb = str(user_id or "").strip()
            _cb = ""
            if _uid_cb not in ("", "0"):
                # 账号名解析由 mgmt 域提供（grape_accounts 归 mgmt；口径仍是按 id 取 username）
                _cb = _mgmt_public_cb.account_username_by_id_any(_uid_cb)
        except Exception as _e:
            log.debug(f"_ensure_assignee_task_card: created_by lookup skipped: {_e}")
            _cb = ""
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id, created_by) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
            (user_id, assignee, repo_key, issue_number, title, task_description,
             _get_issue_subtask_mode(repo_key, issue_number), _get_issue_parent(repo_key, issue_number),
             _get_issue_parent_id(repo_key, issue_number), _cb or (assignee or ""))
        )
        _rt_db.commit()
        return c.lastrowid

def _build_task_conversation_title(issue_number: int, title: str = "") -> str:
    """生成任务会话标题：`Issue #N · {title}`（title 空回退 `Issue #N`）。

    与需求中心看板卡片标题风格一致；超长截断保护（title 部分上限 80 字符）。
    """
    _MAX_TITLE_LEN = 80
    clean_title = (title or "").strip()
    if clean_title:
        if len(clean_title) > _MAX_TITLE_LEN:
            clean_title = clean_title[:_MAX_TITLE_LEN]
        return f"Issue #{issue_number} · {clean_title}"
    return f"Issue #{issue_number}"

def _ensure_task_conversation(user_id: str, repo_key: str, issue_number: int,
                              title: str = "", description: str = "",
                              write_context_on_reuse: bool = False) -> str:
    """为任务查找/复用「Issue #N · {title}」对话（任务中心「去对话」入口）。

    - 复用：按业务键 (user_id, repo_key, issue_number, conv_type='task') 精确检索
      （不依赖「最近 N 条」窗口，Issue #4183）；仅旧格式 `Issue #N` 字符串回退
      仍走窗口扫描，保持存量升级的对外行为不变。
      write_context_on_reuse=True 时再次写入上下文消息（notify 语义：每次分配都刷上下文）
    - 存量升级：复用命中旧标题 `Issue #N` 的会话时刷新为新格式（不覆盖用户自定义标题）
    - 新建：create_conversation(conv_type="task") 并写入任务上下文消息。
    - 失败返回空串（不抛异常，旁路不影响任务建档）；1062 后仍查不到升级为可观测事件。
    """
    from src.kernel.session_manager import (
        get_user_conversations, get_conversation_by_repo_issue_type,
        update_conversation, ConversationDuplicateUnresolved,
    )
    conv_id = ""
    new_title = _build_task_conversation_title(issue_number, title)
    old_title = f"Issue #{issue_number}"
    try:
        matched = get_conversation_by_repo_issue_type(
            user_id, repo_key, issue_number, conv_type="task")
        if matched is None:
            # 旧格式 `Issue #N` 回退：仅此路径保留窗口扫描（存量标题升级语义不变）。
            for conv in get_user_conversations(user_id, repo_key=repo_key, limit=50):
                if conv.get("title") == old_title:
                    matched = conv
                    break
        if matched is not None:
            conv_id = matched["id"]
            update_conversation(conv_id, conv_type="task", issue_number=issue_number)
            if (matched.get("title") or "") == old_title:
                update_conversation(conv_id, title=new_title)
            if write_context_on_reuse:
                ctx_msg = _build_task_context_message(title, description, repo_key, issue_number)
                add_message(conv_id, "agent", ctx_msg)
        else:
            conv_id = create_conversation(user_id, repo_key, title=new_title,
                                          conv_type="task", issue_number=issue_number)
            try:
                ctx_msg = _build_task_context_message(title, description, repo_key, issue_number)
                add_message(conv_id, "agent", ctx_msg)
            except Exception as e:
                log.warning(f"Task conversation write context message failed: {e}")
    except ConversationDuplicateUnresolved as e:
        # Issue #4183：撞唯一键后按业务键仍查不到 = 数据不一致，必须可观测（计数 + error），
        # 不再静默成 WARNING；保留旁路语义（返回空串不阻断任务建档）。
        reason = task_conversation_metrics.observe_unresolved("duplicate_unresolved")
        log.error(f"Task conversation ensure unresolved after duplicate "
                  f"(reason={reason}, repo_key={repo_key}, issue_number={issue_number}, "
                  f"conv_type='task'): {e}")
        return ""
    except Exception as e:
        log.warning(f"Task conversation ensure failed: {e}")
        return ""
    return conv_id

def _resolve_issue_org_id(repo_key: str, explicit_org: str = "") -> str:
    """解析需求归属组织（issue-244-org-attribution-fix-design.md 方案 B）。

    - repo 关联需求（repo_key 非空）：org = `managed_repos.org_id`（仓库权威，单一来源），
      忽略调用方/请求上下文 org——requests 的 user.org_id 只作「查看过滤」，绝不作写库归属。
    - 组织级需求（未来，repo_key 为空且有 explicit_org）：org = 显式选定的组织，
      须由调用方先完成归属性校验（org_member/创建权限），本函数不代校验。
    - 无托管记录 / 无显式 org → 0（个人视图豁免 org 过滤，创建者仍可见）。
    """
    if repo_key:
        from src.domains.mgmt.public import get_org_id_by_repo_key
        org_id = get_org_id_by_repo_key(repo_key) or ""
        return "" if str(org_id) == "0" else str(org_id)  # D105-15：哨兵 0/'0' 统一 ''
    return explicit_org or ""


def ensure_issue_task(repo_key: str, issue_number: int, title: str = "",
                      description: str = "", assignee: str = "",
                      event_type: str = "",
                      subtask_mode: int = -1,
                      sender: str = "",
                      org_id: str = "",
                      owner_user_id: str = "",
                      creator: str = "") -> int | None:
    """Issue 事件幂等建档（任务中心全覆盖，task-center-issue-coverage-design.md §3-A）。

    - assignee 优先 → 对应平台 user_id
    - assignee 为空 → issue_state 已存 assignee
    - 仍为空 → webhook 操作用户（sender，Issue #178：创建人应为操作用户而非 owner）
    - 最后降级 → 仓库 owner（+ warning 日志）
    - assignee/owner 未注册平台（无 unified_user_mapping）→ user_id=0 落库；
      任务中心 mine 查询 `user_id=? OR user_id=0` 展示（P2 拍板修订：展示，不隐藏）
    - 幂等：同一 issue 已存在 active/processing 任务则复用（2026-09-03 修复#15：不再按 assignee 区分，
      防止 webhook 回声导致同一 issue 产生两个不同 assignee 的任务）
    - Issue #805：creator（创建人 login）落库 created_by；复用路径仅空值回填（幂等不覆盖），
      assignee 流转不改变创建人。
    """
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    try:
        owner = split_owner_repo(repo_key)[0] or ""
    except Exception:
        owner = ""
    # Issue #49: assignee 为空时，优先从 issue_state 读取 assignee
    if not assignee:
        try:
            from src.api.session_store import get_issue_stage
            _st = get_issue_stage(repo_key, f"#{issue_number}")
            if _st and _st.get("assignee"):
                assignee = _st["assignee"]
        except Exception:
            pass
    # Issue #178: assignee 仍为空 → 使用 webhook 操作用户（sender），最后降级 owner
    if not assignee:
        if sender:
            assignee = sender
        else:
            assignee = owner or ""
            if owner:
                log.warning(f"ensure_issue_task: no assignee/sender for #{issue_number} repo={repo_key}, fallback to owner '{owner}'")
    target = assignee or owner or ""
    # 显式创建身份优先 > assignee/sender/owner 反查（issue-card-owner-attribution-fix-design.md）
    user_id = owner_user_id if str(owner_user_id or "") not in ("", "0") else (_get_user_id_by_gitcode(target, repo_key) if target else 0)
    if user_id is None:
        # 未注册平台用户 → user_id=0（保留原始 assignee 名字，不 fallback 到 owner）
        user_id = 0
    # org 归属 = 仓库托管组织（repo 关联需求的唯一权威，issue-244-org-attribution-fix-design.md 方案 B）。
    # 不再信任调用方传入的任意 org_id（如请求上下文 user.org_id），杜绝 #244 式串货漂移；
    # 调用方传入的 org_id 仅与仓库值比对，不一致时告警并校准为仓库值。
    from src.domains.mgmt.public import get_org_id_by_repo_key
    _repo_org = get_org_id_by_repo_key(repo_key) or ""
    if org_id and org_id != _repo_org:
        log.warning(f"ensure_issue_task: org drift rejected repo={repo_key} issue=#{issue_number} "
                    f"passed_org={org_id} → repo_org={_repo_org}")
    org_id = _repo_org
    with db_lock:
        c = _rt_db.cursor()
        # 幂等去重：同一平台 issue 唯一一张卡，任何状态都复用（2026-10-06 修复：
        # 此前仅复用 active/processing，awaiting_input/completed/cancelled 时后续事件会新增一行 → 重复卡片）
        c.execute(
            "SELECT id, conversation_id, status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            task_id = row[0]
            linked = row[1]
            _cur_st = row[2] or ""
            # 复用路径：已有行 org 与仓库托管 org 不一致（0/缺失/旧错误值）一律校准为仓库 org
            #（issue-244-org-attribution-fix-design.md：org 唯一权威 = managed_repos.org_id）
            if org_id:
                c.execute(
                    "UPDATE issue SET org_id=? WHERE id=? AND (org_id IS NULL OR org_id != ?)",
                    (org_id, task_id, org_id))
            if _cur_st == "cancelled":
                # Issue #1741：cancelled 是终态，迟到事件（issue_comment/a2a/agent 回调）不得复活，
                # 仅显式 POST /issues/{id}/resume 可恢复（避免取消后卡片重新回到待澄清列表）。
                log.info(f"ensure_issue_task: keep cancelled issue #{issue_number} repo={repo_key} "
                         f"(late event must not revive; resume endpoint only)")
            elif _cur_st not in ("active", "processing", "pending_review"):
                # 复用终态/待澄清卡：重新武装为 active（继续在任务中心处理，避免另建新卡）
                # Issue #4063：pending_review 审核点只能由显式审核动作（review-pass /
                # review-reject / 置位内容变更）驱动，issue_comment 等非审核事件在此
                # 复用分支不得把在位审核点回落 active（否则 AC2 失败）。
                c.execute("UPDATE issue SET status='active', completed_reason=NULL, completed_at=NULL WHERE id=?",
                          (task_id,))
            if title:
                c.execute("UPDATE issue SET title=? WHERE id=?", (title, task_id))
            if subtask_mode >= 0:
                c.execute("UPDATE issue SET is_subtask_mode=? WHERE id=?", (subtask_mode, task_id))
            # 如果传入的 assignee 与现有不同，更新 assignee 并关闭旧 assignee 的任务
            # （#15 修复：复用已有任务时同步 assignee，防止 assignee 与新建 Issue 不匹配）
            _existing_assignee = ""
            _existing_user_id = 0
            try:
                c.execute("SELECT assignee, user_id FROM issue WHERE id=?", (task_id,))
                _r2 = c.fetchone()
                if _r2:
                    _existing_assignee = _r2[0] or ""
                    _existing_user_id = str(_r2[1] or "").strip()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")
            # 归属校准：现有值为空/0 或「默认/承载账号」且与解析值不一致 → 校准为解析值
            # （issue-card-owner-attribution-fix-design.md：只修 0 修不掉非 0 错误归属；
            #   D105 起 user_id 为 uuid 字符串，一律 str 归一后判定，int 混型不再命中哨兵）
            _DEFAULT_OWNER_UID = 1  # 默认/承载账号 id（可调；与历史「空 assignee 命中默认账号」现场一致）
            if (str(user_id or "").strip() not in ("", "0")
                    and _existing_user_id in ("", "0", str(_DEFAULT_OWNER_UID))
                    and _existing_user_id != str(user_id).strip()):
                c.execute("UPDATE issue SET user_id=? WHERE id=?", (user_id, task_id))
                log.info(f"ensure_issue_task: ownership calibrated issue #{issue_number} "
                         f"user_id {_existing_user_id}->{user_id}")
            if target and target != _existing_assignee:
                # Issue #827: 新 assignee 未注册平台（解析 user_id 为空/0）时保留已有非零归属，
                # 防止跨流水线/场景移交把创建人 user_id 清零（「我的需求」随之不可见）。
                # D105 起 user_id 为 uuid 字符串：一律 str 归一比较，不用数值大小判定。
                _resolved_uid = str(user_id or "").strip()
                _preserve_uid = user_id if _resolved_uid not in ("", "0") else (_existing_user_id or "0")
                c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, _preserve_uid, task_id))
                try:
                    _close_other_assignee_tasks(repo_key, issue_number, keep_assignee=target)
                except Exception:
                    pass
            # Issue #805: 创建人幂等补写——仅 created_by 为空时回填，绝不覆盖已有创建人
            # Issue #827: creator 未提供时回退 sender（webhook 操作用户 = 真实创建人），
            # 保证空 created_by 仍能被回补；幂等语义不变（仍只填空值）。
            # 刻意不回退 assignee/target——责任人流转 ≠ 创建人，避免把归属误写成 assignee。
            _creator_val = (creator or "").strip() or (sender or "").strip()
            if _creator_val:
                c.execute(
                    "UPDATE issue SET created_by=? WHERE id=? AND (created_by IS NULL OR created_by='')",
                    (_creator_val, task_id))
            _rt_db.commit()
        else:
            if not title:
                title = _get_issue_title(repo_key, issue_number)
            if not description:
                description = f"{event_type or 'Issue'}：请在任务中心查看并处理。"
            _subtask_val = subtask_mode if subtask_mode >= 0 else _get_issue_subtask_mode(repo_key, issue_number)
            _parent_val = _get_issue_parent(repo_key, issue_number)
            # Issue #827: 创建人落库——creator 优先，缺失时回退 sender（webhook 操作用户）；
            # 不回退 assignee/target（责任人 ≠ 创建人），与 #805 创建人口径一致。
            _created_by_insert = (creator or "").strip() or (sender or "").strip()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id, org_id, created_by) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
                (user_id, target, repo_key, issue_number, title, description,
                 _subtask_val, _parent_val, _get_issue_parent_id(repo_key, issue_number), org_id,
                 _created_by_insert)
            )
            _rt_db.commit()
            task_id = c.lastrowid
            linked = None
    # 「去对话」配套：str(user_id or "") not in ("", "0") 且未关联对话时创建/复用 Issue 会话
    if str(user_id or "") not in ("", "0") and not linked:
        try:
            conv_id = _ensure_task_conversation(user_id, repo_key, issue_number, title, description)
            if conv_id:
                with db_lock:
                    c = _rt_db.cursor()
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, task_id))
                    _rt_db.commit()
        except Exception as e:
            log.warning(f"ensure_issue_task conversation link failed: {e}")
    return task_id

def close_issue_tasks(repo_key: str, issue_number: int) -> int:
    """webhook 兜底（P5）：Issue closed → 该 issue 所有 active/processing 任务进入 pending_review。

    Issue #51 修订：不再直接 completed，而是先设为 pending_review 等待开发者审核。
    只有 stage 已经是 done/pending_review 时才直接关闭。
    """
    from src.kernel.repo_key import to_repo_key
    from src.api.session_store import get_issue_stage, set_issue_stage
    repo_key = to_repo_key(repo_key)
    _issue_id = f"#{issue_number}"
    _stage_info = get_issue_stage(repo_key, _issue_id)
    _stage = (_stage_info or {}).get("stage", "") if _stage_info else ""
    # 人工闸（2026-11-17）：stage=done 只当「人推进的」才算开发者确认。
    # owner_agent 是落库的推进方：user/admin = 人；agent/pipeline/issue = 自动路径。
    _stage_owner = ((_stage_info or {}).get("owner_agent") or "").strip().lower()

    _promoted_review = 0    # 本次调用真实推进到 pending_review 的行数（#2419 通知触发判据）
    with db_lock:
        c = _rt_db.cursor()
        # 检查是否有 agent 正在 running
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running') LIMIT 1",
            (repo_key, _issue_id))
        _has_running = c.fetchone() is not None

        if _stage == "done" and _stage_owner in ("user", "admin"):
            # 只认人工推进的 done（Web 确认 / 管理员确认）→ directly completed
            c.execute(
                "UPDATE issue SET status='completed', completed_reason='issue_closed', completed_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input','pending_review')",
                (repo_key, issue_number)
            )
        elif _stage == "done":
            # stage=done 但推进方不是人（历史自动收尾残留 / 脏数据）→ 不结单，
            # 收口到待人工确认，等人工走 confirm-done。
            log.info(f"close_issue_tasks: stage=done by '{_stage_owner or 'unknown'}' is not "
                     f"human-driven, downgrade {repo_key}/#{issue_number} to pending_review")
            set_issue_stage(repo_key, _issue_id, "pending_review", "pipeline")
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='active'",
                (repo_key, issue_number)
            )
        elif _stage == "pending_review":
            # Issue #85: stage=pending_review → 保持 pending_review，不自动 completed
            # 只将 active 任务推进到 pending_review（processing/awaiting_input 保持原状）
            c.execute(
                "UPDATE issue SET status='pending_review' "
                "WHERE repo_key=? AND issue_number=? AND status='active'",
                (repo_key, issue_number)
            )
            _promoted_review = c.rowcount
        elif not _has_running:
            # 还在 pipeline 流转中且无 agent 运行 → 设为 pending_review 等待开发者确认
            # Issue #85: 无 issue_state 记录（stage 为空）时也走此路径
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
                (repo_key, issue_number)
            )
            _promoted_review = c.rowcount
            set_issue_stage(repo_key, _issue_id, "pending_review", "issue_closed")
        # else: 有 agent 在运行 → 不修改 stage，等 agent 完成后再判定
        _rt_db.commit()
        _affected = c.rowcount
    # Issue #4295：通知投递必须在 db_lock **之外**。maybe_notify_review_point_status
    # 内部要解析通知目标（issue 行 → conv 兜底建/复用 → open_id → 本人 bot ctx/api → cfg），
    # 属于"锁内做外部 IO + 多次 DB 往返"，会把前台请求的临界区越滚越大（顶到 99.9s）。
    # 状态判定与 UPDATE 仍全在锁内（_promoted_review/_affected 已在锁内定格）。
    if _promoted_review > 0:
        maybe_notify_review_point_status(
            repo_key, issue_number, "pending_review",
            "VCS Issue 已关闭，关联任务转入待审核，请确认是否完结")
    return _affected

def _close_other_assignee_tasks(repo_key: str, issue_number: int, keep_assignee: str) -> int:
    """换人：不再把需求标完成（reassigned 语义错位，见 reassign-takeover-no-complete-design.md）。

    单行模型（issue.(repo_key, issue_number) UNIQUE）下 assignee<>keep 基本不命中；
    责任转移由 _ensure_assignee_task_card 更新 assignee 承担。保持 no-op 幂等，不触碰需求状态。
    """
    return 0

def _close_issue_tasks_bot_takeover(repo_key: str, issue_number: int) -> int:
    """bot 接管：不把需求标完成（bot_takeover 语义错位，见 reassign-takeover-no-complete-design.md）。

    接管语义 = bot 自己继续处理，绝不完结需求：
    - stage='done'（本已终态）→ 早退不改状态（已完成的保持 completed，不翻回不重标）；
    - stage='pending_review' → 保持 pending_review；
    - 无 running agent 且活跃 → pending_review 等待开发者确认；
    - 有 agent 在 running → 不修改，等 agent 完成后再判定。
    """
    from src.api.session_store import get_issue_stage, set_issue_stage
    _issue_id = f"#{issue_number}"
    _stage_info = get_issue_stage(repo_key, _issue_id)
    _stage = (_stage_info or {}).get("stage", "") if _stage_info else ""

    _promoted_review = 0    # 本次调用真实推进到 pending_review 的行数（#2419 通知触发判据）
    # 检查是否有 agent 正在 running
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running') LIMIT 1",
            (repo_key, _issue_id))
        _has_running = c.fetchone() is not None

        if _stage == "done":
            # 本已终态：bot 接管不改变完成状态（防止重复完成 / 翻活已完成需求）
            return 0
        elif _stage == "pending_review":
            # Issue #85: stage=pending_review → 保持，不自动 completed
            c.execute(
                "UPDATE issue SET status='pending_review' "
                "WHERE repo_key=? AND issue_number=? AND status='active'",
                (repo_key, issue_number))
            _promoted_review = c.rowcount
        elif not _has_running:
            # 没有 agent 在运行 → 设为 pending_review（Issue #85: 同步 processing 任务）
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
                (repo_key, issue_number))
            _promoted_review = c.rowcount
            set_issue_stage(repo_key, _issue_id, "pending_review", "bot_takeover")
        # else: 有 agent 在运行 → 不修改 stage，等 agent 完成后再判定
        _rt_db.commit()
        _affected = c.rowcount
    # Issue #4295：同 close_issue_tasks ——通知投递移出 db_lock（目标解析含多次 DB 往返
    # 与本人 bot 凭证解析，锁内执行等同把外部 IO 拉进临界区）。
    if _promoted_review > 0:
        maybe_notify_review_point_status(
            repo_key, issue_number, "pending_review",
            "任务已被 bot 接管并转入待审核，请确认后续处理方式")
    return _affected

# ===== Issue #4170：交付终态（PR merged + Issue closed）判定（单点，派发/复位共用）=====

TERMINAL_DELIVERY_SUPPRESS_REASON = "terminal_delivery_suppressed"


def _issue_has_merged_pr(repo_key: str, issue_number) -> bool:
    """承载 PR 合入证据：events 表中该 Issue 存在真实 ``pull_request_merged`` 事件。

    口径对齐 ``src.domains.mgmt.org_dispatch._pr_evidence``——合入判定只看 VCS 权威
    的 merge 事件，不猜 PR 状态。兼容 issue_id 带/不带 ``#`` 两种历史落库形态。
    查询异常返回 False（fail-open：读失败不得误抑制派发）。
    """
    _num = str(native_issue_number(issue_number) or "").strip()
    if not _num:
        return False
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "SELECT 1 FROM events WHERE repo_key=? AND issue_id IN (?, ?) "
                "AND event_type='pull_request_merged' LIMIT 1",
                (repo_key, f"#{_num}", _num))
            return c.fetchone() is not None
    except Exception as _e:
        log.warning(f"terminal-check: merged PR evidence query failed "
                    f"{repo_key}/#{_num}: {_e}")
        return False


def _issue_repo_state_closed(repo_key: str, issue_number) -> bool:
    """Issue 仓库态是否 closed（VCS 权威读；无 token/查询失败返回 False 即 fail-open）。"""
    _num = native_issue_number(issue_number)
    if not _num:
        return False
    try:
        client = _pr_vcs_client(repo_key)
        if client is None:
            return False
        return client.get_issue_state(_num) == "closed"
    except Exception as _e:
        log.warning(f"terminal-check: issue state read failed {repo_key}/#{_num}: {_e}")
        return False


def is_issue_delivery_terminal(repo_key: str, issue_number) -> bool:
    """Issue 是否已达「交付终态」：承载 PR 已 merged **且** Issue 仓库态 closed（双条件）。

    单一判定点，供两处复用：
    - `event_pipeline` 的 ``issue_comment`` 派发守卫（终态仅记录、不派发）；
    - 本模块 ``_resume_issue_waiting`` 的 stage/task_status 复位抑制（终态不写回）。

    reopen 后仓库态变回 open → 脱离终态，守卫自然失效（无需单独 reopen 策略）。
    任一条件读不到/异常 → False（fail-open，宁放过不错杀在途单）。
    """
    try:
        if not _issue_has_merged_pr(repo_key, issue_number):
            return False
        return _issue_repo_state_closed(repo_key, issue_number)
    except Exception as _e:
        log.warning(f"terminal-check failed {repo_key}/#{issue_number}: {_e}")
        return False


# 进入 pending_review 时可能写入的哨兵 owner_agent（非真实流水线节点）；
# resume 回落 stage 时不能直接采用，避免阶段取值非法。
_RESUME_STAGE_SENTINELS = ("", "bot_takeover", "issue_closed", "pending_review")

def _resume_stage(owner_agent: str) -> str:
    """pending_review 恢复到哪个阶段：真实节点用 owner_agent，哨兵回落到默认首节点 issue。"""
    _owner = (owner_agent or "").strip()
    return "issue" if _owner in _RESUME_STAGE_SENTINELS else _owner

def _resume_issue_rows(repo_key: str, issue_number: int, from_statuses: tuple,
                       new_stage: str = "", new_owner: str = "",
                       fallback_pending_review_stage: bool = False) -> tuple:
    """`from_statuses → processing` 的**唯一**状态迁移实现（幂等）。

    返回 ``(changed, stage, owner_agent)``：``stage``/``owner_agent`` 为翻转前读到的
    库内值，供调用方决定 stage 落点。Issue #2816 复审：``_resume_issue_waiting`` 与
    ``resume_issue_from_manual`` 曾各写一份 UPDATE，状态集合与 stage 策略各异；
    此处收敛为单源，两条入口只保留各自的 stage 策略差异。

    Issue #2816 复审（硬违规，MySQL `rowcount` 语义）：生产集群走 MySQL，PyMySQL
    未启用 ``CLIENT_FOUND_ROWS``，匹配但值未变的 UPDATE 返回 ``rowcount=0``。
    ``RESUMABLE_ISSUE_STATUSES`` 含 ``processing``，对已是 processing 的单执行
    new_carrier/reassign 时 UPDATE 匹配但值不变 → 旧实现误判未恢复并 409。这里在
    0 行时回读 status，若仍在 ``from_statuses``（尤其 processing）视为已恢复。

    复审（判断项，事务一致性）：状态翻转与 stage 落点在同一 ``db_lock``/事务内完成，
    避免先提交 status 再单独提交 stage；stage 写失败时不会留下「status=processing
    但 stage 仍旧」的中间态。
    """
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT stage, owner_agent FROM issue WHERE repo_key=? AND issue_number=?",
            (repo_key, issue_number))
        _row = c.fetchone()
        _stage = ((_row[0] if _row else "") or "").strip()
        _owner = ((_row[1] if _row else "") or "").strip()
        _ph = ",".join("?" * len(from_statuses))
        c.execute(
            f"UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
            f"WHERE repo_key=? AND issue_number=? AND status IN ({_ph})",
            (repo_key, issue_number, *from_statuses))
        _changed = c.rowcount > 0
        if not _changed:
            # MySQL 未启用 CLIENT_FOUND_ROWS：matched-but-unchanged → 0 行。
            # 回读当前 status，仍处于可恢复集合时按「已恢复」处理（幂等重试）。
            c.execute(
                "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                (repo_key, issue_number))
            _again = c.fetchone()
            _cur = ((_again[0] if _again else "") or "").strip()
            if _cur in from_statuses:
                _changed = True
        if _changed:
            _target_stage = ""
            _target_owner = _owner
            if new_stage:
                _target_stage = new_stage
                _target_owner = new_owner or _owner
            elif fallback_pending_review_stage and _stage == "pending_review":
                _target_stage = _resume_stage(_owner)
            if _target_stage:
                c.execute(
                    "UPDATE issue SET stage=?, owner_agent=? "
                    "WHERE repo_key=? AND issue_number=?",
                    (_target_stage, _target_owner, repo_key, issue_number))
        _rt_db.commit()
    return _changed, _stage, _owner


def _resume_issue_waiting(repo_key: str, issue_number: int,
                          preserve_review: bool = False) -> bool:
    """「等用户」双态恢复（幂等）：`awaiting_input / pending_review → processing`。

    供两条路径复用（issue-explicit-awaiting-input-api-design.md §3.6 / issue-pending-review-explicit-design.md §3.5）：
    - webhook `issue_comment` 自动恢复（非 bot 用户评论）；
    - `issues clarify` / Agent 显式续接。
    仅当 issue 正处于 `awaiting_input` 或 `pending_review` 时翻回，返回是否真正翻转（幂等、可观测）。

    Issue #1599（审核态漂移收敛）：翻转 status 时同步把 `stage=pending_review` 回落到
    `owner_agent`，避免库内 `status=processing` 但 `stage` 仍停在 pending_review，
    导致展示态继续显示「待审核」而 review-reject 必然 noop。

    Issue #2381：``preserve_review=True`` 表示本次是 redispatch 重放历史评论，不是新的
    用户输入，`pending_review` 审核点不得再次被消费（保留审核点，只恢复 awaiting_input）。

    Issue #2816 复审：状态迁移走 ``_resume_issue_rows`` 单源；仅真实翻转时才回落
    stage（``pending_review`` → ``_resume_stage(owner_agent)``）。

    Issue #4170：交付终态（PR merged + Issue closed）后评论不再复位审核点——
    终态直接短路，stage/task_status 均不写回（不把 pending_review 打回 processing）。
    """
    if is_issue_delivery_terminal(repo_key, issue_number):
        log.info(f"Issue waiting resume suppressed: {repo_key}/#{issue_number} "
                 f"delivery terminal (PR merged + issue closed), stage/task_status preserved")
        return False
    _from_statuses = ("awaiting_input",) if preserve_review else ("awaiting_input", "pending_review")
    try:
        _changed, _stage, _owner = _resume_issue_rows(
            repo_key, issue_number, _from_statuses,
            fallback_pending_review_stage=True)
        if _changed and _stage == "pending_review":
            # stage 已在 _resume_issue_rows 的同一事务内提交；这里只补 task.status
            # 同步（幂等，重复写同值不影响一致性）。
            try:
                from src.api.session_store import set_issue_stage
                set_issue_stage(repo_key, f"#{issue_number}", _resume_stage(_owner), _owner)
            except Exception as _se:
                log.warning(f"resume waiting: sync task status failed "
                            f"{repo_key}/#{issue_number}: {_se}")
        if _changed:
            log.info(f"Issue waiting resume: {repo_key}/#{issue_number} awaiting/pending_review → processing "
                     f"(preserve_review={preserve_review})")
        return _changed
    except Exception as _e:
        log.warning(f"resume issue from waiting failed {repo_key}/#{issue_number}: {_e}")
        return False

# 可被审核点置位覆盖的源状态（与旧 UPDATE 的 IN 集合严格一致）。
_PENDING_REVIEW_SOURCE_STATUSES = ("active", "processing", "awaiting_input")


def _set_issue_pending_review_with_prior(repo_key: str, issue_number: int,
                                         review_points: str = "",
                                         references: str = "") -> tuple[str, str, bool]:
    """设置阶段审核点并回传「写入前状态 → 写后状态」对账三元组（Issue #4103）。

    返回 ``(prior_status, status, changed)``：

    - ``prior_status``：写入前在**同一事务/锁内**实读到的 issue.status（与
      ``issues status get`` 同源）；
    - ``status``：写后复读到的真实 issue.status（未写入时为当前实读值）；
    - ``changed``：**本次是否真实写入**（``cursor.rowcount > 0``）。首次置位、
      审核点内容变更均为 True；并发方抢先置位导致条件更新命中 0 行时必须保持
      False，通知也不得重复触发。

    Issue #4063：已在位（``pending_review``）时按 review_points/review_refs 内容判断——
    内容等价 → 幂等短路零写入；内容变更 → 仅刷新快照，状态不迁移；存量在位行无快照
    （review_points 为空）→ 无法证明变更，按幂等返回 False（兼容 #725）。

    并发语义：源状态属于 ``active/processing/awaiting_input`` 时用**条件写入**
    （``WHERE status=prior_status``）把写入基线钉在读取值上；若读后又被并发事件
    改走，则条件更新不命中，不会覆盖新状态。
    """
    _prior = ""
    _content = (review_points or "").strip()
    _refs = (references or "").strip()
    try:
        with db_lock:
            c = _rt_db.cursor()
            _prior = _read_issue_status(repo_key, issue_number, c=c)
            c.execute(
                "SELECT review_points, review_refs FROM issue "
                "WHERE repo_key=? AND issue_number=?",
                (repo_key, issue_number))
            _row = c.fetchone()
            if not _row:
                return "", "", False
            _stored = ((_row[0] if _row[0] is not None else "") or "").strip()
            _stored_refs = ((_row[1] if len(_row) > 1 and _row[1] is not None else "") or "").strip()
            _wrote = 0
            if _prior == "pending_review":
                if not _stored:
                    # 存量在位行无内容快照：保持 #725 幂等语义（不重复写会话/通知）
                    return _prior, _prior, False
                if _stored == _content and _stored_refs == _refs:
                    return _prior, _prior, False
                c.execute(
                    "UPDATE issue SET review_points=?, review_refs=? "
                    "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                    (_content, _refs, repo_key, issue_number))
                _wrote = c.rowcount
                _rt_db.commit()
            elif _prior in _PENDING_REVIEW_SOURCE_STATUSES:
                c.execute(
                    "UPDATE issue SET status='pending_review', completed_reason=NULL, completed_at=NULL, "
                    "review_points=?, review_refs=? "
                    "WHERE repo_key=? AND issue_number=? AND status=?",
                    (_content, _refs, repo_key, issue_number, _prior))
                _wrote = c.rowcount
                _rt_db.commit()
            _status = _read_issue_status(repo_key, issue_number, c=c)
        _changed = _wrote > 0
        if _changed:
            log.info(f"Issue pending_review set: {repo_key}/#{issue_number} "
                     f"prior={_prior or '<empty>'} status={_status or '<empty>'}")
        return _prior, _status, _changed
    except Exception as _e:
        log.warning(f"set issue pending_review failed {repo_key}/#{issue_number}: {_e}")
        return _prior, _prior, False


def _set_issue_pending_review(repo_key: str, issue_number: int,
                              review_points: str = "", references: str = "") -> bool:
    """Agent 显式设置阶段审核点（幂等）：`active/processing/awaiting_input → pending_review`，不改 stage。

    Issue #4103：布尔返回保留给既有调用方；需要 prior/status 对账的入口改用
    ``_set_issue_pending_review_with_prior``。
    """
    _, _, _changed = _set_issue_pending_review_with_prior(
        repo_key, issue_number, review_points, references)
    return _changed

# Issue #2381：末节点判据统一口径——`next=="done"` 与空串（无下游）均视为末节点。
# 此前 _is_last_stage_node 仅认 "done"、_pipeline_last_node_check 同时认 ""，CLI 流水线
# 末节点 next 为空串时 review-pass 的末节点放宽失效，target=done 恒 noop:not_pending_review。
_LAST_NODE_NEXT_VALUES = ("done", "")

def _next_is_last_node(next_value) -> bool:
    """统一末节点判据：next 为 'done' 或空串（无下游节点）时为真。

    Issue #2381 review: only string values may be interpreted. A missing key
    (None) still means "no downstream node", but any other type (0, False,
    numbers) is a configuration error and must NOT be treated as the last
    node -- the previous `str(value or "")` turned 0 into "" and failed open.
    """
    if next_value is None:
        return True
    if not isinstance(next_value, str):
        return False
    return next_value.strip() in _LAST_NODE_NEXT_VALUES


# ===== Issue #2251: 组织下派聚合收口（组织 Issue 评论 + 状态归一）=====
_ORG_AGG_TERMINAL_TARGETS = ("completed", "needs_review", "failed", "cancelled")
_ORG_AGG_STATUS_LABEL = {
    "completed": "已完成",
    "needs_review": "待人工审核",
    "partial_failed": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}
_ORG_AGG_TARGET_LABEL = {
    "completed": "completed",
    "needs_review": "needs_review",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _org_repo_bot_token(repo_key: str) -> str:
    """按 provider 取组织仓库 bot token（best-effort，未配置返回空串）。"""
    try:
        from src.kernel.repo_key import parse_repo_key
        provider, _, _ = parse_repo_key(repo_key)
        from src.domains.mgmt import public as _mgmt_token
        if provider == "github":
            return _mgmt_token.get_github_token(repo_key=repo_key) or ""
        return _mgmt_token.get_gitcode_token(repo_key=repo_key) or ""
    except Exception as _e:
        log.warning(f"org-aggregate: load bot token failed repo={repo_key}: {_e}")
        return ""


def _org_pr_link(repo_key: str, pr_number: int) -> str:
    if not pr_number:
        return ""
    try:
        from src.kernel.repo_key import parse_repo_key
        provider, owner, name = parse_repo_key(repo_key)
        host = "github.com" if provider == "github" else "gitcode.com"
        return f"[#{int(pr_number)}](https://{host}/{owner}/{name}/pull/{int(pr_number)})"
    except Exception:
        return f"#{int(pr_number)}"


def build_org_issue_aggregate_comment(dispatch: dict, targets: list[dict],
                                     terminal_status: str) -> str:
    """组织 Issue 聚合评论正文：结论 + 每个 target 的子 Issue/任务/PR + 待审/失败清单。"""

    counts: dict[str, int] = {}
    for t in targets:
        st = str(t.get("status") or "")
        counts[st] = counts.get(st, 0) + 1
    label = _ORG_AGG_STATUS_LABEL.get(terminal_status, terminal_status)
    stats = " / ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "无"
    lines = [
        f"## 组织需求聚合结论：{label}",
        "",
        f"- 批次：dispatch #{dispatch.get('id') or ''}",
        f"- 批次状态：**{terminal_status}**",
        f"- 目标统计：{stats}",
        "",
        "| 目标仓库 | 状态 | 子 Issue | 任务 | PR | 备注 |",
        "|---|---|---|---|---|---|",
    ]
    pending_review: list[str] = []
    failed: list[str] = []
    for t in targets:
        repo_key = str(t.get("repo_key") or "")
        st = str(t.get("status") or "")
        child = native_issue_number(t.get("child_issue_number") or "")
        task_id = int(t.get("child_task_id") or 0)
        child_cell = (f"[#{child}]({_vcs_issue_url(repo_key, child)})"
                      if is_actionable_issue_number(child) else "-")
        pr_cell = _org_pr_link(repo_key, int(t.get("pr_number") or 0)) or "-"
        note = str(t.get("error") or "").replace("|", "\\|")[:200] or "-"
        lines.append(
            f"| {repo_key} | {_ORG_AGG_TARGET_LABEL.get(st, st or '-')} | {child_cell} | "
            f"{task_id or '-'} | {pr_cell} | {note} |")
        if st == "needs_review":
            pending_review.append(f"- {repo_key}（子 Issue {child_cell}）：待人工审核")
        elif st == "failed":
            failed.append(f"- {repo_key}（子 Issue {child_cell}）：{note}")
    if pending_review:
        lines.extend(["", "### 待审目标", *pending_review])
    if failed:
        lines.extend(["", "### 失败目标", *failed])
    if not targets:
        lines.append("")
        lines.append("无目标。")
    lines.extend([
        "",
        "聚合由服务端组织流水线自动生成；仅当全部目标进入终态时才会收口。",
    ])
    return "\n".join(lines)


def finalize_org_issue_aggregation(dispatch: dict, targets: list[dict],
                                   terminal_status: str, *,
                                   auto_finalize: bool = False) -> dict:
    """Issue #2251：组织 Issue 幂等收口（聚合评论 + 最终状态归一）。

    - 未全部终态 → ``{"action": "pending", "remaining": [...]}``，不写评论、不改状态
      （禁止提前 pending_review）；
    - 全部 cancelled / 批次 cancelled → 组织 Issue `cancelled`；
    - auto_finalize 且全部 completed → `completed` + stage=done；
    - 其余终态 → `pending_review`（needs_review / failed 原因已在评论列明）；
    - 评论 best-effort（无 token / VCS 失败不阻断状态归一），调用方负责幂等触发。
    """
    org_repo_key = str(dispatch.get("org_repo_key") or "")
    # D105/A12：org_issue_number 已放宽为平台原生 ident（数字或 Gitee IK* 字符串），
    # dispatch.id 已改为 TEXT uuid 主键——两者都不能再 int() 强转（uuid 会直接
    # ValueError，整条下派收口链路 500）。
    issue_number = native_issue_number(dispatch.get("org_issue_number") or "")
    dispatch_id = str(dispatch.get("id") or "")
    if not org_repo_key or not is_actionable_issue_number(issue_number):
        return {"action": "skipped", "reason": "no_org_issue"}

    remaining = [
        {"repo_key": t.get("repo_key") or "", "status": t.get("status") or ""}
        for t in targets
        if str(t.get("status") or "") not in _ORG_AGG_TERMINAL_TARGETS
    ]
    if remaining:
        return {"action": "pending", "remaining": remaining,
                "dispatch_id": dispatch_id, "issue_status": ""}

    statuses = [str(t.get("status") or "") for t in targets]
    comment_posted = False
    comment_error = ""
    body = build_org_issue_aggregate_comment(dispatch, targets, terminal_status)
    _already_posted = False
    if dispatch_id:
        try:
            from src.domains.mgmt import public as _mgmt_public

            _already_posted = bool(
                _mgmt_public.get_dispatch_aggregate_comment(
                    dispatch_id, terminal_status))
        except Exception as _e:
            log.warning(f"org-aggregate: read comment flag failed dispatch={dispatch_id}: {_e}")
    token = _org_repo_bot_token(org_repo_key)
    if token and not _already_posted:
        try:
            from src.domains.runtime.vcs.api_client import VcsApiClient
            from src.kernel.repo_key import parse_repo_key
            provider, owner, name = parse_repo_key(org_repo_key)
            # post_issue_comment 的网络/上游失败以 {"error": ...} 返回（不抛异常）；
            # 必须显式判错，否则会把「评论未发出」记成已发并落 comment_posted=1。
            _resp = VcsApiClient(provider, token, owner, name).post_issue_comment(issue_number, body)
            if isinstance(_resp, dict) and _resp.get("error"):
                raise RuntimeError(str(_resp.get("error"))[:300])
            comment_posted = True
            log.info(f"org-aggregate: comment posted repo={org_repo_key} "
                     f"issue=#{issue_number} dispatch={dispatch_id} status={terminal_status}")
        except Exception as _e:
            log.warning(f"org-aggregate: comment post failed repo={org_repo_key} "
                        f"issue=#{issue_number}: {_e}")
            comment_error = f"comment: {_e}"[:500]
    elif _already_posted:
        log.info(f"org-aggregate: comment already posted, skip dispatch={dispatch_id} "
                 f"status={terminal_status}")
    else:
        log.info(f"org-aggregate: skip comment (no bot token) repo={org_repo_key} "
                 f"issue=#{issue_number}")
    if dispatch_id and comment_posted:
        try:
            from src.domains.mgmt import public as _mgmt_public

            _mgmt_public.mark_dispatch_aggregate_comment(
                dispatch_id, terminal_status)
        except Exception as _e:
            log.warning(f"org-aggregate: persist comment flag failed dispatch={dispatch_id}: {_e}")

    all_completed = bool(statuses) and all(s == "completed" for s in statuses)
    all_cancelled = bool(statuses) and all(s == "cancelled" for s in statuses)
    if terminal_status == "cancelled" or all_cancelled:
        target_status, reason = "cancelled", "org_dispatch_cancelled"
    elif auto_finalize and all_completed:
        target_status, reason = "completed", _AUTO_FINALIZED_REASON
    else:
        target_status, reason = "pending_review", ""

    with db_lock:
        c = _rt_db.cursor()
        if reason:
            c.execute(
                "UPDATE issue SET status=?, completed_reason=?, completed_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status IN "
                "('active','processing','awaiting_input','pending_review')",
                (target_status, reason, org_repo_key, issue_number),
            )
        else:
            c.execute(
                "UPDATE issue SET status=?, completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN "
                "('active','processing','awaiting_input','pending_review')",
                (target_status, org_repo_key, issue_number),
            )
        _rt_db.commit()
        _changed = c.rowcount > 0
        c.execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (org_repo_key, issue_number),
        )
        _row = c.fetchone()
    actual_status = (_row[0] if _row else "") or target_status
    if target_status == "completed" or target_status == "cancelled":
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(org_repo_key, f"#{issue_number}",
                            "done" if target_status == "completed" else target_status,
                            "org_aggregate")
        except Exception as _se:
            log.warning(f"org-aggregate: set stage failed repo={org_repo_key} "
                        f"issue=#{issue_number}: {_se}")
    log.info(f"org-aggregate: finalized dispatch={dispatch_id} status={terminal_status} "
             f"org_issue=#{issue_number} issue_status={actual_status} changed={_changed} "
             f"comment={comment_posted}")
    return {
        "action": "finalized",
        "dispatch_id": dispatch_id,
        "terminal_status": terminal_status,
        "issue_status": actual_status,
        "changed": bool(_changed),
        "comment_posted": comment_posted,
        "comment_error": comment_error,
        "remaining": [],
    }


def _is_last_stage_node(repo_key: str, issue_number: int) -> bool:
    """当前 issue.stage 是否为流水线末节点（next=='done' 或无下游节点）。

    与 `_pipeline_last_node_check` 共用 `_next_is_last_node` 判据（Issue #2381 统一口径）；
    stage 不可推定 / 未知 / 已终态 → 保守返回 False（review-pass 归一兜底用）。
    """
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.domains.mgmt.public import get_pipeline_config  # 跨域走 owner public
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if not _cur or _cur in ("done", "pending_review"):
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key) or {}
        from src.domains.runtime.engine.engine_select import node_matches_agent_role
        # stage 是角色（tag），组织场景节点 id 与 tag 不同 → 必须按角色匹配。
        for _n in (_cfg.get("nodes") or []):
            if node_matches_agent_role(_n, _cur):
                return _next_is_last_node(_n.get("next"))
        return False
    except Exception:
        return False

# Issue #1599：审核动作未命中原因分类所依赖的终态集合
_ISSUE_TERMINAL_STATUSES = ("completed", "cancelled")

def _read_issue_status(repo_key: str, issue_number: int, c=None) -> str:
    """回读 issue 表真实 status（空串 = 无行或回读失败，由调用方兜底）。

    传入 ``c`` 时复用调用方游标（调用方须已持 ``db_lock``），用于同一事务内的读取/写后复读；
    不传则自取锁与游标。
    """
    _sql = "SELECT status FROM issue WHERE repo_key=? AND issue_number=?"
    _args = (repo_key, issue_number)
    try:
        if c is not None:
            c.execute(_sql, _args)
            _r = c.fetchone()
            return ((_r[0] if _r else "") or "").strip()
        with db_lock:
            _cur = _rt_db.cursor()
            _cur.execute(_sql, _args)
            _r = _cur.fetchone()
        return ((_r[0] if _r else "") or "").strip()
    except Exception as _e:
        log.warning(f"read issue status failed {repo_key}/#{issue_number}: {_e}")
        return ""

def _review_checkpoint_invalidated(repo_key: str, issue_number: int) -> bool:
    """Issue #2381：判断 processing 是否由「末节点审核点被 redispatch/回复回落」造成。

    判据：末节点（next in ('done','')）的最新事件已 completed（说明审核点曾置好），
    但 issue 当前 stage 已回落到非末节点（`_resume_issue_waiting` 把 stage 从
    pending_review 回落到 owner_agent）→ 审核点已失效。仅用于 noop 原因码降级分类，
    不改变任何状态。
    """
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.api.pipeline_config import get_pipeline_config
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if not _cur or _cur in ("done", "pending_review"):
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _nodes = (get_pipeline_config(_scene_id, repo_key=repo_key) or {}).get("nodes") or []
        _last_ids = set()
        for _n in _nodes:
            _nid = (_n.get("id") or _n.get("tag") or "").strip()
            if _nid and _next_is_last_node(_n.get("next")):
                _last_ids.add(_nid)
        if not _last_ids or _cur in _last_ids:
            return False
        _latest = _issue_latest_event_status_by_agent(repo_key, _issue_id)
        return any(_latest.get(_nid) == "completed" for _nid in _last_ids)
    except Exception:
        return False

def _review_noop_reason(repo_key: str, issue_number: int, target_status: str) -> str:
    """审核动作未命中时回读真实 status 并给出机器可读原因码。

    - `noop:already_target`：已处于目标态（幂等，无需变更）；
    - `noop:terminal`：已完成/已取消，不可再变更；
    - `noop:not_pending_review`：前置态不符（如 awaiting_input/active）；
    - `noop:review_checkpoint_invalidated`：Issue #2381——末节点审核点已因
      redispatch/用户回复 resume 回落而失效（processing 且 stage 已离开末节点）；
    - `noop:error`：回读失败（无行/DB 异常）。
    """
    _cur = _read_issue_status(repo_key, issue_number)
    if not _cur:
        return "noop:error"
    if _cur == target_status:
        return "noop:already_target"
    if _cur in _ISSUE_TERMINAL_STATUSES:
        return "noop:terminal"
    if (target_status == "completed" and _cur == "processing"
            and _review_checkpoint_invalidated(repo_key, issue_number)):
        return "noop:review_checkpoint_invalidated"
    return "noop:not_pending_review"

# Issue #656: 自动结单完成原因（区别于 review_passed/user_confirmed 人工确认路径）
_AUTO_FINALIZED_REASON = "auto_finalized"

def _pipeline_last_node_check(repo_key: str, issue_number, agent_type: str) -> bool:
    """完成的 agent 是否为该 issue 流水线末节点（next=='done' 或 next 无下游节点）。"""
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.domains.mgmt.public import get_pipeline_config  # 跨域走 owner public
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if _cur != agent_type:
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key) or {}
        _nodes = _cfg.get("nodes") or []
        from src.domains.runtime.engine.engine_select import node_matches_agent_role
        # 末节点判定必须按角色（id 或 tag）命中：组织场景 id=org_aggregate/tag=qa，
        # 事件里的 agent_type 是 tag。只比 id 会让 org 场景永远判不出末节点。
        _ids = {(str(_n.get("id") or "").strip()) for _n in _nodes}
        _ids |= {(str(_n.get("tag") or "").strip()) for _n in _nodes}
        _ids.discard("")
        for _n in _nodes:
            if not node_matches_agent_role(_n, agent_type):
                continue
            _next = (_n.get("next") or "").strip()
            if _next_is_last_node(_next):
                return True
            if _next not in _ids:
                # 配置笔误（next 指向不存在节点）不视为末节点，防误触发自动结单
                log.warning(f"pipeline last-node check: next={_next} not in nodes "
                            f"(misconfig?) {repo_key}/#{issue_number} agent={agent_type}")
            return False
        return False
    except Exception as _e:
        log.warning(f"pipeline last-node check failed {repo_key}/#{issue_number}: {_e}")
        return False

def _find_github_mirror_repo_key(repo_key: str) -> str:
    """查找 GitCode 仓库绑定的 GitHub 镜像 repo_key（confirm-done 同款逻辑，#656 复用）。

    managed_repos 表中同一 owner/repo 可能同时存在 gitcode: 和 github: 两条记录，
    通过 full_name 匹配。找不到返回空串。
    """
    try:
        from src.kernel.repo_key import split_owner_repo
        from src.domains.mgmt import public as _mgmt_public_repo
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            return ""
        gh_key = f"github:{owner}/{repo}"
        # managed_repos 归 mgmt：命中时返回库里的 repo_key 原值（列序/谓词同原语句）
        return _mgmt_public_repo.repo_key_exists_any(gh_key)
    except Exception as e:
        log.warning(f"find github mirror for {repo_key} failed: {e}")
        return ""

def _issue_latest_event_status_by_agent(repo_key: str, issue_id_tag: str) -> dict:
    """该 issue 每个 agent_type 的最新事件状态（seq 最大者），用于「全部节点 completed」检测。

    兼容 events.issue_id 带/不带 `#` 两种历史形态（同一 issue 归并后取全局最新）。

    Issue #3444：去重留痕（status='skipped'/'deduped'）不参与「最新事件」判定。
    这类行由 append_comment_dedup_event 写入，携带真实 agent_type 仅供审计，
    不代表节点执行进度；若按 MAX(seq) 选中，会把末节点已 completed 的真实状态
    覆盖成 skipped，使 auto-finalize / 审核点检测卡在 noop:node_pending。
    内外层查询都排除，口径与 pipeline_status.is_dedup_leftover_event 一致。
    """
    out = {}
    _num_s = (issue_id_tag or "").strip().lstrip("#")
    if not _num_s:
        return out
    try:
        from src.api.pipeline_status import DEDUP_LEFTOVER_STATUSES
        _leftover = tuple(DEDUP_LEFTOVER_STATUSES)
        _leftover_ph = ",".join("?" for _ in _leftover)
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "SELECT agent_type, status, seq FROM events e WHERE repo_key=? "
                "AND issue_id IN (?, ?) "
                f"AND status NOT IN ({_leftover_ph}) "
                "AND seq = (SELECT MAX(seq) FROM events e2 "
                "WHERE e2.repo_key=e.repo_key AND e2.issue_id IN (?, ?) "
                "AND e2.agent_type=e.agent_type "
                f"AND e2.status NOT IN ({_leftover_ph}))",
                (repo_key, f"#{_num_s}", _num_s, *_leftover,
                 f"#{_num_s}", _num_s, *_leftover))
            for _ag, _st, _seq in c.fetchall():
                if _ag:
                    _st = _st or ""
                    _prev = out.get(_ag)
                    # 极端双形态并存时按 seq 最大者取
                    if _prev is None or _seq >= _prev[1]:
                        out[_ag] = (_st, _seq)
        return {k: v[0] for k, v in out.items()}
    except Exception as _e:
        log.warning(f"auto-finalize: latest event status query failed {repo_key}/{issue_id_tag}: {_e}")
        return out

def finalize_issue_to_pending_review(repo_key: str, issue_id: str, agent_type: str,
                                     source: str = "pipeline") -> str:
    """末节点完成后把需求推进到「待人工确认」（不再自动结单）。

    人工闸口径（需求确认只能由人做）：末节点全绿时只写
    ``status=pending_review`` + ``stage=pending_review``，不写 completed、不关 VCS Issue；
    VCS 关单与 completed 只发生在人工 confirm-done（routes/issues.py）。
    失败/QA 拦截分支维持原有「回退 pending_review」语义。

    触发条件（全部满足才推进）：
    - 完成的 agent == issue 当前 stage，且该节点为流水线末节点（next=='done' 或无下游）；
    - 该 issue 全部流水线节点最新事件均 completed（无 running/dispatched），且无 failed 阻断；
    - issue.status 非 pending_review（显式待人工确认场景不自动结单，保留 confirm done 人工路径）。

    迁移：status→pending_review、stage→pending_review（completed_reason/at 清空）。

    有 failed 阻断时同样回退 stage=pending_review（保留 Issue #51 人工确认语义）。

    返回："pending_review" / "pending_review_fallback" / "noop:<reason>"（幂等可重入）。
    """
    try:
        _num_s = (issue_id or "").strip().lstrip("#")
        # S75/D102-O：字符串 ident（gitee "IKTVZQ"）合法——只拒伪 issue_id（#pr_...）、
        # 空、0；数字与字符串 ident 原生保形（原 isdigit 门槛把 gitee 全族拒成 noop:not_issue，
        # 自动结单永远不触发）。
        if not _num_s or _num_s == "0" or _num_s.startswith("pr_"):
            return "noop:not_issue"          # PR 订阅伪 issue_id（#pr_...）等非真实 Issue
        _num = _native_issue_number(_num_s)
        _tag = f"#{_num_s}"

        with db_lock:
            c = _rt_db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                      (repo_key, _num))
            _row = c.fetchone()
        _status = (_row[0] if _row else "") or ""
        if not _row:
            return "noop:no_issue_row"
        if _status in ("completed", "cancelled"):
            # 补偿对齐：status=completed 但 stage 未到 done（历史 set_issue_stage 失败残留）
            # → 读 status 反向对齐 stage（幂等，失败仅记日志，不改变 noop 语义）
            if _status == "completed":
                try:
                    from src.api.session_store import get_issue_stage_name, set_issue_stage
                    if (get_issue_stage_name(repo_key, _tag) or "") != "done":
                        log.info(f"Issue #656: compensate stage=done for completed "
                                 f"{repo_key}/{_tag}")
                        set_issue_stage(repo_key, _tag, "done", "agent")
                except Exception as _se:
                    log.warning(f"auto-finalize compensate stage=done failed "
                                f"{repo_key}/{_tag}: {_se}")
            return "noop:terminal"
        if _status == "pending_review":
            return "noop:pending_review"      # 显式待人工确认（confirm done 路径），不自动结单

        if not _pipeline_last_node_check(repo_key, _num, agent_type):
            return "noop:not_last_node"

        # 「全部节点 completed + 无阻断」检测：每个流水线节点的最新事件
        from src.api.session_store import get_issue_scene
        from src.domains.mgmt.public import get_pipeline_config  # 跨域走 owner public
        _scene = get_issue_scene(repo_key, _tag)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _nodes = (get_pipeline_config(_scene_id, repo_key=repo_key) or {}).get("nodes") or []
        # 事件 agent_type 记的是角色（tag）；组织场景 id != tag，必须用角色对齐，
        # 否则会把 org_aggregate 误判成一直「未完成」。
        _node_ids = [(_n.get("tag") or _n.get("id") or "").strip() for _n in _nodes]
        _node_ids = [n for n in _node_ids if n]
        _latest = _issue_latest_event_status_by_agent(repo_key, _tag)
        for _nid in _node_ids:
            _st = _latest.get(_nid)
            if _st in ("running", "dispatched", "pending"):
                return f"noop:node_running:{_nid}"
            if _st == "failed":
                # 阻断 → 回退 pending_review，等待人工确认（Issue #51 语义保留给异常场景）
                # Issue #2419 修复项 2（#2419 移植）：回退即进入审核点，须推待审核通知
                # （复用 #1211 stage 包装器，review_points 指明失败节点供用户拍板）。
                from src.domains.runtime.issues_center import set_issue_stage_with_pending_review_notify
                set_issue_stage_with_pending_review_notify(
                    repo_key, _tag, "pending_review", agent_type,
                    review_points=f"流水线节点 {_nid} 执行失败，任务已回退至待审核（人工确认路径），"
                                  f"请确认后续处理方式")
                log.info(f"Issue #656: node {_nid} failed → pending_review "
                         f"(manual confirm path) {repo_key}/{_tag}")
                if _nid == "qa":
                    # Issue #1739：qa 失败保留失败原因与关联 PR，便于回流修复
                    # 走统一入口，以节点失败语义标注（与其余门禁点口径一致）
                    try:
                        from src.api.qa_gate import gate_or_annotate
                        gate_or_annotate(repo_key, _num,
                                         node_failure={"status": _st,
                                                       "reason": "qa 节点失败"})
                    except Exception as _qe:
                        log.warning(f"qa gate annotation failed {repo_key}/{_tag}: {_qe}")
                return "pending_review_fallback"
            if _st != "completed":
                return f"noop:node_pending:{_nid}"

        # Issue #1739 + #1963（arch 基线移植）：收尾前统一 QA 结论门禁；拦截后必须落
        # pending_review，否则卡片停在原 stage（既进不了待审核列，也无人被通知）。
        # 我方能力保留：命中失败仍返回 qa_failed_blocked[*] 语义（调用方依赖该返回值）。
        try:
            from src.api.qa_gate import gate_or_annotate
            _qa_info = gate_or_annotate(repo_key, _num)
            if _qa_info.get("blocked"):
                _stage_ok = True
                try:
                    from src.api.session_store import set_issue_stage
                    try:
                        set_issue_stage(repo_key, _tag, "pending_review", agent_type)
                    except Exception as _se:
                        log.warning(f"Issue #1963: qa blocked → pending_review failed "
                                    f"{repo_key}/{_tag}: {_se}, retry once")
                        try:
                            set_issue_stage(repo_key, _tag, "pending_review", agent_type)
                        except Exception as _se2:
                            _stage_ok = False
                            log.warning(f"Issue #1963: qa blocked → pending_review retry failed "
                                        f"{repo_key}/{_tag}: {_se2} (compensated on next event)")
                except Exception as _ie:
                    _stage_ok = False
                    log.warning(f"Issue #1963: pending_review staging unavailable: {_ie}")
                log.info(f"Issue #1739: qa failed → pending_review "
                         f"{repo_key}/{_tag} (seq={_qa_info.get('event_seq')}, stage_ok={_stage_ok})")
                return "qa_failed_blocked" if _stage_ok else "qa_failed_blocked_stage_error"
        except Exception as _qe:
            # 门禁自身异常 fail-open，不阻断正常收尾（与原关闭链路一致）
            log.warning(f"Issue #1739: qa gate check failed (fail-open) "
                        f"{repo_key}/{_tag}: {_qe}")

        # 条件齐备 → 推进到待人工确认（幂等：仅可变状态放行；不写 completed、不关 VCS）
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input')",
                (repo_key, _num))
            _rt_db.commit()
            _changed = c.rowcount > 0
        if not _changed:
            return "noop:not_mutable"
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, _tag, "pending_review", source or "pipeline")
        except Exception as _se:
            # UPDATE 与 set_issue_stage 非原子 → 立即补偿重试一次，仍失败仅记日志
            log.warning(f"finalize stage=pending_review failed {repo_key}/{_tag}: {_se}, retry once")
            try:
                set_issue_stage(repo_key, _tag, "pending_review", source or "pipeline")
            except Exception as _se2:
                log.warning(f"finalize stage=pending_review retry failed {repo_key}/{_tag}: {_se2} "
                            f"(will be compensated on next event)")
        _notify_done(repo_key, _num, "流水线全部节点已完成，需求进入待确认，请人工确认完成。")
        log.info(f"last-node finalize -> pending_review {repo_key}/{_tag} by {source} "
                 f"(agent={agent_type})")
        return "pending_review"
    except Exception as _e:
        log.warning(f"finalize to pending_review failed {repo_key}/{issue_id}: {_e}")
        return "noop:error"

def _review_pass(repo_key: str, issue_number: int, target: str = "processing",
                 evidence: str = "", pr_number: int = 0) -> str:
    """Agent 判定用户通过审核（幂等）。Issue #1211（#1211 移植）：passed_done 且关联
    PR open 时挂钩「📮 有 PR 待合入」通知（evidence 即 QA 结论摘要，pr_number 显式透传）。"""
    _out = _review_pass_inner(repo_key, issue_number, target, evidence)
    if _out == "passed_done":
        try:
            _fire_notify_coro(_pr_pending_merge_notify_task(
                repo_key, issue_number, evidence or "", int(pr_number or 0)))
        except Exception as _e:
            log.warning(f"Issue #1211: review-pass pr pending-merge notify schedule failed: {_e}")
    return _out


def _review_pass_inner(repo_key: str, issue_number: int, target: str = "processing",
                       evidence: str = "") -> str:
    """原 _review_pass 本体（#1211 拆分：外壳挂钩通知，本函数纯状态机）。

    - target=done：**拒绝**。需求完成只能由人工确认（confirm-done / admin-confirm-done），
      agent 没有任何终态通道；返回 ``noop:agent_finalize_forbidden``（路由转 400）。
    - target=processing：pending_review → processing（继续流水线）；
      末节点触达时无下游可续 → 归一为 pending_review（待人工确认），而不是 done。
    - 未命中 → `noop:already_target|terminal|not_pending_review|error`（Issue #1599：
      回读真实 status 后分类，调用方据此给出 changed=false + 真实 status + 原因码）。
    """
    try:
        if str(target or "").strip().lower() == "done":
            log.warning(f"Issue review-pass: agent tried to finalize "
                        f"{repo_key}/#{issue_number} -> rejected (human confirm only)")
            return "noop:agent_finalize_forbidden"
        # Issue #1739：末节点走到「待确认」前仍过一次 QA 结论门禁，但只用于标注/记录——
        # 人工闸下 AI 不再自动收尾，QA 失败不能把人挡在待确认列之外（否则卡片停在原
        # stage，既进不了待审核也无人被通知）。人工确认时自行判断 QA 结论。
        _qa_info: dict | None = None
        if _is_last_stage_node(repo_key, issue_number):
            try:
                from src.api.qa_gate import gate_or_annotate
                _qa_info = gate_or_annotate(repo_key, issue_number)
                if _qa_info.get("blocked"):
                    log.info(f"Issue #1739: qa failed → still route to pending_review "
                             f"(human decides) {repo_key}/#{issue_number}")
            except Exception as _qe:
                log.warning(f"Issue #1739: qa gate check failed (fail-open) "
                            f"{repo_key}/#{issue_number}: {_qe}")
                _qa_info = {}
        if _is_last_stage_node(repo_key, issue_number):
            # 末节点 + processing → 归一 pending_review（无下游可续，等人工确认；不再 done）
            with db_lock:
                c = _rt_db.cursor()
                c.execute(
                    "UPDATE issue SET status='pending_review', completed_reason=NULL, completed_at=NULL "
                    "WHERE repo_key=? AND issue_number=? "
                    "AND status IN ('active','processing','awaiting_input','pending_review')",
                    (repo_key, issue_number))
                _rt_db.commit()
                _changed = c.rowcount > 0
            if _changed:
                from src.api.session_store import set_issue_stage
                set_issue_stage(repo_key, f"#{issue_number}", "pending_review", "agent")
                log.info(f"Issue review-pass normalized last-node -> pending_review: "
                         f"{repo_key}/#{issue_number} evidence={evidence[:60]}")
                return "pending_review"
            return _review_noop_reason(repo_key, issue_number, "pending_review")
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                (repo_key, issue_number))
            _rt_db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue review-pass(processing): {repo_key}/#{issue_number} evidence={evidence[:60]}")
            return "passed_processing"
        return _review_noop_reason(repo_key, issue_number, "processing")
    except Exception as _e:
        log.warning(f"review_pass failed {repo_key}/#{issue_number}: {_e}")
        return "noop:error"

def _review_reject(repo_key: str, issue_number: int, target: str = "processing",
                   reason: str = "") -> str:
    """Agent 判定用户未通过审核（幂等）：`pending_review → processing|awaiting_input` 打回/转澄清。

    返回 `rejected_processing|rejected_awaiting_input`（命中）；未命中返回
    `noop:already_target|terminal|not_pending_review|error`（Issue #1599：回读真实 status 分类，
    不再以 `noop` 冒充 `status=pending_review`）。
    """
    try:
        _t = target if target in ("processing", "awaiting_input") else "processing"
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                f"UPDATE issue SET status=?, completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                (_t, repo_key, issue_number))
            _rt_db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue review-reject({_t}): {repo_key}/#{issue_number} reason={reason[:60]}")
            # Issue #2419 修复项 2：打回转澄清是审核点状态变化，须主动触达用户（❓ 待澄清卡片）。
            _questions = (f"审核未通过，Agent 需要你补充信息后继续：{(reason or '').strip()}").rstrip("：")
            maybe_notify_review_point_status(repo_key, issue_number, _t, _questions)
            return f"rejected_{_t}"
        return _review_noop_reason(repo_key, issue_number, _t)
    except Exception as _e:
        log.warning(f"review_reject failed {repo_key}/#{issue_number}: {_e}")
        return "noop:error"

def _get_issue_subtask_mode(repo_key: str, issue_number: int) -> int:
    """从 issue_state 读取 is_subtask_mode（任务创建时同步）。
    子任务（有 parent_issue_id）也算多任务模式。"""
    from src.api.session_store import get_issue_stage
    issue_id = f"#{issue_number}"
    state = get_issue_stage(repo_key, issue_id) or {}
    if state.get("is_subtask_mode"):
        return 1
    # 有 parent_issue_id 的子任务也算多任务模式
    parent = state.get("parent_issue_id", "") or ""
    if parent and str(parent).replace("#", "").strip():
        return 1
    return 0

def _get_issue_parent_id(repo_key: str, issue_number: int) -> str:
    """从 issue_state 读取 parent_issue_id（文本，如 "#20"；空=普通任务）。"""
    from src.api.session_store import get_issue_stage
    issue_id = f"#{issue_number}"
    state = get_issue_stage(repo_key, issue_id) or {}
    return (state.get("parent_issue_id") or "").strip()

def _get_issue_parent(repo_key: str, issue_number: int):
    """从 issue_state 读取 parent_issue_id 并归一（数字→int；IK* 字符串原样保留，
    与 parent_issue_id（TEXT）双列同源不分叉）。"""
    parent = _get_issue_parent_id(repo_key, issue_number)
    parent_num = str(parent).replace("#", "").strip()
    if not parent_num:
        return 0
    if parent_num.isdigit():
        return int(parent_num)
    # issue-ident（IK*）：保留原生形态（issue.parent_issue_number 列可存 TEXT）
    return parent_num

def _get_issue_title(repo_key: str, issue_number: int) -> str:
    """从 events 表取该 issue 最新事件的标题（零 API 调用）。
    
    跳过 title 为事件类型名（如 issue_opened/issue_updated）或 A2A 占位标题的行，
    这些不是 Issue 真实标题。回退到 issue_opened 事件的 payload 中提取标题。
    """
    _EVENT_TYPE_NAMES = {
        "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
        "issue_closed", "push", "tag_created", "release_created",
        "pull_request_opened", "pull_request_updated", "pull_request_merged",
        "note_hook_pr", "vod_signal", "a2a_event", "build_start", "build_failure",
        "deploy_start", "deploy_failure", "deploy_finish",
    }
    def _is_placeholder(t):
        return not t or t in _EVENT_TYPE_NAMES or t.startswith("A2A:") or t.startswith("A2A (订阅):")
    with db_lock:
        c = _rt_db.cursor()
        try:
            c.execute(
                "SELECT title FROM events WHERE repo_key=? AND issue_id=? AND title!='' "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, f"#{issue_number}")
            )
            row = c.fetchone()
            if row:
                t = row[0] or ""
                if not _is_placeholder(t):
                    return t
            # payload 兜底：从 issue_opened 事件的 payload 中提取真实标题
            c.execute(
                "SELECT payload FROM events WHERE repo_key=? AND issue_id=? AND event_type='issue_opened' "
                "ORDER BY seq ASC LIMIT 1",
                (repo_key, f"#{issue_number}")
            )
            prow = c.fetchone()
            if prow:
                import json as _json
                try:
                    _pl = _json.loads(prow[0] or "{}")
                except Exception:
                    _pl = {}
                _obj = _pl.get("object_attributes", {})
                _iss = _pl.get("issue", {})
                _pt = ""
                if isinstance(_obj, dict):
                    _pt = _obj.get("title", "") or ""
                if not _pt and isinstance(_iss, dict):
                    _pt = _iss.get("title", "") or ""
                if _pt and not _is_placeholder(_pt):
                    return _pt
            return ""
        except Exception:
            return ""

def _build_task_context_message(title: str, task_description: str, repo_key: str, issue_number: int) -> str:
    """构建写入会话的任务上下文消息（用户侧可见）"""
    lines = [f"📋 任务分配 · Issue #{issue_number}"]
    lines.append(f"仓库: {repo_key}")
    if title:
        lines.append(f"Issue 标题: {title}")
    lines.append("")
    lines.append(f"任务描述: {task_description}")
    lines.append("")
    lines.append("请在下方与我沟通处理进度，或提出疑问。")
    return "\n".join(lines)

def _vcs_issue_url(repo_key: str, issue_number: int) -> str:
    """repo_key → VCS Issue 公开页 URL（gitcode/github/gitlab 分流）。

    无编号 / 坏仓库（缺 owner 或 repo）→ ''。未知 provider 前缀 → 默认 GitCode。
    """
    from src.kernel.repo_key import split_owner_repo, issue_web_url
    if not issue_number:
        return ""
    owner, repo = split_owner_repo(repo_key)
    if not owner or not repo:
        return ""
    # D16/S26：host/路径统一从平台卡派生（新平台自动正确）
    return issue_web_url(str(repo_key), issue_number)

def _build_notify_actions(repo_key: str, issue_number: int, conv_id: str) -> list:
    """通知卡快捷按钮：进入任务会话（cmd 直达）+ 查看 Issue（VCS 公开页只读）。

    设计：docs/weekly-05/feishu-notification-card-quick-actions-design.md。
    """
    _iss = f"#{issue_number}" if issue_number else ""
    actions = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "💬 进入任务会话"},
        "value": {"cmd": "/task-open", "repo_key": repo_key,
                  "issue_number": _iss, "conv_id": conv_id or ""},
    }]
    _url = _vcs_issue_url(repo_key, issue_number)
    if _url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看 Issue ↗"},
            "url": _url,
        })
    return actions

_IM_NOTIFY_TEMPLATES = {
    "task": "blue",
    "awaiting": "orange",
    "pending_review": "red",
    "pr_pending_merge": "green",  # Issue #1211（#1211 移植）：PR 待合入绿卡
}

_IM_NOTIFY_TITLES = {
    "task": "📋 信息知会",
    "awaiting": "❓ 需求待澄清",
    "pending_review": "🔎 需求待审核",
    "pr_pending_merge": "📮 有 PR 待合入",  # Issue #1211（#1211 移植）
}

def _build_notify_fields(repo_key: str, issue_number: int) -> list:
    """通知卡结构化键值对字段：仓库 / Issue（带 VCS 链接）/ 需求标题（替代纯文本行，移动端对齐更好）。"""
    from src.kernel.repo_key import from_repo_key
    _iss = f"#{issue_number}" if issue_number else ""
    _iss_link = ""
    if _iss:
        _url = _vcs_issue_url(repo_key, issue_number)
        if _url:
            _iss_link = f"[{_iss}]({_url})"
    _title = _get_issue_title(repo_key, issue_number) or "—"
    return [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**📦 仓库**\n{from_repo_key(repo_key) or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**🎯 Issue**\n{_iss_link or '—'}"}},
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**📌 需求标题**\n{_title}"}},
    ]

def _notify_note() -> str:
    """通知卡底部时间戳（灰字注释）。"""
    return f"发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"

_IM_NOTIFY_COUNTERS: dict[str, int] = {}

def _im_notify_counter(key: str) -> None:
    """通知计数器（cluster-im-lease-stability-design.md §3.3）。"""
    _IM_NOTIFY_COUNTERS[key] = _IM_NOTIFY_COUNTERS.get(key, 0) + 1

def get_im_notify_counters() -> dict:
    return dict(_IM_NOTIFY_COUNTERS)

def _im_notify_begin(ntype: str, repo_key, issue_number, user_id, open_id, ctx):
    _im_notify_counter("attempted")
    _ctx_name = getattr(ctx, "name", "") if ctx else ""
    log.info(f"[im-notify] type={ntype} repo={repo_key} issue=#{issue_number} "
             f"user={user_id} open_id={open_id or ''} ctx={_ctx_name or 'none'}")

def _im_notify_skip(ntype: str, issue_number, user_id, ctx, open_id):
    if not ctx:
        _reason = "no_ctx"
    elif not open_id:
        _reason = "no_openid"
    else:
        _reason = "no_api"
    _im_notify_counter(f"skipped_{_reason}")
    log.warning(f"[im-notify] type={ntype} issue=#{issue_number} user={user_id} reason={_reason}")

async def _run_notify(ctx, api, user_id: str, conv_id: str, open_id: str,
                      task_description: str, repo_key: str, issue_number: int, cfg: dict):
    """任务分配通知：仅静态模板消息 + 飞书通知，不执行任何后台 LLM/ChatAgent。

    §十 原则：ChatAgent 只在用户真实回复消息时执行；任务分配/通知不得触发 opencode/stream，
    也不注入用户个人 token 用于 GitCode 写操作（见 agent-tasks-direct-create-binding-fix-design.md §10.4）。
    """
    try:
        # 静态模板 agent 消息（不生成 LLM 回复，提示用户去任务中心『去对话』）。
        # 任务上下文/描述已由 _ensure_task_conversation 预写入，此处仅追加指针消息，不重复内容。
        if conv_id:
            try:
                from src.kernel.repo_key import from_repo_key
                _tpl = (f"📋 任务已分配 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"请到任务中心『去对话』查看并处理。你的第一条回复将创建会话。")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Task notify: static template written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Task notify: static template write failed: {e}")

        # 飞书仅作通知（D3）
        _im_notify_begin("task", repo_key, issue_number, user_id, open_id, ctx)
        # A4（2026-11-10）：扇出到该用户全部已连接渠道（不再只认飞书本人 bot）
        from src.domains.runtime.im.notify import notify_user
        notify_text = (f"你有一条新任务待处理。\n\n{task_description}"
                       if (task_description or "").strip() else "你有一条新任务待处理。")
        _res = await notify_user(user_id, "task", notify_text, repo_key=repo_key,
                                 issue_number=issue_number, conv_id=conv_id)
        if _res.get("sent"):
            _im_notify_counter("sent_ok")
        else:
            _im_notify_counter("sent_failed")
            # Issue #2419 移植可观测口径：无任何渠道配置（no_ctx 等价形态）单独计数。
            # 任务分配族与 awaiting / pending_review 两族必须同口径：修复前三族都由
            # `_im_notify_skip` 计 skipped_no_ctx，只有这里并进 sent_failed 会让
            # 「用户没配渠道」在任务族指标里彻底消失（PR #2077 复审 Required）。
            if not (_res.get("failed") or _res.get("skipped")):
                _im_notify_counter("skipped_no_ctx")
            log.warning(f"Task notify: no channel delivered (sent={_res.get('sent')} "
                        f"failed={_res.get('failed')} skipped={_res.get('skipped')})")
    except Exception as e:
        log.warning(f"Task notify _run_notify error: {e}")

async def _run_awaiting_notify(ctx, api, user_id: str, conv_id: str, open_id: str,
                               questions: str, repo_key: str, issue_number: int, cfg: dict):
    """待澄清通知：仅静态模板消息 + 飞书卡片，不触发 ChatAgent / 不跑 LLM（与 _run_notify 同规约）。

    进入 awaiting_input 的语义就是「等用户输入」，必须主动触达用户；fd：本人 chat bot（open_id 为 app 级）。
    """
    try:
        from src.kernel.repo_key import from_repo_key
        if conv_id:
            try:
                _tpl = (f"❓ 需要你补充信息 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"{questions}")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Task awaiting: questions written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Task awaiting: template write failed: {e}")

        _im_notify_begin("awaiting", repo_key, issue_number, user_id, open_id, ctx)
        # Issue #3510：UUID 账号 id 已由 notify_user/outbox 归一，不得在此 int() 短路——
        # 否则 D105 后所有 UUID 用户都会退化成 skipped_no_ctx（#4158 事故形态）。
        # A4：全渠道扇出（微信/企微/Slack/企微等没有卡片就降级文本+回数字）
        from src.domains.runtime.im.notify import notify_user
        notify_text = (f"Agent 需要你补充以下信息：\n\n{questions}"
                       if (questions or "").strip() else "Agent 需要你补充信息。")
        _res = await notify_user(user_id, "awaiting", notify_text, repo_key=repo_key,
                                 issue_number=issue_number, conv_id=conv_id)
        if _res.get("sent"):
            _im_notify_counter("sent_ok")
        else:
            _im_notify_counter("sent_failed")
            # Issue #2419 移植可观测口径：无任何渠道配置（no_ctx 等价形态）单独计数，
            # 保留 A4 全渠道扇出主语义不变。
            if not (_res.get("failed") or _res.get("skipped")):
                _im_notify_counter("skipped_no_ctx")
            log.warning(f"Task awaiting: no channel delivered (sent={_res.get('sent')} "
                        f"failed={_res.get('failed')} skipped={_res.get('skipped')})")
    except Exception as e:
        log.warning(f"Task awaiting _run_awaiting_notify error: {e}")

async def _run_pending_review_notify(ctx, api, user_id: str, conv_id: str, open_id: str,
                                     review_points: str, references: str,
                                     repo_key: str, issue_number: int, cfg: dict):
    """待审核通知：仅静态模板消息 + 飞书卡片，不触发 ChatAgent / 不跑 LLM（与 _run_awaiting_notify 同规约）。

    进入 pending_review 的语义就是「交付审核点请用户拍板」，必须主动触达用户；本机本人 chat bot（open_id 为 app 级）。
    """
    try:
        from src.kernel.repo_key import from_repo_key
        _refs_suffix = (f"\n\n参考: {references}" if (references or "").strip() else "")
        if conv_id:
            try:
                _tpl = (f"🔎 需要你审核确认 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"{review_points}{_refs_suffix}")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Issue pending-review: review points written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Issue pending-review: template write failed: {e}")

        _im_notify_begin("pending_review", repo_key, issue_number, user_id, open_id, ctx)
        # Issue #3510：UUID 账号 id 已由 notify_user/outbox 归一，不得在此 int() 短路——
        # 否则 D105 后待审核通知对 UUID 用户永远 reason=skipped_no_ctx（#4158 事故形态）。
        # A4：全渠道扇出（待审核 = 必须主动触达，用户可能只开着某一个渠）
        from src.domains.runtime.im.notify import notify_user
        notify_text = (f"请审核确认以下交付审核点：\n\n{review_points}{_refs_suffix}"
                       if (review_points or "").strip() else "请审核确认交付结果。")
        _res = await notify_user(user_id, "pending_review", notify_text, repo_key=repo_key,
                                 issue_number=issue_number, conv_id=conv_id)
        if _res.get("sent"):
            _im_notify_counter("sent_ok")
        else:
            _im_notify_counter("sent_failed")
            # Issue #2419 移植可观测口径：无任何渠道配置（no_ctx 等价形态）单独计数。
            if not (_res.get("failed") or _res.get("skipped")):
                _im_notify_counter("skipped_no_ctx")
            log.warning(f"Issue pending-review: no channel delivered (sent={_res.get('sent')} "
                        f"failed={_res.get('failed')} skipped={_res.get('skipped')})")
    except Exception as e:
        log.warning(f"Issue pending-review _run_pending_review_notify error: {e}")


def _fire_notify_coro(coro) -> None:
    """通知协程 fire-and-forget 调度：有事件循环 → create_task（不阻塞端点）；
    无（worker 线程/同步上下文）→ run_coroutine_threadsafe 投递回主 loop 执行。

    直接 asyncio.run 会新建事件循环，而 IM 共享 aiohttp ClientSession 绑定主 loop
    → 跨 loop 抛 "Timeout context manager should be used inside a task"（token
    获取失败 → no token available → 卡片静默丢失）。与 chat.py
    _dispatch_push_to_main_loop 同款修复：投递回 BotManager 保存的主 loop。
    无主 loop（测试/启动异常）→ 兜底本线程 asyncio.run（同步可断言）。"""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        from src import api as _api_pkg
        bm = getattr(getattr(_api_pkg, "main", None), "bot_manager", None)
        loop = getattr(bm, "loop", None)
        if loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.add_done_callback(
                    lambda f: log.warning(f"[im-notify] background notify failed: {f.exception()}")
                    if not f.cancelled() and f.exception() else None)
                return
            except Exception as e:
                log.warning(f"[im-notify] notify dispatch to main loop failed: {e}")
        try:
            asyncio.run(coro)
        except Exception as e:
            log.warning(f"[im-notify] background notify run failed: {e}")


def _issue_status_by_tag(repo_key: str, issue_id: str) -> str:
    """按 issue_id（#N/N）读 issue.status（查询失败/无行 → ''）。"""
    try:
        _num = int(str(issue_id or "").lstrip("#"))
        if not _num:
            return ""
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                      (repo_key, _num))
            row = c.fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _schedule_pending_review_notify(repo_key: str, issue_number: int,
                                    review_points: str, references: str = ""):
    """缺口 2（Issue #1211）：解析 issue 归属用户通知目标 → 调度 _run_pending_review_notify。

    复用显式 pending-review 端点同款链路（_resolve_issue_notify_targets + fire-and-forget），
    best-effort：解析/调度失败仅日志。
    """
    try:
        _num = int(issue_number or 0)
        if not _num:
            return
        targets = _resolve_issue_notify_targets(repo_key, _num)
        _fire_notify_coro(_run_pending_review_notify(
            targets["ctx"], targets["api"], targets["user_id"], targets["conv_id"],
            targets["open_id"], review_points or "请审核确认交付结果", references or "",
            repo_key, _num, targets["cfg"]))
    except Exception as e:
        log.warning(f"[im-notify] schedule pending-review notify {repo_key}#{issue_number} failed: {e}")


def _schedule_awaiting_notify(repo_key: str, issue_number: int, questions: str):
    """Issue #2419：awaiting_input 状态变化通知调度（复用 #1211 目标解析 + fire-and-forget）。

    与 _schedule_pending_review_notify 同链路：_resolve_issue_notify_targets（本人 chat bot
    open_id / conv 兜底建 / cfg）→ _run_awaiting_notify（❓ 需求待澄清 orange 卡片 + 会话模板）。
    best-effort：解析/调度失败仅日志。
    """
    try:
        _num = int(issue_number or 0)
        if not _num:
            return
        targets = _resolve_issue_notify_targets(repo_key, _num)
        _fire_notify_coro(_run_awaiting_notify(
            targets["ctx"], targets["api"], targets["user_id"], targets["conv_id"],
            targets["open_id"], questions or "Agent 需要你补充信息。",
            repo_key, _num, targets["cfg"]))
    except Exception as e:
        log.warning(f"[im-notify] schedule awaiting notify {repo_key}#{issue_number} failed: {e}")


def maybe_notify_review_point_status(repo_key: str, issue_number: int, new_status: str,
                                     text: str = "") -> None:
    """Issue #2419 修复项 2：任务进入审核点状态（pending_review / awaiting_input）统一事件分发。

    状态事件 → 飞书卡片的扩覆盖入口：调用方在确认「状态真实变更」后调用（幂等由各入口
    UPDATE WHERE 排除当前状态保证，重复置入 0 行不触发——沿用 #1211 机制，防通知风暴）。
    - new_status == "pending_review" → 🔎 需求待审核（red，review_points=text）
    - new_status == "awaiting_input" → ❓ 需求待澄清（orange，questions=text）
    其他状态 no-op。best-effort：失败仅日志，不阻断调用方主流程。
    复用 #1211 卡片模板与 instance 路由（_resolve_issue_notify_targets → 本人 chat bot）。
    """
    try:
        _num = int(issue_number or 0)
        if not _num or new_status not in ("pending_review", "awaiting_input"):
            return
        if new_status == "pending_review":
            _schedule_pending_review_notify(repo_key, _num, text or "请审核确认交付结果")
        else:
            _schedule_awaiting_notify(repo_key, _num, text or "Agent 需要你补充信息。")
    except Exception as e:
        log.warning(f"[im-notify] review-point dispatch {repo_key}#{issue_number}->{new_status} failed: {e}")


def set_issue_stage_with_pending_review_notify(repo_key: str, issue_id: str, stage: str,
                                               owner_agent: str, review_points: str = "",
                                               references: str = "", **kwargs) -> None:
    """缺口 2（Issue #1211）：set_issue_stage 调用点包装——stage 流转到 pending_review
    且状态真实变更时，调度待审核通知（复用显式端点同款 _run_pending_review_notify）。

    其余语义与 session_store.set_issue_stage 完全一致（**kwargs 透传 a2a_seq/assignee）。
    设计决策（Spec）：不在 session_store 内统一 hook（避免反向依赖 im 层），由
    a2a/webhook（webhook.py）与 flow/reset（events.py）调用点显式换用本包装。

    「仅真实变更时」双重守卫：
    - stage 之前不是 pending_review（重复置入不重复打扰）；
    - issue.status 经 set_issue_stage 同步后为 pending_review（awaiting_input/completed 等
      被同步排除保持原状的路径不通知）。
    """
    from src.api.session_store import set_issue_stage, get_issue_stage_name
    try:
        _prev_stage = (get_issue_stage_name(repo_key, issue_id) or "")
    except Exception:
        _prev_stage = ""
    _status_before = _issue_status_by_tag(repo_key, issue_id)
    set_issue_stage(repo_key, issue_id, stage, owner_agent, **kwargs)
    try:
        if stage != "pending_review" or _prev_stage == "pending_review":
            return
        _status_after = _issue_status_by_tag(repo_key, issue_id)
        if _status_after == "pending_review" and _status_before != "pending_review":
            _num = int(str(issue_id or "").lstrip("#") or 0)
            if _num:
                log.info(f"[im-notify] stage→pending_review real change {repo_key}#{_num}, "
                         f"schedule review notify")
                _schedule_pending_review_notify(repo_key, _num, review_points, references)
    except Exception as e:
        log.warning(f"[im-notify] pending-review stage notify hook {repo_key}/{issue_id} failed: {e}")



# ===== PR 待合入通知（Issue #1211，#1211 移植自 weekly-1102-zrr）=====

# Issue #1211：(repo_key, pr_number) 待合入通知幂等去重集合（进程级，QA 复审/双路径不重复轰炸）。
_PR_PENDING_MERGE_NOTIFIED: set = set()
_PR_PENDING_MERGE_SET_CAP = 10000
# open PR 扫描分页参数与单 issue 通知上限（防大仓库误轰炸）
_PR_SCAN_PAGE_SIZE = 50
_PR_SCAN_MAX_PAGES = 4
_PR_NOTIFY_MAX_PER_ISSUE = 3


def _pr_pending_merge_dedup_mark(repo_key: str, pr_number: int) -> bool:
    """(repo_key, pr_number) 已通知 → True；首次 → 记账返回 False（进程级幂等）。"""
    key = (repo_key, int(pr_number))
    if key in _PR_PENDING_MERGE_NOTIFIED:
        return True
    if len(_PR_PENDING_MERGE_NOTIFIED) >= _PR_PENDING_MERGE_SET_CAP:
        _PR_PENDING_MERGE_NOTIFIED.clear()
        log.info("[im-notify] pr pending-merge dedup set capped, cleared")
    _PR_PENDING_MERGE_NOTIFIED.add(key)
    return False


def _pr_vcs_client(repo_key: str):
    """repo_key → VcsApiClient（bot token；解析失败/无 token → None，不抛出）。"""
    try:
        from src.kernel.repo_key import parse_repo_key
        provider, owner, repo = parse_repo_key(repo_key)
        if not owner or not repo:
            return None
        token = ""
        if provider == "github":
            from src.domains.mgmt.public import get_github_token as _get_gh
            token = _get_gh(repo_key=repo_key) or ""
        else:
            from src.domains.mgmt.public import get_repo_bot_token as _get_gc
            token = _get_gc(repo_key=repo_key) or ""
        if not token:
            log.info(f"[im-notify] pr client repo={repo_key} reason=no_bot_token")
            return None
        from src.domains.runtime.vcs.api_client import VcsApiClient
        return VcsApiClient(provider, token, owner, repo)
    except Exception as e:
        log.warning(f"[im-notify] pr client repo={repo_key} failed: {e}")
        return None


def _pr_ref_of(side) -> str:
    """head/base 兼容提取（dict.ref / dict.label / 裸字符串）。"""
    if isinstance(side, dict):
        return str(side.get("ref") or side.get("label") or "")
    return str(side or "")


def _pr_web_url_fallback(provider: str, owner: str, repo: str, pr_number: int) -> str:
    if not (owner and repo and pr_number):
        return ""
    if provider == "github":
        return f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    if provider == "gitlab":
        return f"https://gitlab.com/{owner}/{repo}/-/merge_requests/{pr_number}"
    return f"https://gitcode.com/{owner}/{repo}/pulls/{pr_number}"


def _pr_info_from(pr: dict, provider: str, owner: str, repo: str) -> dict:
    """VCS PR 原始 dict → 通知用 pr_info（number/title/url/head/base）。"""
    _n = pr.get("number") or pr.get("iid") or 0
    try:
        _n = int(_n)
    except Exception:
        _n = 0
    _url = str(pr.get("html_url") or "").strip()
    if not _url:
        _url = _pr_web_url_fallback(provider, owner, repo, _n)
    return {"number": _n, "title": str(pr.get("title") or ""), "url": _url,
            "head": _pr_ref_of(pr.get("head")), "base": _pr_ref_of(pr.get("base"))}


def _pr_related_issue_nums(pr: dict) -> set:
    """PR ↔ Issue 关联信号（容忍各平台/各端点形态）：

    - GitCode 原生：PR 详情 `issues` 数组（unlink 端点同款判据）/ `inner_issue_nums` 编码串；
    - 通用回退：body / title 中的 `#N` 引用（经 Issue #728 上下文过滤，代码块内不算）。
    """
    nums = set()
    try:
        issues = pr.get("issues")
        if isinstance(issues, list):
            for it in issues:
                if isinstance(it, dict):
                    try:
                        nums.add(int(it.get("number")))
                    except Exception:
                        pass
        inner = pr.get("inner_issue_nums")
        if isinstance(inner, list):
            inner = ",".join(str(x) for x in inner)
        if isinstance(inner, str) and inner.strip():
            import re as _re
            for m in _re.finditer(r"#(\d+)", inner):
                nums.add(int(m.group(1)))
        from src.domains.runtime.vcs.issue_ref_filter import extract_related_issue_nums
        for text in (pr.get("body") or "", pr.get("title") or ""):
            if text:
                nums.update(extract_related_issue_nums(str(text)) or [])
        nums.discard(0)
    except Exception:
        pass
    return nums


def _find_open_prs_for_issue(client, issue_number: int) -> list:
    """扫描仓库 open PR 列表，返回与 issue 关联的 PR（分页有界，best-effort）。"""
    matched = []
    try:
        for _page in range(1, _PR_SCAN_MAX_PAGES + 1):
            resp = client.list_prs(state="open", page=_page, per_page=_PR_SCAN_PAGE_SIZE)
            if isinstance(resp, dict):
                if resp.get("error"):
                    log.warning(f"[im-notify] list prs failed: {resp.get('error')}")
                break
            prs = [p for p in (resp or []) if isinstance(p, dict)]
            for pr in prs:
                if issue_number in _pr_related_issue_nums(pr):
                    matched.append(pr)
            if len(prs) < _PR_SCAN_PAGE_SIZE:
                break
    except Exception as e:
        log.warning(f"[im-notify] find open prs failed: {e}")
    return matched[:_PR_NOTIFY_MAX_PER_ISSUE]


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _pr_change_stats(client, pr_number: int) -> dict:
    """PR 变更统计（files/additions/deletions；失败返回零值不阻断）。"""
    stats = {"files": 0, "additions": 0, "deletions": 0}
    try:
        resp = client.get_pr_files(pr_number)
        if isinstance(resp, list):
            files = [f for f in resp if isinstance(f, dict)]
            stats["files"] = len(files)
            for f in files:
                stats["additions"] += _safe_int(f.get("additions"))
                stats["deletions"] += _safe_int(f.get("deletions"))
        elif isinstance(resp, dict) and resp.get("error"):
            log.warning(f"[im-notify] pr files #{pr_number} failed: {resp.get('error')}")
    except Exception as e:
        log.warning(f"[im-notify] pr files #{pr_number} error: {e}")
    return stats


def _build_pr_pending_merge_fields(repo_key: str, issue_number: int, pr_info: dict,
                                   qa_summary: str) -> list:
    """PR 待合入通知卡字段：基础三件套（仓库/Issue/标题）+ PR 链接/分支/增删行数/QA 结论。"""
    fields = _build_notify_fields(repo_key, issue_number)
    _n = pr_info.get("number") or 0
    _url = (pr_info.get("url") or "").strip()
    _pr_link = f"[#{_n}]({_url})" if (_n and _url) else (f"#{_n}" if _n else "—")
    _head = (pr_info.get("head") or "").strip() or "—"
    _base = (pr_info.get("base") or "").strip() or "—"
    fields.append({"is_short": True, "text": {"tag": "lark_md",
                   "content": f"**📮 PR**\n{_pr_link}"}})
    fields.append({"is_short": True, "text": {"tag": "lark_md",
                   "content": f"**🌿 分支**\n{_head} → {_base}"}})
    fields.append({"is_short": True, "text": {"tag": "lark_md",
                   "content": f"**📁 变更**\n{pr_info.get('files', 0)} 文件 "
                              f"+{pr_info.get('additions', 0)}/-{pr_info.get('deletions', 0)}"}})
    _qa = (qa_summary or "").strip()
    if _qa:
        fields.append({"is_short": False, "text": {"tag": "lark_md",
                       "content": f"**✅ QA 结论**\n{_qa}"}})
    return fields


def _build_pr_pending_merge_actions(repo_key: str, issue_number: int, conv_id: str,
                                     pr_url: str) -> list:
    """PR 待合入快捷按钮：基础两件套（进入会话/查看 Issue）+ 查看 PR。"""
    actions = _build_notify_actions(repo_key, issue_number, conv_id)
    if pr_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看 PR ↗"},
            "url": pr_url,
        })
    return actions


def _build_pr_pending_merge_conv_message(pr_info: dict, qa_summary: str,
                                         repo_key: str, issue_number: int) -> str:
    """会话兜底模板消息（无 chat bot / 发送失败时任务会话仍可见）。"""
    from src.kernel.repo_key import from_repo_key
    _n = pr_info.get("number") or 0
    _url = (pr_info.get("url") or "").strip()
    lines = [f"📮 PR #{_n} 待合入 · Issue #{issue_number}"]
    lines.append(f"仓库: {from_repo_key(repo_key) or '—'}")
    lines.append(f"分支: {(pr_info.get('head') or '—')} → {(pr_info.get('base') or '—')}")
    lines.append(f"变更: {pr_info.get('files', 0)} 文件"
                 f"（+{pr_info.get('additions', 0)} / -{pr_info.get('deletions', 0)}）")
    if _url:
        lines.append(f"PR 链接: {_url}")
    _qa = (qa_summary or "").strip()
    if _qa:
        lines.append("")
        lines.append(f"QA 结论：{_qa}")
    lines.append("")
    lines.append("QA 已通过，请到仓库完成人工合入。")
    return "\n".join(lines)


async def _run_pr_pending_merge_notify(ctx, api, user_id: int, conv_id: str, open_id: str,
                                       pr_info: dict, qa_summary: str,
                                       repo_key: str, issue_number: int, cfg: dict):
    """PR 待合入通知（Issue #1211）：QA 通过且关联 PR open 待人工合入时主动触达。

    与 _run_pending_review_notify 同规约：仅静态模板消息 + 飞书卡片，不触发 ChatAgent / 不跑 LLM。
    兜底：未配置 chat bot / 发送失败 → 任务会话仍写入「📮 PR #n 待合入」模板消息（add_message 路径）。
    """
    try:
        if conv_id:
            try:
                _tpl = _build_pr_pending_merge_conv_message(pr_info, qa_summary, repo_key, issue_number)
                add_message(conv_id, "agent", _tpl)
                log.info(f"Issue #1211: PR pending-merge template written to conv={conv_id}")
            except Exception as e:
                log.warning(f"Issue #1211: PR pending-merge template write failed: {e}")

        _im_notify_begin("pr_pending_merge", repo_key, issue_number, user_id, open_id, ctx)
        if open_id and api and ctx:
            _n = pr_info.get("number") or 0
            notify_text = f"QA 已通过，PR #{_n} 待人工合入。"
            _qa = (qa_summary or "").strip()
            if _qa:
                notify_text += f"\n\n{_qa}"
            try:
                await api.send_card_to_user(
                    ctx, open_id, notify_text, title=_IM_NOTIFY_TITLES["pr_pending_merge"],
                    template=_IM_NOTIFY_TEMPLATES["pr_pending_merge"],
                    actions=_build_pr_pending_merge_actions(
                        repo_key, issue_number, conv_id, (pr_info.get("url") or "").strip()),
                    fields=_build_pr_pending_merge_fields(repo_key, issue_number, pr_info, qa_summary),
                    note=_notify_note())
                _im_notify_counter("sent_ok")
            except Exception as e:
                _im_notify_counter("sent_failed")
                log.warning(f"Issue #1211: PR pending-merge feishu failed: {e}")
        else:
            _im_notify_skip("pr_pending_merge", issue_number, user_id, ctx, open_id)
    except Exception as e:
        log.warning(f"Issue #1211: _run_pr_pending_merge_notify error: {e}")


async def _pr_pending_merge_notify_task(repo_key: str, issue_number: int,
                                        qa_summary: str, pr_number_hint: int):
    """async 包装：VCS 发现挪出事件循环（端点路径不阻塞 loop）。"""
    await asyncio.to_thread(maybe_notify_prs_pending_merge, repo_key, issue_number,
                            qa_summary, pr_number_hint)


def maybe_notify_prs_pending_merge(repo_key: str, issue_number: int,
                                   qa_summary: str = "", pr_number_hint: int = 0) -> int:
    """Issue #1211 挂钩点：QA 通过/自动结单后检测关联 open PR → 调度「📮 有 PR 待合入」通知。

    - pr_number_hint>0（review-pass 端点显式透传）→ 直接取 PR 详情，state open 才通知；
      =0 → 扫描仓库 open PR 列表按关联匹配（GitCode issues 原生 / inner_issue_nums / body·title #N）。
    - (repo_key, pr_number) 进程级去重：同 PR 复审/双路径不重复（skipped_duplicate 计数）。
    - 无关联 open PR → skipped_no_open_pr；无 bot token → skipped_no_client。
    - 全链 best-effort：任一步失败仅日志，不影响调用方主流程。返回本次调度通知的 PR 数。
    """
    scheduled = 0
    try:
        from src.kernel.repo_key import to_repo_key, parse_repo_key
        repo_key = to_repo_key(repo_key)
        _num = int(issue_number or 0)
        if not _num:
            return 0
        client = _pr_vcs_client(repo_key)
        if client is None:
            _im_notify_counter("skipped_no_client")
            return 0
        provider, owner, repo = parse_repo_key(repo_key)
        candidates = []
        if pr_number_hint:
            try:
                detail = client.get_pr(int(pr_number_hint))
            except Exception as e:
                log.warning(f"[im-notify] get pr #{pr_number_hint} error: {e}")
                detail = {}
            if isinstance(detail, dict) and not detail.get("error") \
                    and str(detail.get("state") or "").lower() in ("open", "opened"):
                candidates = [detail]
            else:
                log.info(f"[im-notify] pr pending-merge hint #{pr_number_hint} not open, skip")
        else:
            candidates = _find_open_prs_for_issue(client, _num)
        if not candidates:
            _im_notify_counter("skipped_no_open_pr")
            return 0
        _qa = (qa_summary or "").strip()[:500]
        targets = _resolve_issue_notify_targets(repo_key, _num)
        for pr in candidates:
            pr_info = _pr_info_from(pr, provider, owner, repo)
            if not pr_info["number"]:
                continue
            if _pr_pending_merge_dedup_mark(repo_key, pr_info["number"]):
                _im_notify_counter("skipped_duplicate")
                log.info(f"[im-notify] pr pending-merge duplicate skip "
                         f"repo={repo_key} pr=#{pr_info['number']}")
                continue
            pr_info.update(_pr_change_stats(client, pr_info["number"]))
            _fire_notify_coro(_run_pr_pending_merge_notify(
                targets["ctx"], targets["api"], targets["user_id"], targets["conv_id"],
                targets["open_id"], pr_info, _qa, repo_key, _num, targets["cfg"]))
            scheduled += 1
        return scheduled
    except Exception as e:
        log.warning(f"[im-notify] maybe_notify_prs_pending_merge {repo_key}#{issue_number} error: {e}")
        return scheduled



def _resolve_issue_notify_targets(repo_key: str, issue_number: int) -> dict:
    """issue 归属用户的通知目标解析（复用 pending-review 显式端点同款链路）。

    issue 行（user_id/assignee/title/conversation_id）→ conv 兜底建/复用 →
    open_id（user_id 优先，assignee 兜底）→ 本人 chat bot ctx/api → cfg。
    任一步失败仅日志，字段保持默认空值（runner 走 skip 可观测路径）。
    """
    out = {"user_id": 0, "assignee": "", "title": "", "conv_id": "", "open_id": "",
           "ctx": None, "api": None, "cfg": {}}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT user_id, assignee, title, conversation_id FROM issue "
                      "WHERE repo_key=? AND issue_number=?", (repo_key, issue_number))
            row = c.fetchone()
        if not row:
            return out
        out["user_id"] = row[0] or 0
        out["assignee"] = row[1] or ""
        out["title"] = row[2] or ""
        out["conv_id"] = row[3] or ""
        _uid_ok = str(out["user_id"] or "").strip() not in ("", "0")  # arch11 D105: user_id 为 UUID 串
        if _uid_ok and not out["conv_id"]:
            conv_id = _ensure_task_conversation(out["user_id"], repo_key, issue_number,
                                                out["title"], "", write_context_on_reuse=False)
            if conv_id:
                out["conv_id"] = conv_id
                try:
                    with db_lock:
                        c = db.cursor()
                        c.execute("UPDATE issue SET conversation_id=? WHERE repo_key=? AND issue_number=?",
                                  (conv_id, repo_key, issue_number))
                        db.commit()
                except Exception:
                    pass
        out["open_id"] = (_get_feishu_open_id_by_user_id(out["user_id"])
                          if _uid_ok else "") or _get_feishu_open_id(out["assignee"], repo_key)
        try:
            from src.api import main as api_main
            bm = api_main.bot_manager
            if bm:
                out["ctx"] = _resolve_user_chat_bot(bm, out["user_id"])
                out["api"] = bm.api
        except Exception:
            pass
        if _uid_ok:
            try:
                out["cfg"] = get_chat_config(out["user_id"]) or {}
            except Exception:
                out["cfg"] = {}
    except Exception as e:
        log.warning(f"[im-notify] resolve targets {repo_key}#{issue_number} failed: {e}")
    return out


def _resolve_user_chat_bot(bm, user_id: str):
    """按用户 chat_agent_session.im_app_id 取其本人 chat bot（open_id 为 app 级，须用本人 bot 发送）。

    旧 IM 下线 A2 M2：凭证来源从 legacy BotManager 内存表改为 grape_t_channel_installation；
    入参 bm 保留仅为了旧调用点兼容，**不再使用**。
    用户未配置本人 bot → 返回 None（不通知，不回落他人 bot，避免命名空间不匹配静默失败）。
    """
    from src.domains.runtime.im.engine_wiring import get_active_installation
    inst = get_active_installation("user", owner_user_id=str(user_id), channel_type="feishu")
    if not inst:
        log.info(f"[im-notify] resolve user={user_id} reason=no_installation")
        return None
    cfg = inst.get("config") or {}
    app_id, secret = str(cfg.get("app_id", "")), str(cfg.get("app_secret", ""))
    if not app_id or not secret:
        log.warning(f"[im-notify] resolve user={user_id} reason=empty_credential")
        return None
    from src.domains.runtime.im.feishu_async import BotContext
    log.info(f"[im-notify] resolve user={user_id} bot={app_id} ok")
    return BotContext(app_id=app_id, app_secret=secret, bot_type="chat_bot")

# Issue #3621：stage=created 挂单态的 owner_agent 哨兵——非真实流水线节点，
# 避免 UI/生命周期消费方把 "created" 误当归属节点展示（stage 与 owner 不复用同名值）。
_STAGE_CREATED_OWNER = "pending_trigger"

class TaskCreateBody(BaseModel):
    repo_key: str = ""
    title: str = ""
    description: str = ""
    assignee: str = ""
    scene_id: str = ""
    is_subtask_mode: int = 0
    # 内部字段：组织下派子任务的组织上下文快照引用（不参与普通建单）
    org_context_snapshot_id: str = ""

    # Issue #3621：可选开关（默认 true 零破坏）。false = 只建单 + 同步平台 Issue，
    # 跳过流水线启动（stage=created 挂单，后续经 trigger_pending_issue_pipeline 首次触发入链）
    trigger_pipeline: bool = True

class TaskStatusBody(BaseModel):
    status: str = ""

_TASK_STATUS_FLOWS = {
    "active": ("processing", "awaiting_input", "pending_review", "cancelled"),
    "processing": ("awaiting_input", "pending_review", "completed", "cancelled", "active"),
    "awaiting_input": ("processing", "pending_review", "cancelled"),
    "pending_review": ("processing", "completed", "cancelled"),
    "completed": ("pending_review",),
    "cancelled": ("active",),
}

class TaskConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"

def _cleanup_issue_attachments(repo_key: str, issue_number):
    """删除指定 issue 的所有附件（含用户手工上传）。**破坏性**：整目录清理，
    仅供明确丢弃场景使用；issue 关闭走 `_cleanup_auto_transferred_attachments`。"""
    try:
        from src.kernel.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        _attach_cleanup_dir(_owner, _rname, str(issue_number))
    except Exception as _e:
        log.warning(f"Attachment cleanup failed: {_e}")

def confirm_issue_done(repo_key: str, issue_number: "int | str", confirmed_by: str = "issue_comment",
                       extra_statuses: tuple = (), skip_stage_if_no_change: bool = False):
    """Issue 评论确认完成 → 关闭该 issue 所有 active 任务 + stage=done

    ``extra_statuses``：在主路径状态（active/processing）之外额外允许收敛的状态，
    供终态载体单处置复用（awaiting_input/pending_review）。
    ``skip_stage_if_no_change``：无真实状态变更时不推进 stage，避免落成
    「status=awaiting_input + stage=done」的不一致（Issue #2816 审查）。
    """
    statuses = ("active", "processing") + tuple(extra_statuses or ())
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ("
            + ",".join("?" for _ in statuses) + ")",
            (confirmed_by, repo_key, issue_number, *statuses))
        affected = c.rowcount
        _rt_db.commit()
    if affected or not skip_stage_if_no_change:
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{issue_number}", "done", "user")
    log.info(f"Issue #{issue_number} confirmed done by {confirmed_by} ({affected} tasks closed)")
    if affected:
        _notify_done(repo_key, issue_number, f"需求已确认完成（{confirmed_by}）。")
    # Issue #1748 补充：关闭后回收平台自动转存的评论媒体/编辑器图片（保留用户手工附件）
    _cleanup_auto_transferred_attachments(repo_key, issue_number)
    return affected

def mark_issue_manual_status(repo_key: str, issue_number: "int | str",
                              reason: str = "needs_human_terminal") -> str:
    """把 issue 置「等用户」人工态（status+stage）并返回当前状态（域内状态迁移单源）。

    Issue #2816：终态载体单被短路后由 a2a_terminal_guard 调用；状态/阶段取自
    ``pipeline_constants``（``PAUSED_ISSUE_STATUSES`` / ``MANUAL_ISSUE_STAGE``），
    避免 api 模块内再硬编码。Spec 要求「folded + 载体 terminal 的单据 stage 置人工态」，
    故 stage 同步写为 ``MANUAL_ISSUE_STAGE``；``patrol.issue_terminal`` 依据 status
    判定终态，不会因 stage 变更产生额外影响。
    可标记起始状态含 ``pending_review``（终态载体命中的在途审核态也要落人工态）；
    已处于暂停态时 UPDATE 不命中，但返回实际状态，供响应如实呈现。
    """
    from src.api.pipeline_constants import (
        MANUAL_ISSUE_STAGE, MARKABLE_PAUSE_STATUSES, PAUSED_ISSUE_STATUSES)
    from src.domains.runtime.vcs.api_client import _native_issue_number
    _paused = PAUSED_ISSUE_STATUSES[0]
    num = _native_issue_number(str(issue_number).lstrip("#"))
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET status=?, completed_reason=?, completed_at=NULL "
            "WHERE repo_key=? AND issue_number=? AND status IN ("
            + ",".join("?" for _ in MARKABLE_PAUSE_STATUSES) + ")",
            (_paused, reason, repo_key, num, *MARKABLE_PAUSE_STATUSES))
        _rt_db.commit()
        c.execute("SELECT status, stage, owner_agent FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (repo_key, num))
        row = c.fetchone()
    status = (row[0] or "") if row else ""
    if status == _paused:
        _owner = (row[2] or "") if row else ""
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{num}", MANUAL_ISSUE_STAGE, _owner)
        except Exception as _e:
            log.warning(f"mark_issue_manual_status: set stage failed "
                        f"{repo_key}/#{num}: {_e}")
        log.info(f"Issue #{issue_number} → {_paused}/{MANUAL_ISSUE_STAGE} ({reason})")
    return status


def resume_issue_from_manual(repo_key: str, issue_number: "int | str",
                             stage: str = "coding", owner_agent: str = "coding") -> bool:
    """把人工态/暂停态恢复为 processing 并推进 stage（幂等）。

    Issue #2816：``terminal_disposition.new_carrier`` / ``reassign`` 处置后调用。
    api 层不再直写 ``UPDATE issue``，状态集合单源于
    ``pipeline_constants.RESUMABLE_ISSUE_STATUSES``；状态迁移与
    ``_resume_issue_waiting`` 共用 ``_resume_issue_rows`` 单源。

    Issue #2816 复审（硬违规）：``UPDATE`` 未命中（``completed`` / ``cancelled`` 等
    已收敛单据、或并发改写）时**不得**再写 stage——否则 ``set_issue_stage`` 会把
    任务状态同步回 ``processing``，落成「status 终态 + stage=coding」并复活派发。
    """
    from src.api.pipeline_constants import RESUMABLE_ISSUE_STATUSES
    from src.domains.runtime.vcs.api_client import _native_issue_number
    num = _native_issue_number(str(issue_number).lstrip("#"))
    try:
        # 状态翻转与 stage 落点在同一事务内完成；已是 processing 的单（MySQL
        # rowcount=0）由 _resume_issue_rows 回读 status 视为幂等恢复。
        changed, _cur_stage, _ = _resume_issue_rows(
            repo_key, num, tuple(RESUMABLE_ISSUE_STATUSES),
            new_stage=stage, new_owner=owner_agent)
    except Exception as e:
        log.warning(f"resume_issue_from_manual: status update failed {repo_key}/#{num}: {e}")
        return False
    if not changed:
        log.info(f"Issue #{issue_number} not resumable (terminal or changed concurrently), "
                 f"stage untouched")
        return False
    try:
        # stage 已随状态事务提交；此处仅同步 task.status（幂等）。
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{num}", stage, owner_agent)
    except Exception as e:
        log.warning(f"resume_issue_from_manual: sync task status failed: {e}")
    log.info(f"Issue #{issue_number} resumed from manual → processing/{stage}")
    return True


def get_issue_lifecycle_status(repo_key: str, issue_number: "int | str") -> str:
    """读 Issue 当前 ``status``（只读；无记录/异常返回空串）。

    Issue #2816 复审：人工处置入口需在恢复前判状态上限——``completed`` /
    ``cancelled`` 等已收敛单据不得再被 ``new_carrier`` / ``reassign`` 拉回。
    """
    from src.domains.runtime.vcs.api_client import _native_issue_number
    num = _native_issue_number(str(issue_number).lstrip("#"))
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                      (repo_key, num))
            row = c.fetchone()
        return ((row[0] if row else "") or "").strip()
    except Exception as e:
        log.warning(f"get_issue_lifecycle_status failed {repo_key}/#{num}: {e}")
        return ""


def cancel_issue_task(repo_key: str, issue_number: int):
    """Issue 评论取消任务（Issue #1741：状态集合补齐 awaiting_input/pending_review，避免静默失效）"""
    with db_lock:
        c = _rt_db.cursor()
        affected = cancel_issue_rows(c, repo_key, issue_number)
        _rt_db.commit()
    log.info(f"Issue #{issue_number} cancelled ({affected} tasks)")
    # Issue #1748 补充：取消后同样回收自动转存媒体（保留用户手工附件）
    _cleanup_auto_transferred_attachments(repo_key, issue_number)
    return affected

def resume_issue_task(repo_key: str, issue_number: int):
    """Issue 评论恢复任务"""
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET status='active', completed_reason=NULL, completed_at=NULL "
            "WHERE repo_key=? AND issue_number=? AND status='cancelled'",
            (repo_key, issue_number))
        affected = c.rowcount
        _rt_db.commit()
    log.info(f"Issue #{issue_number} resumed ({affected} tasks)")
    return affected

class ProductionReleaseError(Exception):
    """发布校验链拒绝（detail 携带 code/message）。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

_PRODUCTION_RELEASE_APPROVAL_TTL = int(
    os.environ.get("PRODUCTION_RELEASE_APPROVAL_TTL") or 24 * 3600)

_SHA40_RE = None  # lazy import re

def _is_full_sha(candidate_sha: str) -> bool:
    """候选提交须为完整 40 位十六进制 SHA（短 SHA 不足以钉住候选版本）。"""
    global _SHA40_RE
    if _SHA40_RE is None:
        import re
        _SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
    return bool(_SHA40_RE.match(candidate_sha or ""))

def _latest_sre_acceptance(repo_key: str, issue_number: int) -> dict | None:
    """取该需求最近一条已完成 sre 事件（验收证据锚点）。

    返回 {seq, payload}；无已完成 sre 事件返回 None（未验收）。
    """
    import json as _json
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "SELECT seq, payload FROM events WHERE repo_key=? AND issue_id=? "
                "AND agent_type='sre' AND status='completed' ORDER BY seq DESC LIMIT 1",
                (repo_key, f"#{issue_number}"))
            row = c.fetchone()
        if not row:
            return None
        try:
            payload = _json.loads(row[1] or "{}")
        except Exception:
            payload = {}
        return {"seq": row[0], "payload": payload if isinstance(payload, dict) else {}}
    except Exception as _e:
        log.warning(f"_latest_sre_acceptance failed: {_e}")
        return None

def is_release_subissue(repo_key: str, issue_number) -> bool:
    """判断 issue 是否为发布子单（release_target_env 非空即发布子单，普通 issue 恒 False）。

    PR #818 审查修复（Optional-3 回声竞态兜底）：
    - 兼容 "#N" 形态的 issue_id（webhook 回声 resolve_issue_id 返回带 # 前缀）；
    - 绑定字段落库前的竞态窗口内，标题前缀 `发布子单：` 作为兜底识别
      （该前缀为本入口专属命名，普通 issue 不会命中）。
    """
    try:
        _num = str(issue_number or "").lstrip("#").strip()
        # issue-ident（IK*）：双格式兼容——数字串→int，IK* 原样保留（勿 int() 强转）。
        from src.domains.runtime.vcs.api_client import _native_issue_number
        _num = _native_issue_number(_num)
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "SELECT 1 FROM issue WHERE repo_key=? AND issue_number=? "
                "AND (release_target_env != '' OR title LIKE '发布子单：%') LIMIT 1",
                (repo_key, _num))
            return c.fetchone() is not None
    except Exception:
        return False

def _release_subissue_title(issue_number: int, target_env: str, candidate_sha: str) -> str:
    """发布子单标题：`发布子单：需求 #{N} → {env} @ {sha12}`（平台 Issue 标题，可辨识）。"""
    _sha_short = (candidate_sha or "")[:12]
    _env = (target_env or "").strip()
    _title = f"发布子单：需求 #{issue_number} → {_env} @ {_sha_short}"
    return _title[:200]

def _release_subissue_body(repo_key: str, issue_number: int, candidate_sha: str,
                           target_env: str, evidence_ref: str,
                           approved_by: "str | int", approved_at: str) -> str:
    """发布子单 body：`## 父任务`（现有子任务识别机制）+ `## 发布绑定`（绑定要素落平台）。"""
    from src.kernel.repo_key import from_repo_key
    _repository = from_repo_key(repo_key) or repo_key.split(":", 1)[-1]
    _lines = [
        f"## 父任务",
        f"- 关联父任务：{_repository}#{issue_number}",
        "",
        "## 发布绑定",
        f"- 源需求：{issue_number}",
        f"- 候选提交：{candidate_sha}",
        f"- 目标环境：{target_env}",
        f"- 验收证据：{evidence_ref or '（见源需求 SRE 验收事件）'}",
        f"- 批准来源：user_id={approved_by or 0}（{approved_at or ''}）",
    ]
    return "\n".join(_lines)

def _sync_release_fields(repo_key: str, issue_number: "int | str", parent_issue_number: "int | str",
                         candidate_sha: str, target_env: str, evidence_ref: str,
                         approved_by: "str | int", approved_at: str) -> None:
    """将父任务关联（parent_issue_id/parent_issue_number/is_subtask_mode）+ 发布绑定要素
    写回发布子单 issue 行（创建时仅此一次；拒绝路径零写入）。"""
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET parent_issue_id=?, parent_issue_number=?, is_subtask_mode=1, "
            "release_candidate_sha=?, release_target_env=?, release_evidence_ref=?, "
            "release_approved_by=?, release_approved_at=?, updated_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=?",
            (f"#{_native_issue_number(parent_issue_number)}", _native_issue_number(parent_issue_number),
             candidate_sha, target_env, evidence_ref or "", approved_by or 0,
             approved_at or "", repo_key, _native_issue_number(issue_number)))
        _rt_db.commit()

def create_release_subissue(repo_key: str, issue_number: "int | str", candidate_sha: str,
                            target_env: str, evidence_ref: str = "",
                            approved_by: "str | int" = "", approved_at: str = "",
                            api_token: str = "", dispatch: bool = True) -> dict:
    """为 done/completed 终态需求创建「发布子单」（方案 C：复用现有子任务机制）。

    校验链（按序，任一失败抛 ProductionReleaseError，**拒绝路径零写入**——
    不建平台 Issue、不动 issue 行、不增删 events，原验收证据天然保留）：
    1. 终态前置：源需求 issue.status 必须为 completed（非终态走正常流水线，拒绝）；
    2. SRE 验收前置：无已完成 sre 事件 → 拒绝（未验收不能发布）；
    3. 候选一致性：candidate_sha 须为完整 40 位 SHA 且与 sre 验收 payload 中的
       candidate_sha（如有记录）一致；
    4. 目标环境：非空且 ≤64 字符；
    5. 批准时效：approved_at 距今 ≤ TTL（默认 24h）；
    6. 唯一性：同（源需求+候选SHA+目标环境）已存在发布子单（任意终态）→ 409 duplicate。

    成功路径：平台创建真实子任务 Issue（标题/body 含 `## 父任务` 链接，复用现有子任务
    识别机制）→ issue 行建档（is_subtask_mode=1 + parent_issue_id=源需求）+ release_*
    绑定字段 → stage=sre（直通 sre，不经 vod/issue/coding/qa）→ dispatch=True 时服务内
    直调 a2a_endpoint 派发 sre 执行部署。子单终态 release_result 即发布结果三分支。
    """
    from src.kernel.repo_key import to_repo_key, split_owner_repo, parse_repo_key
    repo_key = to_repo_key(repo_key)
    # PR #799 R1 语义保留：落库前统一小写——唯一绑定与验收记录对照口径一致，
    # 大小写变体不得绕过唯一性重复创建发布子单
    candidate_sha = (candidate_sha or "").strip().lower()
    target_env = (target_env or "").strip()
    evidence_ref = (evidence_ref or "").strip()

    # 1) 终态前置
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                  (repo_key, _native_issue_number(issue_number)))
        _row = c.fetchone()
    if not _row:
        raise ProductionReleaseError("issue_not_found", "需求不存在", status=404)
    if (_row[0] or "") != "completed":
        raise ProductionReleaseError(
            "not_terminal",
            f"需求当前状态为 {_row[0] or '(空)'}，非 completed 终态：生产续接仅适用于已完成需求"
            f"（进行中需求请走正常流水线，不使用发布入口）")

    # 2) SRE 验收前置
    _sre_ev = _latest_sre_acceptance(repo_key, issue_number)
    if not _sre_ev:
        raise ProductionReleaseError(
            "no_sre_acceptance",
            "该需求无已完成的 SRE 验收事件：缺少验收证据，拒绝创建发布子单")

    # 3) 候选一致性（格式 + 与验收记录对照）
    if not _is_full_sha(candidate_sha):
        raise ProductionReleaseError(
            "invalid_candidate_sha",
            "candidate_sha 必须为完整 40 位十六进制提交 SHA（短 SHA 不足以钉住候选版本）")
    _accepted_sha = str(_sre_ev["payload"].get("candidate_sha") or "").strip()
    if _accepted_sha and _accepted_sha.lower() != candidate_sha.lower():
        raise ProductionReleaseError(
            "candidate_mismatch",
            f"候选提交与 SRE 验收记录不一致：验收记录 candidate_sha={_accepted_sha}，"
            f"请求 candidate_sha={candidate_sha}（候选变更拒绝发布）")

    # 4) 目标环境
    if not target_env or len(target_env) > 64:
        raise ProductionReleaseError(
            "invalid_target", "target_env 必填且不超过 64 字符（指定生产环境标识）")

    # 5) 批准时效
    from datetime import datetime, timezone as _tz
    _approved_at = (approved_at or "").strip()
    _parsed = None
    if _approved_at:
        try:
            _parsed = datetime.fromisoformat(_approved_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            _parsed = None
    if not _parsed:
        _parsed = datetime.now(_tz.utc)
        _approved_at = _parsed.isoformat()
    if _parsed.tzinfo is None:
        _parsed = _parsed.replace(tzinfo=_tz.utc)
    _age = (datetime.now(_tz.utc) - _parsed).total_seconds()
    if _age > _PRODUCTION_RELEASE_APPROVAL_TTL or _age < -_PRODUCTION_RELEASE_APPROVAL_TTL:
        raise ProductionReleaseError(
            "approval_expired",
            f"批准已过期：approved_at={_approved_at} 距今 {_age / 3600:.1f}h，"
            f"超过有效期 {_PRODUCTION_RELEASE_APPROVAL_TTL / 3600:.0f}h")

    # 6) 唯一性（显式查询前置；拒绝路径发生在平台建单之前——零外部写入）
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT id, issue_number, release_result FROM issue "
            "WHERE repo_key=? AND parent_issue_number=? AND release_target_env != '' "
            "AND release_candidate_sha=? AND release_target_env=? ORDER BY id DESC LIMIT 1",
            (repo_key, _native_issue_number(issue_number), candidate_sha, target_env))
        _dup = c.fetchone()
    if _dup:
        raise ProductionReleaseError(
            "duplicate",
            f"该绑定已存在发布子单（sub_issue_number={_dup[1]}，result={_dup[2] or '-'}）："
            f"同（需求+候选SHA+目标环境）禁止重复提交", status=409)

    # 平台建单：真实子任务 Issue（复用现有子任务机制——body 含 `## 父任务` 链接）
    _owner, _rname = split_owner_repo(repo_key)
    _provider, _, _ = parse_repo_key(repo_key)
    _title = _release_subissue_title(issue_number, target_env, candidate_sha)
    _body_txt = _release_subissue_body(repo_key, issue_number, candidate_sha,
                                       target_env, evidence_ref, approved_by, _approved_at)
    _sub_num = 0
    try:
        # D21/S32：统一命令层建 Issue（存量平台内部委托 _post_*，gitee 走卡路径）
        from src.domains.runtime.vcs.api_client import VcsApiClient
        _client = VcsApiClient(_provider, api_token or "", _owner, _rname)
        _resp = _client.create_issue(_title, _body_txt, assignee="")
        if isinstance(_resp, dict):
            # issue-ident（IK*）：gitee 等平台返回字符串编号，int() 强转必炸——
            # 统一走 _native_issue_number（模块级已导入；数字串→int，IK* 原样保留）。
            _sub_num = _native_issue_number(_resp.get("number") or _resp.get("iid") or 0)
    except Exception as _e:
        raise ProductionReleaseError(
            "release_create_failed",
            f"创建发布子单 Issue 失败：{str(_e)[:200]}", status=502)
    if not _sub_num:
        raise ProductionReleaseError(
            "release_create_failed",
            "创建发布子单 Issue 失败（平台响应无 issue number）", status=502)
    # Issue #453 代发台账：建单入账（webhook 回声对账）
    try:
        from src.kernel.outbound_ledger import record_outbound, content_hash_md5
        record_outbound(_provider, repo_key, "issue", str(_sub_num),
                        issue_number=str(_sub_num),
                        content_hash=content_hash_md5(_body_txt))
    except Exception as _le:
        log.warning(f"outbound_ledger record (release subissue) failed: {_le}")

    # issue 行建档（复用现有子任务机制：is_subtask_mode=1 + parent_issue_id=源需求）
    try:
        # 建档时即声明子任务模式（ensure_issue_task 写入 is_subtask_mode）
        _task_id = ensure_issue_task(
            repo_key, _sub_num, title=_title, description=_body_txt,
            assignee="", event_type="production_release",
            subtask_mode=1, owner_user_id=str(approved_by or ""))
    except Exception as _e:
        log.warning(f"release subissue ensure_issue_task failed: {_e}")
        raise ProductionReleaseError(
            "release_archive_failed", f"发布子单建档失败：{str(_e)[:200]}", status=500)
    if not _task_id:
        raise ProductionReleaseError("release_archive_failed", "发布子单建档失败", status=500)
    # 父任务关联 + 绑定要素落库（parent_issue_id/number + is_subtask_mode + release_* 一次写入）
    # PR #818 审查修复（R2 部分失败显式上报）：绑定/置阶段失败不再静默——
    # 子单缺 release 绑定会导致 webhook 回声进 vod 二次派发、回填钩子不识别、
    # 唯一性查询不计数；失败即抛 release_archive_failed（与建档失败路径一致）。
    # 唯一索引冲突（并发双提交绕过前置查询的兜底命中）映射 409 duplicate。
    try:
        _sync_release_fields(repo_key, _sub_num, _native_issue_number(issue_number), candidate_sha, target_env,
                             evidence_ref, approved_by, _approved_at)
    except Exception as _e:
        _err_txt = str(_e)
        if "unique" in _err_txt.lower() or "1062" in _err_txt:
            raise ProductionReleaseError(
                "duplicate",
                f"该绑定已存在发布子单（并发提交命中唯一约束，sub_issue_number={_sub_num}）",
                status=409)
        raise ProductionReleaseError(
            "release_archive_failed",
            f"发布子单绑定要素落库失败（sub_issue_number={_sub_num}，平台 Issue 已创建，"
            f"请人工核对）：{str(_e)[:200]}", status=500)
    # 直通 sre：设 stage=sre（跨过 vod/issue/coding/qa）
    try:
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{_sub_num}", "sre", "sre")
    except Exception as _e:
        raise ProductionReleaseError(
            "release_archive_failed",
            f"发布子单置 stage=sre 失败（sub_issue_number={_sub_num}，绑定要素已落库，"
            f"请人工核对阶段路由）：{str(_e)[:200]}", status=500)
    log.info(f"release subissue created: sub=#{_sub_num} parent=#{_native_issue_number(issue_number)} "
             f"repo={repo_key} sha={candidate_sha[:12]} env={target_env} approved_by={approved_by}")

    out = {"ok": True, "sub_issue_number": _sub_num, "issue_id": _task_id,
           "repo_key": repo_key, "parent_issue_number": _native_issue_number(issue_number),
           "candidate_sha": candidate_sha, "target_env": target_env,
           "evidence_ref": evidence_ref or "", "status": "sre", "seq": 0}

    if not dispatch:
        return out

    # 标准 A2A 派发 sre（服务内直调 a2a_endpoint，与 handoff_issue 同模式：
    # jwt 分支内部再校验 managed_repos，双保险；绑定上下文随 context/events.payload 持久化）
    _seq = _dispatch_release_sre(
        repo_key, _native_issue_number(issue_number), _sub_num, candidate_sha,
        target_env, evidence_ref or "", approved_by=approved_by or 0)
    if _seq:
        out["seq"] = _seq
    else:
        out["dispatch_error"] = "sre 派发未成功（事件未创建或被拦截），发布子单保留 stage=sre，可人工处理"
        log.warning(f"release subissue #{_sub_num}: sre dispatch returned no seq")
    return out

def _dispatch_release_sre(repo_key: str, issue_number: int, sub_issue_number: int,
                          candidate_sha: str, target_env: str, evidence_ref: str,
                          approved_by: "str | int" = "") -> int:
    """为发布子单派发携带发布上下文的 sre a2a_event，返回事件 seq（失败返回 0）。

    复用标准 /api/a2a 链路（鉴权、场景节点校验、去重、事件落库、引擎选择全保留）。
    事件挂子单 issue 时间线（issue_id=#{sub_issue_number}），payload 携带
    parent_issue/is_subtask_mode/context（不再需要旁路 production_release 锚点——
    回填按 task.issue_id 反查 issue 行即可）。
    """
    import json as _json
    from src.kernel.repo_key import from_repo_key
    from src.domains.runtime.routes.webhook import a2a_endpoint, A2AHandoffBody
    _repository = from_repo_key(repo_key) or repo_key.split(":", 1)[-1]
    _account = _repository.split("/", 1)[0] if "/" in _repository else ""
    _context = (f"发布子单 #{sub_issue_number}：源需求 #{issue_number} 已完成 SRE 验收并经用户批准"
                f"发布到指定环境。候选提交 {candidate_sha}，目标环境 {target_env}，"
                f"验收证据 {evidence_ref or '（见源需求 SRE 验收事件）'}。"
                f"请按生产发布流程执行部署，并在完成后回填实际部署结果"
                f"（部署成功/失败/未执行三分支）。")
    _a2a_body = A2AHandoffBody(agent="sre", payload={
        "repository": _repository,
        "account": _account,
        "issue_number": sub_issue_number,
        "pr_number": "",
        "context": _context,
        "is_subtask_mode": True,
        "parent_issue": f"#{_native_issue_number(issue_number)}",
    })
    _user = {"id": str(approved_by or ""), "username": "", "auth_type": "jwt"}
    # a2a_endpoint 为 async：创建端点为同步 def（线程池执行，无运行中 loop），直接
    # asyncio.run；若未来出现 async 调用方，用独立线程跑新 loop 兜底，避免嵌套 loop 报错。
    _res = None
    try:
        import asyncio as _aio2
        try:
            _loop = _aio2.get_running_loop()
        except RuntimeError:
            _loop = None
        if _loop and _loop.is_running():
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _res = _ex.submit(lambda: _aio2.run(a2a_endpoint(None, _a2a_body, _user))).result(
                    timeout=60)
        else:
            _res = _aio2.run(a2a_endpoint(None, _a2a_body, _user))
    except Exception as _e:
        log.warning(f"_dispatch_release_sre failed: {_e}")
        return 0
    if isinstance(_res, dict):
        _seq = int(_res.get("seq") or 0)
        if _seq > 0:
            return _seq
    _body_txt = ""
    try:
        _raw = getattr(_res, "body", b"") or b""
        if isinstance(_raw, memoryview):
            _raw = _raw.tobytes()
        _parsed = _json.loads(bytes(_raw).decode("utf-8"))
        _body_txt = str(_parsed)
    except Exception:
        pass
    log.warning(f"_dispatch_release_sre: a2a returned no seq (res={_body_txt[:200] or _res})")
    return 0

def _has_deploy_url(result_text: str) -> bool:
    """从 sre 结果文本提取部署证据（与 _extract_deploy_url 同口径：artifacts/issue_summary 含 deploy_url）。"""
    import re as _re
    _txt = (result_text or "").strip()
    if not _txt:
        return False
    try:
        import json as _json
        _parsed = _json.loads(_txt)
        if isinstance(_parsed, dict):
            for _key in ("artifacts", "issue_summary"):
                _val = _parsed.get(_key)
                _cands = [str(x) for x in _val] if isinstance(_val, list) else (
                    [_val] if isinstance(_val, str) else [])
                if any(_re.search(r"deploy_url:\s*https?://\S+", _c) for _c in _cands):
                    return True
    except (ValueError, TypeError):
        pass
    return bool(_re.search(r"deploy_url:\s*https?://\S+", _txt))

def _backfill_release_subissue_result(repo_key: str, issue_number: int,
                                      exit_code: int, result_text: str = "") -> bool:
    """发布子单终态回填（结果三分支，幂等）。

    - exit_code != 0 → deploy_failed（release_failure_reason 取 result_text 摘要）；
    - exit_code == 0 且结构化产出携带 deploy_url 证据 → deployed_success；
    - exit_code == 0 但无部署证据 → not_executed（阶段 completed 不视为发布成功）。
    已回填（release_result 非空）不再覆盖；非发布子单（release_target_env 空）零影响。
    """
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "SELECT release_target_env, release_result FROM issue "
                "WHERE repo_key=? AND issue_number=?",
                (repo_key, _native_issue_number(issue_number)))
            row = c.fetchone()
        if not row or not (row[0] or ""):
            return False   # 非发布子单
        if row[1]:
            return True    # 幂等：已回填
    except Exception as _e:
        log.warning(f"_backfill_release_subissue_result lookup failed: {_e}")
        return False
    if exit_code != 0:
        _result = "deploy_failed"
        _reason = ((result_text or "").strip() or "sre 任务失败（exit_code != 0）")[:500]
    elif _has_deploy_url(result_text or ""):
        _result = "deployed_success"
        _reason = ""
    else:
        _result = "not_executed"
        _reason = ""
    # B1 SQL 双模式：'release_' || ? 的 || 拼接不被 mysql_compat 翻译层覆盖（仅
    # 'gitcode:' || col 一种模式），MySQL 后端会把 || 当逻辑或 → 写入 0。
    # 改为应用层拼接，双后端语义一致。
    _completed_reason = f"release_{_result}"
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "UPDATE issue SET release_result=?, release_failure_reason=?, "
                "status='completed', completed_reason=?, "
                "stage='done', owner_agent='sre', completed_at=datetime('now'), "
                "updated_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND (release_result IS NULL OR release_result='')",
                (_result, _reason, _completed_reason, repo_key, _native_issue_number(issue_number)))
            _changed = c.rowcount > 0
            _rt_db.commit()
        if _changed:
            log.info(f"release subissue #{issue_number} backfilled: result={_result} "
                     f"repo={repo_key} exit={exit_code}")
        return _changed
    except Exception as _e:
        log.warning(f"_backfill_release_subissue_result write failed: {_e}")
        return False

# ===== Issue #3349：建单查重与上游超时策略 =====
# 背景：建单端点 `POST /api/issues/{owner}/{repo}` 是 async 路由，但查重曾逐页翻页
# （state=open&per_page=50，上限 1000 页）。open issue 越多查重越慢，单个建单请求在
# await 点之间独占事件循环，并发建单互相排队 → 超过网关 upstream timeout → 502。修法：
#   ① 查重改查平台库（本地 `issue` 台账，索引定位，成本与 open issue 数无关）；
#   ② 台账不可判定时才降级 VCS 探针，且**固定页数上限 + 显式单页超时**；
#   ③ 所有 VCS 出站一律 `asyncio.to_thread`，不允许在 async 路由内同步阻塞。
# Issue #3349 数值来源（B2 五源闸门，2026-11-24 架构守护条件 1 整改）：
# 本组常量的**可判定约束**（本项目已有记录）：
#   - CREATE_UPSTREAM_TIMEOUT 必须显著小于网关 upstream timeout：nginx
#     `proxy_read_timeout 300s`（deploy/nginx-grape.conf:156/176/211），25s 留 12 倍余量；
#   - 层级序 DEDUP(8) < HTTP(15) < UPSTREAM(25)：探针/单请求超时逐级放大，
#     保证查重探针先于建单 socket 超时、socket 超时先于请求级放弃，超时分层可判别；
#   - RECONCILE(5) + grace(5) < HTTP(15)：对账探针整体必须在建单 socket 超时内完成。
# 具体取值（8/15/25/5）的**上游延迟实测（建单/查重 P95/P99）尚未进行**，
# 已按「不成立则登记待决策」口径登记：docs/issue-3349/task.md「待决策数值」节，
# 实测后以 P99×3 为初值复核本组取值。
DEDUP_UPSTREAM_TIMEOUT = 8          # 查重探针单页超时（秒）；来源：层级序推导，待上游 P99 实测复核
DEDUP_FALLBACK_MAX_PAGES = 3       # 台账不可用时的降级扫描页数上限（成本有界：3×50=150 条）
DEDUP_FALLBACK_PAGE_SIZE = 50      # 与存量 GitCode 查重页大小一致（既有行为，spec §3）
CREATE_HTTP_TIMEOUT = 15           # 建单 POST socket 超时（秒）；来源：层级序推导，待上游 P99 实测复核
CREATE_UPSTREAM_TIMEOUT = 25       # 建单请求级放弃阈值（秒）；上界约束=网关 300s（nginx conf）
CREATE_RECONCILE_TIMEOUT = 5       # 建单超时后的对账探针超时（秒）；来源：层级序推导
RECONCILE_ABANDON_GRACE = 5         # 对账探针请求级放弃的额外宽限（秒）：wait_for 上限 = 探针超时 + 本宽限，
#                                   # 使 socket 超时先于放弃阈值触发、超时分支按探针失败而非放弃判别


def _dedup_repo_key(owner: str, repo: str, provider: str = "gitcode") -> str:
    """(owner, repo, provider) → 统一 repo_key（`provider:owner/repo`）。"""
    from src.kernel.repo_key import to_repo_key
    return to_repo_key(f"{owner}/{repo}", provider or "gitcode")


_OPEN_ISSUE_LEDGER_SQL = (
    "SELECT issue_number FROM issue "
    "WHERE repo_key=? AND {title_match} AND vcs_exists=1 "
    "  AND NOT (status='completed' AND completed_reason='issue_closed') "
    "ORDER BY id DESC LIMIT 1"
)


def _find_open_issue_by_title_ledger(owner: str, repo: str, title: str,
                                     provider: str = "gitcode") -> tuple[bool, int | None]:
    """平台库（本地 `issue` 台账）幂等查重 —— Issue #3349 主路径。

    台账行由两条链路写入：建单成功后的 `ensure_issue_task`、以及 webhook 回流建档，
    因此「同仓同名 open Issue」在平台侧有完整记录，无需向 VCS 翻页。

    查询分两步：① `repo_key + title` 精确命中（`idx_issue_repo_title` 索引定位，
    成本与仓内 issue 数无关）；② 未命中再退回同仓 `TRIM(title)` 比对（兼容历史行
    标题两端带空白，仓库级索引仍然生效）。

    Returns:
        (authoritative, issue_number)：
        - ``(True, n)``   台账命中同名 open Issue → 直接复用该单号；
        - ``(True, None)``该仓库已有台账，确认无同名 open Issue（台账权威，无需打 VCS）；
        - ``(False, None)``该仓库无台账行或查询异常 → 调用方降级 VCS 探针。
    """
    repo_key = _dedup_repo_key(owner, repo, provider)
    norm_title = (title or "").strip()
    if not repo_key or not norm_title:
        return False, None
    try:
        with db_lock:
            c = db.cursor()
            # 「open」口径：VCS 侧未确认删除，且不是「已关闭」终态
            # （issue_closed 回流会把 status 置 completed + completed_reason='issue_closed'，
            #  关闭后同名重开仍应放行，见 Issue #343/#3349 口径一致性）。
            row = c.execute(
                _OPEN_ISSUE_LEDGER_SQL.format(title_match="title=?"),
                (repo_key, norm_title)).fetchone()
            if not row:
                row = c.execute(
                    _OPEN_ISSUE_LEDGER_SQL.format(title_match="TRIM(title)=?"),
                    (repo_key, norm_title)).fetchone()
            if row and row[0]:
                return True, int(row[0])
            has_any = c.execute("SELECT 1 FROM issue WHERE repo_key=? LIMIT 1",
                                (repo_key,)).fetchone()
        return (bool(has_any), None)
    except Exception as e:
        log.warning(f"dedup ledger query failed (degrade to VCS probe): repo={repo_key}: {e}")
        return False, None


def _find_open_issue_by_title_vcs(api_token: str, owner: str, repo: str, title: str,
                                  provider: str = "gitcode",
                                  max_pages: int = DEDUP_FALLBACK_MAX_PAGES,
                                  timeout: int = DEDUP_UPSTREAM_TIMEOUT) -> int | None:
    """VCS 有界查重探针（平台库不可用时的降级路径，Issue #3349）。

    与存量全量翻页的差别：**不再翻到末页**——页数上限固定，单页显式超时，成本与
    open issue 总数解耦。策略本体仍在 VcsApiClient.find_open_issue_by_title
    （D36/S56 端口-适配器，GitHub 走 Search API 单请求精确匹配，其余平台按卡
    方言分页/状态取值），这里只承载 #3349 的有界参数。

    Raises:
        RuntimeError: 查重失败（凭据失效/网络不可达/上游异常）——保持 Issue #343
            「查重失败不放行」语义，避免静默重复建单。
    """
    from src.domains.runtime.vcs.api_client import create_client
    _rk = f"{provider}:{owner}/{repo}"   # 恒带前缀：client 按 provider 选卡/策略
    _client = create_client(_rk, api_token)
    return _client.find_open_issue_by_title(title, max_pages=max(1, int(max_pages)),
                                            timeout=timeout)


def _find_open_issue_by_title(api_token: str, owner: str, repo: str, title: str,
                             provider: str = "gitcode",
                             use_ledger: bool = True,
                             max_pages: int = DEDUP_FALLBACK_MAX_PAGES,
                             timeout: int = DEDUP_UPSTREAM_TIMEOUT) -> int | None:
    """幂等查找：同仓库已存在同名 open Issue → 返回其 number（避免重复建 issue）。

    D36/S56：策略下沉 VcsApiClient.find_open_issue_by_title（端口-适配器形态，
    调用方零平台分支）——github Search API vs 分页遍历、分页终止、
    {data:[]}/list 响应形态、token 失效抛 RuntimeError（#343/#344 契约）
    全部在适配器内部实现并由 S55/S56 用例锁死。
    Issue #3349：主路径改查平台库（本地 `issue` 台账，`(repo_key,title)` 索引
    定位），台账无法判定时才降级到**有界** VCS 探针（固定页数 + 显式单页超时，
    经适配器端口参数承载），使查重成本与 open issue 数解耦；调用方须用
    `asyncio.to_thread` 卸载，禁止在 async 路由内直接同步调用。

    返回 issue number（找到）或 None（真没找到）。token 失效时抛 RuntimeError。
    """
    if use_ledger:
        authoritative, num = _find_open_issue_by_title_ledger(owner, repo, title, provider=provider)
        if num is not None:
            return num
        if authoritative:
            return None
    return _find_open_issue_by_title_vcs(api_token, owner, repo, title,
                                          provider=provider, max_pages=max_pages,
                                          timeout=timeout)


def _gitcode_assignee_member_check(api_token: str, owner: str, repo: str, username: str) -> str:
    """Issue #649: 校验 assignee 是否为 GitCode 仓库成员（GET /collaborators/{u}/permission）。

    返回三态：
    - "member"     确认是成员（有协作者权限记录，可被指派）
    - "not_member" 确认非成员（上游 404）
    - "unknown"    无法判定（403/429/网络等）——放行交给上游 create 最终裁决，
                   避免「查询接口本身无权限」被误判成非成员而阻断合法请求。
    """
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _uerr
    if not username or not api_token:
        return "unknown"
    _url = (f"https://gitcode.com/api/v5/repos/{owner}/{_up.quote(repo, safe='')}"
            f"/collaborators/{_up.quote(username, safe='')}/permission?access_token={api_token}")
    _req = _ur.Request(_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with _ur.urlopen(_req, timeout=15) as _resp:
            _raw = _resp.read().decode(errors="replace")
        import json as _json
        _data = _json.loads(_raw) if _raw.strip() else {}
        # Gitee 系返回 {access_level, permission, role_name...}；access_level<=10 视为无实质权限
        _lvl = int(_data.get("access_level") or 0)
        return "member" if _lvl >= 20 else "not_member"
    except _uerr.HTTPError as e:
        if e.code == 404:
            return "not_member"
        log.warning(f"assignee member check undetermined (HTTP {e.code}): {username} @ {owner}/{repo}")
        return "unknown"
    except Exception as e:
        log.warning(f"assignee member check failed: {e}")
        return "unknown"

def _post_gitcode_issue(api_token: str, owner: str, repo: str, title: str,
                        body_text: str = "", assignee: str = "") -> dict:
    """创建 GitCode Issue（v5 实测：必须 access_token query + form-encoded，勿用 Bearer+JSON body）。

    Gitee 系 API 特点：POST /repos/{owner}/{repo}/issues?access_token=xxx
    body 为 application/x-www-form-urlencoded 的 title/body/assignee；
    JSON body 会返回 400 "body不能为空"（2026-08-21 实测）。
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _uerr
    params = {"title": title}
    if body_text:
        params["body"] = body_text
    if assignee:
        params["assignee"] = assignee
    url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues?access_token={api_token}"
    req = _ur.Request(url, data=_up.urlencode(params).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return _json.loads(raw) if raw.strip() else {}
    except _uerr.HTTPError as e:
        raise RuntimeError(f"GitCode API POST issues failed ({e.code}): {e.read().decode(errors='replace')[:300]}")
    except _uerr.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")

def _post_github_issue(api_token: str, owner: str, repo: str, title: str,
                       body_text: str = "", assignee: str = "") -> dict:
    """创建 GitHub Issue（POST /repos/{owner}/{repo}/issues，JSON body + Bearer token）。"""
    from src.domains.mgmt.public import _github_req
    body = {"title": title}
    if body_text:
        body["body"] = body_text
    if assignee:
        body["assignees"] = [assignee]
    return _github_req("POST", f"https://api.github.com/repos/{owner}/{repo}/issues", api_token, body)

def _patch_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（追加多任务模式标记）。GitCode 版。"""
    import urllib.request as _ur, urllib.error as _uerr, json as _json
    url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue_num}?access_token={api_token}"
    data = _json.dumps({"body": body_text}).encode()
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="PATCH")
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            _out = _json.loads(resp.read().decode()) if resp.read else {}
    except Exception:
        raise
    # Issue #453 台账（S21，第六轮补漏）：多任务模式 patch issue body → issue_edit 记账，
    # 编辑回声对账（原先 marker 检测在 GitCode 必 miss）
    try:
        from src.kernel.outbound_ledger import record_outbound, content_hash_md5
        from src.kernel.repo_key import to_repo_key
        record_outbound("gitcode", to_repo_key(f"{owner}/{repo}", "gitcode"), "issue_edit",
                        str(issue_num), issue_number=str(issue_num),
                        content_hash=content_hash_md5(body_text))
    except Exception as _le:
        log.warning(f"outbound_ledger record (_patch_issue_body) failed: {_le}")
    return _out

def _patch_github_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（GitHub 版）。"""
    from src.domains.mgmt.public import _github_req
    _out = _github_req("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}",
                       api_token, {"body": body_text})
    # Issue #453 台账（S21，第六轮补漏）：GitHub 多任务模式 patch issue body → issue_edit 记账
    try:
        from src.kernel.outbound_ledger import record_outbound, content_hash_md5
        from src.kernel.repo_key import to_repo_key
        record_outbound("github", to_repo_key(f"{owner}/{repo}", "github"), "issue_edit",
                        str(issue_num), issue_number=str(issue_num),
                        content_hash=content_hash_md5(body_text))
    except Exception as _le:
        log.warning(f"outbound_ledger record (_patch_github_issue_body) failed: {_le}")
    return _out

def create_standard_task(repo_key: str, title: str, description: str,
                         bot_token: str, assignee: str = "", labels=None) -> dict:
    """真实 GitCode Issue 创建 + 标准任务建档（onboarding 模块调用，与手动创建任务同一原子路径）。

    - 幂等：同仓库同标题 open Issue 已存在 → 不重复创建（复用 _find_open_issue_by_title）
    - 建档：ensure_issue_task（幂等，天然单条任务上下文消息）
    - token：bot_token 注入（onboarding 无登录用户上下文，用仓库 bot token）
    - 不额外写入任何消息：任务上下文消息只由 ensure_issue_task → _ensure_task_conversation 产生 1 条
    """
    from src.kernel.repo_key import to_repo_key, split_owner_repo, parse_repo_key
    repo_key = to_repo_key(repo_key)
    owner, rname = split_owner_repo(repo_key)
    _provider, _, _ = parse_repo_key(repo_key)
    if not title or not title.strip():
        return {"ok": False, "code": 400, "message": "任务标题必填"}
    if not bot_token:
        return {"ok": False, "code": 400, "message": "bot token 缺失"}
    if not assignee:
        assignee = owner or ""
    # Issue #344: 查重失败时返回明确错误，不抛异常中断 onboarding 流程
    try:
        issue_num = _find_open_issue_by_title(bot_token, owner, rname, title.strip(),
                                              provider=_provider)
    except RuntimeError as e:
        return {"ok": False, "code": 502, "message": f"查重失败，Bot Token 可能失效: {str(e)[:200]}"}
    created = False
    if issue_num is not None:
        # 已有同标题 open Issue → 直接建档（幂等：不再重复创建 Issue）
        # S70：gitee 字符串 ident 保持原生形态（int() 强转必炸）。
        from src.domains.runtime.vcs.api_client import _native_issue_number
        issue_num = _native_issue_number(issue_num)
    else:
        # D21/S32：统一命令层建 Issue（存量平台内部委托 _post_*，gitee 走卡路径）
        from src.domains.runtime.vcs.api_client import VcsApiClient
        _client = VcsApiClient(_provider, bot_token, owner, rname)
        resp = _client.create_issue(title.strip(), (description or "").strip(), assignee=assignee)
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            return {"ok": False, "code": 502,
                    "message": f"创建 Issue 失败（响应无 issue number）: {str(resp)[:200]}"}
        # S70：gitee 字符串 ident 保持原生形态。
        from src.domains.runtime.vcs.api_client import _native_issue_number as _nin
        issue_num = _nin(n)
        # Issue #453 代发台账（写点6/7）：建任务主通道入账
        try:
            from src.kernel.outbound_ledger import record_outbound, content_hash_md5
            record_outbound(_provider, repo_key, "issue", str(issue_num), issue_number=str(issue_num),
                            content_hash=content_hash_md5((description or "").strip()))
        except Exception as _le:
            log.warning(f"outbound_ledger record (create_standard_task) failed: {_le}")
        created = True
        # Issue #3250：写回查重索引
        try:
            from src.domains.runtime.vcs.api_client import remember_open_issue_title
            remember_open_issue_title(_provider, owner, rname, title.strip(), issue_num)
        except Exception:
            pass
    task_id = ensure_issue_task(repo_key, issue_num, title.strip(), description or "",
                                assignee=assignee, event_type="onboarding")
    if not task_id:
        return {"ok": False, "code": 500, "message": "任务建档失败"}
    # task_id 语义 = 需求卡片 issue.id（issues_center P2：主字段 issue_id，兼容保留 task_id）
    return {"ok": True, "issue_id": task_id, "task_id": task_id,
            "issue_number": issue_num, "created": created,
            "message": "任务已创建（Issue 已同步）" if created else "任务已创建（复用已有 Issue）"}

def _refresh_user_gitcode_token(user: dict) -> str:
    """用户自己的 GitCode token 401 时主动刷新一次（带锁，防并发）。

    返回刷新后的新 token；无 refresh_token / 刷新失败返回空串（不抛异常，
    由调用方决定最后提示「重新绑定/登录 GitCode」）。
    """
    try:
        from src.api.deps import try_refresh_gitcode_token
        return try_refresh_gitcode_token(str(user.get("id") or "")) or ""
    except Exception as _e:
        log.warning(f"create_task: refresh user gitcode token failed: {_e}")
        return ""

def _knowledge_warm_budget() -> float:
    """Issue #2132 B1 建单预热时延预算（秒），env KNOWLEDGE_WARM_BUDGET_SEC 可调。

    非法/非正数回退默认 0.8s；预热是纯缓存优化，不能占满「建单 < 2s」的验收预算。
    """
    try:
        _v = float(os.environ.get("KNOWLEDGE_WARM_BUDGET_SEC", "0.8"))
    except (TypeError, ValueError):
        return 0.8
    return _v if _v > 0 else 0.8


# 后台预热任务强引用（asyncio 只弱引用任务，不留引用会被 GC 掉）
_knowledge_warm_tasks: set = set()


async def _issue_opened_pipeline_trigger(repo_key: str, owner: str, rname: str, issue_num,
                                          title: str, creator_login: str, is_github: bool) -> dict:
    """Issue #3621：issue_opened 同步触发共用路径（建单直发与挂单后续首次触发复用同一实现）。

    方案 D（duplicate-issue-opened-event-fix-design.md）：按平台发原生 payload，
    使补发事件与对应平台真实 webhook 形态一致（可读性/排查 + 去重指纹一致），
    不再对 GitCode 也发 GitHub 格式合成 payload 造成「GitHub 事件」误判。
    Issue #343/#649: pipeline 创建人 = 创建者 external_login（creator_login），
    与 assignee 解耦——不传 assignee 时创建人字段仍正确，不再回填 assignee。
    Issue #453：内部同步触发标记（跳过台账判定）——payload 为内部构造，非 webhook
    回流；台账检查会命中并拦掉触发，_grape_internal_trigger 让读侧跳过台账判定
    （同步触发是我们自己要开工，与 webhook 回流去重语义相反）。
    扫雷⑩ S3：调用方多为 async 处理器，run_pipeline 同步全量执行会阻塞事件循环
    ——to_thread 卸载到线程池，语义保持同步等待完成。
    返回 run_pipeline 结果 dict（含 seq）。
    """
    import json as _pipe_json
    _assign = creator_login or ""
    if is_github:
        _pipe_payload = {
            "action": "opened",
            # D102-O 同族收口：issue number 原生保形（gitee 等平台为字符串
            # ident，int() 强转会炸同步触发——实弹 IKTVZQ）；流水线进程内
            # ident 链已字符串化（D102-G），此处直传原生形态
            "issue": {"number": issue_num, "title": title,
                      "user": {"login": _assign} if _assign else {}},
            "repository": {"full_name": f"{owner}/{rname}"},
            "sender": {"login": _assign} if _assign else {},
        }
        _pipe_headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
    else:
        _pipe_payload = {
            "object_kind": "issue",
            "object_attributes": {"iid": issue_num, "title": title,
                                  "action": "open"},
            "project": {"path_with_namespace": f"{owner}/{rname}"},
            "user": {"username": _assign} if _assign else {},
        }
        _pipe_headers = {"Content-Type": "application/json", "X-Gitcode-Event": "issue_opened"}
    _pipe_payload["_grape_internal_trigger"] = True
    from src.domains.runtime.pipeline.event_pipeline import run_pipeline
    return await asyncio.to_thread(
        run_pipeline, _pipe_json.dumps(_pipe_payload).encode(), _pipe_headers,
        account=repo_key, repo_key_override=repo_key)


def _pending_trigger_event_count(repo_key: str, issue_num) -> int:
    """挂单触发相关事件的计数（runtime 域连接；用于失败/成功后的落地校验）。

    Issue #3621 复审：仅统计触发类事件（Issue/PR 实体事件），排除仓库订阅等
    旁路事件——否则订阅事件会误判 409「已有流水线事件」。
    """
    from src.domains.runtime.vcs.event_types import ALL_ISSUE_EVENTS, ALL_PR_EVENTS
    _types = tuple(set(ALL_ISSUE_EVENTS) | set(ALL_PR_EVENTS))
    _ph = ",".join("?" * len(_types))
    with db_lock:
        c = _rt_db.cursor()
        c.execute(f"SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id=? "
                  f"AND event_type IN ({_ph})",
                  (repo_key, f"#{issue_num}", *_types))
        return int(c.fetchone()[0] or 0)


def _rollback_pending_issue_trigger(repo_key: str, issue_num) -> bool:
    """触发失败时回滚已认领的 created→vod（仅当仍为 vod 且未落事件）；返回是否回滚。

    与认领同在 ``db_lock`` + runtime 域连接上完成，回滚后该单可再次触发（不卡死）。
    Issue #3621 复审：run_pipeline 内 set_issue_stage 可能已把 status 同步为 processing，
    无事件回滚时须一并恢复 status=active；但 cancelled/completed 为终态，不复活。
    """
    with db_lock:
        c = _rt_db.cursor()
        # 复审：仅触发类事件落地才视为入链成功不回滚（旁路/订阅事件不阻断回滚）
        if _pending_trigger_event_count(repo_key, issue_num):
            return False
        c.execute(
            "UPDATE issue SET stage='created', owner_agent=?, "
            "status=CASE WHEN status IN ('cancelled','completed') THEN status ELSE 'active' END, "
            "updated_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND stage='vod'",
            (_STAGE_CREATED_OWNER, repo_key, str(issue_num)))
        _rolled = c.rowcount
        _rt_db.commit()
    return bool(_rolled)


def _claim_pending_issue_created(repo_key: str, issue_num, issue_id: str):
    """同步临界区：Issue stage/status 校验 + 事件计数 + CAS 认领 created→vod。

    Issue #3621：逻辑与原来同锁同事务，唯一变化是**不在 async 函数里直接持 db_lock**
    （同步阻塞锁会卡住事件循环，async-blocking 棘轮 A6/A7）；调用方用 asyncio.to_thread。
    返回 (issue_row, claimed_rowcount)。
    """
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT stage, title, created_by, assignee, status, id FROM issue "
                  "WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
                  (repo_key, str(issue_num)))
        _row = c.fetchone()
        if not _row:
            raise HTTPException(status_code=404, detail={"code": 404,
                                                         "message": f"Issue #{issue_num} 需求卡片不存在，无法触发"})
        _stage = str(_row[0] or "")
        _status = str(_row[4] or "")
        if _status in ("cancelled", "completed"):
            raise HTTPException(status_code=400, detail={"code": 400,
                                                         "message": f"Issue #{issue_num} status={_status}，终态需求不可触发"})
        if _stage != "created":
            raise HTTPException(status_code=409, detail={"code": 409,
                                                         "message": f"Issue #{issue_num} 当前 stage={_stage or '未知'}，仅未入链的挂单（stage=created）可触发"})
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id=?", (repo_key, issue_id))
        _evt_count = int(c.fetchone()[0] or 0)
        if _evt_count:
            # 复核：仅触发类事件才算入链（排除仓库订阅等旁路事件，防误判 409）
            _evt_count = _pending_trigger_event_count(repo_key, issue_num)
        if _evt_count:
            raise HTTPException(status_code=409, detail={"code": 409,
                                                         "message": f"Issue #{issue_num} 已有流水线事件，不重复入链"})
        # 条件 UPDATE 认领：并发下仅一个请求能把 created 切到 vod；
        # status 并发取消窗口由 CAS 条件兜底（取消只改 status 不改 stage）
        c.execute("UPDATE issue SET stage='vod', owner_agent='vod', updated_at=datetime('now') "
                  "WHERE repo_key=? AND issue_number=? AND stage='created' "
                  "AND status NOT IN ('cancelled','completed')",
                  (repo_key, str(issue_num)))
        _claimed = c.rowcount
        _rt_db.commit()
    return _row, _claimed


def _pending_issue_status_row(repo_key: str, issue_num):
    """同步读：认领失败后回查 issue.status（用于区分并发取消 400 / 并发触发 409）。"""
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
                  (repo_key, str(issue_num)))
        return c.fetchone()


async def trigger_pending_issue_pipeline(repo_key: str, issue_num, actor: str = "",
                                         operator_id: "str | int" = 0,
                                         route_issue_id: int = 0) -> dict:
    """Issue #3621：对 ``trigger_pipeline=false`` 建出的挂单（stage=created）首次触发入链。

    复用建单直发的 issue_opened 同步触发路径（``_issue_opened_pipeline_trigger``），
    触发后行为与直接建单一致（stage 切换到 vod、产生同形态 issue_opened 事件）。
    幂等/一致性保护：
    - stage 非 ``created`` → 409（已入链/已收敛，不重复入链）
    - status 为 ``cancelled``/``completed`` → 400（终态不可触发；Issue #3621 复审：
      状态校验并入同锁认领，与并发取消窗口原子互斥）
    - 该 issue 已存在触发类流水线事件 → 409（防重复触发产生重复事件）
    - stage/status 校验、事件计数、`created→vod` 认领在同一锁/事务内完成；认领是
      条件 UPDATE（CAS，含 status NOT IN ('cancelled','completed')），并发触发/并发
      取消仅一个成功，其余 400/409，不会重复入链
    - 触发失败（抛错或未落事件）→ 回滚为 stage=created（无事件时，status 恢复
      active、保留终态），可重试，不卡死
    """
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    owner, rname = split_owner_repo(repo_key)
    _issue_id = f"#{issue_num}"
    # 1) 状态/事件校验 + 2) 原子认领 created→vod（同一 db_lock/事务；用 runtime 域连接）
    # 事件循环保护：db_lock 是同步阻塞资源，放到线程里跑，避免卡住 async 处理链（async-blocking 棘轮 A6）
    _row, _claimed = await asyncio.to_thread(
        _claim_pending_issue_created, repo_key, issue_num, _issue_id)
    if not _claimed:
        # 认领失败：并发取消 → 400；并发触发/其它 → 409
        _cur = await asyncio.to_thread(_pending_issue_status_row, repo_key, issue_num)
        if _cur and str(_cur[0] or "") in ("cancelled", "completed"):
            raise HTTPException(status_code=400, detail={"code": 400,
                                                         "message": f"Issue #{issue_num} status={_cur[0]}，终态需求不可触发"})
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": f"Issue #{issue_num} 已被其它触发认领，不重复入链"})
    _title = str(_row[1] or "")
    _creator = (str(_row[2] or "") or actor or "").strip()
    _db_issue_id = int(_row[5] or 0)

    def _audit_triggered(seq: int) -> None:
        """QA C3（#3913）：created→vod 首次触发入链留审计；失败仅告警不阻断。"""
        try:
            from src.domains.mgmt.public import log_audit
            log_audit("issue_pipeline_triggered_from_created",
                      operator_id=(operator_id or 0),
                      detail={"repo_key": repo_key,
                              "issue_id": (route_issue_id or _db_issue_id),
                              "issue_number": issue_num,
                              "from_stage": "created", "to_stage": "vod",
                              "actor": (actor or _creator or ""),
                              "seq": int(seq or 0)})
        except Exception as _ae:
            log.warning(f"trigger_pending: audit log failed for #{issue_num}: {_ae}")

    # 3) 走同一同步触发路径；失败/未落事件 → 回滚为 created 以便重试
    try:
        _pipe_result = await _issue_opened_pipeline_trigger(
            repo_key, owner, rname, issue_num, _title, _creator, repo_key.startswith("github:"))
    except Exception as _e:
        # run_pipeline 可能已落事件后才抛错：事件已存在即视为入链成功（不误报失败）
        if _pending_trigger_event_count(repo_key, issue_num):
            log.warning(f"trigger_pending: pipeline raised after event landed for #{issue_num}: {_e}")
            _audit_triggered(0)
            return {"ok": True, "issue_number": issue_num, "stage": "vod", "seq": 0}
        _rolled = _rollback_pending_issue_trigger(repo_key, issue_num)
        log.warning(f"trigger_pending: pipeline raised for #{issue_num}, rolled_back={_rolled}: {_e}")
        raise HTTPException(status_code=502, detail={
            "code": 502,
            "message": ("流水线触发失败，已回滚为待触发状态，可重试" if _rolled
                        else "流水线触发失败，请检查流水线事件后再试")})
    _seq = int((_pipe_result or {}).get("seq", 0) or 0)
    _evt_after = _pending_trigger_event_count(repo_key, issue_num)
    if not _evt_after:
        _rolled = _rollback_pending_issue_trigger(repo_key, issue_num)
        log.warning(f"trigger_pending: no event after pipeline for #{issue_num}, rolled_back={_rolled}")
        raise HTTPException(status_code=502, detail={
            "code": 502,
            "message": ("流水线未落事件，已回滚为待触发状态，可重试" if _rolled
                        else "流水线未落事件，请检查流水线事件后再试")})
    log.info(f"trigger_pending: triggered pipeline for #{issue_num}, seq={_seq}")
    _audit_triggered(_seq)
    return {"ok": True, "issue_number": issue_num, "stage": "vod", "seq": _seq}


def _is_upstream_timeout(exc: Exception) -> bool:
    """判断异常是否为上游超时（socket 超时 / 请求级放弃 / urllib 超时包装）。

    Issue #3349：建单超时必须走「对账 + 504 可判别失败」分支，不能与普通 5xx 混淆。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    _msg = str(exc).lower()
    return "timed out" in _msg or "timeout" in _msg


async def _run_upstream(fn, *args, abandon_after: float = CREATE_UPSTREAM_TIMEOUT, **kwargs):
    """同步上游调用卸载到工作线程，并施加请求级放弃阈值（Issue #3349）。

    事件循环只等 ``await``，线程里跑的是同步 urllib/requests；超过 ``abandon_after``
    立刻释放事件循环与请求。线程内的调用无法真正中断（可能仍在完成），因此调用方在
    超时分支必须做一次对账探针，避免把「其实已建成」当成失败。
    """
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=abandon_after)


async def _reconcile_created_issue(api_token: str, owner: str, repo: str, title: str,
                                   provider: str = "gitcode") -> int | None:
    """建单超时后的对账探针：确认上游是否其实已经建成（Issue #3349）。

    只查 VCS 最新一页（有界、短超时），命中即按建成处理（写台账 + 返回成功），
    从而把「超时但已建成」和「超时且未建成」区分开；探针自身失败返回 None，
    由调用方返回 504（retryable + 幂等键），重试会先查重，不会重复建单。
    """
    try:
        return await _run_upstream(
            _find_open_issue_by_title_vcs, api_token, owner, repo, title,
            provider=provider, max_pages=1, timeout=CREATE_RECONCILE_TIMEOUT,
            abandon_after=CREATE_RECONCILE_TIMEOUT + RECONCILE_ABANDON_GRACE)
    except Exception as e:
        log.warning(f"create reconcile probe failed (report upstream_timeout): {e}")
        return None


async def create_task_via_issue(body: TaskCreateBody, user: dict = Depends(get_current_user_or_api_key)):
    """Task Center 新建任务：同步在 GitCode 仓库创建 Issue，并按既有流程建档。

    端点 `POST /api/tasks/create` 已废弃移除（Phase C2，见 issues-api-execution-plan.md）——
    对外统一入口为 `POST /api/issues/{owner}/{repo}`（issues.py 复用本逻辑）。
    """
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    if not body.repo_key or not body.title.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库和标题必填"})
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "任务描述必填"})
    repo_key = to_repo_key(body.repo_key)
    owner, rname = split_owner_repo(repo_key)
    # provider 分流：github: → GitHub API，gitcode: → GitCode API
    _is_github = repo_key.startswith("github:")
    # D35/S55：provider 由 repo_key 前缀派生（gitee 不再折叠为 gitcode）
    from src.kernel.repo_key import parse_repo_key as _prk_d35
    _provider = _prk_d35(repo_key)[0]
    # _preauth_token: autopilot 等内部调用（auth_type=api_key/internal）预注入 bot token
    # JWT 登录用户的 token 是 session JWT 不是 GitCode API token，不能走此路径
    # grape_ 前缀的是用户级 API Key（CLI 用户），同样不是 GitCode token，不能走此路径
    _preauth_token = ""
    # token 来源标记（Issue #3427 复审 Spec-1）：
    # _resolver_source: 单点解析器命中层（rc/global/user），空 = 解析器未命中；
    # _token_from_user_fallback: legacy 用户 token 兜底是否真正提供了本次 token。
    _resolver_source = ""
    _token_from_user_fallback = False
    if user.get("auth_type") in ("api_key", "internal") and user.get("token"):
        _token_val = user["token"]
        if not _token_val.startswith("grape_"):
            _preauth_token = _token_val
    if _preauth_token:
        api_token = _preauth_token
    else:
        # Issue #3427（复审 Spec-1）：建单入口与 host 同口径——单点解析器首选
        # （固定顺序 rc → global → user，probe=True 对命中 token 探活，过期但
        # 非空的 bot token 也能暴露为 TOKEN_REJECTED_UPSTREAM → 401）。
        # 用户 token 仅在解析器全链未命中（TOKEN_MISSING / TOKEN_DECRYPT_FAILED）
        # 时兜底；解析器已判 401 时禁止兜底（继续用同一失效 token 只会再失败）。
        # github / gitcode / gitee 等所有 provider 分支同口径（含分层 error_code）。
        from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
        from src.kernel.credential_errors import (CredentialErrorCode,
                                                  credential_error_code_for,
                                                  credential_error_detail_for)
        _p_name = _provider
        try:
            from src.domains.runtime.vcs.platform_card import get_platform_card
            _c = get_platform_card(_provider)
            _p_name = (_c.name if _c else _provider) or _provider
        except Exception as _e:
            log.debug(f"platform card lookup failed: {_e}")
        api_token = ""
        _cred = resolve_credential_detailed(repo_key, probe=True)
        if _cred and _cred.ok and _cred.token:
            log.info(f"create_task: credential resolver hit "
                     f"source={_cred.source} repo={repo_key}")
            api_token = _cred.token
            _resolver_source = _cred.source or ""
        elif (credential_error_code_for(_cred)
                in (CredentialErrorCode.TOKEN_MISSING.value,
                    CredentialErrorCode.TOKEN_DECRYPT_FAILED.value)):
            # 兜底：用户 token（get_user_token_for_repo 按 repo_key 前缀派生
            # provider——S70/D37 口径：gitee 仓不折叠为 gitcode 链）。
            from src.api.deps import get_user_token_for_repo
            api_token = get_user_token_for_repo(repo_key, user["username"],
                                                user_id=str(user.get("id") or ""))
            _token_from_user_fallback = bool(api_token)
        if not api_token:
            # Spec：按错误码分层返回 400/401（status 取 detail["code"]）
            _detail = credential_error_detail_for(_cred, _p_name)
            raise HTTPException(status_code=_detail["code"], detail=_detail)
    # 是否使用「用户自己的 GitCode token」：用于 401 时区分「用户登录失效」（引导重新绑定）
    # 与「仓库 bot token 失效」（提示检查仓库 Bot Token 配置）两套文案。
    # Issue #3427 复审 Spec-1：按 token 实际来源判定——仅 legacy 用户 token 兜底命中
    # 或解析器 source=user（用户身份）时为 True；rc/global bot 来源 401 属仓库 bot
    # 配置问题，走 bot-token 文案/分层码，不触发用户刷新与 OAuth 跳转。
    # GitHub 语境下 source=user 同样是用户凭据失效（需 401，不得当 bot 502），
    # 但 GitHub 无 GitCode OAuth 刷新链路——下方 401 分支对 _is_github 单独文案、
    # 不返回 action=reauth_required（复审 Spec-2）。
    _using_user_token = ((not _preauth_token)
                         and (_token_from_user_fallback
                              or _resolver_source == "user"))
    # Issue #649: assignee 不再默认填创建者——非成员默认指派会触发上游 403，
    # 误导排查方向。不传 assignee 时留空，由仓库方后续分派；显式传入时走下方成员资格预校验。
    assignee = (body.assignee or "").strip()
    # 内部流程（场景归属 issue_creator / pipeline 事件创建人）仍需要创建者的平台登录名，
    # 单独解析 _creator_login，绝不回填到 assignee（Issue #343: external_login 优先）。
    _creator_login = user["username"]
    try:
        from src.domains.mgmt.public import list_platform_links
        _provider_name = _provider
        # A 批：创建者平台登录名取"身份行 ∪ PAT 凭据域"（PAT 的 login 同样有效）
        for ident in list_platform_links(user["id"]):
            if ident.get("provider") == _provider_name and ident.get("external_login"):
                _creator_login = ident["external_login"]
                break
    except Exception:
        pass  # 查不到就用 grape username fallback
    # Issue #3349: 建单幂等键（沿用查重键 owner/repo/title）——随 409/502/504 响应返回，
    # 调用方重试时可直接对账「上一发到底有没有建成」。
    _idempotency_key = f"{owner}/{rname}/{body.title.strip()}"
    # 1) 幂等：同 title open issue 已存在 → 提示用户任务已存在
    # Issue #343: 查重失败（token 失效）时给明确提示，不静默放行导致重复建单
    # Issue #3349: 查重（台账查询 + 可能的有界 VCS 探针）是同步 IO，必须离开事件循环，
    # 否则上游变慢时单个建单请求会独占事件循环、并发建单整体排队 → 网关 502。
    try:
        # 同步 urllib 出站调用必须离开事件循环（否则 GitCode 慢响应会把整个进程卡住，
        # 实测单次 15~20s 期间所有 UI 请求一起排队）
        issue_num = await asyncio.to_thread(
            _find_open_issue_by_title, api_token, owner, rname, body.title.strip(),
            provider=_provider, timeout=DEDUP_UPSTREAM_TIMEOUT)
    except RuntimeError as _dedup_err:
        from src.domains.runtime.vcs.token_error import is_auth_error_exc, BOT_TOKEN_INVALID_MSG, GITCODE_USER_TOKEN_INVALID_MSG
        if is_auth_error_exc(_dedup_err) and _using_user_token:
            if _is_github:
                # GitHub 用户凭据 401：按分层码返回 401（非 bot 502），
                # 且不得跳 GitCode OAuth（GitHub 无该刷新链路）。
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "error_code": "TOKEN_REJECTED_UPSTREAM",
                    "message": "查重失败，GitHub 用户凭据已被上游拒绝（401，已失效或被吊销），"
                               "请重新绑定 GitHub 账号或改用仓库 Bot Token 后重试",
                })
            # 用户自己的 GitCode token 401 → 主动刷新一次并重试；仍失败给「重新登录」引导
            _renewed = _refresh_user_gitcode_token(user)
            if _renewed:
                api_token = _renewed
                try:
                    issue_num = await asyncio.to_thread(
                        _find_open_issue_by_title, api_token, owner, rname, body.title.strip(),
                        provider=_provider, timeout=DEDUP_UPSTREAM_TIMEOUT)
                except RuntimeError as _dedup_err2:
                    _dedup_err = _dedup_err2
                else:
                    _dedup_err = None
            if _dedup_err is not None:
                # 用户自己的 GitCode token 确认失效（刷新失败或重试仍 401）
                # → 401 + reauth_required：前端 handleResp 收到后自动跳 GitCode OAuth 授权页，强制重新授权
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "message": f"查重失败，{GITCODE_USER_TOKEN_INVALID_MSG}",
                    "action": "reauth_required",
                    "idempotency_key": _idempotency_key,
                })
        elif is_auth_error_exc(_dedup_err):
            # bot 来源 401（rc/global bot token 失效）→ bot-token 文案 + 分层码，
            # 不触发用户 token 刷新与 OAuth 跳转（Issue #3427 复审 Spec-1）。
            # 状态码与 credential_errors 映射对齐：TOKEN_REJECTED_UPSTREAM → 401。
            raise HTTPException(status_code=401, detail={"code": 401,
                                                         "error_code": "TOKEN_REJECTED_UPSTREAM",
                                                         "message": f"查重失败，{BOT_TOKEN_INVALID_MSG}: {str(_dedup_err)[:200]}",
                                                         "idempotency_key": _idempotency_key})
        else:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"查重失败: {str(_dedup_err)[:200]}",
                                                         "idempotency_key": _idempotency_key})
    created = False
    if issue_num is not None:
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": f"任务已存在（Issue #{issue_num}），请输入新的任务标题",
                                                     "issue_number": issue_num,
                                                     "idempotency_key": _idempotency_key})
    if issue_num is None:
        # Issue #649: 本次建单请求级 trace_id（错误响应附带，便于排查定位）
        import uuid as _uuid
        _trace_id = f"tr_{_uuid.uuid4().hex[:16]}"
        # Issue #649: 显式 assignee + GitCode → 上游创建前预校验成员资格；
        # 非成员直接给出「非成员」语义错误（含 trace_id），不再无谓打上游 403 再误报。
        if (not _is_github) and assignee:
            # Issue #3349：该预校验是一次同步 VCS 调用，卸载到线程，不占事件循环。
            _member_state = await asyncio.to_thread(
                _gitcode_assignee_member_check, api_token, owner, rname, assignee)
            if _member_state == "not_member":
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": f"创建 Issue 失败：assignee {assignee} 不是仓库 {owner}/{rname} 的成员，"
                               f"无法被指派。可不传 assignee（留空由仓库方分派），或改填该仓库成员。",
                    "trace_id": _trace_id,
                    "upstream_detail": f"member precheck: collaborator permission lookup returned 404 "
                                       f"for assignee '{assignee}' on {owner}/{rname}",
                })
        # Issue #1245: 建单前场景可用性预校验（查重 + assignee 预校验之后、GitCode 建单之前）。
        # 静态只读校验，不通过返回 400 + problems[] + trace_id；此时尚未产生任何 VCS/DB 副作用。
        # 显式场景直接校验；智能推荐（vod-auto）/留空时回落到仓库默认场景一并校验，
        # 与后续写入 issue 的 scene_id 口径一致，避免默认/推荐路径产生死单。
        _scene_for_precheck = body.scene_id if (body.scene_id and body.scene_id != "vod-auto") else ""
        # 显式选择的场景严格拦单；以下回落的（仓库 dev_scene / 内置 default）
        # 只告警不拦——用户没有修复入口，拦了也只会得到无法处理的错误。
        # （3576aaa 语义；此段曾在 to_thread 改造提交中被误删，QA A14 判 BLOCKED）
        _scene_is_explicit = bool(_scene_for_precheck)
        if not _scene_for_precheck:
            try:
                from src.domains.mgmt.public import get_repo_scene  # 跨域走 owner public
                _scene_for_precheck = get_repo_scene(repo_key) or ""
            except Exception as _rs_err:
                log.warning(f"create_task: get_repo_scene for precheck failed: {_rs_err}")
                _scene_for_precheck = ""
        if _scene_for_precheck:
            from src.domains.runtime.scene_precheck import precheck_scene, summarize_problems
            # 显式选择的场景严格拦单；以下回落的只告警不拦——用户没有修复入口
            # （3576aaa 语义；此段曾在 to_thread 改造时被误删，QA A14 判 BLOCKED）
            _precheck = precheck_scene(_scene_for_precheck, repo_key, user,
                                       explicit=_scene_is_explicit)
            if not _precheck.get("ok"):
                _pc_problems = _precheck.get("problems") or []
                _pc_summary = summarize_problems(_pc_problems)
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"所选场景当前不可执行，已阻止建单：{_pc_summary}",
                    "problems": _pc_problems,
                    "trace_id": _precheck.get("trace_id") or _trace_id,
                })
        # 拼接 Task Center 上传的附件到 issue body（Issue #648：拼装逻辑抽为可复用函数）
        _desc_with_attach = (body.description or "").strip()
        _pending_attachments = _attach_list(owner, rname, "pending")
        _attach_md = _attachments_markdown_block(_pending_attachments)
        if _attach_md:
            _desc_with_attach += _attach_md
        # Issue #343 问题十一修复: 捕获建单 API 403/422，转成友好报错而非 500

        async def _reconcile_or_504() -> dict:
            # Issue #3349：超时/放弃 ≠ 一定没建成。先做一次有界对账探针：
            # 命中 → 视作建成并继续正常建档（不重复建单）；探不到 → 504 + 幂等键。
            # 首次建单与 401 刷新后的建单重试共用本路径，超时语义保持一致。
            _reconciled_num = await _reconcile_created_issue(
                api_token, owner, rname, body.title.strip(), provider=_provider)
            if _reconciled_num is not None:
                log.info(f"create_task: upstream timeout but issue #{_reconciled_num} exists "
                         f"(reconciled) repo={repo_key} title={body.title.strip()[:80]}")
                return {"number": _reconciled_num, "reconciled": True}
            raise HTTPException(status_code=504, detail={
                "code": 504,
                "error": "upstream_timeout",
                "message": f"创建 Issue 超时（{CREATE_UPSTREAM_TIMEOUT}s 内上游未返回），结果未知；"
                           f"可用同一标题重试：重试会先按标题查重，若上游已建成将返回 409（含 issue_number），"
                           f"不会重复建单。",
                "retryable": True,
                "idempotency_key": _idempotency_key,
                "trace_id": _trace_id,
                "verify_hint": f"可先 GET /api/issues/{owner}/{rname}?q={body.title.strip()} "
                               f"确认上游是否已建成，再决定是否重试",
            })

        try:
            # D35/S55：统一建单入口（卡驱动；gitee 自动正确，存量平台内置卡对齐）
            # Issue #3349：建单 POST 卸载到工作线程 + 请求级放弃阈值（25s），
            # 避免上游饱和时请求在网关排队到 upstream timeout（502）。
            resp = await _run_upstream(
                _create_issue_unified, api_token, _provider, owner, rname,
                body.title.strip(), _desc_with_attach, assignee=assignee,
                abandon_after=CREATE_UPSTREAM_TIMEOUT)
        except Exception as _create_err:
            # Issue #3349：超时/放弃走有界对账探针：命中按建成返回；探不到 504 + 幂等键。
            if _is_upstream_timeout(_create_err):
                resp = await _reconcile_or_504()
                _create_err = None
            _err_msg = str(_create_err)
            # 从 RuntimeError 消息提取 HTTP code（格式: "failed (403): ..."）
            import re as _re
            _codes = _re.findall(r'\((\d{3})\)', _err_msg)
            _http_code = int(_codes[0]) if _codes else 0
            if _http_code == 401 and _using_user_token:
                if _is_github:
                    # GitHub 用户凭据 401：直接分层 401，不刷新 GitCode OAuth。
                    raise HTTPException(status_code=401, detail={
                        "code": 401,
                        "error_code": "TOKEN_REJECTED_UPSTREAM",
                        "message": "创建 Issue 失败：GitHub 用户凭据已被上游拒绝（401，已失效或被吊销），"
                                   "请重新绑定 GitHub 账号或改用仓库 Bot Token 后重试",
                    })
                # 用户自己的 GitCode token 401 → 刷新一次再重试；仍失败走下方 401 分支给「重新登录」引导
                _renewed = _refresh_user_gitcode_token(user)
                if _renewed:
                    api_token = _renewed
                    try:
                        resp = await _run_upstream(
                            _create_issue_unified, api_token, _provider, owner, rname,
                            body.title.strip(), _desc_with_attach,
                            assignee=assignee,
                            abandon_after=CREATE_UPSTREAM_TIMEOUT)
                    except Exception as _create_err2:
                        # Issue #3349（QA 审查）：刷新后的建单重试超时与首次建单保持同一语义——
                        # 走有界对账探针（命中按建成返回；探不到 504 + 幂等键），
                        # 不再落入下方按错误码提取的 502 通用分支。
                        if _is_upstream_timeout(_create_err2):
                            resp = await _reconcile_or_504()
                            _create_err = None
                        else:
                            _create_err = _create_err2
                        _err_msg = str(_create_err2)
                        _codes = _re.findall(r'\((\d{3})\)', _err_msg)
                        _http_code = int(_codes[0]) if _codes else 0
                    else:
                        # 刷新重试成功：交由下方统一「响应无 issue number」校验提取 number
                        _create_err = None
            if _create_err is None:
                pass  # 刷新重试成功，跳过异常分支
            elif _http_code == 403:
                # Issue #523: 403 不再仅凭状态码断言 assignee 无效——提取上游响应正文（脱敏+截断）按语义分流
                _up_403_parts = _re.split(r'\(403\):\s*', _err_msg, maxsplit=1)
                _up_403 = _up_403_parts[1].strip() if len(_up_403_parts) > 1 else ''
                # 基础脱敏：token/凭据类键值不回显
                _up_403 = _re.sub(
                    r'(?i)(access_token|refresh_token|token|password|authorization|api[_-]?key|secret[_-]?key|ak|sk)[=:]\s*\S+',
                    r'\1=***', _up_403)[:200]
                _up_403_low = _up_403.lower()
                if 'prohibited for model repository and dataset repository' in _up_403_low:
                    # GitCode Space/模型仓/数据集仓能力限制 → 仓库类型不支持建单
                    raise HTTPException(status_code=403, detail={
                        "code": 403,
                        "message": "创建 Issue 失败：当前仓库类型（如 GitCode Space/模型仓/数据集仓）"
                                   "不支持创建 Issue，请改用普通代码仓发起协作"
                                   + (f"（上游响应: {_up_403}）" if _up_403 else "")
                    })
                if _up_403 and any(kw in _up_403_low for kw in (
                        'user not found', 'user is not found', 'permission', 'forbidden',
                        'member', 'privilege', 'unauthorized', 'not authorized')):
                    # Issue #649: 上游 403 权限/成员类 → 归类为「非成员/无权指派」语义错误，
                    # 主文案指向真实原因（成员资格），不再误报「assignee 用户名无效」误导排查；
                    # trace_id + 上游脱敏原文作为辅助字段（detail，非主文案）保留可追溯。
                    if assignee:
                        raise HTTPException(status_code=403, detail={
                            "code": 403,
                            "message": f"创建 Issue 失败：无权指派 assignee（assignee={assignee}，"
                                       f"非仓库 {owner}/{rname} 成员或无指派权限）。"
                                       f"可不传 assignee（留空由仓库方分派）后重试。",
                            "trace_id": _trace_id,
                            "upstream_detail": _up_403,
                        })
                    # assignee 为空的权限类 403（创建者自身无权限等）→ 通用提示附上游脱敏正文
                    raise HTTPException(status_code=403, detail={
                        "code": 403,
                        "message": f"创建 Issue 失败（上游 403）: {_up_403}"
                    })
                # 无法识别的 403 → 通用提示附上游脱敏正文，不猜测归因
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": f"创建 Issue 失败（上游 403）: {_up_403 or _err_msg[:200]}"
                })
            elif _http_code == 422:
                raise HTTPException(status_code=422, detail={
                    "code": 422,
                    "message": f"创建 Issue 失败：参数错误: {_err_msg[:200]}"
                })
            elif _http_code == 401:
                # Issue #345: 区分用户 token / bot token 两套 401 提示
                if _using_user_token:
                    # 用户自己的 GitCode token 401 且刷新失败 → 401 + reauth_required，
                    # 前端自动跳 GitCode OAuth 授权页强制重新授权（用户登录凭据已失效）
                    from src.domains.runtime.vcs.token_error import GITCODE_USER_TOKEN_INVALID_MSG
                    raise HTTPException(status_code=401, detail={
                        "code": 401,
                        "message": f"创建 Issue 失败：{GITCODE_USER_TOKEN_INVALID_MSG}",
                        "action": "reauth_required",
                    })
                from src.domains.runtime.vcs.token_error import BOT_TOKEN_INVALID_MSG
                # 状态码与 credential_errors 映射对齐：TOKEN_REJECTED_UPSTREAM → 401
                # （bot 401 与用户 401 同码不同文案，不再套 502）。
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "error_code": "TOKEN_REJECTED_UPSTREAM",
                    "message": f"创建 Issue 失败：{BOT_TOKEN_INVALID_MSG}",
                })
            else:
                raise HTTPException(status_code=502, detail={
                    "code": 502,
                    "message": f"创建 Issue 失败: {_err_msg[:200]}"
                })
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"创建 Issue 失败（响应无 issue number）: {str(resp)[:200]}"})
        # S70：gitee 等平台 issue ident 为字符串（"IKSZ3I"），int() 强转必炸——
        # 保持平台原生形态（_native_issue_number：数字串→int，其余原样）。
        from src.domains.runtime.vcs.api_client import _native_issue_number
        issue_num = _native_issue_number(n)
        # Issue #453 代发台账（写点6/7，前端建单入口）：入账
        try:
            from src.kernel.outbound_ledger import record_outbound, content_hash_md5
            from src.kernel.repo_key import parse_repo_key
            _prov, _, _ = parse_repo_key(repo_key)
            record_outbound(_prov, repo_key, "issue", str(issue_num), issue_number=str(issue_num),
                            content_hash=content_hash_md5(_desc_with_attach))
        except Exception as _le:
            log.warning(f"outbound_ledger record (create issue) failed: {_le}")
        created = True
        # Issue #3250：把新标题写回查重索引，批量造单后续不必再翻页
        try:
            from src.domains.runtime.vcs.api_client import remember_open_issue_title
            remember_open_issue_title(_provider, owner, rname, body.title.strip(), issue_num)
        except Exception:
            pass
        # 附件从 pending 迁移到实际 issue_number（OBS: copy + delete old; 本地: rename）
        from src.domains.runtime.storage import get_storage
        _storage = get_storage()
        _pending_atts = _attach_list(owner, rname, "pending")
        if _pending_atts:
            for _att in _pending_atts:
                _fname = _att["filename"]
                if _storage:
                    # OBS: read pending → save to issue_number → delete pending
                    _old_key = _attach_obs_key(owner, rname, "pending", _fname)
                    _new_key = _attach_obs_key(owner, rname, str(issue_num), _fname)
                    _data = _storage.read_bytes(_old_key)
                    if _data:
                        _storage.save_bytes(_new_key, _data)
                        _storage.delete_object(_old_key)
                else:
                    # 本地: move file
                    _old_path = os.path.join(_attach_base_dir(owner, rname, "pending"), _fname)
                    _new_dir = _attach_base_dir(owner, rname, str(issue_num))
                    os.makedirs(_new_dir, exist_ok=True)
                    try:
                        os.rename(_old_path, os.path.join(_new_dir, _fname))
                    except Exception as _e:
                        log.warning(f"attachment file rename failed: {_e}")
    # 2) 场景选择：用户选了具体场景 → 写入 scene_id（issue_opened 读到有值则跳过推荐）
    #    选智能推荐（vod-auto）或留空 → 仓库无默认场景时不写 scene_id → issue_opened 走智能推荐
    #    Issue #415: 无显式场景且仓库配置默认场景（managed_repos.dev_scene）→ 回退写入，
    #    保证对话通道创建的 Issue 详情页「流水线场景」不显示 --。
    try:
        from src.api.session_store import set_issue_scene
        from src.domains.runtime.engine.engine_select import _repo_user_id
        _scene_to_write = ""
        if body.scene_id and body.scene_id != "vod-auto":
            _scene_to_write = body.scene_id
        else:
            # 无显式场景 → 仓库默认场景兜底（未配置则保持空，走智能推荐）
            try:
                from src.domains.mgmt.public import get_repo_scene  # 跨域走 owner public
                _scene_to_write = get_repo_scene(repo_key) or ""
            except Exception as _re:
                log.warning(f"create_task: get_repo_scene failed: {_re}")
                _scene_to_write = ""
        if _scene_to_write:
            # Issue #343 问题十一修复: issue_creator 必须是 GitCode/GitHub 登录名（external_login），
            # 不能用平台名 user["username"]——_repo_user_id 查的是 unified_user_mapping.gitcode_user
            scene_uid = _repo_user_id(repo_key, issue_creator=_creator_login) or 0
            set_issue_scene(repo_key, f"#{issue_num}", _scene_to_write, scene_user_id=scene_uid)
            log.info(f"create_task: scene={_scene_to_write} set for #{issue_num}"
                     + ("" if (body.scene_id and body.scene_id != "vod-auto") else " (repo default fallback)"))
    except Exception as _e:
        # log 为模块顶部导入（L17）。勿在此加局部 import log——except 内先引用
        # 后 import 会 UnboundLocalError 吞真实异常（D102-C 教训，曾实弹）。
        log.warning(f"create_task: set_issue_scene failed: {_e}")
    # 3) 自动检测多任务模式：根据场景的 issue skill 是否为 issue-multitask
    _auto_subtask_mode = 0
    if body.scene_id and body.scene_id != "vod-auto":
        try:
            from src.domains.mgmt.public import _query_scene_by_id  # 跨域走 owner public
            _scene_obj = _query_scene_by_id(body.scene_id, repo_key=repo_key)
            if _scene_obj:
                _nodes = _scene_obj.get("nodes", []) or []
                _issue_skill = next((n.get("skill", "") for n in _nodes if n.get("id") == "issue"), "")
                if _issue_skill == "issue-multitask":
                    _auto_subtask_mode = 1
                    log.info(f"Auto multi-task mode: scene={body.scene_id} issue skill=issue-multitask")
        except Exception as _e:
            log.warning(f"Auto multi-task detection failed: {_e}")

    # 4) 按既有流程幂等建档（同 issue 任务 id 复用）；org 归属由 ensure_issue_task 统一以仓库为准
    task_id = ensure_issue_task(repo_key, issue_num, body.title.strip(), body.description or "", assignee=assignee,
                                subtask_mode=_auto_subtask_mode,
                                owner_user_id=str(user.get("id") or ""),
                                # Issue #3621：创建人身份落 created_by，供挂单后续首次触发复用
                                creator=_creator_login)
    if not task_id:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "任务建档失败"})
    _snap_id = str(getattr(body, "org_context_snapshot_id", "") or "")
    if _snap_id:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                "UPDATE issue SET org_context_snapshot_id=? "
                "WHERE repo_key=? AND issue_number=?",
                (_snap_id, repo_key, issue_num),
            )
            _rt_db.commit()
    # 5) 多任务模式（自动检测）→ Issue body 追加标记 + issue_state 标记
    if _auto_subtask_mode:
        try:
            from src.api.session_store import set_subtask_mode
            set_subtask_mode(repo_key, f"#{issue_num}", True)
        except Exception as _e:
            log.warning(f"create_task: set_subtask_mode failed: {_e}")
        try:
            _marker = "\n\n---\n[multi-task-mode] 本任务启用多任务模式，将由 Issue Agent 自动判断是否拆分为子任务执行。"
            # Issue #3349：PATCH 是同步 VCS 调用，卸载到工作线程（否则多任务模式建单同样阻塞事件循环）
            if _is_github:
                # Issue #343: GitHub 写操作走 bot token
                await asyncio.to_thread(
                    _patch_github_issue_body, api_token, owner, rname, issue_num,
                    (body.description or "") + _marker)
            else:
                # Issue #343: GitCode 写操作走 bot token
                await asyncio.to_thread(
                    _patch_issue_body, api_token, owner, rname, issue_num,
                    (body.description or "") + _marker)
        except HTTPException:
            raise  # reauth_required 需要传递给前端
        except Exception as _e:
            log.warning(f"create_task: patch issue body for multi-task-mode failed: {_e}")

    # Issue #89: 同步触发 pipeline 事件，不依赖 webhook（webhook 异步可能丢事件）
    # 新建 Issue 时直接创建 issue_opened 事件 → VOD Agent 处理
    # 复用已有 Issue 时创建 a2a_event 事件 → issue Agent 处理
    # Issue #3621：trigger_pipeline=false 时只建单不触发流水线（stage=created 挂单）
    _trigger_pipeline = bool(getattr(body, "trigger_pipeline", True))
    # Issue #3621：trigger_pipeline=false 时只建单不触发流水线（stage=created 挂单）
    if created and not _trigger_pipeline:
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{issue_num}", "created", _STAGE_CREATED_OWNER,
                            assignee=assignee or "")
            log.info(f"create_task: pipeline skipped for #{issue_num} (trigger_pipeline=false)")
        except Exception as _e:
            log.warning(f"create_task: set created stage failed: {_e}")
    elif created:
        try:
            from src.api.session_store import set_issue_stage
            # 初始化 issue_state（vod 阶段）
            set_issue_stage(repo_key, f"#{issue_num}", "vod", "vod", assignee=assignee or "")
            # 同步触发 pipeline（不依赖 webhook，task_center 创建的事件直接入队）
            # run_pipeline 内部会调 append_event 存事件 + 路由 + 入队
            # 方案 D（duplicate-issue-opened-event-fix-design.md）：按平台发原生 payload，
            # 使补发事件与对应平台真实 webhook 形态一致（可读性/排查 + 去重指纹一致），
            # 不再对 GitCode 也发 GitHub 格式合成 payload 造成「GitHub 事件」误判。
            import json as _pipe_json
            # Issue #343/#649: pipeline 创建人 = 创建者 external_login（_creator_login），
            # 与 assignee 解耦——不传 assignee 时创建人字段仍正确，不再回填 assignee
            _assign = _creator_login or ""
            if _is_github:
                _pipe_payload = {
                    "action": "opened",
                    # D102-O 同族收口：issue number 原生保形（gitee 等平台为字符串
                    # ident，int() 强转会炸同步触发——实弹 IKTVZQ）；流水线进程内
                    # ident 链已字符串化（D102-G），此处直传原生形态
                    "issue": {"number": issue_num, "title": body.title.strip(),
                              "user": {"login": _assign} if _assign else {}},
                    "repository": {"full_name": f"{owner}/{rname}"},
                    "sender": {"login": _assign} if _assign else {},
                }
                _pipe_headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
                # Issue #453：内部同步触发标记（同 GitCode 分支，跳过台账判定）
                _pipe_payload["_grape_internal_trigger"] = True
            else:
                _pipe_payload = {
                    "object_kind": "issue",
                    "object_attributes": {"iid": issue_num, "title": body.title.strip(),
                                          "action": "open"},
                    "project": {"path_with_namespace": f"{owner}/{rname}"},
                    "user": {"username": _assign} if _assign else {},
                }
                _pipe_headers = {"Content-Type": "application/json", "X-Gitcode-Event": "issue_opened"}
            # Issue #453：建单直发同步触发——payload 为内部构造，非 webhook 回流。
            # 该 issue 已入台账（建单入账），台账检查会命中并拦掉这次触发；
            # _grape_internal_trigger 标记让读侧跳过台账判定（同步触发是我们自己要开工，
            # 与 webhook 回流去重语义相反）。
            # 审查 P0-1 修正：run_pipeline 调用移回 if/else 之外（两平台共用）——
            # 原版本误缩进进 else（GitCode）分支，GitHub 建单后不再同步触发流水线。
            _pipe_payload["_grape_internal_trigger"] = True
            from src.domains.runtime.pipeline.event_pipeline import run_pipeline
            # 扫雷⑩ S3：create_task_via_issue 是 async 处理器，run_pipeline 同步
            # 全量执行会阻塞事件循环（建单卡顿最重处）——to_thread 卸载到线程池，
            # 语义保持同步等待完成（结果仍写入日志/返回链路）。
            # Issue #2132 B1：预热知识缓存的时延预算——在预算内等缓存就绪（让本轮
            # run_pipeline 的 collect_knowledge_files 命中缓存、少打 OBS），超预算就把
            # 预热留在后台继续跑，建单返回不被对象存储读取拖住（预算 env
            # KNOWLEDGE_WARM_BUDGET_SEC，默认 0.8s；预热失败仅告警，不影响建单主流程）。
            import asyncio as _aio2132
            try:
                from src.domains.runtime.engine.service import warm_knowledge_cache
                _warm = _aio2132.ensure_future(
                    _aio2132.to_thread(warm_knowledge_cache, repo_key,
                                       agent_type="", issue_id=f"{issue_num}"))
                # 预算内等待；超时不取消任务（wait_for 的取消对 to_thread 只是丢弃等待，
                # 线程继续跑；显式 shield 语义更直白，避免未来 warm 变协程后被真取消）
                try:
                    await _aio2132.wait_for(_aio2132.shield(_warm), timeout=_knowledge_warm_budget())
                except _aio2132.TimeoutError:
                    log.info("create_task: knowledge warm exceeded budget, continuing in background")
                _knowledge_warm_tasks.add(_warm)
                _warm.add_done_callback(_knowledge_warm_tasks.discard)
            except Exception as _we:
                log.debug(f"create_task: knowledge warm skipped: {_we}")
            _pipe_result = await asyncio.to_thread(
                run_pipeline, _pipe_json.dumps(_pipe_payload).encode(), _pipe_headers,
                account=repo_key, repo_key_override=repo_key)
            log.info(f"create_task: triggered pipeline for #{issue_num}, seq={_pipe_result.get('seq', 0)}")
        except Exception as _e:
            log.warning(f"create_task: sync pipeline trigger failed: {_e}")
    return {"ok": True, "issue_id": task_id, "task_id": task_id,
            "issue_number": issue_num, "created": created,
            "assignee": assignee, "is_subtask_mode": _auto_subtask_mode,
            "trigger_pipeline": bool(_trigger_pipeline),
            "message": ("任务已创建（挂单，未触发流水线）" if (created and not _trigger_pipeline)
                        else ("任务已创建，Issue 已同步" if created else "任务已创建（复用已有 Issue）"))}

_ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

# Issue #648：GitCode 同步评论是永久评论，不能写 7 天签名 URL（到期失效）；
# 同步时重新签发长效链接（10 年）作为永久可访问 URL 的过渡方案
_ATTACH_SYNC_URL_EXPIRES = 10 * 365 * 24 * 3600

_ATTACH_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html"}

_ATTACH_CODE_EXTS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sh", ".sql", ".tf", ".dockerfile"}

_ATTACH_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z"}

_ATTACH_ALL_EXTS = _ATTACH_IMAGE_EXTS | _ATTACH_DOC_EXTS | _ATTACH_CODE_EXTS | _ATTACH_ARCHIVE_EXTS

_ATTACH_MAX_SIZE = (int(os.environ.get("ATTACH_MAX_SIZE_MB", "200")) * 1024 * 1024
                    )  # 单个附件上限：默认 200MB，可用 ATTACH_MAX_SIZE_MB 覆盖（需同步 nginx client_max_body_size）

def _attach_obs_key(owner: str, repo: str, issue_number, filename: str) -> str:
    """生成 OBS 对象键：{PREFIX}/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"""
    from src.kernel.config import OBS_PREFIX
    prefix = (OBS_PREFIX or "").strip("/")
    return f"{prefix}/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"

def _attach_base_dir(owner: str, repo: str, issue_number) -> str:
    """附件本地存储目录（OBS 未启用时使用）：data/issue_attachments/{owner}/{repo}/{issue_number}/"""
    return os.path.join(WORKSPACE_DIR, "..", "data", "issue_attachments", owner, repo, str(issue_number))

def _attach_save(owner: str, repo: str, issue_number, filename: str, content: bytes) -> str:
    """保存附件。OBS 启用时上传到 OBS，否则写本地磁盘。返回可访问 URL。"""
    from src.domains.runtime.storage import get_storage
    storage = get_storage()
    if storage:
        key = _attach_obs_key(owner, repo, issue_number, filename)
        storage.save_bytes(key, content)
        url = storage.temporary_url(key, expires=7 * 24 * 3600)  # 7 天签名有效期
        log.info(f"Attachment saved to OBS: {key} ({len(content)} bytes)")
        return url
    # 本地降级
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    os.makedirs(attach_dir, exist_ok=True)
    fpath = os.path.join(attach_dir, filename)
    with open(fpath, "wb") as f:
        f.write(content)
    from src.domains.mgmt.public import get_browser_callback_url as _bcb
    _base_url = (_bcb() or "").rstrip("/")
    return f"{_base_url}/static/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"

def _attach_delete(owner: str, repo: str, issue_number, filename: str) -> bool:
    """删除单个附件。OBS 启用时删 OBS 对象，否则删本地文件。"""
    from src.domains.runtime.storage import get_storage
    storage = get_storage()
    if storage:
        key = _attach_obs_key(owner, repo, issue_number, filename)
        return storage.delete_object(key)
    fpath = os.path.join(_attach_base_dir(owner, repo, issue_number), filename)
    if os.path.isfile(fpath):
        os.unlink(fpath)
        return True
    return False

def _attach_count(owner: str, repo: str, issue_number) -> int:
    """Issue #926：仅统计附件数量（分页列表瘦身）。

    与 _attach_list 同存储口径，但跳过 URL 预签名与元数据 dict 构造。
    """
    from src.domains.runtime.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        items, _ = storage.list_objects_with_meta(prefix_key)
        return sum(1 for item in items if item["key"].rsplit("/", 1)[-1])
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    if os.path.isdir(attach_dir):
        return sum(1 for f in os.listdir(attach_dir)
                   if os.path.isfile(os.path.join(attach_dir, f)))
    return 0

def _attach_list(owner: str, repo: str, issue_number) -> list[dict]:
    """列出附件。返回 [{filename, url, size, is_image}]。"""
    from src.domains.runtime.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        items, _ = storage.list_objects_with_meta(prefix_key)
        from src.domains.mgmt.public import get_meta_value as _gmv  # 跨域走 owner public
        result = []
        for item in items:
            fname = item["key"].rsplit("/", 1)[-1]
            if not fname:
                continue
            ext = os.path.splitext(fname)[1].lower()
            result.append({
                "filename": fname,
                "url": storage.temporary_url(item["key"], expires=7 * 24 * 3600),
                "size": item.get("size", 0),
                "is_image": ext in _ATTACH_IMAGE_EXTS,
            })
        return result
    # 本地降级
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    # Issue #471 E1：附件 URL 是浏览器面地址——走 browser_callback_url
    from src.domains.mgmt.public import get_browser_callback_url as _bcb
    _base_url = (_bcb() or "").rstrip("/")
    result = []
    if os.path.isdir(attach_dir):
        for fname in sorted(os.listdir(attach_dir)):
            fpath = os.path.join(attach_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            result.append({
                "filename": fname,
                "url": f"{_base_url}/static/issue_attachments/{owner}/{repo}/{issue_number}/{fname}",
                "size": os.path.getsize(fpath),
                "is_image": ext in _ATTACH_IMAGE_EXTS,
            })
    return result

def _attachments_markdown_block(attachments) -> str:
    """Issue #648：把附件清单拼为「## 附件」markdown 链接清单（GitCode 同步过渡方案）。

    图片用 ![]()（可直接预览），其余用 []()（下载链接）；
    空清单/无效条目返回空串（调用方按需追加）。
    """
    lines = []
    for _att in attachments or []:
        if not isinstance(_att, dict):
            continue
        _fname = str(_att.get("filename") or "").strip()
        _url = str(_att.get("url") or "").strip()
        if not _fname or not _url:
            continue
        if _att.get("is_image"):
            lines.append(f"![{_fname}]({_url})")
        else:
            lines.append(f"[{_fname}]({_url})")
    if not lines:
        return ""
    return "\n\n## 附件\n" + "\n".join(lines)

def sync_issue_attachment_comment(repo_key: str, issue_number, attachment: dict) -> bool:
    """Issue #648 过渡方案：附件上传后向 VCS issue 同步「## 附件」链接清单评论。

    best-effort：token 获取 / VCS 调用失败仅记日志并返回 False，不影响上传主流程。
    仅支持 gitcode（GitHub 附件同步不在本次范围）。
    """
    try:
        from src.kernel.repo_key import parse_repo_key
        _prov, owner, rname = parse_repo_key(repo_key)
        # S74/D49：附件同步评论卡化——原 `if _prov != "gitcode": return
        # False` 把 gitee 等平台整族静默跳过；token 走 provider 感知策略链
        # （get_repo_token_with_refresh：devops_agent_session/仓库 bot/全局
        # bot/用户 token 四级），客户端按 provider 构造。
        _att = dict(attachment) if isinstance(attachment, dict) else {}
        _url = str(_att.get("url") or "")
        # Issue #648：传入 url 是 OBS 7 天签名 URL，GitCode 评论永久存在会到期失效；
        # OBS 后端启用时按对象键重新签发长效链接（本地降级路径 URL 本身永久，无需处理）
        if _url:
            try:
                from src.domains.runtime.storage import get_storage
                _storage = get_storage()
                if _storage:
                    # 文件名只能从 URL 的 **path** 里取：OBS 签名串（Signature=…）
                    # 本身可能含 "/"，按整串 rsplit 会把签名碎片当文件名，
                    # 重新签发出来的对象键不存在 → 评论链接 404（2026-11-06 实障）。
                    from urllib.parse import urlparse
                    _safe_name = urlparse(_url).path.rsplit("/", 1)[-1]
                    if _safe_name:
                        _att["url"] = _storage.temporary_url(
                            _attach_obs_key(owner, rname, issue_number, _safe_name),
                            expires=_ATTACH_SYNC_URL_EXPIRES)
            except Exception as e:
                log.warning(f"attachment sync comment long-url resign failed "
                            f"repo={repo_key} #{issue_number}: {e}")
        _md = _attachments_markdown_block([_att])
        if not _md:
            return False
        from src.domains.mgmt.public import get_repo_token_with_refresh  # 跨域走 owner public
        token = get_repo_token_with_refresh(repo_key) or ""
        if not token:
            log.warning(f"attachment sync comment skipped: no repo token repo={repo_key}")
            return False
        from src.domains.runtime.vcs.api_client import VcsApiClient
        client = VcsApiClient(_prov, token, owner, rname)
        client.post_issue_comment(_native_issue_number(issue_number), _md.strip())
        log.info(f"attachment sync comment posted: repo={repo_key} issue=#{issue_number}"
                 f" file={(attachment or {}).get('filename')}")
        return True
    except Exception as e:
        log.warning(f"attachment sync comment failed repo={repo_key} #{issue_number}: {e}")
        return False

def _attach_cleanup_dir(owner: str, repo: str, issue_number):
    """删除整个 issue 的附件目录。任务完成/取消时调用。"""
    from src.domains.runtime.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        count = storage.delete_prefix(prefix_key)
        if count > 0:
            log.info(f"Attachment cleanup: deleted {count} OBS objects for {owner}/{repo}/{issue_number}")
        return
    import shutil
    _attach_dir = _attach_base_dir(owner, repo, issue_number)
    if os.path.isdir(_attach_dir):
        shutil.rmtree(_attach_dir, ignore_errors=True)
        log.info(f"Attachment cleanup: removed {_attach_dir}")

def _user_image_ext(url: str, content_type: str = "") -> str:
    """从 URL 末段/Content-Type 推断图片扩展名（默认 .png）。"""
    last = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    ext = os.path.splitext(last)[1].lower()
    if ext in _ATTACH_IMAGE_EXTS:
        return ext
    ct_map = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
              "image/svg+xml": ".svg", "image/bmp": ".bmp"}
    return ct_map.get(content_type, ".png")

def _collect_issue_user_images(owner: str, repo: str, issue_number, texts, repo_key: str = "") -> dict:
    """提取 Issue 正文/评论中 raw.gitcode.com/user-images 图片链接并鉴权下载转存为平台附件。

    复用 _attach_save 存储语义（OBS 签名 URL 或 /static/issue_attachments/）。
    返回值 {"ok": [{"filename", "url", "size"}], "failed": [{"url", "reason"}]}，
    顶层异常不外抛（返回带 error 字段的降级结果）。
    """
    from src.domains.runtime.vcs.api_client import (extract_gitcode_user_image_urls, USER_IMAGE_MAX_COUNT)

    def _classify(u: str, content_type: str):
        return (None, _user_image_ext(u, content_type))

    return _collect_authed_attachments(
        owner, repo, issue_number, texts, repo_key,
        extract_urls=extract_gitcode_user_image_urls,
        download_method="download_user_image", max_count=USER_IMAGE_MAX_COUNT,
        classify=_classify, filename_prefix="user-image")


def _get_task_issue_info(task_id: int) -> tuple[str, int, str] | None:
    """从 tasks 表取 repo_key + issue_number + conversation_id（关联 agent_tasks 的键）。"""
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, conversation_id FROM issue WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return None
    return row[0], row[1], row[2] or ""

def _find_agent_task_ids(repo_key: str, issue_number) -> list[str]:
    """按 repo_key + issue_id 关联 agent_tasks（agent 执行产生的 task_messages 以此为外键）。

    issue_number 兼容 int / str：
    - int 56 或 '56' → 规范化为 '#56'（agent_tasks.issue_id 统一带 # 前缀存储）
    - '#56' → 原样使用
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo_key)
    iid = str(issue_number).strip()
    if not iid.startswith("#"):
        iid = "#" + iid
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT id FROM agent_tasks WHERE repo_key=? AND issue_id=? ORDER BY created_at DESC",
            (rk, iid))
        return [r[0] for r in c.fetchall()]

def _build_assignee_filter(assignees: str, clauses: list, params: list):
    """将逗号分隔的 assignees 转成 SQL 过滤（单值 =? / 多值 IN），就地追加到 clauses/params。"""
    _list = [a.strip() for a in (assignees or "").split(",") if a.strip()]
    if len(_list) == 1:
        clauses.append("assignee=?")
        params.append(_list[0])
    elif len(_list) > 1:
        clauses.append(f"assignee IN ({','.join('?' * len(_list))})")
        params.extend(_list)

def _build_creator_filter(creators: str, clauses: list, params: list):
    """Issue #805：将逗号分隔的 creators 转成 created_by SQL 过滤（单值 =? / 多值 IN）。

    与 _build_assignee_filter 并列：assignees 仍按责任人过滤（向后兼容），
    creators 按创建人过滤（与 GitCode issue 作者口径一致）。
    """
    _list = [a.strip() for a in (creators or "").split(",") if a.strip()]
    if len(_list) == 1:
        clauses.append("created_by=?")
        params.append(_list[0])
    elif len(_list) > 1:
        clauses.append(f"created_by IN ({','.join('?' * len(_list))})")
        params.extend(_list)

# ===================== Issue #2082（#2082 移植）：issues 组公共分页语义 =====================

# 分页上限：per_page 超上限夹取到 100（不 400，防 Agent 流水线因参数被拒中断，
# 与 milestones #900 / git-repo 组口径一致）
ISSUES_PER_PAGE_MAX = 100


def issues_default_per_page() -> int:
    """issues 组默认每页条数（灰度开关 ISSUES_DEFAULT_PER_PAGE，Issue #2082）。

    - 未设置 / 非法值 → 默认 20（终结默认全量倾泻）
    - 置 0 → 回退 legacy 语义（per_page 缺省 = 不分页，全量+终态截断）
    - 每次调用即时读 env：灰度切换无需重启，值域夹取 [0, ISSUES_PER_PAGE_MAX]
    """
    raw = (os.environ.get("ISSUES_DEFAULT_PER_PAGE") or "").strip()
    if not raw:
        return 20
    try:
        val = int(raw)
    except ValueError:
        return 20
    return max(0, min(ISSUES_PER_PAGE_MAX, val))


def resolve_issues_page_params(page, per_page) -> tuple[int, int]:
    """issues 组公共分页解析（list / mine 共用，Issue #2082 分页语义统一）。

    - page：1-based，缺省/非法 → 1
    - per_page：缺省 → 灰度默认（20，开关置 0 → legacy 全量）；
      0 = 显式全量（逃生口）；> ISSUES_PER_PAGE_MAX 夹取 100
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    if per_page is None:
        per_page = issues_default_per_page()
    else:
        try:
            per_page = int(per_page)
        except (TypeError, ValueError):
            per_page = issues_default_per_page()
    per_page = max(0, min(int(per_page), ISSUES_PER_PAGE_MAX))
    return page, per_page


def _batch_fetch_pr_issue_keys(keys: list) -> set:
    """批量查 events 表，返回 issue 本身是 PR 的 (repo_key, issue_id) 集合。

    跨仓库 A2A 场景中，agent 在目标仓库创建 PR（而非 Issue），
    issue 表存的 issue_number 实际是 PR 编号，前端链接应用 /pull/ 而非 /issues/。

    判定收紧（需求详情页标签误判修复）：PR 类事件挂 #N 有两种含义——
    ① 跨仓 A2A 建的"PR 形态需求"（issue_number 即 PR 号，无 issue 生命周期事件）；
    ② 普通真实 Issue 的交付关联（PR resolved/Closes #N，PR 事件只是来交付这个 issue 的，
    issue 侧同时存在 issue_opened 等生命周期事件）。
    ② 不算 PR——否则需求流转到待审核/已完成时详情页 Issue 行错标成 PR。
    只有"存在 PR 事件且从无 issue 生命周期事件"的编号才判为 ①。
    """
    from src.domains.runtime.vcs.event_types import ALL_PR_EVENTS, ALL_ISSUE_EVENTS
    _pr_types = tuple(ALL_PR_EVENTS)
    _issue_types = tuple(ALL_ISSUE_EVENTS)
    out: set = set()
    if not keys:
        return out
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
        _flat = [v for k in _chunk for v in k]
        try:
            with db_lock:
                c = _rt_db.cursor()
                _ph = ",".join("?" * len(_pr_types))
                c.execute(
                    f"SELECT DISTINCT repo_key, issue_id FROM events "
                    f"WHERE ({_ors}) AND event_type IN ({_ph})",
                    _flat + list(_pr_types))
                _pr_hits = {(row[0], row[1]) for row in c.fetchall()}
                if not _pr_hits:
                    continue
                # 排除②：同编号存在 issue 生命周期事件 → 真实 Issue，PR 只是交付关联
                _ph2 = ",".join("?" * len(_issue_types))
                _ors2 = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
                c.execute(
                    f"SELECT DISTINCT repo_key, issue_id FROM events "
                    f"WHERE ({_ors2}) AND event_type IN ({_ph2})",
                    _flat + list(_issue_types))
                _issue_hits = {(row[0], row[1]) for row in c.fetchall()}
                out |= (_pr_hits - _issue_hits)
        except Exception as e:
            log.warning(f"batch pr_issue_keys fetch failed: {e}")
    return out

def _batch_fetch_issue_state(keys: list) -> dict:
    """批量查 issue 单表的 stage/scene_id，返回 {(repo_key, issue_id): {"stage":..,"scene_id":..}}（防 N+1）。

    Phase C3：读 issue 卡片行（keys 中 issue_id 为 '#N'，转 issue_number）。
    """
    out: dict = {}
    if not keys:
        return out
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_number=?)"] * len(_chunk))
        _flat = []
        for _k in _chunk:
            _n = str(_k[1]).lstrip("#")
            _flat.extend([_k[0], _native_issue_number(_n) if str(_n or "").strip() else -1])
        try:
            with db_lock:
                c = _rt_db.cursor()
                c.execute("SELECT repo_key, issue_number, stage, scene_id FROM issue WHERE " + _ors, _flat)
                for row in c.fetchall():
                    out[(row[0], f"#{row[1]}")] = {"stage": row[2] or "", "scene_id": row[3] or ""}
        except Exception as e:
            log.warning(f"batch issue fetch failed: {e}")
    return out

def _batch_running_agent_tasks(keys: list) -> set:
    """批量查"正在运行"的 agent_tasks，返回 {(repo_key, issue_id)}（防 N+1，Issue #85 雷达动画依据）。

    口径（local-engine-busy-slots-zombie-fix-design.md §8）：与流水线点亮一致。
    认 `running` 与 `pending_self_report` 两种：
    - `running`：任务真正被认领执行；
    - `pending_self_report`：Issue #537 的主控停机中间态，scheduler 明确「视同 running」等待
      worker 自报（见 scheduler.py 的 poll 分支），此时事件仍是 running、流水线仍点亮。
      此前只认 running，导致这类需求卡片**流水线亮着但需求中心没有雷达**（用户侧表现为
      「执行中的卡片没有扫描效果」）。
    `queued`（排队未认领/事件 pending）与 `dispatched`（历史 pull 语义、事件未同步 running）
    仍不算，避免「任务仅排队但雷达已在扫描」的反向不一致。
    """
    out: set = set()
    if not keys:
        return out
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
        _flat = [v for k in _chunk for v in k]
        try:
            with db_lock:
                c = _rt_db.cursor()
                c.execute(
                    "SELECT repo_key, issue_id FROM agent_tasks "
                    "WHERE status IN ('running','pending_self_report') AND (" + _ors + ")", _flat)
                out.update((row[0], row[1]) for row in c.fetchall())
        except Exception as e:
            log.warning(f"batch agent_tasks fetch failed: {e}")
    return out

# P1-8：事件状态展示别名 —— events.status='takeover'（本地接手）在 agent_statuses 里
# 归一成 'running'，与流水线卡片（compute_node_statuses / EVENT_STATUS_MAP 的
# takeover→STATUS_RUNNING）同口径；原始值仍以 raw_status 透出。
_EVENT_STATUS_ALIASES = {"takeover": "running"}


def _batch_fetch_agent_statuses(keys: list) -> dict:
    """批量查各 Agent 节点最新事件状态（Issue #126: 与菜单流水线数据源一致）。

    从 events 表查每个 (repo_key, issue_id) 下各 agent_type 的最新事件 status，
    与 pipeline.py 的 _get_issue_pipeline 使用相同的 AGENT_ORDER + STAGE_STATUS_MAP 逻辑。

    Returns:
        {(repo_key, issue_id): [{agent, label, status, status_num, latest_seq, latest_title}, ...]}
    """
    from src.domains.runtime.routes.pipeline import AGENT_ORDER, AGENT_LABELS, STAGE_STATUS_MAP
    from src.api.pipeline_status import DEDUP_LEFTOVER_STATUSES

    out: dict = {}
    if not keys:
        return out

    # 批量查询 events（分 chunk 防 SQL 过长）
    _all_rows = []
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
        _flat = [v for k in _chunk for v in k]
        try:
            with db_lock:
                c = _rt_db.cursor()
                c.execute(
                    "SELECT repo_key, issue_id, agent_type, status, seq, title "
                    "FROM events WHERE " + _ors + " ORDER BY seq ASC",
                    _flat
                )
                _all_rows.extend(c.fetchall())
        except Exception as e:
            log.warning(f"batch agent_statuses fetch failed: {e}")

    # 按 (repo_key, issue_id) 分组
    _events_by_key: dict = {}
    for row in _all_rows:
        _key = (row[0], row[1])
        _events_by_key.setdefault(_key, []).append(row)

    # 为每个 key 构建 per-agent 状态列表
    for key in keys:
        _issue_events = _events_by_key.get(key, [])
        _statuses = []
        for i, agent in enumerate(AGENT_ORDER):
            # row: (repo_key, issue_id, agent_type, status, seq, title)
            # Issue #3444：status='skipped'/'deduped' 为去重留痕（真实 agent_type 仅
            # 供审计），取「最新事件」时排除，避免覆盖 running/completed 的真实状态。
            _agent_events = [
                e for e in _issue_events
                if e[2] == agent and str(e[3] or "") not in DEDUP_LEFTOVER_STATUSES]
            if not _agent_events:
                _statuses.append({
                    "agent": agent,
                    "label": AGENT_LABELS[i],
                    "status": "pending",
                    "status_num": 0,
                    "latest_seq": None,
                    "latest_title": "",
                })
            else:
                _latest = _agent_events[-1]  # seq ASC → 最后一条即最新
                _raw_status = _latest[3] or "pending"
                # P1-8：events.status='takeover'（有人本地接手）在展示口径上等同 running。
                # 流水线卡片侧（compute_node_statuses / EVENT_STATUS_MAP）早已映射成运行中，
                # 这里若原样透传，同一节点会在同页出现「执行中」与「待执行」两种结论。
                _status = _EVENT_STATUS_ALIASES.get(_raw_status, _raw_status)
                _row = {
                    "agent": agent,
                    "label": AGENT_LABELS[i],
                    "status": _status,
                    "status_num": STAGE_STATUS_MAP.get(_status, 0),
                    "latest_seq": _latest[4],
                    "latest_title": _latest[5] or "",
                }
                if _raw_status != _status:
                    # 保留原始口径，供需要区分「本地接手」与「worker 在跑」的调用方使用
                    _row["raw_status"] = _raw_status
                    _row["takeover"] = True
                _statuses.append(_row)
        out[key] = _statuses

    return out

def get_handoff_targets(repo_key: str, issue_number: int, current_scene_id: str = "") -> dict:
    """Issue #337: 计算某需求「可移交目标流水线」列表（列表模式详情移交入口数据源）。

    - 数据源：`get_scenes(repo_key)`（与 /scenes/with-pipeline 同源；仓库作用域即权限过滤，
      调用方仍需先 `check_repo_access`）。
    - 过滤：仅保留含 vod 节点的场景（与 _cross_pipeline_handoff 目标节点约定一致）。
      Issue #450：不再排除当前流水线，目标列表包含当前场景自身（支持同流水线内移交接续）。
    - 返回 {targets: [{id, name, display_name, description, work_branch, scene_type}],
            current_scene_id}。空列表照常返回（前端展示「无可用目标」，非静默失败）。
    """
    from src.domains.mgmt.public import get_scenes  # 跨域走 owner public
    _cur = (current_scene_id or "").strip() or "default"
    targets = []
    try:
        for s in get_scenes(repo_key=repo_key):
            _sid = s.get("id") or ""
            if not _sid:
                continue
            _nodes = s.get("nodes") or []
            _has_vod = any((n.get("id") or n.get("tag")) == "vod" for n in _nodes)
            if not _has_vod:
                continue
            targets.append({
                "id": _sid,
                "name": s.get("name") or _sid,
                "display_name": s.get("display_name") or s.get("name") or _sid,
                "description": s.get("description") or "",
                "work_branch": s.get("work_branch") or "main",
                "scene_type": s.get("scene_type") or "standard",
            })
    except Exception as _e:
        log.warning(f"get_handoff_targets failed repo={repo_key} issue=#{issue_number}: {_e}")
        targets = []
    return {"targets": targets, "current_scene_id": _cur}

def _query_tasks(where: str, params: list, order_by: str, include_user_id: bool = False,
                 limit: int = None, offset: int = 0) -> list[dict]:
    """查询 tasks 并富化（scene / stage / is_running / is_pr）为对外 dict（my_tasks / repo_all_tasks 共享）。

    - where / params / order_by 由调用方构造（列与排序方向经白名单，值参数化，防注入）
    - include_user_id: 结果是否包含 user_id（repo_all 需要）
    - limit/offset（Issue #542）：可选真分页（None=不限，返回全部）
    - issue_state / agent_tasks 均批量查询，避免逐任务 N+1
    - is_pr: 批量查 events 表 event_type 判断是否为 PR 事件（跨仓库 A2A 场景 issue_number 实际是 PR 编号）
    """
    _select = (
        "SELECT id, assignee, repo_key, issue_number, title, task_description, conversation_id, "
        "status, completed_reason, created_at, completed_at, is_subtask_mode, parent_issue_number, parent_issue_id, created_by, org_id"
        + (", user_id" if include_user_id else "")
        + ", vcs_exists, vcs_checked_at"
    )
    _vcs_idx = 16 + (1 if include_user_id else 0)
    _limit_sql = ""
    if limit is not None and int(limit) > 0:
        _limit_sql = f" LIMIT {int(limit)} OFFSET {int(offset)}"
    with db_lock:
        c = _rt_db.cursor()
        try:
            c.execute(f"{_select} FROM issue WHERE {where} ORDER BY {order_by}{_limit_sql}", params)
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"Task query failed: {e}")
            rows = []
    if not rows:
        return []

    from src.kernel.repo_key import to_repo_key, from_repo_key
    from src.domains.mgmt.public import get_pipeline_config  # 跨域走 owner public

    _keys = [(r[2] or "", f"#{r[3]}") for r in rows]
    _stage_map = _batch_fetch_issue_state(_keys)
    _running = _batch_running_agent_tasks(_keys)
    _agent_status_map = _batch_fetch_agent_statuses(_keys)
    _pr_issue_keys = _batch_fetch_pr_issue_keys(_keys)

    # Issue #415: 仓库默认场景缓存（无 Issue 级场景时展示回退用，managed_repos.dev_scene）
    _repo_default_scene: dict[str, str] = {}
    for idx, r in enumerate(rows):
        _rk = _keys[idx][0]
        if _rk in _repo_default_scene:
            continue
        try:
            from src.domains.mgmt.public import get_repo_scene  # 跨域走 owner public
            _repo_default_scene[_rk] = get_repo_scene(_rk) or ""
        except Exception:
            _repo_default_scene[_rk] = ""

    # P1-B: Pre-resolve scene names in batch (loop-out).
    # Collect unique (scene_id, repo_key) pairs and resolve once, instead of
    # calling get_pipeline_config per-card (348× → 2-5× with cache).
    _scene_name_cache: dict[tuple, str] = {}
    for idx, r in enumerate(rows):
        _rk = _keys[idx][0]
        _st = _stage_map.get((_rk, _keys[idx][1]))
        _issue_scene_id = (_st.get("scene_id") or "") if _st else ""
        if not _issue_scene_id:
            _issue_scene_id = _repo_default_scene.get(_rk, "")  # 展示回退：仓库默认场景
        _scene_key = (_issue_scene_id, _rk)
        if _scene_key not in _scene_name_cache:
            _scene_name = ""
            try:
                _cfg = get_pipeline_config(_issue_scene_id, repo_key=_rk)
                if _cfg:
                    _scene_name = _cfg.get("display_name") or _cfg.get("name", _issue_scene_id)
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")
            _scene_name_cache[_scene_key] = _scene_name

    tasks_out = []
    for idx, r in enumerate(rows):
        _rk, _issue_id = _keys[idx]
        _st = _stage_map.get((_rk, _issue_id))
        _issue_scene_id = (_st.get("scene_id") or "") if _st else ""
        _stage = (_st.get("stage") or "") if _st else ""
        if not _stage:
            # Issue #2111（#1214/#1999 遗留收敛，#1214 移植）：/mine 出口空值归一——建档/阶段推进
            # 窗口 issue 表该列可为 NULL/空串，原样透传（""）与 detail/list 出口
            # 「恒非空 vod」口径分歧；与 DB 默认值一致归一并打 warning 保留可观测性。
            log.warning(f"issues: issue #{r[3]} stage empty from issue table "
                        f"(mine/query_tasks exit), normalized to 'vod'")
            _stage = "vod"
        if _issue_scene_id:
            _scene_id = _issue_scene_id
            _scene_source = "vod"  # VOD 识别
        else:
            # 无 Issue 级场景 → 仓库默认场景（Issue #415：回退展示，避免详情页显示 --）
            _scene_id = _repo_default_scene.get(_rk, "")
            _scene_source = "repo_default"
        _scene_name = _scene_name_cache.get((_scene_id, _rk), "")
        _has_running_agent = bool(r[3]) and _stage in ("vod", "issue", "coding", "qa", "sre") and ((_rk, _issue_id) in _running)
        # Issue #2072: 数值形态的字符串编号归一为 int（与 DB 列/Pydantic 模型/其他端点一致）。
        # 但不能无条件 int()——gitee 等平台的 issue_number 是字母 ident（如 IKSQ4O），
        # 强转会抛 ValueError 把整页打成 500。统一走 _native_issue_number：
        # 数字→int，非数字 ident→原样 str（仓库既有一等数据形态，见 db_setup 的
        # 「非数字 ident 按 text 原样存储」与 event_pipeline 的写入口径）。
        _issue_number = _native_issue_number(r[3])
        item = {
            "id": r[0], "assignee": r[1], "repo_key": r[2], "issue_number": _issue_number,
            "title": r[4] or "", "task_description": r[5] or "",
            "conversation_id": r[6] or "", "status": r[7],
            "completed_reason": r[8] or "", "created_at": normalize_event_time(r[9]), "completed_at": normalize_event_time(r[10]),
            "is_subtask_mode": r[11] or 0,
            "parent_issue_number": r[12] or 0,
            "parent_issue_id": r[13] or "",
            # Issue #805: 创建人（issue.created_by，建档固化，不随 assignee 流转变化）
            "creator": r[14] or "",
            # 组织需求/子单所属组织：需求中心据此加载组织下派详情。
            "org_id": r[15] or "",
            "scene_id": _scene_id,
            "scene_name": _scene_name,
            "scene_source": _scene_source,
            "stage": _stage,
            "is_running": _has_running_agent,
            "agent_statuses": _agent_status_map.get((_rk, _issue_id), []),
            "is_pr": (_rk, _issue_id) in _pr_issue_keys,
            # Issue #995：VCS 存在性（默认 1=未知/存在；0=VCS 侧已删除）；未知列值按存在处理。
            "vcs_exists": bool(r[_vcs_idx]) if r[_vcs_idx] is not None else True,
            "vcs_checked_at": r[_vcs_idx + 1] or "",
            # Issue #995 spec 验收面（2026-11-13 补齐）：存在性核验结果语义字段。
            # - vcs_check：ok=已核验存在 / missing=已核验删除（仅审计模式可见）
            #   / unavailable=未核验（索引失败/后台未跑，fail-open 按存在处理）
            # - is_vcs_deleted：vcs_check=missing 的布尔投影（前端过滤/标记）
            "is_vcs_deleted": (r[_vcs_idx] == 0 and bool(r[_vcs_idx + 1])),
            "vcs_check": ("missing" if (r[_vcs_idx] == 0 and r[_vcs_idx + 1])
                          else ("ok" if r[_vcs_idx + 1] else "unavailable")),
        }
        if include_user_id:
            item["user_id"] = r[16] or 0
        tasks_out.append(item)
    return tasks_out

from src.domains.runtime.vcs.api_client import _native_issue_number  # S73：issue ident 平台原生形态

def _mirror_repo_keys(repo_key: str) -> "list[str]":
    """查找同仓多平台镜像 repo_key 全集（S97/D72 仓库能力抽象）。

    managed_repos 表中同一 owner/repo 可能同时存在多平台记录
    （gitcode:/github:/gitee:/gitlab:…），通过 full_name 匹配、平台卡集合
    界定候选前缀。返回除 repo_key 自身外的全部镜像键（顺序稳定）。
    找不到返回空表。
    """
    try:
        from src.kernel.repo_key import split_owner_repo, repo_key_candidates
        from src.domains.mgmt import public as _mgmt_public_mirror
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            return []
        full = f"{owner}/{repo}"
        cands = [c for c in repo_key_candidates(full) if c != repo_key]
        if not cands:
            return []
        # managed_repos 归 mgmt：候选键求交集，块内 ORDER BY repo_key 与原语句一致
        return _mgmt_public_mirror.repo_keys_existing_any(cands)
    except Exception as e:
        log.warning(f"find mirrors for {repo_key} failed: {e}")
        return []

def _notify_done(repo_key: str, issue_number: int, text: str) -> None:
    """完成通知（A4）：扇出到该用户全部已连接渠道；异常不影响结单主流程。"""
    try:
        from src.domains.runtime.im.notify import notify_issue_event
        notify_issue_event(repo_key, issue_number or 0, "done", text)
    except Exception as _e:
        log.warning(f"[im-notify] done notify failed {repo_key}#{issue_number}: {_e}")

def _is_auto_transferred_attachment(name: str) -> bool:
    """判断附件名是否由平台自动转存（评论媒体/编辑器图片）而来。"""
    if not name:
        return False
    return name.startswith(_AUTO_TRANSFER_PREFIXES)

def _cleanup_auto_transferred_attachments(repo_key: str, issue_number) -> int:
    """Issue #1748：回收平台自动转存的评论媒体/编辑器图片，保留用户手工附件。

    issue 确认完成/取消时调用。单条评论的自动转存副本对已关闭 issue 无复用价值，
    但用户手工上传的附件（可能是交付依据/证据）不能删，故不做整目录清理。
    异常一律吞掉，清理失败不影响关闭流程；返回删除数量。
    """
    removed = 0
    try:
        from src.kernel.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        if not _owner or not _rname:
            return 0
        _num = str(issue_number)
        from src.domains.runtime.storage import get_storage
        storage = get_storage()
        if storage:
            # Issue #1748 复审：OBS 模式下 list_keys + 每对象 delete_object 都是网络往返
            # （N 个文件 ≈ N+1 RTT），不能挂在「关闭/取消 issue」的同步链路上 →
            # 丢给后台线程执行，接口立即返回（清理失败只丢日志，不影响业务）。
            def _reap_obs() -> None:
                _n = 0
                try:
                    for _key in storage.list_keys(_attach_obs_key(_owner, _rname, _num, "")):
                        if _is_auto_transferred_attachment(os.path.basename(str(_key))):
                            if storage.delete_object(_key):
                                _n += 1
                except Exception as _e:
                    log.warning(f"Attachment cleanup (obs, async) failed: {_e}")
                if _n:
                    log.info(f"Attachment cleanup (auto-transferred): {_n} object(s) "
                             f"for {_owner}/{_rname}/{_num}")
            import threading
            threading.Thread(target=_reap_obs, name="attach-reap", daemon=True).start()
            return 0
        _dir = _attach_base_dir(_owner, _rname, _num)
        if os.path.isdir(_dir):
            for _name in os.listdir(_dir):
                if not _is_auto_transferred_attachment(_name):
                    continue
                try:
                    os.remove(os.path.join(_dir, _name))
                    removed += 1
                except OSError as _e:
                    log.warning(f"Attachment cleanup: remove {_name} failed: {_e}")
            if removed:
                log.info(f"Attachment cleanup (auto-transferred): {removed} file(s) in {_dir}")
    except Exception as _e:
        log.warning(f"Auto-transferred attachment cleanup failed: {_e}")
    return removed

def cancel_issue_rows(c, repo_key: str, issue_number: int) -> int:
    """Issue #1741：把同一 repo_key+issue_number 的可取消行统一置为终态取消。

    评论取消路径（cancel_issue_task）与 cancel 端点共用，避免跨层重复实现；
    completed/cancelled 行保持终态不变，重复取消不再刷新 completed_at（幂等）。
    """
    c.execute(
        "UPDATE issue SET status='cancelled', completed_reason='user_cancelled', completed_at=datetime('now') "
        "WHERE repo_key=? AND issue_number=? AND status IN (?,?,?,?)",
        (repo_key, issue_number, *_CANCELLABLE_ISSUE_STATUSES))
    return c.rowcount

def _create_issue_unified(api_token: str, provider: str, owner: str, repo: str,
                          title: str, body_text: str = "", assignee: str = "",
                          timeout: int = CREATE_HTTP_TIMEOUT) -> dict:
    """D35/S55：建单统一入口——VcsApiClient.create_issue（卡驱动路径 + 方言
    assignee 语义，gitee 自动正确）；存量平台行为由内置卡对齐（github token 头
    + JSON、gitcode form）。供手工建单链与 onboarding 共用，_post_github_issue /
    _post_gitcode_issue 的分派收敛至此。失败抛 RuntimeError（调用方 403/422
    友好化与 401 刷新重试逻辑语义保持）。

    Issue #3349（架构守护条件 3 整改）：``timeout`` 接线建单 POST 的 socket
    超时（默认 ``CREATE_HTTP_TIMEOUT``，spec §4.2），经 VcsApiClient.create_issue
    透传到统一请求入口——此前该常量仅测试引用，生产路径实际走 urllib 默认 30s，
    与 spec 声明的 15s 不符。
    """
    from src.domains.runtime.vcs.api_client import VcsApiClient
    try:
        resp = VcsApiClient(provider, api_token, owner, repo).create_issue(
            title, body_text, assignee=assignee, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"create issue failed: {e}")
    if not isinstance(resp, dict) or "error" in resp:
        raise RuntimeError(f"create issue failed: {str(resp)[:300]}")
    return resp

def _collect_authed_attachments(owner: str, repo: str, issue_number, texts, repo_key: str, *,
                                extract_urls, download_method: str, max_count: int,
                                classify, filename_prefix: str, use_uuid: bool = False,
                                prefilter=None, analyzer=None) -> dict:
    """Issue #315/#1748 共用采集编排：提取链接 → 鉴权下载 → 白名单校验 → 转存平台附件。

    参数化差异：链接提取函数、下载方法、类型/扩展名判定、文件名策略、预过滤、分析器。
    token 仅服务端构造请求头使用，不写入日志/注入上下文/返回值。
    顶层异常不外抛（返回带 error 字段的降级结果），绝不阻塞 prompt 组装主流程。
    """
    from src.kernel.repo_key import parse_repo_key as _prk
    _provider = _prk(repo_key or f"gitcode:{owner}/{repo}")[0]
    result: dict = {"ok": [], "failed": []}
    try:
        urls: list[str] = []
        for t in texts or []:
            if not t:
                continue
            for u in extract_urls(str(t)):
                if u not in urls:
                    urls.append(u)
        if not urls:
            return result
        # 数量上限：超出部分降级说明（不静默丢弃）
        for u in urls[max_count:]:
            result["failed"].append({"url": u, "reason": "exceeds count limit"})
        urls = urls[:max_count]

        repo_key = repo_key or f"gitcode:{owner}/{repo}"
        token = ""
        try:
            from src.domains.mgmt.public import get_gitcode_token
            token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            token = ""
        if not token:
            for u in urls:
                result["failed"].append({"url": u, "reason": "no repo token"})
            return result

        import uuid
        from src.domains.runtime.vcs.api_client import create_client
        client = create_client(repo_key, token)
        seq = 0
        for u in urls:
            if prefilter is not None and not prefilter(u):
                log.info(f"{filename_prefix} ignored (non-media): {owner}/{repo}/{issue_number}")
                continue
            dl = getattr(client, download_method)(u)
            if "error" in dl:
                result["failed"].append({"url": u, "reason": str(dl["error"])})
                continue
            content_type = str(dl.get("content_type") or "")
            info = classify(u, content_type)
            if not info:
                log.info(f"{filename_prefix} ignored (unsupported type): "
                         f"{owner}/{repo}/{issue_number} ct={content_type or 'unknown'}")
                continue
            kind, ext = info
            if use_uuid:
                # MR !1145 Informational-2：#1748 也用固定前缀（user-attachment-<uuid>），
                # 让「自动转存」可仅凭前缀判定，避免"32 hex + 媒体扩展名"启发式误删
                # 恰好同形的用户手工附件。
                filename = f"{filename_prefix}-{uuid.uuid4().hex}{ext}"
            else:
                seq += 1
                filename = f"{filename_prefix}-{seq}{ext}"
            try:
                url = _attach_save(owner, repo, issue_number, filename, dl["content"])
                item = {"filename": filename, "url": url,
                        "size": dl.get("size", len(dl["content"]))}
                analysis = ""
                if analyzer is not None and kind in ("image", "video"):
                    analysis = analyzer(kind, dl["content"], ext)
                if kind is not None:
                    item["kind"] = kind
                    item["source_url"] = u
                if analyzer is not None:
                    item["analysis"] = analysis
                result["ok"].append(item)
                log.info(f"{filename_prefix} saved: {owner}/{repo}/{issue_number}/{filename} "
                         f"({dl.get('size', 0)} bytes, kind={kind}, analyzed={bool(analysis)})")
            except Exception as e:
                result["failed"].append({"url": u, "reason": f"save failed: {e}"})
    except Exception as e:
        result["error"] = str(e)
        log.warning(f"_collect_authed_attachments failed: {owner}/{repo}/{issue_number} -> {e}")
    return result

def _analyze_media_attachment(kind: str, content: bytes, ext: str = "") -> str:
    """Issue #1748：复用现有图片/视频理解能力分析已下载的评论媒体附件。

    返回带「## 用户上传的图片/视频分析」标题的注入文本；分析不可用时返回空串。
    分析模块缺失/异常一律降级为空串，由上层注入明确原因，不阻塞主流程。
    """
    try:
        if kind == "image":
            from src.domains.runtime.agents.image_understanding import understand_image_from_bytes
            return understand_image_from_bytes(content, ext or ".png")
        if kind == "video":
            from src.domains.runtime.agents.image_understanding import understand_video_from_bytes
            text = understand_video_from_bytes(content, ext or ".mp4")
            if text and "## 用户上传的视频分析" not in text:
                text = f"## 用户上传的视频分析\n{text}\n\n## 请基于以上视频信息回答用户问题"
            return text
    except Exception as e:
        log.warning(f"_analyze_media_attachment failed ({kind}): {e}")
    return ""

def _collect_issue_media_attachments(owner: str, repo: str, issue_number, texts,
                                     repo_key: str = "", analyze: bool = True) -> dict:
    """Issue #1748：提取评论中的 user-attachments 媒体附件并鉴权下载转存为平台附件。

    评论附件直链需登录态（匿名 401），且事件注入原本不含媒体分析，形成双重断链。
    本函数在事件构建期用服务端凭证下载，经 `_attach_save` 转存为平台附件，
    返回无 401 的读取通道 URL，供上层注入媒体分析段落。

    Spec 限定 media-only：非媒体扩展名预过滤、未知类型下载后按 Content-Type 再判，
    非图片/视频一律不落盘不注入；扩展名仅接受媒体白名单并以 UUID 命名。
    返回值顶层异常不外抛（带 error 字段），绝不阻塞 prompt 组装主流程。
    """
    from src.domains.runtime.vcs.api_client import (
        extract_gitcode_user_attachment_urls, USER_ATTACHMENT_MAX_COUNT,
        attachment_kind, _attachment_ext, is_media_attachment_url,
    )

    def _classify(u: str, content_type: str):
        kind = attachment_kind(u, content_type)
        ext = _attachment_ext(u, content_type, kind)
        if kind not in ("image", "video") or not ext:
            return None
        return (kind, ext)

    return _collect_authed_attachments(
        owner, repo, issue_number, texts, repo_key,
        extract_urls=extract_gitcode_user_attachment_urls,
        download_method="download_user_attachment", max_count=USER_ATTACHMENT_MAX_COUNT,
        classify=_classify, filename_prefix="user-attachment", use_uuid=True,
        prefilter=is_media_attachment_url,
        analyzer=_analyze_media_attachment if analyze else None)

"""需求中心（Issues Center）领域逻辑（issues_center）。

从 src/domains/runtime/routes/tasks.py 拆分（tasks-module-refactor-and-task-id-disambiguation-design.md）：
- issue 建档/状态流转/VCS 创建/附件/任务对话/平台映射/需求列表查询等「需求中心」领域函数集中于此，
- routes/tasks.py 只保留「执行实例」动作（complete/current），其他路由与业务模块均从本模块导入，
- 本模块为叶子：**不得 import routes.tasks / routes.issues**（防环形依赖）。
"""

# Issue #1748 补充：平台自动转存媒体的文件名形态——#315 = user-image-<n>.<ext>；
# #1748 = <uuid32>.<媒体扩展名>（UUID 命名由 spec 约定）。只回收这两类，
# 用户手工上传的附件一律保留。
_AUTO_TRANSFER_PREFIXES = ("user-image-", "user-attachment-")

_CANCELLABLE_ISSUE_STATUSES = ("active", "processing", "awaiting_input", "pending_review")
