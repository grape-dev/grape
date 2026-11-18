"""OAuth 流程与账号生命周期守卫组件（Issue #3054 P3）。

路由（routes/grape_auth.py）只做参数整形与调用，业务规则收敛在本模块的
小类里，避免在 2600+ 行的 handler 里堆逻辑/裸 SQL：

- ``OAuthStateStore``        非 OAuth state 的 nonce 绑定/消费（多流程并存，只清自己）；
                            外加 state 的 peep/consume 门面（先比 provider 再 DELETE）
- ``LoginThrottlePolicy``    与 ``/login`` 同口径的限流/失败计数/成功清理；
- ``UnbindGuard``            原子「至少保留一种登录方式」解绑 + 拒绝态错误映射；
- ``AccountDeactivationCascade`` 注销级联（账号 status=3 + 身份行失效/清 token +
                            token_version 回收）与状态→错误码映射；
- ``ChannelGate``            回调/关联/注册侧的渠道 enabled 复查。

三库边界：本模块只读写 mgmt 自有表（grape_accounts / external_identities /
oauth_states / login_failures）与渠道开关读路径，不新增任何 runtime 域 SQL。
"""
from __future__ import annotations

from fastapi import HTTPException

from src.kernel.config import log


class OAuthStateStore:
    """登录流程 nonce cookie 的多槽管理与 state 消费门面。

    P3-4：原实现是全局单槽 cookie（后发覆盖）且 mismatch 无条件 delete_cookie
    ⇒ 双标签页/外部链接可以把登录打挂（线上 state_invalid）。现在：
    - cookie 值 = ``nonce1.nonce2...``（保留最近 ``MAX_SLOTS`` 个）；
    - 校验只判「本 state 的 nonce 是否在集合内」，mismatch **不动 cookie**；
    - 消费只摘掉自己那一槽，其他人的 nonce 原样保留。
    """

    COOKIE_NAME = "oauth_nonce"
    MAX_SLOTS = 5
    MAX_AGE = 600

    # ── cookie 读写 ───────────────────────────────────────────────
    @classmethod
    def tokens(cls, request) -> "list[str]":
        """读取 cookie 中的全部 nonce（容错：无 request/无 cookie → 空列表）。"""
        raw = ""
        if request is not None:
            try:
                raw = request.cookies.get(cls.COOKIE_NAME, "") or ""
            except Exception:
                raw = ""
        return [t for t in raw.split(".") if t]

    @classmethod
    def _attrs(cls) -> dict:
        from src.domains.mgmt.auth.security_config import is_production

        return {"max_age": cls.MAX_AGE, "httponly": True, "samesite": "lax",
                "secure": is_production(), "path": "/"}

    @classmethod
    def add(cls, response, request, nonce: str) -> None:
        """发起登录：追加 nonce（不覆盖别人的槽）。"""
        if not nonce:
            return
        tokens = [t for t in cls.tokens(request) if t != nonce]
        tokens.append(nonce)
        tokens = tokens[-cls.MAX_SLOTS:]
        response.set_cookie(key=cls.COOKIE_NAME, value=".".join(tokens), **cls._attrs())

    @classmethod
    def consume(cls, response, request, nonce: str) -> None:
        """只摘掉本次已消费的 nonce；集合空了才删 cookie。"""
        tokens = [t for t in cls.tokens(request) if t != nonce]
        if tokens:
            response.set_cookie(key=cls.COOKIE_NAME, value=".".join(tokens), **cls._attrs())
        else:
            response.delete_cookie(cls.COOKIE_NAME, path="/")

    @classmethod
    def nonce_ok(cls, request, state_nonce: str) -> bool:
        """state 的 nonce 是否命中 cookie 集合（防 login CSRF）。"""
        if not state_nonce:
            return False
        return state_nonce in cls.tokens(request)

    # ── state 消费门面（P3-5：先比 provider 再 DELETE，可 peep） ──
    @classmethod
    def peek(cls, state: str, provider: str = "") -> "dict | None":
        """只读校验 state（不消费）——校验密码等前置失败后仍可原地重试。"""
        return cls.validate(state, provider, consume=False)

    @classmethod
    def consume_state(cls, state: str, provider: str = "") -> "dict | None":
        """原子消费 state（一次性）；provider 不符时**不消费**并返回 None。"""
        return cls.validate(state, provider, consume=True)

    @classmethod
    def validate(cls, state: str, provider: str = "", consume: bool = True) -> "dict | None":
        """state 校验/原子消费的唯一实现（P3-5）。

        语义：
        - 先比 provider 再 DELETE——错平台回调/双标签页串味不会把合法 state 烧掉；
        - `consume=False` 只读校验（关联/注册在校验密码前 peep，成功后才真正消费）；
        - 原子性靠 `DELETE ... WHERE state=? AND expires>?` 的 rowcount（MySQL 多实例下
          db_lock 只在本进程内有效，不能靠 `SELECT ... FOR UPDATE` 的方言差异）。
        返回 dict 额外带 `_provider`（从 DB 记录恢复）。
        """
        import json as _json
        import time as _time

        from src.domains.mgmt.user import db as user_db

        try:
            with user_db.db_lock:
                c = user_db._mg_db.cursor()
                c.execute(
                    "SELECT provider, data_json, expires FROM oauth_states WHERE state=?",
                    (state,),
                )
                row = c.fetchone()
            if not row:
                log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
                return None
            db_provider = row[0]
            if provider and provider != db_provider:
                log.warning(
                    f"validate_oauth_state: provider mismatch (expected={provider}, db={db_provider})")
                return None
            if _time.time() > (row[2] or 0):
                log.warning(f"validate_oauth_state: state={state[:20]}... expired")
                return None
            if consume:
                with user_db.db_lock:
                    c = user_db._mg_db.cursor()
                    c.execute(
                        "DELETE FROM oauth_states WHERE state=? AND expires > ?",
                        (state, _time.time()),
                    )
                    consumed = c.rowcount
                    user_db._mg_db.commit()
                if consumed == 0:
                    log.warning(
                        f"validate_oauth_state: state={state[:20]}... already consumed or expired")
                    return None
            data = _json.loads(row[1] or "{}")
            data["_provider"] = db_provider
            return data
        except Exception as e:
            log.warning(f"validate_oauth_state failed: {e}")
            return None


