
"""VCS 平台适配器 —— 能力抽象（接口隔离原则）。

一个平台适配器不再是一个 22 方法的巨型对象，而是四类能力的并集：
  TokenCapability      身份/凭据字段（token 链只需这一片）
  RepoCapability       仓库读写（建仓 / 协作者 / 仓库列举）
  WebhookCapability    钩子生命周期
  NamespaceCapability  「所属空间」清单 + 完整路径解析（同一事实源）
`VCSProvider` 只是这些能力的组合门面；调用方依赖门面，实现侧按能力拆分，
新增平台只覆写偏离点。
"""

from abc import ABC, abstractmethod

from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import im as _im
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens
from src.kernel.ports import vcs_cards as _vcs_cards
from src.kernel.token_resolver import _audit_source


#: 单页长度。来源=厂商文档：GitHub / GitLab 列表接口的 `per_page` 上限均为 100
#: （GitLab 默认 20、上限 100；GitHub 默认 30、上限 100），取平台允许的最大页长，
#: 用于把「翻页次数」压到最少。登记于 docs/issue-3580/measurements.md「列表分页上限」。
_PER_PAGE = 100
#: 列表分页上限（沿用 base `kernel/token_resolver.py:387/443` 的历史值，不是新拍的数字）：
#: per_page=100（GitHub/GitLab 文档的最大页长）→ 20 页 = 2000 条封顶。超出即停止翻页
#: 并打 WARNING（列表会被截断，用户可能少看到仓库/协作者），口径登记于
#: docs/issue-3580/measurements.md「列表分页上限」。
_MAX_LIST_PAGES = 20
_MAX_LIST_PAGES_WARN = (
    "vcs list pagination hit the {pages}-page cap ({rows} rows): result truncated, "
    "raise the cap or add search-based paging if this account really has more")

class TokenCapability(ABC):
    """身份与凭据字段能力（token 链最小依赖面）。"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 标识: "gitcode" / "github"。"""

    @abstractmethod
    def verify_token(self, token: str) -> dict:
        """验证 token 有效性，返回用户信息 dict（含 login 字段）。
        token 无效时抛 RuntimeError。"""

    @abstractmethod
    def get_user_login(self, token: str) -> str:
        """获取 token 对应的用户名。"""

    def token_expiry(self, token: str) -> dict:
        """凭据自身有效期元数据（默认无：多数平台不提供）。

        返回 {"expires_at": "YYYY-MM-DD", "scopes": [...], "revoked": bool} 或 {}。
        有能力的平台（GitLab PAT）覆盖它；调用方（凭据绑定）按需取，读不到就落空。
        """
        return {}

    @abstractmethod
    def token_storage_field(self) -> str:
        """仓库级 bot token 存储列名。"""

    @abstractmethod
    def webhook_token_field(self) -> str:
        """webhook token 存储列名。"""

    @abstractmethod
    def session_token_field(self) -> str:
        """devops_agent_session token 列名。"""

    @abstractmethod
    def global_bot_meta_key(self) -> str:
        """全局 bot token meta key。"""

    @abstractmethod
    def global_bot_status_meta_key(self) -> str:
        """全局 bot token 过期状态 meta key（per-provider, R21.2）。"""

    @abstractmethod
    def pat_create_url(self) -> str:
        """PAT 创建页链接。"""

    @abstractmethod
    def fetch_user_token(self, username: str, user_id: str) -> str:
        """获取用户 token（带刷新）。
        统一走 get_user_token_for(username, user_id, provider)。"""

    def resolve_repo_token(self, repo_key: str) -> str:
        """仓库 token 统一读链（2026-11-13 if/else 收口，基类默认能力）。

        默认走 TokenResolver 标准策略链；owner-identity 回落由
        GenericVcsProvider 按卡能力位 owner_identity_token_fallback 派生
        （配置驱动，新平台卡内声明即得）。存量专用实现（如有）子类覆写。
        """
        from src.kernel.token_resolver import TokenResolver
        return TokenResolver(self).get_token(repo_key) or ""

