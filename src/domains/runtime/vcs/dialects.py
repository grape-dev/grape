"""webhook 方言策略（D2/D12）——GitHub 系与 GitLab 系两个策略族。

策略模式落位：方言模板承载 normalize（事件归一）与 verify（验签）两族算法；
GenericBackend（适配器）持有一个 PlatformCard + 一个方言策略，为配置驱动的
新平台（gitee 起零代码接入）提供 webhook 后端能力。

存量三平台（gitcode/github/gitlab）继续使用各自的既有 Backend 类（行为不变，
S9 回归锁）；本模块只服务"新平台=纯配置"路径（S6/S16 验收）。

验签语义（D12）：
- hmac：GitHub 系（X-Hub-Signature-256 / X-Gitee-Token?），sha256(body, secret)
- token_compare：Gitee 实测语义——X-Gitee-Signature 头即 secret 明文直比；
  GitLab 系——X-Gitlab-Token 头与 secret 直比
"""
from __future__ import annotations

import hashlib
import hmac as _hmac

from .base import VcsBackend
from .event_types import (
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_COMMENT, PR_REVIEW,
    PUSH, TAG_CREATED,
)


def _first(payload: dict, *keys, default=""):
    for k in keys:
        v = payload.get(k)
        if v:
            return v
    return default


class WebhookDialect:
    """方言策略基类：normalize + verify。"""

    def normalize(self, payload: dict, event_header: str, source: str,
                  event_map_overrides: dict | None = None) -> dict:
        raise NotImplementedError

    def verify(self, headers: dict, raw_body: bytes, secret: str) -> "tuple[bool, str]":
        raise NotImplementedError


class GitHubDialect(WebhookDialect):
    """GitHub 系方言（github / gitee / gitcode-v5 风格：平铺 payload + action 字段）。

    事件头值 → 统一事件类型；issue/PR 细分按 payload.action（open/close/reopen/...）。
    """

    # 事件头值 → 统一类型（gitee/github 兼容集）
    _EVENT_MAP = {
        "push": PUSH,
        "ping": "ping",
        "issues": ISSUE_OPENED,
        "issue": ISSUE_OPENED,
        "issue_comment": ISSUE_COMMENT,
        "pull_request": PR_OPENED,
        "pull_request_review": PR_REVIEW,
        "note": ISSUE_COMMENT,
        # S70：gitee 原生头值形态（官方文档：'Push Hook'/'Note Hook'/
        # 'Issue Hook'/'Merge Request Hook'/'Tag Push Hook'/'Release Hook'）
        # ——规范化（去 ' Hook' 后缀 + 空格转下划线）后落既有键 + 补齐映射。
        "merge_request": PR_OPENED,
        "tag_push": "tag_push",
        "release": "release",
    }

    @staticmethod
    def _canonical_event_header(event_header: str) -> str:
        """S70：gitee 原生事件头规范化——'Note Hook'→'note'、
        'Merge Request Hook'→'merge_request'；github 裸名不受影响。"""
        h = str(event_header or "").strip().lower()
        if h.endswith(" hook"):
            h = h[:-len(" hook")].strip()
        return h.replace(" ", "_")

    def _issue_type(self, action: str, payload: dict) -> str:
        changes = payload.get("changes") or {}
        if action in ("opened", "open", "created"):
            return ISSUE_OPENED
        if action in ("closed", "close"):
            return ISSUE_CLOSED
        if action == "reopened":
            return ISSUE_REOPENED
        if action in ("updated", "update", "edit", "edited"):
            if "assignees" in changes or "assignee" in changes:
                return ISSUE_ASSIGNED
            if "labels" in changes:
                return ISSUE_LABELED
            return ISSUE_UPDATED
        if action == "assigned":
            return ISSUE_ASSIGNED
        if action == "labeled":
            return ISSUE_LABELED
        return ISSUE_UPDATED

    def _pr_type(self, action: str, payload: dict) -> str:
        merged = (payload.get("pull_request") or payload.get("pr") or {}).get("merged")
        if action in ("closed", "close") and merged:
            return PR_MERGED
        if action in ("closed", "close"):
            return PR_CLOSED
        if action in ("opened", "open"):
            return PR_OPENED
        if action in ("synchronize", "updated", "edited"):
            return PR_UPDATED
        return PR_UPDATED

    def normalize(self, payload: dict, event_header: str, source: str,
                  event_map_overrides: dict | None = None) -> dict:
        evt_map = dict(self._EVENT_MAP)
        if event_map_overrides:
            evt_map.update(event_map_overrides)
        # S70：gitee 原生头值（'Note Hook' 等）规范化后再查表
        _canon = self._canonical_event_header(event_header)
        evt = evt_map.get(_canon, event_header)
        action = str(_first(payload, "action", default=""))

        if evt in (ISSUE_OPENED,) or _canon in ("issues", "issue"):
            evt = self._issue_type(action, payload)
        elif evt in (PR_OPENED,) or _canon in ("pull_request", "merge_request"):
            evt = self._pr_type(action, payload)
        elif evt == ISSUE_COMMENT:
            # gitee/github：PR 上的评论也有 issue_comment 头（pull_request_url 存在）
            if _first(payload, "pull_request_url", "pull_request"):
                evt = PR_COMMENT

        repo = payload.get("repository") or {}
        repo_name = repo.get("full_name") or "/".join(
            x for x in (repo.get("owner", {}).get("login") if isinstance(repo.get("owner"), dict) else "",
                        repo.get("name")) if x) or str(_first(payload, "repo", default=""))
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        actor = str(sender.get("login", ""))
        branch = str(_first(payload, "ref", default="")).replace("refs/heads/", "") or \
            str(((payload.get("pull_request") or {}).get("base") or {}).get("ref", ""))
        title = str((payload.get("issue") or payload.get("pull_request") or payload.get("pr") or {}).get("title", ""))
        return {"source": source, "type": evt, "repository": repo_name,
                "branch": branch, "actor": actor, "payload": payload, "title": title}

    def verify(self, headers: dict, raw_body: bytes, secret: str) -> "tuple[bool, str]":
        _h = {str(k).lower(): v for k, v in (headers or {}).items()}
        # S70（官方文档实锤）：gitee 密码模式投递头 = X-Gitee-Token（值=password
        # 明文，常量时间比较）。原只认 X-Gitee-Signature → 真实投递必 403。
        tok = _h.get("x-gitee-token") or ""
        if tok:
            if _hmac.compare_digest(tok, secret or "\x00"):
                return True, ""
            return False, "signature mismatch"
        # Gitee 实测：X-Gitee-Signature 头 = secret 明文（token_compare 语义）
        sig = _h.get("x-gitee-signature") or ""
        if sig:
            if _hmac.compare_digest(sig, secret or "\x00"):
                return True, ""
            return False, "signature mismatch"
        hub = _h.get("x-hub-signature-256") or ""
        if hub:
            expected = "sha256=" + _hmac.new((secret or "").encode(), raw_body, hashlib.sha256).hexdigest()
            if _hmac.compare_digest(hub, expected):
                return True, ""
            return False, "signature mismatch"
        return False, "missing signature"