class LoginThrottlePolicy:
    """``/login`` 同口径的限流策略（P3-2）。

    原 ``associate`` 直调 ``verify_grape_account``，没有 ``check_throttle`` /
    ``record_failure`` ⇒ 被锁账号可用「错几次 + 正确密码走 associate」拿 JWT。
    这里把「检查→计数→成功清理」收敛成一条口径，``/login`` 与 OAuth 的三个
    写入口共用同一张 ``login_failures`` 表与阈值。
    """

    @staticmethod
    def locked_detail(throttle: dict) -> dict:
        """把 ``check_throttle`` 的拒绝结果转成 HTTP 429 detail（与 /login 逐字段一致）。"""
        detail = {"code": 429,
                  "error_code": throttle.get("error_code", ""),
                  "message": throttle.get("message", "")}
        if "email_configured" in throttle:
            detail["email_configured"] = throttle["email_configured"]
        if throttle.get("captcha"):
            detail["captcha"] = True
        return detail

    @classmethod
    def check_or_raise(cls, identifier: str, ip: str) -> None:
        """被限流就抛 429（含账号锁定/IP 锁定两种）。"""
        from src.domains.mgmt.auth import login_throttle

        throttle = login_throttle.check_throttle(identifier, ip)
        if not throttle.get("allowed"):
            raise HTTPException(status_code=429, detail=cls.locked_detail(throttle))

    @classmethod
    def registration_key(cls, provider: str, ip: str) -> str:
        """注册失败计数键：只按 渠道+IP，**不用**客户端提交的用户名/邮箱。

        原实现用 identifier（用户名优先）做键，会把注册失败计进登录的账号维度 →
        任何人拿受害者用户名打 5 次注册就能锁他 15 分钟（Issue #3054 QA）。
        """
        return f"reg:{provider or '-'}:{ip or '-'}"

    @classmethod
    def record_registration_failure(cls, provider: str, ip: str) -> None:
        """记录一次注册失败（键=渠道+IP，可被限流读取但不牵连他人账号）。"""
        cls.record_failure(cls.registration_key(provider, ip), ip)

    @staticmethod
    def record_failure(identifier: str, ip: str) -> None:
        from src.domains.mgmt.auth import login_throttle

        login_throttle.record_failure(identifier, ip)

    @staticmethod
    def clear(identifier: str, ip: str = "") -> None:
        from src.domains.mgmt.auth import login_throttle

        login_throttle.clear_failures(identifier, ip)

    @staticmethod
    def enforce_ip_rate_limit(ip: str) -> None:
        """OAuth 端点 IP 滑动窗口限流（与 GET /login/<provider> 同参数）。"""
        from src.domains.mgmt.auth import login_throttle

        if not login_throttle.check_oauth_rate_limit(
                ip, login_throttle.OAUTH_LOGIN_LIMIT, login_throttle.OAUTH_LOGIN_WINDOW):
            raise HTTPException(status_code=429,
                                detail={"code": 429, "message": "请求过于频繁，请稍后再试"})


