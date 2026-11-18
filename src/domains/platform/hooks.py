"""D109 关系写入钩子（用户↔仓库平台，全部 fail-safe）。

原则：**钩子绝不阻断主流程**。登录、绑定、PAT 保存/删除都是既有热路径，写关系失败
（表未建、DB 忙、锁冲突）只记 warning，不影响原流程返回。

模型（2026-11-14 裁定）：`grape_t_user_repo_platform_link` **只存关系与时间**，不存来源位。
因此写入点归一为两个动作：

1. `link` —— 建立/恢复关系：
   - `on_oauth_login(user_id, oauth_provider)`：OAuth **登录成功**或**绑定成功**，按
     `channel-repo-links.json` 的配对展开到目标 repo_provider（纯登录渠道 = 没配对，见 importer）。
     ⚠ 这份配置只决定"要不要写用户↔平台仓库关系"；**取 token 的 OAuth/PAT 两种模式与它无关**
     （deps：①PAT ②OAuth 恒定）。
   - `on_pat_saved(user_id, repo_provider)`：PAT 保存，**不经渠道、不查 link**（平台级凭据，
     audience 天然匹配；只靠渠道会漏掉"只配 PAT 从不登录"的用户）。
2. `revoke` —— 撤销关系：**只有该平台再无任何其它来源时**才真正写 revoked：
   - `on_oauth_unbound`：解绑渠道（排除"其它渠道"这一来源）；
   - `on_pat_deleted`：删除 PAT（排除"PAT"这一来源）。
   "还有没有其它来源"每次现查（凭据域 user_credentials kind=pat + 活跃绑定按 link 展开），
   不从关系表读来源 —— 这正是删掉 via_bits 后必须由读取算的部分。

开关：`GRAPE_PLATFORM_LINK_HOOKS=0` 可整体关闭（灰度 / 紧急回滚用，不删代码）。
"""
from __future__ import annotations

import json
import os

from src.kernel.config import log

_ENABLED = os.environ.get("GRAPE_PLATFORM_LINK_HOOKS", "1") != "0"


def _run(what: str, fn) -> None:
    if not _ENABLED:
        return
    try:
        fn()
    except Exception as e:  # 绝不外抛
        log.warning("platform-link hook %s failed (不影响主流程): %s: %s", what, type(e).__name__, e)


def _targets(session, oauth_provider: str) -> list[str]:
    """该身份渠道能驱动的仓库平台集合 = 跨名配对 ∪ 渠道自身（同域天然可用）。

    2026-11-14 裁定分两层（用户质询"同域配对是错误配置"后定稿）：
      ① **同域不是授权**：渠道自己就是仓库平台时天然可驱动自己（登录即建本平台关系），
         不需要也不应该配一条 (c → c)——配了是自造无意义数据，导入期告警、管理面拒收；
      ② **跨名才是显式授权**：配了 (c → p)（p≠c）→ 允许 c 的令牌驱动 p。
    判定不看 relation 档位 / drivers / scope（上游 API 在调用时回答能力是否足够）。
    """
    from src.kernel.persistence.repositories_platform_object import (
        ChannelRepoLinkRepo,
        RepoPlatformRepo,
    )

    targets = [str(link.repo_provider)
               for link in ChannelRepoLinkRepo.list_by_oauth(session, oauth_provider)]
    # 同域：渠道本身在平台集合里 → 天然可驱动自己
    if RepoPlatformRepo.get(session, oauth_provider) is not None and oauth_provider not in targets:
        targets.insert(0, oauth_provider)
    return targets


def _has_pat(session, user_id: str, repo_provider: str) -> bool:
    """凭据域是否还有该用户在该平台的 PAT（user_credentials kind=pat）。"""
    try:
        from sqlalchemy import select

        from src.kernel.persistence.models import UserCredentialModel

        row = session.execute(
            select(UserCredentialModel.user_id).where(
                UserCredentialModel.user_id == str(user_id),
                UserCredentialModel.provider == repo_provider,
                UserCredentialModel.kind == "pat",
            )
        ).first()
        return bool(row)
    except Exception:  # 表缺列 / 未建：保守认为"有来源"，宁可关系不撤
        return True


def _active_identities(user_id: str) -> list[str]:
    """该用户仍活跃的登录渠道（status ∈ {1,4}，与前端绑定列表同口径）。"""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities

        return [str(i.get("provider")) for i in (get_user_identities(str(user_id)) or []) if i.get("provider")]
    except Exception:
        return []


