"""Token 获取统一接口（策略模式 + 职责链）。

Issue #372 R18: 统一 13 个散落函数为 TokenResolver 策略链。

设计文档: docs/weekly-08/token-config/04-token-resolver.md

模块结构:
  VCSProvider (ABC)
    ├── GitCodeProvider
    └── GitHubProvider
  TokenStrategy (基类)
    ├── RepoBotToken             (第 1 级)
    ├── GlobalBotToken           (第 2 级)
    └── UserToken                (第 3 级)
  TokenResult (数据类)
  TokenResolver (统一入口)

三个语义入口:
  get_token()           — 获取可用执行 token（含过期短路）
  read_stored_token()   — 读取存储原始值（不过期检查、不刷新）
  resolve()             — 获取 TokenResult（token + level + provider）
  detect_and_mark_expired() — fail_task 401 统一检测 + 标记
"""

from abc import ABC, abstractmethod

from dataclasses import dataclass, field

from typing import Optional

from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import im as _im
from src.kernel.ports import pipeline as _pipeline
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens
from src.kernel.ports import vcs_cards as _vcs_cards

_CREATE_REPO_KWARG_PROVIDERS = {"gitcode", "github"}


def create_repo_kwargs(provider: str, auto_init: bool = False,
                       default_branch: str = "") -> dict:
    """建仓可选参数（仅支持自定义默认分支/初始化的 provider 才透传）。

    provider 差异收敛在 kernel 层，避免调用方（mgmt/repos.py）出现平台名比较。
    """
    if str(provider or "").lower() in _CREATE_REPO_KWARG_PROVIDERS:
        return {"auto_init": bool(auto_init),
                "default_branch": (default_branch or "main")}
    return {}