class LoginMethodPolicy:
    """「至少保留一种登录方式」判据（唯一来源；解绑前置闸 / 转移前校验共用）。

    与 `identity_unbind.KeepOnePredicate` 是**同一条语义的两种表达**：
    - 本类 = 读判定，供前置闸与错误文案用（这里是唯一的人类可读口径）；
    - KeepOnePredicate = 写判定，在单条 UPDATE 的 WHERE 里原子裁决（防并发双解绑）。
    两处的登录状态集合必须一致 —— 改 `LOGIN_STATUSES` 时要同步那边的 `status IN (1,4)`。

    status=4 是「绑定仍在、token 过期」：重新授权即可恢复，算一种可用登录方式。
    旧实现只数 status=1，会让持有 (1)+(4) 的无密码用户在 UI 前置闸被 400 挡住，
    服务层那条原子判据根本走不到（Issue #3054 QA）。
    """

    LOGIN_STATUSES: tuple = (1, 4)

    @classmethod
    def alternative_login_exists(cls, grape_user_id: str) -> bool:
        """该账号除「本次要移除的这一条」外，是否还有可用登录方式。"""
        from src.domains.mgmt.auth.grape_account_db import count_user_identities, has_password

        if has_password(grape_user_id):
            return True
        return count_user_identities(grape_user_id, statuses=cls.LOGIN_STATUSES) > 1

    @classmethod
    def would_lock_out(cls, grape_user_id: str) -> bool:
        """移除这条绑定后是否会失去最后一种登录方式。"""
        return not cls.alternative_login_exists(grape_user_id)


class UnbindGuard:
    """解绑守卫：原子「至少保留一种登录方式」+ 拒绝态错误映射。"""

    KEEP_ONE_MESSAGE = "至少保留一种登录方式，请先设置 Grape 密码"

    @staticmethod
    def remove(grape_user_id: str, provider: str, require_keep_one: bool = True) -> bool:
        """执行解绑；返回 False 表示「没有该活跃绑定」或「会失去最后一种登录方式」。"""
        from src.domains.mgmt.auth.grape_account_db import delete_external_identity

        return delete_external_identity(grape_user_id, provider,
                                        require_keep_one=require_keep_one)

    @staticmethod
    def keep_one_error(grape_user_id: str) -> HTTPException:
        """解绑被原子判据拒绝时的用户可见错误。"""
        return HTTPException(status_code=400, detail={
            "code": 400,
            "message": UnbindGuard.KEEP_ONE_MESSAGE,
            "need_set_password": True,
        })

    @classmethod
    def rejection(cls, grape_user_id: str) -> HTTPException:
        """解绑返回 False 后区分两种原因（并发时另一端应看到 keep-one 文案）。"""
        if LoginMethodPolicy.would_lock_out(grape_user_id):
            return cls.keep_one_error(grape_user_id)
        return HTTPException(status_code=404,
                             detail={"code": 404, "message": "未绑定该平台"})