class RepoCapability(ABC):
    """仓库读写能力（建仓 / 协作者 / 仓库列举）。"""

    def create_repo(self, api_token: str, name: str, description: str = "",
                    private: bool = False, owner: str = "", path: str = "") -> dict:
        """D42/S62 端口：在 token 用户名下建仓，返回平台 repo dict。

        owner 非空 = 在指定组织/空间下建仓（平台实现支持时）；path 为仓库 URL 路径
        （GitCode 特有，缺省等于 name）。默认不支持的实现返回 error dict（调用方按
        error 处理）。
        """
        return {"error": f"create_repo unsupported for {self.provider_name}"}

    @abstractmethod
    def add_collaborator(self, api_token: str, owner: str, repo: str,
                         username: str, permission: str = "") -> bool:
        """邀 bot 为协作者。幂等：已是协作者返回 True。
        permission 缺省时用各适配器存量默认（Gitee 系 maintainer / GitHub push）。
        """

    @abstractmethod
    def remove_collaborator(self, api_token: str, owner: str, repo: str,
                            username: str) -> bool:
        """移除协作者。best-effort，失败不阻断。"""

    def accept_collaborator_invite(self, bot_token: str, owner: str, repo: str) -> bool:
        """D39/S59：接受协作者邀请（bot 被邀后生效）。

        邀请制平台在适配器覆写；generic 直加平台（PUT 即成员）默认 True。
        """

        return True

    def get_collaborator_permission(self, api_token: str, owner: str,
                                    repo: str, username: str) -> "dict | str | None":
        """D42/S62 端口：查协作者权限（dict/str/None 三形态归一）。

        默认 None（未实弹验证平台 → 调用方 fail-closed/不阻断语义）。
        """
        return None

    def list_collaborators(self, api_token: str, owner: str,
                           repo: str) -> "list[dict]":
        """D44/S66 端口：拉仓库协作者列表（分页，平台原生字段）。"""
        return []

    def normalize_created_repo(self, raw: dict) -> dict:
        """建仓响应 → 全平台同形的仓库身份（补 full_name/owner/name，原字段保留）。

        平台差异收在对齐器里（`src.kernel.vcs.repo_ref`，按卡 dialect 注册）；这里
        是给各 provider 的 create_repo 复用的通用钩子——路由层因此零平台分支。
        """
        from src.kernel.vcs.repo_ref import normalize_repo_payload

        return normalize_repo_payload(self.provider_name, raw, getattr(self._card, "dialect", ""))

class WebhookCapability(ABC):
    """钩子生命周期能力（建/删/查）。"""

    @abstractmethod
    def setup_webhook(self, api_token: str, owner: str, repo: str,
                      webhook_url: str, webhook_secret: str) -> int:
        """创建 webhook，返回 webhook_id。"""

    @abstractmethod
    def remove_webhook(self, api_token: str, owner: str, repo: str,
                       hook_id: str) -> bool:
        """删除 webhook。"""

class NamespaceCapability(ABC):
    """「所属空间」能力：清单 + 完整路径解析同源。"""

    def list_create_spaces(self, api_token: str) -> "list[dict]":
        """可建仓的「所属空间」清单（GitLab 组/子组；其它平台暂无 → 空 = 只有个人空间）。

        返回 [{path, name, kind}]：`path` 可直接作为建仓 owner（GitLab 用 full_path，
        含子组）。默认空实现——平台没有组织空间概念时，创建表单只渲染"个人空间"。
        """
        return []
    def resolve_namespace(self, api_token: str, owner: str) -> "dict | None":
        """完整空间路径 → {id, path}；不支持组织空间的平台返回 None。

        与 list_create_spaces 共享同一路径口径：**列得出的必须建得成**。
        默认 None（fail-closed：调用方按「空间不存在」处理，不放权）。
        """
        return None



class VCSProvider(TokenCapability, RepoCapability, WebhookCapability, NamespaceCapability):
    """组合门面：一个平台适配器 = 四类能力的并集（调用方只依赖本类）。"""