def get_bot_identity_token(repo_key: str) -> str:
    """回帖/评论专用：只解析 **bot 身份** token（仓库 bot → 全局 bot），不落用户 token。

    为什么需要（2026-11-15 实测）：路径 B 仓库（managed_repos.host_user_id 非空，如
    songspace/flask-blog）下，执行链按设计跳过 repo/global bot → 落到用户 token，
    于是 agent 处理完的回帖都挂在用户账号名下。回帖语义要求「系统/agent 发言」，
    必须以机器人账号（如 grace_bot）出镜；执行链（clone/推送）仍保持原优先级不变。
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo_key or "")
    if not rk:
        return ""
    try:
        provider = create_provider_from_repo_key(rk)
    except Exception as e:  # noqa: BLE001
        log.debug(f"get_bot_identity_token: provider 解析失败 ({repo_key}): {e}")
        return ""
    for strat in (RepoBotToken(provider, bypass_path_b=True),
                  GlobalBotToken(provider, bypass_path_b=True)):
        try:
            tok = strat.get_token(rk)
        except Exception as e:  # noqa: BLE001
            log.debug(f"get_bot_identity_token: {strat.level} 取 token 失败: {e}")
            tok = ""
        if tok:
            return tok
    return ""


class VCSProvider(ABC):
    """VCS 平台接口。GitCode / GitHub 各自实现。

    策略类通过此接口调用平台能力，不硬编码 provider 分支。
    """

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

    @abstractmethod
    def add_collaborator(self, api_token: str, owner: str, repo: str,
                         username: str, permission: str = "") -> bool:
        """邀 bot 为协作者。幂等：已是协作者返回 True。
        permission 缺省时用各适配器存量默认（Gitee 系 maintainer / GitHub push）。
        """

    def accept_collaborator_invite(self, bot_token: str, owner: str, repo: str) -> bool:
        """D39/S59：接受协作者邀请（bot 被邀后生效）。

        邀请制平台在适配器覆写；generic 直加平台（PUT 即成员）默认 True。
        """

        return True

    @abstractmethod
    def remove_collaborator(self, api_token: str, owner: str, repo: str,
                            username: str) -> bool:
        """移除协作者。best-effort，失败不阻断。"""

    @abstractmethod
    def setup_webhook(self, api_token: str, owner: str, repo: str,
                      webhook_url: str, webhook_secret: str) -> int:
        """创建 webhook，返回 webhook_id。"""

    @abstractmethod
    def remove_webhook(self, api_token: str, owner: str, repo: str,
                       hook_id: str) -> bool:
        """删除 webhook。"""

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

    def create_repo(self, api_token: str, name: str, description: str = "",
                    private: bool = False, owner: str = "", path: str = "") -> dict:
        """D42/S62 端口：在 token 用户名下建仓，返回平台 repo dict。

        owner 非空 = 在指定组织/空间下建仓（平台实现支持时）；path 为仓库 URL 路径
        （GitCode 特有，缺省等于 name）。默认不支持的实现返回 error dict（调用方按
        error 处理）。
        """
        return {"error": f"create_repo unsupported for {self.provider_name}"}

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
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self._card.name} API {method} {path} failed ({e.code}): {e}") from e

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
                for ident in _accounts.get_user_identities_with_tokens(acc["id"]):
                    if ident.get("provider") == self.provider_name and ident.get("access_token"):
                        return ident["access_token"]
        except Exception as e:
            log.warning(f"fetch_owner_identity_token failed (provider={self.provider_name}, "
                        f"owner={owner}): {e}")
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
                    owner="", path="") -> dict:
        """D42/S62 端口：建仓（POST /user/repos，指定 owner 时 /orgs/{owner}/repos）。

        generic 形态：query_token 走 form（Gitee 系 v5 写接口要求），
        其余 JSON；返回平台 repo dict（full_name/owner/name 由调用方归一）。
        """
        data = {"name": name, "description": description or "",
                "private": "true" if private else "false"}
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
        while page <= 20:
            batch = self._http("GET", f"/user/repos?per_page=100&page={page}", api_token)
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
            if len(batch) < 100:
                break
            page += 1
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
        while page <= 20:
            batch = self._http("GET",
                f"/repos/{owner}/{repo}/collaborators?per_page=100&page={page}", api_token)
            if not isinstance(batch, list) or not batch:
                break
            for c in batch:
                if isinstance(c, dict):
                    out.append(c)
            if len(batch) < 100:
                break
            page += 1
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


class GitCodeProvider(GenericVcsProvider):
    """GitCode 平台（D22：GenericVcsProvider 基座 + 存量委托覆盖）。

    基座提供：verify_token（/user 卡鉴权）、存储字段（卡）、fetch_user_token
    （D3 统一入口 → user_credentials PAT）。
    覆盖仅保留协议验证过的存量委托：collaborator 邀请流与 webhook 创建
    （v5 form/事件集语义，repo_manager 存量序列）——批次 1 验证后退役。
    """

    def __init__(self):
        card = _vcs_cards.get_platform_card("gitcode")
        if card is None:
            card = _vcs_cards.build_platform_card("gitcode", {})
        super().__init__(card)

    def get_user_login(self, token: str) -> str:
        # D85 迁移修复：身份探测委托存量网络缝 repo_manager.get_user_gitcode_login
        # （与 GitHubProvider 对称；verify_token 直连导致 mock 缝失效/实网调用）
        return _repos.get_user_gitcode_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        ok, _err = _repos.add_collaborator(api_token, owner, repo, username, permission)
        return ok

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            _repos.remove_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def accept_collaborator_invite(self, bot_token, owner, repo):
        # D39/S59：存量邀请制语义（v5 邀请列表→匹配→接受）
        return _repos.accept_invite_for_repo(bot_token, owner, repo)

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="", auto_init=False, default_branch="main") -> dict:
        # D42/S62：存量委托（v5 form 语义；owner=组织空间，path=仓库 URL 路径）
        return _repos.create_repo(api_token=api_token, name=name, description=description,
                                  private=private, owner=owner, path=path,
                                  auto_init=auto_init, default_branch=default_branch)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        # D42/S62：存量委托（含 429/5xx 退避）
        return _repos.find_existing_webhook(api_token, owner, repo, webhook_url)

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        # D42/S62：存量委托（成员仓库+is_admin 标注）
        return _repos.fetch_repos_with_role(api_token)

    def get_collaborator_permission(self, api_token, owner, repo, username):
        # D42/S62：存量委托（返回 permission dict，含 access_level）
        return _repos.get_member_permission(api_token, owner, repo, username)

    def list_collaborators(self, api_token, owner, repo):
        # D44/S66：存量委托（feishu_async 存量实现，含分页与字段形态）
        return _im._fetch_gitcode_collaborators(owner, repo, api_token)

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        return _repos.setup_webhook(api_token, owner, repo, webhook_url, webhook_password=webhook_secret)

    def setup_repo_webhook(self, bot_token, owner, repo, webhook_url):
        # D38/S58：存量形态 v5 password=bot_token（形态在适配器，调用方零分支）
        return _repos.setup_webhook(bot_token, owner, repo, webhook_url, webhook_password=bot_token)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            _repos.remove_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False

class GitHubProvider(GenericVcsProvider):
    """GitHub 平台（D22：GenericVcsProvider 基座 + 存量委托覆盖，同 GitCode 说明）。"""

    def __init__(self):
        card = _vcs_cards.get_platform_card("github")
        if card is None:
            card = _vcs_cards.build_platform_card("github", {})
        super().__init__(card)
        # resolve_repo_token/fetch_owner_identity_token 走 GenericVcsProvider 基类：
        # 卡能力位 owner_identity_token_fallback=true（configs/platforms/github.json）
        # 派生回落语义，基类默认实现按 provider_name="github" 查 external_identities
        # ——与原 db.get_github_token 尾段（D32-补丁）同链等价。

    def get_user_login(self, token: str) -> str:
        return _repos.get_user_github_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="push"):
        return _repos.add_github_collaborator(api_token, owner, repo, username, permission)

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            _repos.remove_github_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def accept_collaborator_invite(self, bot_token, owner, repo):
        # D39/S59：存量邀请制语义（PATCH /user/repository_invitations）
        return _repos.accept_github_invite(bot_token, owner, repo)

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="", auto_init=False, default_branch="") -> dict:
        # D42/S62：存量委托（owner=组织空间；GitHub 仓库路径恒等于 name，path 忽略）
        return _repos.create_github_repo(api_token=api_token, name=name, description=description,
                                         private=private, owner=owner, path=path,
                                         auto_init=auto_init)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        # D42/S62：存量委托（FR-5.1 空列表=None 不误判）
        return _repos.find_existing_github_webhook(api_token, owner, repo, webhook_url)

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        # D42/S62：存量委托
        return _repos.fetch_github_repos_with_role(api_token)

    def get_collaborator_permission(self, api_token, owner, repo, username):
        # D42/S62：存量委托（返回 role_name 字符串）
        return _repos.get_github_collaborator_permission(api_token, owner, repo, username)

    def list_collaborators(self, api_token, owner, repo):
        # D44/S66：存量委托（feishu_async 存量实现，含分页与字段形态）
        return _im._fetch_github_collaborators(owner, repo, api_token)

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        return _repos.setup_github_webhook(api_token, owner, repo, webhook_url, webhook_secret)

    def setup_repo_webhook(self, bot_token, owner, repo, webhook_url):
        # D38/S58：存量形态 secret=""（bot token 不作 webhook 凭据，形态在适配器）
        return _repos.setup_github_webhook(bot_token, owner, repo, webhook_url, "")

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            _repos.remove_github_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False

def create_provider(provider: str) -> VCSProvider:
    """根据 provider 标识创建 VCSProvider 实例（D22 统一工厂）。

    - 覆盖层注册表（S72 收口：if 链→注册表，新增平台不再改本函数体）：
      gitcode/github/gitlab = GenericVcsProvider 子类（方言偏离基座的显式
      例外——gitcode/github 存量委托、gitlab /projects 形态）；
    - config-only 平台（gitee 等）：GenericVcsProvider（纯卡驱动，零代码）；
    - 未知 provider：GitCodeProvider（存量兜底语义不变）。
    """
    cls = _PROVIDER_OVERRIDES.get(provider)
    if cls is not None:
        return cls()
    try:
        card = _vcs_cards.get_platform_card(provider)
    except Exception:
        card = None
    if card is not None:
        return GenericVcsProvider(card)
    # D105i：逃逸口可观测——纯配卡平台静默落 GitCode 兜底 = 卡缺能力字段，
    # 是“github 系假设”类静默错误的源头，必须留痕而非无声吞掉
    log.warning("provider factory fallback: provider=%r has no card override/module, "
                "returning GitCodeProvider (check platform card completeness)", provider)
    return GitCodeProvider()

def create_provider_from_repo_key(repo_key: str) -> VCSProvider:
    """根据 repo_key 前缀创建 VCSProvider（D21 补：卡驱动工厂，消二分）。

    repo_key 前缀 → create_provider 统一分派：config-only 平台（gitee 等）
    返回 GenericVcsProvider（卡能力），存量三平台返回原类（行为不变）。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, _, _ = parse_repo_key(repo_key or "")
    return create_provider(provider)