class AccountDeactivationCascade:
    """账号注销级联 + 账号状态→错误码映射（P3-3）。

    注销只动 mgmt 自有表：grape_accounts.status=3、external_identities 失效并
    清 token、token_version 回收（JWT 立即失效）。runtime 侧数据清理不属于注销
    级联（由各自的域流程负责），本类不新增跨域 SQL。
    """

    status_codes = {0: "ACCOUNT_DEACTIVATED", 2: "ACCOUNT_DELETED",
                    3: "ACCOUNT_DEACTIVATING"}
    messages = {0: "该账号已被停用，请联系管理员",
                2: "该账号已注销，无法登录",
                3: "该账号正在注销中，暂时无法登录"}

    @classmethod
    def soft_delete(cls, user_id: str) -> bool:
        """进入冷静期并级联失效身份行/清 token/回收 token_version。"""
        from src.domains.mgmt.auth.grape_account_db import (
            increment_token_version,
            soft_delete_account,
        )

        ok = soft_delete_account(user_id)
        if not ok:
            return False
        try:
            increment_token_version(user_id)
        except Exception as e:  # 回收失败不得掩盖注销结果（下次会话校验仍会拦）
            log.warning(f"deactivation: increment_token_version failed for {user_id}: {e}")
        return True

    # ── 状态 → 错误码（冷静期 / 已禁用 / 已删除 三分） ────────────
    @classmethod
    def rollback_signup(cls, user_id: str) -> bool:
        """OAuth 建号后中途失败的回滚：释放 email / username，不进冷静期占坑。

        与 `soft_delete`（用户主动注销，冷静期保留凭据以便恢复）区分开：
        这类账号从未被用户使用，保留只会让同一邮箱/用户名 30 天内对所有人不可用。
        """
        from src.domains.mgmt.auth.grape_account_db import release_failed_signup_account

        return release_failed_signup_account(user_id)

    @classmethod
    def oauth_error_code(cls, status: int) -> str:
        """OAuth 回调跳转用的错误码（前端 i18n key 后缀）。"""
        if status == 0:
            return "account_disabled"
        if status == 3:
            return "account_deactivating"
        return "account_deleted"

    @classmethod
    def http_error(cls, status: int) -> HTTPException:
        """密码/关联登录用的 HTTP 错误（与 /login 同口径）。"""
        status = int(status or 0)
        return HTTPException(status_code=403, detail={
            "code": 403,
            "error_code": cls.status_codes.get(status, "ACCOUNT_DEACTIVATED"),
            "message": cls.messages.get(status, "该账号已被停用，请联系管理员"),
        })


class ChannelGate:
    """渠道开关复查（P3-7）：state 签发后渠道被停用，本次登录/注册不得再完成。

    与 ``GET /login/<provider>`` 同源判据（grape_t_oauth_channels.enabled）。差异
    只在「未知渠道」分支：state 已签发即证明发起时渠道存在，这里只拦**明确读到
    enabled=0** 的情形；读表失败/无行一律放行，避免回调侧因对象表未迁移或历史
    渠道缺行把存量登录打挂。
    """

    DISABLED_DETAIL = {"code": 403, "error_code": "CHANNEL_DISABLED",
                       "message": "该平台登录渠道已停用，请联系管理员"}

    @staticmethod
    def is_disabled(provider: str) -> bool:
        try:
            from src.domains.platform.public import is_table_backed

            if not is_table_backed():
                return False
            from sqlalchemy.orm import Session

            from src.kernel.persistence.engine import get_engine
            from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

            with Session(get_engine()) as s:
                row = OAuthChannelRepo.get(s, str(provider))
            if row is None:
                return False
            return not bool(getattr(row, "enabled", 1))
        except Exception as e:
            log.warning(f"channel-enabled recheck failed (fail-open): {e}")
            return False

    @classmethod
    def raise_if_disabled(cls, provider: str) -> None:
        """API 入口用：明确停用则抛 403。"""
        if cls.is_disabled(provider):
            raise HTTPException(status_code=403, detail=dict(cls.DISABLED_DETAIL))