class GitLabDialect(WebhookDialect):
    """GitLab 系方言（object_kind + object_attributes 嵌套 + action 细分）。"""

    def _map(self, object_kind: str, action: str, payload: dict) -> str:
        changes = payload.get("changes") or {}
        if object_kind == "push":
            return PUSH
        if object_kind == "tag_push":
            return TAG_CREATED
        if object_kind == "issue":
            if action == "open":
                return ISSUE_OPENED
            if action == "close":
                return ISSUE_CLOSED
            if action == "reopen":
                return ISSUE_REOPENED
            if action == "update":
                if "assignees" in changes or "assignee_id" in changes:
                    return ISSUE_ASSIGNED
                if "labels" in changes:
                    return ISSUE_LABELED
                return ISSUE_UPDATED
            return ISSUE_UPDATED
        if object_kind == "note":
            obj = payload.get("object_attributes") or {}
            return PR_COMMENT if str(obj.get("noteable_type", "")) == "MergeRequest" else ISSUE_COMMENT
        if object_kind == "merge_request":
            if action == "merge" or (payload.get("object_attributes") or {}).get("state") == "merged":
                return PR_MERGED
            if action == "close":
                return PR_CLOSED
            if action == "reopen":
                return PR_OPENED
            if action == "open":
                return PR_OPENED
            return PR_UPDATED
        return object_kind

    def normalize(self, payload: dict, event_header: str, source: str,
                  event_map_overrides: dict | None = None) -> dict:
        kind = str(payload.get("object_kind", ""))
        obj = payload.get("object_attributes") or {}
        action = str(obj.get("action", "")).lower()
        # D32/S52：event_map_overrides 生效（原死参数）——卡可把平台私有
        # kind 映射到 canonical 类型（与 GitHub 系方言的 overrides 语义一致，
        # 命中即优先于内置映射；未命中走内置）
        evt = ""
        if event_map_overrides and kind in event_map_overrides:
            evt = str(event_map_overrides[kind])
        if not evt:
            evt = self._map(kind, action, payload)
        repo = ((payload.get("project") or {}).get("path_with_namespace")
                or (payload.get("repository") or {}).get("url", ""))
        actor = str((payload.get("user") or {}).get("username", ""))
        branch = str(obj.get("source_branch", "")) or str(_first(payload, "ref", default="")).replace("refs/heads/", "")
        return {"source": source, "type": evt, "repository": repo,
                "branch": branch, "actor": actor, "payload": payload,
                "title": str(obj.get("title", ""))}

    def verify(self, headers: dict, raw_body: bytes, secret: str) -> "tuple[bool, str]":
        _h = {str(k).lower(): v for k, v in (headers or {}).items()}
        token = _h.get("x-gitlab-token") or ""
        if not token:
            return False, "missing signature"
        if _hmac.compare_digest(token, secret or "\x00"):
            return True, ""
        return False, "signature mismatch"


_DIALECTS = {"github": GitHubDialect, "gitlab": GitLabDialect}


def get_dialect(name: str) -> "WebhookDialect | None":
    cls = _DIALECTS.get(name)
    return cls() if cls else None


class GenericBackend(VcsBackend):
    """配置驱动的通用 webhook 后端（适配器）：PlatformCard + 方言策略。

    用于存量三平台之外的新平台（gitee 等）——平台卡 + 方言模板即完整后端，
    零平台代码（S6/S17 验收）。
    """

    def __init__(self, card, dialect: WebhookDialect):
        self.card = card
        self.dialect = dialect
        self.name = card.provider
        self.webhook_path = card.webhook_path
        self.event_header_key = card.event_header

    def normalize(self, raw, event_header) -> dict:
        payload = self.decode_payload(raw)
        return self.dialect.normalize(payload, event_header, self.card.provider,
                                      self.card.event_map_overrides)

    def verify(self, headers: dict, raw_body: bytes, secret: str) -> "tuple[bool, str]":
        return self.dialect.verify(headers, raw_body, secret)