def _has_other_source(session, user_id: str, repo_provider: str, exclude: str) -> bool:
    """该平台是否还有其它凭据来源。exclude ∈ {"pat", "oauth"} = 正在被撤销的那一类。"""
    if exclude != "pat" and _has_pat(session, user_id, repo_provider):
        return True
    if exclude != "oauth":
        for ch in _active_identities(user_id):
            if repo_provider in _targets(session, ch):
                return True
    return False


def _kick_user_projects_refresh(user_id: str) -> None:
    """关系变更后刷新用户"项目列表"（2026-11-14 闭环规则）。

    做法：异步把 `user_repos` 标记过期（`invalidate_user_repos`），下一次
    `/api/repos/member` 直接回源 VCS 刷新 —— 不必等 30 分钟 TTL，也不在登录热路径上
    打网络（真正回源仍在请求时发生）。失败只告警。
    """
    def _do():
        try:
            from src.domains.mgmt.public import invalidate_user_repos

            # P1-3：grape_accounts 主键自 D105 起是 UUIDv7 字符串，int() 必抛 ValueError
            # 并被下面 except 吞成 warning —— 全量 UUID 用户的关系变更刷新从未执行。
            # 与 repos.py / templates.py 的调用方同口径：原值直传（user_repos.user_id 是
            # varchar(36)/TEXT，活库实测 72 行 UUID 形态）。
            invalidate_user_repos(str(user_id))
        except Exception as e:
            log.warning("platform-link hook refresh projects failed (不影响主流程): %s", e)

    if not _ENABLED:
        return
    try:
        import threading

        threading.Thread(target=_do, daemon=True, name=f"d109-projects-{user_id}").start()
    except Exception:  # 线程不可用时同步做一次（只是 UPDATE，代价低）
        _do()


def on_oauth_login(user_id: str, oauth_provider: str) -> None:
    """OAuth 登录/绑定成功 → 按 link 展开建立关系（幂等，复活 revoked）。"""

    def _do():
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo
        from src.kernel.persistence.uow import uow

        with uow() as s:
            for repo_provider in _targets(s, oauth_provider):
                UserRepoPlatformLinkRepo.link(s, str(user_id), repo_provider)

    _run(f"on_oauth_login({user_id},{oauth_provider})", _do)
    _kick_user_projects_refresh(user_id)


def on_oauth_unbound(user_id: str, oauth_provider: str) -> None:
    """OAuth 解绑 → 该渠道展开的平台关系：无其它来源才撤销。"""

    def _do():
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo
        from src.kernel.persistence.uow import uow

        with uow() as s:
            for repo_provider in _targets(s, oauth_provider):
                if _has_other_source(s, user_id, repo_provider, exclude="oauth"):
                    continue
                UserRepoPlatformLinkRepo.revoke(s, str(user_id), repo_provider, "oauth_unbound")

    _run(f"on_oauth_unbound({user_id},{oauth_provider})", _do)
    _kick_user_projects_refresh(user_id)


def on_pat_saved(user_id: str, repo_provider: str) -> None:
    """PAT 保存 → 直写该平台关系（不经渠道、不查 link）。"""

    def _do():
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo
        from src.kernel.persistence.uow import uow

        with uow() as s:
            UserRepoPlatformLinkRepo.link(s, str(user_id), str(repo_provider))

    _run(f"on_pat_saved({user_id},{repo_provider})", _do)
    _kick_user_projects_refresh(user_id)


def on_pat_deleted(user_id: str, repo_provider: str) -> None:
    """PAT 删除 → 该平台关系：无其它来源才撤销。"""

    def _do():
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo
        from src.kernel.persistence.uow import uow

        with uow() as s:
            if _has_other_source(s, user_id, str(repo_provider), exclude="pat"):
                return
            UserRepoPlatformLinkRepo.revoke(s, str(user_id), str(repo_provider), "credential_deleted")

    _run(f"on_pat_deleted({user_id},{repo_provider})", _do)
    _kick_user_projects_refresh(user_id)


def on_platform_disabled(user_id: str, repo_provider: str) -> None:
    """用户主动禁用某平台 → 写 revoked(disabled_by_user)（凭据不删）。"""

    def _do():
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo
        from src.kernel.persistence.uow import uow

        with uow() as s:
            UserRepoPlatformLinkRepo.revoke(s, str(user_id), str(repo_provider), "disabled_by_user")

    _run(f"on_platform_disabled({user_id},{repo_provider})", _do)
    _kick_user_projects_refresh(user_id)