@dataclass
class TokenResult:
    """token 获取结果，携带级别信息。"""
    token: str = ""               # token 字符串（向后兼容：薄包装取 .token 返回 str）
    level: str = ""               # "session" / "repo_bot" / "global_bot" / "user"（执行链实际生效级别，受过期短路影响）
    provider: Optional[VCSProvider] = None  # VCSProvider 实例
    repo_key: str = ""            # 仓库标识
    expired: bool = False         # 是否已标记过期（用户 token status=4 时 expired=True 但仍返回 token）
    issue_id: str = ""            # R20.6.1: 自循环检测用——is_self_loop_actor 需要查 running task
    config_level: str = ""        # FR-10.3: 配置级别（不受过期短路影响，供自循环检测读取）

class TokenStrategy:
    """token 获取策略基类。每个子类代表 fallback 链中的一级。

    方法分层契约（关键——决定了上层 get_token / read_stored_token 的语义分离）：
    - _fetch(): 纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
      供 read_stored_token() 和 get_token() 共用，是两者的公共底层。
    - _is_expired(): 查过期标记。只读标记字段，不调 VCS API。
    - get_token(): = _fetch() + _is_expired() 检查。过期则短路返回空串。
      这是"执行 token"语义——需要当前可用的 token。
    """

    error_code = ""  # 子类覆盖

    def __init__(self, provider: VCSProvider):
        self.provider = provider

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。_fetch 读值 → _is_expired 检查过期 → 过期则短路返回空串。"""
        token = self._fetch(repo_key)
        if token and self._is_expired(repo_key):
            return ""  # 短路：已标记过期，不返回
        return token

    def _normalize_repo_key(self, repo_key: str) -> str:
        """标准化 repo_key（to_repo_key）。空则返回空串。子类复用避免重复 import。"""
        from src.kernel.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        return rk or ""

    def _is_path_b(self, repo_key: str) -> bool:
        """路径 B 仓库判断（str(host_user_id or "") not in ("", "0")）。子类复用避免重复 import get_host_user_id。"""
        return str(self._get_host_user_id(repo_key) or "") not in ("", "0")

    def _get_host_user_id(self, repo_key: str) -> "str | int":
        """获取 host_user_id。子类复用避免重复 import。"""
        return _tokens.get_host_user_id(repo_key)

    @abstractmethod
    def _fetch(self, repo_key: str) -> str:
        """纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。"""
        raise NotImplementedError

    def _is_expired(self, repo_key: str) -> bool:
        """查过期标记。只读标记字段，不调 VCS API 验证。默认 False。"""
        return False

    def mark_expired(self, repo_key: str):
        """子类实现：标记 token 过期。"""
        pass

    def clear_expired(self, repo_key: str):
        """子类实现：清除过期标记。"""
        pass

    @property
    def level(self) -> str:
        """策略级别标识。子类覆盖。"""
        return ""

class RepoBotToken(TokenStrategy):
    """第 2 级：managed_repos.{token_storage_field}

    bypass_path_b=True：回帖/评论专用身份解析——路径 B（用户自己的仓库）也允许用 bot 身份。
    默认 False 保持历史语义（路径 B 降级为用户 token），避免改到执行链。
    """

    error_code = "REPO_TOKEN_EXPIRED"

    def __init__(self, provider: VCSProvider, bypass_path_b: bool = False):
        super().__init__(provider)
        self.bypass_path_b = bypass_path_b

    @property
    def level(self) -> str:
        return "repo_bot"

    def _fetch(self, repo_key: str) -> str:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        # 路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）；
        # 回帖身份解析传 bypass_path_b=True —— agent 的评论必须来自 bot 账号。
        if self._is_path_b(repo_key) and not self.bypass_path_b:
            return ""
        field = self.provider.token_storage_field()  # bot_gitcode_token / bot_github_token
        # S82：grape_t_repo_credentials 唯一权威（rc 优先无条件；列=历史回落）
        # D99：cred_type 归一='bot'（值域 {'bot','webhook'}，平台由 repo_key 承载）
        try:
            _rc = _tokens.get_repo_credential_meta(repo_key, "bot")
            if _rc and _rc.get("value"):
                # D90-N4（拍板 1A/2B/3A）：bot token 消费审计（全量，Core 通道）
                try:
                    from src.kernel.persistence.credential_usage_log import log_usage
                    log_usage(_rc.get("id") or "", 0,
                              credential_kind="repo_credentials",
                              executor_context="token_resolve",
                              operation="repo_bot_hit", repo_key=repo_key)
                except Exception:
                    pass  # 审计失败不阻断
                return _rc["value"]
        except Exception as _rc_err:
            log.warning(f"RepoBotToken: grape_t_repo_credentials read failed: {_rc_err}")
        # F4/D106（终态第一批）：卡列空平台（gitee/gitlab）无物理列——
        # legacy 列回落分支对其不存在，直接返回空（不触发下方 SQL 报错噪音）。
        if not field:
            return ""
        # ------------------------------------------------------------------
        # LEGACY 列回落分支（F4/D106 显式标记）：
        # 仅为 gitcode/github 存量行服务；gitee/gitlab 侧由 D106 回填迁移
        # （db_setup.backfill_gitee_gitlab_token_columns_to_rc）迁入 rc 后
        # 写路径置空，本分支对其零命中。
        # 独立消费审计（operation=repo_bot_hit_legacy_column，与 rc 命中的
        # repo_bot_hit 区分）：回填窗口内旧列消费不留审计盲区。
        # 退出 checklist（满足后本分支可删）：
        # ①回填迁移全量盖章 → ②写路径轮换置空存量列 → ③生产日志连续
        #   观察期零 "historical column fallback hit"（下线判据）→
        # ④删除本分支 + 评估 DROP COLUMN。
        # ------------------------------------------------------------------
        try:
            # managed_repos 归 mgmt：列名与语句都由归属域持有（本文件不再拼 SQL）。
            # 任意拓扑原语：自有 DSN → mgmt 连接；单库回落进程库（同一份语句）。
            raw = _repos.repo_bot_token_from_legacy_column_any(repo_key, field)
            if raw:
                # 挂账 #12 观测：log.warning 即采集（下线判据 = grep 此日志零命中）
                log.warning(f"RepoBotToken: historical column fallback hit "
                            f"(rc miss) repo={repo_key} field={field}")
                # N3：managed_repos 列同走通道加密，读侧解密（明文兼容覆盖存量）
                # F4/D106：legacy 列命中独立消费审计（回填期旧列消费可观测，
                # 与 rc 命中审计分离——旧列消费清零是回落分支下线前置）
                try:
                    from src.kernel.persistence.credential_usage_log import log_usage
                    log_usage("", 0, credential_kind="repo_credentials",
                              executor_context="token_resolve",
                              operation="repo_bot_hit_legacy_column",
                              repo_key=repo_key)
                except Exception:
                    pass
                return _tokens.repo_secret_decrypt(raw)
        except Exception as e:
            log.warning(f"RepoBotToken._fetch: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return False
        try:
            return _repos.repo_bot_token_status_any(repo_key) == "expired"
        except Exception as e:
            log.debug(f"RepoBotToken._is_expired: {e}")
        return False

    def mark_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "expired")
        except Exception as e:
            log.warning(f"RepoBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "")
        except Exception as e:
            log.warning(f"RepoBotToken.clear_expired: {e}")

class GlobalBotToken(TokenStrategy):
    """第 3 级：全局 bot token（meta 表 {global_bot_meta_key}）

    bypass_path_b=True：回帖/评论专用身份解析（理由同 RepoBotToken）。
    """

    error_code = "GLOBAL_TOKEN_EXPIRED"

    def __init__(self, provider: VCSProvider, bypass_path_b: bool = False):
        super().__init__(provider)
        self.bypass_path_b = bypass_path_b

    @property
    def level(self) -> str:
        return "global_bot"

    def _fetch(self, repo_key: str) -> str:
        # 路径 B 仓库跳过全局 bot token（§13.1）；回帖身份解析允许 bypass。
        if self._is_path_b(repo_key) and not self.bypass_path_b:
            return ""
        # 复用现有 _cached_get_meta 缓存逻辑
        # S87/D63：统一 get_global_bot_token_for（meta 派生键 + 存量两平台文件 fallback）
        token = _tokens.get_global_bot_token_for(self.provider.provider_name)
        # D91：缺失告警（路径 A 全局 bot 是主力凭据，静默空串会让"配置错误不可见"
        # ——失败只在任务报错时暴露，运维无从区分"未配置"与"配置错/被误删"）。
        # 路径 A UserToken 不生效，此处空串=策略链必然失败，告警是唯一观测点。
        if not token:
            log.warning(
                f"global bot token missing for provider={self.provider.provider_name} "
                f"(repo_key={repo_key}): 路径 A 仓库策略链将无可用凭据——"
                f"请检查 meta key global_bot_{self.provider.provider_name}_token 或文件 fallback"
            )
        return token

    def _is_expired(self, repo_key: str) -> bool:
        status_key = self.provider.global_bot_status_meta_key()
        try:
            return _tokens.get_meta_value(status_key) == "expired"
        except Exception:
            return False

    def mark_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        try:
            _tokens.set_meta_value(status_key, "expired")
            _tokens.invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        try:
            _tokens.set_meta_value(status_key, "")
            _tokens.invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.clear_expired: {e}")

class UserToken(TokenStrategy):
    """第 4 级：用户 token（external_identities，带刷新）。

    特殊行为：
    - _fetch 内部调 get_user_token_with_refresh（含 OAuth 自动刷新）
    - R25.3: _is_expired 检查 status=4 → 执行链短路返回空串（与其他层一致）
    - 回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过过期检查（best-effort）
    - 新增 _fetch_stored() 方法：只读 external_identities.access_token，不触发 refresh。
      供 read_stored_token() 调用。
    """

    error_code = "TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "user"

    def _fetch(self, repo_key: str) -> str:
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return ""  # 路径 A 不走用户 token
        try:
            account = _accounts.get_grape_account_by_id(host_user_id)
            if account and account.get("username"):
                return self.provider.fetch_user_token(account["username"], host_user_id)
        except Exception as e:
            log.warning(f"UserToken._fetch: {e}")
        return ""

    def _fetch_stored(self, repo_key: str) -> str:
        """只读 external_identities.access_token（解密），不触发 OAuth refresh。

        供 read_stored_token() 调用——A2A 鉴权 / session 写入只需原始值。
        """
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return ""
        try:
            account = _accounts.get_grape_account_by_id(host_user_id)
            if not account:
                return ""
            # external_identities 归 mgmt：列序与 status IN (1, 4) 口径由归属域持有
            raw = _accounts.stored_access_token_any(account["id"], self.provider.provider_name)
            if raw:
                return _tokens.decrypt(raw)
        except Exception as e:
            log.warning(f"UserToken._fetch_stored: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        """R25.3: 检查 status=4（token 过期），执行链短路返回空串。

        与 RepoBotToken / GlobalBotToken 三层一致——
        过期标记后 get_token() 短路，不再返回过期 token。

        回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过此检查，
        保留 best-effort 发评论能力。
        """
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return False  # 路径 A 仓库不走用户 token
        return _accounts.is_identity_expired(host_user_id, self.provider.provider_name)

    def mark_expired(self, repo_key: str):
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") not in ("", "0"):
            _accounts.mark_identity_expired(host_user_id, self.provider.provider_name)

    def clear_expired(self, repo_key: str):
        """用户重新绑定时 create/update_external_identity 已恢复 status=1，无需额外操作。"""
        pass

def detect_and_mark_expired(repo_key: str) -> str:
    """模块级便捷入口：按 repo_key 造 provider → TokenResolver 验证并标记过期。

    IM 直连执行（vod/chat handler）401 时需要；实现本是 TokenResolver 的方法，
    原先按模块函数导入必然 ImportError，这里补薄封装，口径与 service.fail_task R18 一致。
    """
    try:
        _provider = create_provider_from_repo_key(repo_key)
        return TokenResolver(_provider).detect_and_mark_expired(repo_key) or ""
    except Exception as e:
        log.warning(f"detect_and_mark_expired({repo_key}) failed: {e}")
        return ""


class TokenResolver:
    """token 获取统一入口。按优先级链执行策略，第一个返回非空 token 的策略胜出。

    三个语义入口：
    - get_token(): 获取可用执行 token（含过期短路）。供 pipeline / agent / clone / push 用。
    - read_stored_token(): 读取存储原始值（不过期检查、不刷新）。供 A2A 鉴权 / session 写入用。
    - resolve(): 获取 TokenResult（token + level + provider）。供需要级别差异的调用方用。
    - resolve_config_level(): 获取配置级别（不检查过期、不触发 refresh）。供自循环检测用。
    - detect_and_mark_expired(): fail_task 401 统一检测 + 标记。
    """

    def __init__(self, provider: VCSProvider):
        self.provider = provider
        # 策略链序 = 架构常量（D90-N2② 2026-11-07；D101 方案三 2026-11-08
        # 四改三——第 1 级 DevopsAgentSessionToken 退役：R25.9 回声治理后
        # session token 列零写入、策略恒空转，死级清除）。
        # 每级回答一个归属问题——
        # 1 仓库 bot → 2 全局 bot → 3 用户凭据。
        # 变更链序 = 架构变更，须走契约评审（评审载体=
        # docs/weekly-09/d101-task-token-snapshot-retirement-design.md §二）；
        # source/cred_type 只是元数据（审计归因/观测分轨），不驱动排序
        # （动态排序会使命中序列不可比、挂账 #12 零命中判据失效）。
        self.strategies = [
            RepoBotToken(provider),
            GlobalBotToken(provider),
            UserToken(provider),
        ]

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。含过期短路。返回空串=无可用 token。"""
        if not repo_key:
            return ""
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        for strategy in self.strategies:
            token = strategy.get_token(repo_key)
            if token:
                return token
        return ""

    def read_stored_token(self, repo_key: str) -> str:
        """读取存储的原始 token 值。不过期检查、不触发 OAuth refresh、不调 VCS API。

        契约（写死，不可变）：
        1. 不检查过期标记——即使 token 被标记 expired 也返回原始值
        2. 不触发 OAuth refresh——UserToken 策略用 _fetch_stored 跳过 refresh
        3. 走缓存层——_cached_get_meta 的 60s 缓存仍生效
        4. 返回空串=该 repo 无任何 token 存储（不是"过期"，是"未配置"）

        供 A2A 鉴权（get_repo_bot_token_no_global）/ session 写入用。
        """
        if not repo_key:
            return ""
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        for strategy in self.strategies:
            # UserToken 策略用 _fetch_stored 跳过 refresh
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                return token
        return ""

    def resolve(self, repo_key: str) -> TokenResult:
        """获取 TokenResult（token + level + provider + repo_key + expired + config_level）。

        遍历策略链，第一个返回非空 token 的策略胜出。
        返回的 TokenResult 携带级别信息供 TokenActionHandler 分发。
        FR-10.3: config_level 字段填充配置级别（不受过期短路影响）。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        # FR-20.7: config_level 从胜出策略推算，不再单独调 resolve_config_level（避免两次策略链扫描）
        for strategy in self.strategies:
            token = strategy.get_token(repo_key)
            if token:
                expired = strategy._is_expired(repo_key)
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                    expired=expired,
                    config_level=strategy.level,
                )
        # 全部策略未命中：config_level 取 resolve_config_level 结果
        _config_level = self.resolve_config_level(repo_key).level
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key, config_level=_config_level)

    def resolve_config_level(self, repo_key: str) -> TokenResult:
        """获取配置级别 TokenResult（不检查过期、不触发 refresh）。

        供自循环检测（handler.is_self_loop_actor）用——判断仓库的 token 配置级别，
        不需要实际 token 值（可能已过期），只需要知道是路径 A（bot token）还是路径 B（user token）。

        与 resolve() 的区别：
        - resolve() 调 get_token()（含过期短路），token 过期时可能跳到下一级 → level 不准确
        - resolve_config_level() 调 _fetch()（纯读，不检查过期），返回真实配置级别

        UserToken 策略用 _fetch_stored 跳过 OAuth refresh。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        for strategy in self.strategies:
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                # FR-20.10: resolve_config_level 也填充 expired 字段
                # 供 session_store 判断是否写入 session token
                _expired = strategy._is_expired(repo_key) if hasattr(strategy, '_is_expired') else False
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                    expired=_expired,
                )
        # 无 token 配置 → 返回空级别（pipeline 不检测自循环）
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key)

    def detect_and_mark_expired(self, repo_key: str) -> str:
        """fail_task 401 时调用：按优先级找到当前生效的策略，验证 token 有效性后标记过期。

        流程：
        1. 遍历策略链，用 _fetch（纯读，不检查过期标记）找到第一个有 token 的策略
        2. 调 provider.verify_token(token) 验证 token 有效性
           - 验证有效 → 401 是其他原因（权限不足等），不标记过期，返回 ""
           - 验证 401 → 标记过期（幂等：已标记则不重复），返回 error_code
           - 验证超时/5xx → 不标记（R21.3.5），返回 ""（401 可能是其他原因）
        3. 所有策略 _fetch 都返回空 → 返回 "NO_TOKEN_AVAILABLE"（R16.6.1）

        R21.3.5: 区分 401 和超时/5xx——只对 401 标记过期。
        R21.3.3: 原 R18.5.10 "超时视为验证失败" 设计取消。
        """
        for strategy in self.strategies:
            token = strategy._fetch(repo_key)  # 不检查过期标记，查原始值
            if token:
                # 幂等：已标记过则不重复标记（R16.1.6）
                if strategy._is_expired(repo_key):
                    return strategy.error_code  # 已标记，直接返回 error_code
                # 先验证 token 有效性（通过 provider 接口调 VCS API GET /user）
                try:
                    self.provider.verify_token(token)
                    # token 验证有效 → 401 是其他原因，不标记过期
                    return ""
                except Exception as e:
                    # R21.3.5: 区分 401 和超时/5xx
                    http_code = _vcs_cards.extract_http_status_from_exc(e)
                    if http_code == 401:
                        # token 验证 401 → 标记过期
                        strategy.mark_expired(repo_key)
                        return strategy.error_code
                    else:
                        # 超时/5xx → 不标记（R21.3.5），返回空
                        log.warning(f"TokenResolver.detect_and_mark_expired: verify non-401 error for {repo_key}: {str(e)[:100]}")
                        return ""
        # 所有策略 _fetch 都返回空（token 已被短路或未配置）→ 无可用 token
        return "NO_TOKEN_AVAILABLE"  # R16.6.1