class GenericVcsProvider(VCSProvider):
    """配置驱动通用平台实现（D15/K3）：能力全部来自仓库平台卡，零平台代码。

    - verify_token/get_user_login：卡 api_base + /user + api_auth（query_token/bearer）
    - collaborator/webhook：卡路径模板（github 系 REST 形态）
    - fetch_user_token：D3 统一入口（OAuth 身份）→ user_credentials `<p>_pat`（批次 0 PAT 存储）
    - token_storage_field 返回空串：仓库级 bot token 分列归批次 1（M1/M2）
    """

    def __init__(self, card):
        self._card = card

    @property
    def provider_name(self) -> str:
        return self._card.provider

    def _http(self, method: str, path: str, token: str, data: dict | None = None,
              form: bool = False) -> dict:
        import json as _json
        import urllib.request
        import urllib.error
        url = f"{self._card.api_base}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._card.api_auth == "query_token":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={token}"
        elif self._card.api_auth == "private_token":
            # S72：GitLab 系鉴权头 = PRIVATE-TOKEN（原 else 分支发 Bearer → 401）
            headers["PRIVATE-TOKEN"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
        if form and data:
            # D41/D42：Gitee 系 v5 写接口实测要求 x-www-form-urlencoded（JSON 会 400）
            import urllib.parse as _up
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = _up.urlencode(data).encode()
        else:
            body = _json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        import time as _time
        _t0 = _time.monotonic()
        _log_path = path.split("?")[0]          # 不落 query（可能含 access_token）
        try:
            from src.kernel.upstream_http import open_with_policy as _open_policy
            # timeout=30 来源（2026-11-23 实测）：自建 worker → gitlab.com
            # /api/v4/user 连打 10 次为 0.72–1.74s（p50≈0.97s）；30s 约为实测
            # 上界的 17 倍，能吃掉跨区抖动而不把整条建仓链路拖死。
            with _open_policy(
                    req, timeout=30,
                    ca_bundle=str(getattr(self._card, "ca_bundle", "") or ""),
                    allow_redirects=self._card.dialect != "gitlab",
            ) as resp:
                _payload = _json.loads(resp.read().decode("utf-8"))
                log.info(f"vcs upstream {self._card.provider} {method} "
                         f"{self._card.api_base}{_log_path} -> {getattr(resp, 'status', 200)} "
                         f"{( _time.monotonic() - _t0) * 1000:.0f}ms")
                return _payload
        except urllib.error.HTTPError as e:
            # 带上游响应体：4xx 的 {"message": ...} 才是可行动的诊断（建仓被拒等）
            try:
                _detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                _detail = ""
            log.warning(f"vcs upstream {self._card.provider} {method} "
                        f"{self._card.api_base}{_log_path} -> {e.code} "
                        f"{( _time.monotonic() - _t0) * 1000:.0f}ms")
            raise RuntimeError(
                f"{self._card.name} API {method} {path} failed ({e.code}): {e}"
                + (f" | {_detail}" if _detail else "")) from e

    def verify_token(self, token: str) -> dict:
        info = self._http("GET", "/user", token)
        if not isinstance(info, dict) or not info:
            raise RuntimeError(f"{self._card.name} token verify: empty user info")
        return info

    def resolve_repo_token(self, repo_key: str) -> str:
        """仓库 token 统一读链（2026-11-13 if/else 收口，基类能力）：

        标准策略链（TokenResolver：session→repo_bot→global→user(host_user_id)）
        → 全空且卡声明 owner_identity_token_fallback=true 时，按仓库 owner
        用户名查其该平台 identity token（D32-补丁语义下沉，原为
        db.get_github_token 尾段专属回落）。

        抽象口径：所有平台（含未来 config-only 新平台）经此单一入口获得
        与原平台专属 getter 等价的能力面；新平台要回落语义只需卡内声明
        owner_identity_token_fallback，零代码分支。
        """
        from src.kernel.token_resolver import TokenResolver
        token = TokenResolver(self).get_token(repo_key)
        if token:
            return token
        if not getattr(self._card, "owner_identity_token_fallback", False):
            return ""
        try:
            from src.kernel.repo_key import to_repo_key, parse_repo_key
            _rk = to_repo_key(repo_key, provider=self.provider_name)
            if not _rk:
                return ""
            _p, owner, _name = parse_repo_key(_rk)
            return self.fetch_owner_identity_token(owner)
        except Exception as e:
            log.warning(f"resolve_repo_token fallback failed (provider={self.provider_name}, "
                        f"repo_key={repo_key}): {e}")
        return ""

    def fetch_owner_identity_token(self, owner: str) -> str:
        """owner 的本平台 identity token（能力位 owner_identity_token_fallback
        的执行体，基类默认按 external_identities 直查——OAuth 标准形态）。

        平台有特殊解析（如 github 存量委托）时子类覆写。
        """
        if not owner:
            return ""
        try:
            acc = _accounts.get_grape_account_by_username(owner)
            if acc:
                # strict=True（Issue #3732）：mgmt 域读失败必须显式进入本分支并
                # 标注原因——token 解析失败不得无声回落仓库 bot token。
                for ident in _accounts.get_user_identities_with_tokens(acc["id"], strict=True):
                    if ident.get("provider") == self.provider_name and ident.get("access_token"):
                        return ident["access_token"]
        except Exception as e:
            log.warning(f"fetch_owner_identity_token failed "
                        f"(credential_unavailable: mgmt_read_failed, "
                        f"provider={self.provider_name}, owner={owner}): {e}")
        return ""

    def get_user_login(self, token: str) -> str:
        return str(self.verify_token(token).get("login", ""))

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        try:
            self._http("PUT", f"/repos/{owner}/{repo}/collaborators/{username}", api_token,
                       {"permission": permission})
            return True
        except Exception:
            return False

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            self._http("DELETE", f"/repos/{owner}/{repo}/collaborators/{username}", api_token)
            return True
        except Exception:
            return False

    def accept_collaborator_invite(self, bot_token, owner, repo) -> bool:
        """D39/S59 端口：接受协作者邀请（bot 被邀后生效）。

        形态由适配器内聚：generic 直加平台（PUT collaborators 即成员，
        无邀请确认步骤）返回 True；邀请制平台在子类覆写走存量流程。
        """
        return True

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="", auto_init: bool = False,
                    default_branch: str = "") -> dict:
        """D42/S62 端口：建仓（POST /user/repos，指定 owner 时 /orgs/{owner}/repos）。

        generic 形态：query_token 走 form（Gitee 系 v5 写接口要求），
        其余 JSON；返回平台 repo dict（full_name/owner/name 由调用方归一）。
        """
        data = {"name": name, "description": description or "",
                "private": "true" if private else "false"}
        if auto_init:
            # 卡声明 create_repo_options 的平台（gitee 已实测生效）才透传
            data["auto_init"] = "true"
            if default_branch:
                data["default_branch"] = default_branch
        if path:
            data["path"] = path
        form = self._card.api_auth == "query_token"
        endpoint = f"/orgs/{owner}/repos" if owner else "/user/repos"
        return self._http("POST", endpoint, api_token, data, form=form)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        """D42/S62 端口：查指向 webhook_url 的已有 webhook（id 或 None）。

        FR-5.1 同语义：hooks 列表为空（token 权限不足）返回 None 不误判。
        """
        hooks = self._http("GET", f"/repos/{owner}/{repo}/hooks?per_page=100", api_token)
        if not isinstance(hooks, list):
            return None
        ids = [h.get("id") for h in hooks if isinstance(h, dict)
               and (h.get("url") == webhook_url or h.get("config", {}).get("url") == webhook_url)]
        return ids[0] if ids else None

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        """D42/S62 端口：拉用户仓库列表（含 is_admin 标注）。

        generic 形态：GET /user/repos 分页；is_admin 取 permissions.admin。

        D102-A（2026-11-08 实弹发现）：Gitee /user/repos 列表响应对 owner
        仓库也不带可用 permissions.admin——纯 permissions 判定把仓库 owner
        错标 Member。owner 兜底：owner.login == 当前用户 login（GET /user
        一次取回）→ is_admin=True，与 permissions.admin 取或；login 拉取
        失败降级纯 permissions 判定（不炸列表拉取）。非 owner 仓库判定
        不变（permissions 缺失降级 False 不误报权限的原则保留）。

        2026-11-13：补 created_at 回填。前端项目大厅按「已托管 > 已加入 > 其它」
        排序、同级按 created_at 倒序，generic 平台不回填该字段时全部沉底
        （gitee 实测 7 条落第 7/8 页，首屏永远看不到）。各平台字段名不同：
        Gitee/GitHub/GitLab 用 created_at，个别实现用 created_on / created。
        """
        out, page = [], 1
        # D102-A: owner 兜底基准 login（失败降级 None=纯 permissions 判定）
        _my_login = ""
        try:
            _my_login = self.get_user_login(api_token)
        except Exception as e:
            # 降级不阻断：角色可能显示 Member（原行为），列表完整返回
            log.warning(f"fetch_repos_with_role: owner login fetch failed "
                        f"(fallback to permissions-only): {e}")
        while page <= _MAX_LIST_PAGES:
            batch = self._http("GET", f"/user/repos?per_page={_PER_PAGE}&page={page}", api_token)
            if not isinstance(batch, list) or not batch:
                break
            for r in batch:
                if not isinstance(r, dict):
                    continue
                perms = r.get("permissions") or {}
                _owner = (r.get("owner") or {}).get("login", "") if isinstance(r.get("owner"), dict) else ""
                # Gitee v5 的仓库对象用 namespace 表示归属（owner 可能缺）；
                # 取不到时再按 full_name 的命名空间段兜底。此前 owner 为空会让
                # 角色判定（owner==我的 login ⇒ admin）全部落空、界面显示"我明明
                # 是管理员"却读成 Member（2026-11-14 实锤）。
                if not _owner:
                    _ns = r.get("namespace")
                    if isinstance(_ns, dict):
                        _owner = _ns.get("path") or _ns.get("name") or ""
                _full = r.get("full_name") or ""
                if not _owner and "/" in _full:
                    _owner = _full.split("/", 1)[0]
                if not _full and _owner and r.get("name"):
                    _full = f"{_owner}/{r.get('name')}"
                # D102-A: owner 兜底 || permissions.admin（两者取或）
                _is_admin = bool(perms.get("admin")) or (
                    bool(_my_login) and _owner == _my_login)
                out.append({
                    "full_name": _full,
                    "owner": _owner,
                    "name": r.get("name") or "",
                    "is_admin": _is_admin,
                    "html_url": r.get("html_url") or "",
                    "created_at": (r.get("created_at") or r.get("created_on")
                                   or r.get("created") or ""),
                })
            if len(batch) < _PER_PAGE:
                break
            page += 1
        if page > _MAX_LIST_PAGES:
            log.warning(_MAX_LIST_PAGES_WARN.format(pages=_MAX_LIST_PAGES, rows=len(out)))
        return out

    def get_collaborator_permission(self, api_token, owner, repo, username) -> "dict | str | None":
        """D42/S62 端口：查协作者权限。

        generic 平台权限形态未实弹验证，返回 None（调用方语义：不确定
        不阻断，零危害默认）；存量平台子类覆写走原实现
        （GitCode 返回 permission dict，GitHub 返回 role_name 字符串）。
        """
        return None

    def list_collaborators(self, api_token, owner, repo) -> "list[dict]":
        """D44/S66 端口：拉仓库协作者列表（IM 群成员映射同步消费）。

        generic 形态：GET /repos/{owner}/{repo}/collaborators 分页
        （github 系路径，卡驱动鉴权）；字段归一（login/name + 平台原生
        login 字段），调用方按 nick/login 匹配。存量平台子类委托覆写。
        """
        out, page = [], 1
        while page <= _MAX_LIST_PAGES:
            batch = self._http("GET",
                f"/repos/{owner}/{repo}/collaborators?per_page={_PER_PAGE}&page={page}", api_token)
            if not isinstance(batch, list) or not batch:
                break
            for c in batch:
                if isinstance(c, dict):
                    out.append(c)
            if len(batch) < _PER_PAGE:
                break
            page += 1
        if page > _MAX_LIST_PAGES:
            log.warning(_MAX_LIST_PAGES_WARN.format(pages=_MAX_LIST_PAGES, rows=len(out)))
        return out

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        # S70（实弹发现）：Gitee 系 v5（api_auth=query_token）的建钩必须带布尔位
        # 事件集（issues_events/note_events 等）——events 列表是 GitHub 语义，
        # gitee 全忽略 → issue/评论事件不投递（实弹 hook 建出 push-only）。
        # 布尔位集合与 repo_manager 存量 GitCode 序列逐字节对齐（v5 家族同款）。
        _body = {
            "url": webhook_url, "secret": webhook_secret,
            "events": ["push", "issues", "issue_comment", "pull_request"],
        }
        if getattr(self._card, "api_auth", "") == "query_token":
            _body = {
                "url": webhook_url,
                "password": webhook_secret,
                "push_events": True,
                "issues_events": True,
                "merge_requests_events": True,
                "note_events": True,
                "tag_push_events": True,
                "build_events": True,
                "project_events": True,
                "wiki_events": True,
                "release_events": True,
            }
        resp = self._http("POST", f"/repos/{owner}/{repo}/hooks", api_token, _body)
        return int(resp.get("id", 0) or 0)

    def setup_repo_webhook(self, bot_token, owner, repo, webhook_url):
        """D38/S58 端口：托管仓库 webhook 创建（bot token 鉴权）。

        凭据形态由适配器内聚（D36 同款"能力/形态在适配器，调用方零分支"）：
        Gitee 系 v5 password=bot_token；GitHub secret=""（存量语义）；
        卡平台走基座 generic 形态。调用方（user/db 升级链）只传
        (bot_token, owner, repo, url)，不感知平台。
        """
        return self.setup_webhook(bot_token, owner, repo, webhook_url,
                                  webhook_secret=bot_token)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            self._http("DELETE", f"/repos/{owner}/{repo}/hooks/{hook_id}", api_token)
            return True
        except Exception:
            return False

    def token_storage_field(self) -> str:
        return self._card.token_storage_field

    def webhook_token_field(self) -> str:
        return self._card.webhook_token_field

    def session_token_field(self) -> str:
        return self._card.session_token_field

    def global_bot_meta_key(self) -> str:
        return self._card.global_bot_meta_key

    def global_bot_status_meta_key(self) -> str:
        return self._card.global_bot_status_meta_key

    def pat_create_url(self) -> str:
        return self._card.pat_create_url

    def fetch_user_token(self, username: str, user_id: str) -> str:
        """获取用户 token（带刷新）。解析序 = D10 拍板（无 flag，单序）：
        ① <p>_pat（用户显式配置 = 明确意图，design.md:190）
        ② OAuth 身份（PAT 缺席时兜底，D3 统一入口带刷新）
        命中 level 落 log（分轨观测，P-8 决议口径）。

        actor 语义（D90-N2③ 定性，2026-11-07）：user_id = 凭据归属者
        （credential owner——路径 B 场景即 host_user_id，非 API 调用者）；
        触发者审计走 credential_usage_log.initiating_actor_id 专列。
        不要"修"成双参数——拆 owner/actor 是零消费方设计。
        """
        provider = self._card.provider
        # —— ① PAT（用户显式配置）——
        try:
            cfg = _tokens.get_credential(user_id, provider, kind="pat") or {}
            pat_token = str(cfg.get("token", "") or "")
        except Exception:
            pat_token = ""
        # —— ② OAuth 身份（兜底，带刷新）——
        from src.api.deps import get_user_token_for
        oauth_token = get_user_token_for(username, user_id=user_id, provider=provider) or ""

        if pat_token:
            log.info(f"fetch_user_token hit: provider={provider} level=pat")
            _audit_source(user_id, provider, "pat")
            return pat_token
        if oauth_token:
            log.info(f"fetch_user_token hit: provider={provider} level=oauth (pat miss)")
            _audit_source(user_id, provider, "oauth")
            return oauth_token
        return ""


def _platform_card(provider: str):
    """读平台卡；未绑定 vcs_cards 端口时返回 None。

    主控进程由 assembly 层绑定端口；worker 进程的 MANIFEST 不含
    ``src/api/contracts_wiring.py``，端口永远绑不上。kernel 层不得 import
    domains（R1 架构守卫），所以这里不做「本域默认卡」兜底，而是把
    「没有卡」交回上层：provider 构造抛 PortNotBound，token_service 那些
    入口统一降级为空结果（worker 本来就不做 repo token 链）。
    """
    try:
        return _vcs_cards.get_platform_card(provider)
    except Exception:  # noqa: BLE001 - 端口未绑定/装配缺失都按无卡处理
        return None


def _require_card(provider: str):
    """取卡；取不到时抛 PortNotBound（语义：本进程没有装配层）。"""
    from src.kernel.ports._base import PortNotBound

    card = _platform_card(provider)
    if card is None:
        raise PortNotBound(
            f"kernel port 'vcs_cards' is not bound for provider {provider!r} "
            "(no assembly layer in this process)")
    return card