class TokenActionHandler(ABC):
    """R18.6.2: token 操作职责链接口。按级别差异化执行 token 相关操作。

    策略链负责"取 token + 级别标识"，职责链负责"按级别执行差异化操作"。
    """

    @abstractmethod
    def is_self_loop_actor(self, actor: str, event_type: str,
                           result: TokenResult) -> bool:
        """R18.6.8: 判断 actor 是否是 agent 回声（自循环检测）。"""

    @abstractmethod
    def get_comment_token(self, result: TokenResult) -> str:
        """R18.6.9: 获取用于发 Issue/PR 评论的 token。
        可能不同于执行 token——例如仓库级 bot 过期时 fallback 到全局 bot。"""

    @abstractmethod
    def post_issue_comment(self, repo_key: str, issue_num: str,
                           body: str, result: TokenResult,
                           agent_type: str = "agent") -> bool:
        """R18.6.10: 发 Issue 评论。按级别差异化处理 actor 和标记。
        成功返回 True，失败返回 False（调用方据此 append RESULT_POST_FAILED）。
        agent_type 透传真实 Agent 身份（如 vod/coding/sre），避免占位「agent agent」。"""

    @abstractmethod
    def mark_expired(self, result: TokenResult) -> str:
        """标记当前级别 token 过期，返回 error_code。"""

    @abstractmethod
    def clear_expired(self, result: TokenResult):
        """清除当前级别过期标记（token 重新配置时调用）。"""

class SessionTokenHandler(TokenActionHandler):
    """D101 方案三：session 级已退役（第 1 级策略删除后 level 值域只剩
    repo_bot / global_bot / user，create_action_handler 不再产出本类）。

    基线保留：类符号与其方法签名仍在此（基线模块/用例按名引用），
    仅移除对已退役 DevopsAgentSessionToken 的依赖——session 列零写入后
    无物可标记/清除，故降级为无副作用。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        return False  # session token 的 actor 取决于 token 所有者，降级为不检测

    def get_comment_token(self, result):
        return ""  # session token scope 可能受限，不用于发评论

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        # session token 通常不用于发评论
        return False

    def mark_expired(self, result):
        # D101 方案三：session 列零写入（upsert 形参已删、解绑清理段已删），
        # 无 session token 可标记——error_code 同步退役（见 test_d101s ③）。
        return ""

    def clear_expired(self, result):
        # 同上：session token_status 不再由本域写入。
        return None

class RepoBotTokenHandler(TokenActionHandler):
    """R18.6.4: 第 2 级仓库级 bot token。单仓库独立 bot 账号。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # actor = managed_repos.bot_username
        if not actor or not result.repo_key:
            return False
        try:
            # managed_repos 归 mgmt（原语句在 kernel 直连进程主库，三库拓扑下恒 1146）
            bot_username = _repos.repo_bot_username_any(result.repo_key)
            return bool(bot_username) and actor == bot_username
        except Exception as e:
            log.debug(f"RepoBotTokenHandler.is_self_loop_actor: {e}")
        return False

    def get_comment_token(self, result):
        # 仓库级 bot 有效 → 用仓库级 bot token
        if result.token and not result.expired:
            return result.token
        # 仓库级 bot 过期 → fallback 到全局 bot
        if result.provider:
            try:
                return _tokens.get_global_bot_token_for(result.provider.provider_name) or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).mark_expired(result.repo_key)
        return "REPO_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).clear_expired(result.repo_key)

class GlobalBotTokenHandler(TokenActionHandler):
    """R18.6.5: 第 3 级全局 bot token。所有路径 A 仓库共用。"""

    def is_self_loop_actor(self, actor, event_type, result):
        if not actor:
            return False
        try:
            # S87/D63：统一口径——provider 的全局 bot username 集合（github 单值源）
            _bot_names = _tokens.get_global_bot_usernames_for(result.provider.provider_name)
            return actor in _bot_names if _bot_names else False
        except Exception:
            return False

    def get_comment_token(self, result):
        # 全局 bot 有效 → 用全局 bot token
        if result.token and not result.expired:
            return result.token
        # 全局 bot 过期 → fallback 到仓库级 bot
        if result.provider and result.repo_key:
            try:
                from src.kernel.token_resolver import RepoBotToken
                _repo_strategy = RepoBotToken(result.provider)
                _repo_token = _repo_strategy._fetch(result.repo_key)
                if _repo_token and not _repo_strategy._is_expired(result.repo_key):
                    return _repo_token
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).mark_expired(result.repo_key)
        return "GLOBAL_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).clear_expired(result.repo_key)

class UserTokenHandler(TokenActionHandler):
    """R18.6.6: 第 4 级用户 token。路径 B 专属。

    核心矛盾：路径 B 下 agent 和用户用同一个 token，actor 完全相同。
    需要引入 task 执行状态作为额外维度（四条件组合判断）。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        # R20.1.1: 四条件组合判断
        if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
            return False
        if not result.repo_key:
            return False
        # 条件 1: 路径 B 仓库（通过 result.level == "user" 已确认）
        # 条件 2: actor == host_vcs_username
        try:
            host_vcs_username = _accounts.get_host_vcs_username(result.repo_key, result.provider.provider_name if result.provider else "")
            if not host_vcs_username or actor != host_vcs_username:
                return False
        except Exception:
            return False
        # 条件 4: 该 issue 有正在执行的 task
        return _pipeline.check_running_task(result.repo_key, result.issue_id or "")

    def get_comment_token(self, result):
        # R25.3: 回帖链 best-effort——直接用 _fetch 读存储 token（绕过 _is_expired 检查）
        # 执行链（get_token/resolve）对 status=4 短路返回空串，但回帖需要试发"Token 已过期"评论
        if result.provider and result.repo_key:
            try:
                _ut = UserToken(result.provider)
                _tok = _ut._fetch(result.repo_key)  # 绕过 _is_expired，best-effort
                if _tok:
                    return _tok
            except Exception:
                pass
        # 用户 token 完全不可用 → fallback 全局 bot token
        if result.provider:
            try:
                return _tokens.get_global_bot_token_for(result.provider.provider_name) or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        try:
            return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)
        except Exception:
            # 用户 token 过期 → 回帖失败 → R20.2 降级处理
            return False

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            UserToken(result.provider).mark_expired(result.repo_key)
        return "TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 用户重新绑定时 create/update_external_identity 已恢复 status=1
        pass

def create_action_handler(result: TokenResult) -> TokenActionHandler:
    """R18.6.7: 根据 TokenResult.level 创建对应 Handler。

    D101 方案三：session 级已退役——第 1 级删除后 level 值域只剩
    repo_bot / global_bot / user（session 分支随之消亡，防复活守卫见
    tests/test_d101s_session_cleanup.py）。
    """
    if result.level == "repo_bot":
        return RepoBotTokenHandler()
    elif result.level == "global_bot":
        return GlobalBotTokenHandler()
    elif result.level == "user":
        return UserTokenHandler()
    return None

def _post_comment_via_provider(provider: VCSProvider, repo_key: str,
                                issue_num: str, body: str, token: str,
                                agent_type: str = "agent") -> bool:
    """R18.6.10 辅助函数：通过 provider 接口发 Issue 评论。

    内部委托 event_pipeline._post_result_to_issue 的评论发送逻辑，
    但只发评论（不含 conclusion 提取等业务逻辑），加 grape-auto-comment 标记。
    agent_type 透传真实 Agent 身份（如 vod/coding/sre），避免占位「agent agent」。
    """
    if not token or not repo_key or not issue_num:
        return False
    try:
        from src.kernel.repo_key import from_repo_key
        _repo = from_repo_key(repo_key)
        if not _repo:
            return False
        # 复用现有 _post_result_to_issue 发评论（含去重、conclusion 提取等逻辑）
        _pipeline._post_result_to_issue(_repo, str(issue_num), body, token, agent_type, repo_key,
                                        agent_result=False)
        return True
    except Exception as e:
        from src.kernel.config import log
        log.warning(f"_post_comment_via_provider failed: {e}")
        return False

def credential_workflows_write(user_id: str, provider: str) -> "bool | None":
    """D105-18：凭据级 workflows 写能力（三态，gh-aw ADR-0002 显式声明制）。

    返回：True=显式声明有写权限（push 不删 workflows）；False=显式声明受限
    （删）；None=未声明（调用方回退平台名旧逻辑，保证存量零回归）。
    高权限是凭据配置时显式声明、可审计的事实——不做运行时内省（平台不
    暴露 token scope）。声明位置：凭据域 <p>_pat 配置的 workflows_write 键。
    """
    try:
        cfg = _tokens.get_credential(user_id, provider, kind="pat") or {}
        if "workflows_write" not in cfg:
            return None
        return cfg.get("workflows_write") is True
    except Exception:
        return None

def _audit_source(user_id: str, provider: str, source: str) -> None:
    """D102-M：用户级 token 命中来源审计（usage_log，失败不阻断）。

    source ∈ {pat, oauth}——fetch_user_token 双通道命中来源进
    credential_usage_log（"配了 PAT 走了 OAuth"类问题可从审计排查）。
    """
    try:
        from src.kernel.persistence.credential_usage_log import log_usage
        log_usage("", user_id, credential_kind="user_credentials",
                  executor_context="token_resolve",
                  operation=f"user_token_hit_{source}", repo_key=f"{provider}:")
    except Exception:
        pass  # 审计失败不阻断业务


class GitLabProvider(GenericVcsProvider):
    """GitLab 平台（S72/D47：GenericVcsProvider 基座 + /projects/:id 形态覆盖）。

    实测驱动（2026-11-05）：基座 github 形态路径（/repos/{o}/{r}/...）对
    GitLab 全断——GitLab REST 是 /projects/{owner}%2F{repo} 家族；/user 的
    登录名字段是 username（非 login）；协作者是 project members（按
    user_id）；建钩 body 带 token + 布尔位事件集（与 Gitee v5 同族教训）。
    基座 _http 已补 private_token → PRIVATE-TOKEN 头（本类直接复用）。
    """

    _GL_MAINTAINER = 40  # GitLab access_level：Developer=30 / Maintainer=40

    def __init__(self):
        card = _vcs_cards.get_platform_card("gitlab")
        if card is None:
            card = _vcs_cards.build_platform_card("gitlab", {})
        super().__init__(card)

    def _pid(self, owner, repo) -> str:
        import urllib.parse as _up
        return f"/projects/{_up.quote(f'{owner}/{repo}', safe='')}"

    def verify_token(self, token: str) -> dict:
        info = self._http("GET", "/user", token)
        if not isinstance(info, dict) or not info:
            raise RuntimeError("GitLab token verify: empty user info")
        return info

    def get_user_login(self, token: str) -> str:
        # GitLab /user 字段 = username（非 login）
        return str(self.verify_token(token).get("username", ""))

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        try:
            users = self._http("GET", f"/users?username={username}", api_token)
            uid = 0
            if isinstance(users, list) and users and isinstance(users[0], dict):
                uid = int(users[0].get("id", 0) or 0)
            if not uid:
                return False
            self._http("POST", f"{self._pid(owner, repo)}/members", api_token,
                       {"user_id": uid, "access_level": self._GL_MAINTAINER})
            return True
        except Exception:
            return False

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            users = self._http("GET", f"/users?username={username}", api_token)
            uid = 0
            if isinstance(users, list) and users and isinstance(users[0], dict):
                uid = int(users[0].get("id", 0) or 0)
            if not uid:
                return False
            self._http("DELETE", f"{self._pid(owner, repo)}/members/{uid}", api_token)
            return True
        except Exception:
            return False

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="") -> dict:
        if owner:
            # GitLab 组织空间建仓需要 namespace_id，未在本次范围内落地 → 显式失败
            return {"error": "GitLab 暂不支持在指定命名空间创建仓库"}
        return self._http("POST", "/projects", api_token,
                          {"name": name, "description": description or "",
                           "visibility": "private" if private else "public"})

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        hooks = self._http("GET", f"{self._pid(owner, repo)}/hooks?per_page=100", api_token)
        if not isinstance(hooks, list):
            return None
        for h in hooks:
            if isinstance(h, dict) and h.get("url") == webhook_url:
                return h.get("id")
        return None

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        projects = self._http("GET", "/projects?membership=true&per_page=100", api_token)
        if not isinstance(projects, list):
            return []
        out = []
        for p in projects:
            if not isinstance(p, dict):
                continue
            full = p.get("path_with_namespace") or ""
            perms = (p.get("permissions") or {})
            proj_access = ((perms.get("project_access") or {}).get("access_level", 0) or 0)
            group_access = ((perms.get("group_access") or {}).get("access_level", 0) or 0)
            out.append({
                "full_name": full, "name": p.get("path", ""),
                "owner": full.split("/")[0] if "/" in full else "",
                "is_admin": max(int(proj_access), int(group_access)) >= self._GL_MAINTAINER,
                "html_url": p.get("web_url", ""),
            })
        return out

    def get_collaborator_permission(self, api_token, owner, repo, username):
        try:
            users = self._http("GET", f"/users?username={username}", api_token)
            uid = 0
            if isinstance(users, list) and users and isinstance(users[0], dict):
                uid = int(users[0].get("id", 0) or 0)
            if not uid:
                return None
            m = self._http("GET", f"{self._pid(owner, repo)}/members/all/{uid}", api_token)
            if isinstance(m, dict):
                lvl = int(m.get("access_level", 0) or 0)
                return {"access_level": lvl, "permission": "admin" if lvl >= self._GL_MAINTAINER else ""}
            return None
        except Exception:
            return None

    def list_collaborators(self, api_token, owner, repo):
        members = self._http("GET", f"{self._pid(owner, repo)}/members/all?per_page=100", api_token)
        if not isinstance(members, list):
            return []
        return [{"login": m.get("username", ""), "id": m.get("id", 0)} for m in members if isinstance(m, dict)]

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        # GitLab 形态：POST /projects/:id/hooks，token=密钥 + 布尔位事件集
        resp = self._http("POST", f"{self._pid(owner, repo)}/hooks", api_token, {
            "url": webhook_url,
            "token": webhook_secret,
            "push_events": True,
            "tag_push_events": True,
            "issues_events": True,
            "note_events": True,
            "merge_requests_events": True,
            "pipeline_events": False,
            "wiki_page_events": True,
            "release_events": True,
        })
        return int(resp.get("id", 0) or 0)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            self._http("DELETE", f"{self._pid(owner, repo)}/hooks/{hook_id}", api_token)
            return True
        except Exception:
            return False

"""Token 获取统一接口（策略模式 + 职责链）。

Issue #372 R18: 统一 13 个散落函数为 TokenResolver 策略链。

设计文档: docs/weekly-08/token-config/04-token-resolver.md

模块结构:
  VCSProvider (ABC)
    ├── GitCodeProvider
    └── GitHubProvider
  TokenStrategy (基类)
    ├── RepoBotToken             (第 1 级，D101 方案三后链首——原 session 级退役)
    ├── GlobalBotToken           (第 2 级)
    └── UserToken                (第 3 级)
  TokenResult (数据类)
  TokenResolver (统一入口)

三个语义入口:
  get_token()           — 获取可用执行 token（含过期短路）
  read_stored_token()   — 读取存储原始值（不过期检查、不刷新）
  resolve()             — 获取 TokenResult（token + level + provider）
  detect_and_mark_expired() — fail_task 401 统一检测 + 标记
"""

# S72：方言覆盖层注册表——key=provider，value=GenericVcsProvider 子类。
# 收敛为数据（消 if 链开闭点）：新平台的方言若偏离基座，注册一行；
# 若不偏离（gitee），零注册零代码走卡。
_PROVIDER_OVERRIDES = {
    "gitcode": GitCodeProvider,
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
}