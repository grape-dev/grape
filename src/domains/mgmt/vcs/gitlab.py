"""GitLab 方言（/projects/:id 寻址 + 组/子组命名空间）。"""

from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import im as _im
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens

from .base import GenericVcsProvider, VCSProvider, _require_card


class GitLabProvider(GenericVcsProvider):
    """GitLab 平台（S72/D47：GenericVcsProvider 基座 + /projects/:id 形态覆盖）。

    实测驱动（2026-11-05）：基座 github 形态路径（/repos/{o}/{r}/...）对
    GitLab 全断——GitLab REST 是 /projects/{owner}%2F{repo} 家族；/user 的
    登录名字段是 username（非 login）；协作者是 project members（按
    user_id）；建钩 body 带 token + 布尔位事件集（与 Gitee v5 同族教训）。
    基座 _http 已补 private_token → PRIVATE-TOKEN 头（本类直接复用）。
    """

    _GL_MAINTAINER = 40  # GitLab access_level：Developer=30 / Maintainer=40

    def __init__(self, card=None):
        # Issue #3580：一实例一卡——允许传入任意 GitLab 实例卡（如 gitlab-corp）；
        # 缺省仍取内置 gitlab 卡（存量行为不变，缺卡仍 fail-closed）。
        super().__init__(card or _require_card("gitlab"))

    def _pid(self, owner, repo) -> str:
        import urllib.parse as _up
        return f"/projects/{_up.quote(f'{owner}/{repo}', safe='')}"

    def verify_token(self, token: str) -> dict:
        info = self._http("GET", "/user", token)
        if not isinstance(info, dict) or not info:
            raise RuntimeError("GitLab token verify: empty user info")
        return info

    def token_expiry(self, token: str) -> dict:
        """PAT 自身元数据（GitLab 15.4+ `/personal_access_tokens/self`）。

        单独成方法而不是塞进 verify_token：校验身份的调用点不需要多打一次网络；
        只有「绑定凭据」这类要落到期时间的调用方显式取（见 settings.py）。
        读不到（旧版本/非 PAT/权限不足）返回 {}，不阻断绑定。
        """
        try:
            pat = self._http("GET", "/personal_access_tokens/self", token)
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(pat, dict) or not pat.get("expires_at"):
            return {}
        return {
            "expires_at": str(pat.get("expires_at") or ""),
            "scopes": pat.get("scopes") or [],
            "revoked": bool(pat.get("revoked")),
        }

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

    def list_create_spaces(self, api_token: str) -> "list[dict]":
        """GitLab：当前 token 能建仓的组（Developer=30 及以上）。

        用 `/groups?min_access_level=30`（含用户所属的组）；子组不在默认返回里，
        由创建表单允许直接填完整组路径（`grp/sub`）覆盖——建仓时按 full_path 精确
        解析 namespace_id，等价于下拉里选中它。取组失败降级为空（不阻断个人空间建仓）。
        """
        try:
            rows = self._http("GET", "/groups?min_access_level=30&per_page=100", api_token)
        except Exception as e:  # noqa: BLE001
            log.warning(f"gitlab list_create_spaces failed: {e}")
            return []
        out = []
        for g in rows if isinstance(rows, list) else []:
            if not isinstance(g, dict):
                continue
            path = str(g.get("full_path") or "")
            if path:
                out.append({"path": path, "name": str(g.get("name") or path), "kind": "group"})
        return out

    def resolve_namespace(self, api_token: str, owner: str) -> "dict | None":
        """完整组/子组路径 → {id, path}；与 list_create_spaces 同一事实源。

        2026-11-22 实锤：旧实现用 `/namespaces?search=<owner>` 精确比对 full_path，
        对含 "/" 的子组路径返回 0 条 → 下拉列得出、建仓却必 502。改为按
        `/groups/<urlencoded full_path>` 精确解析（GitLab 官方路径寻址），
        `/namespaces?search=` 仅作兜底。fail-closed：解析不到即返回 None。
        """
        import urllib.parse as _up

        if not owner:
            return None
        try:
            group = self._http("GET", "/groups/" + _up.quote(owner, safe=""), api_token)
            if isinstance(group, dict) and \
                    str(group.get("full_path") or "").lower() == owner.lower():
                return {"id": int(group.get("id") or 0),
                        "path": str(group.get("full_path") or owner)}
        except Exception as e:  # noqa: BLE001 - 子组不存在时上游 404 是正常分支
            log.debug(f"gitlab namespace lookup by full_path failed ({owner}): {e}")
        # 兜底：父组可见但路径寻址受限时，用搜索 + 精确比对
        try:
            rows = self._http("GET", f"/namespaces?search={_up.quote(owner, safe='')}&per_page=100",
                              api_token)
        except Exception as e:  # noqa: BLE001
            log.warning(f"gitlab namespace lookup failed ({owner}): {e}")
            return None
        for ns in rows if isinstance(rows, list) else []:
            if isinstance(ns, dict) and str(ns.get("full_path") or "").lower() == owner.lower():
                try:
                    return {"id": int(ns.get("id") or 0),
                            "path": str(ns.get("full_path") or owner)}
                except Exception:  # noqa: BLE001
                    return None
        return None

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="") -> dict:
        """建仓；owner 非空 = 建到指定组/子组（先解析 namespace_id）。"""
        body = {"name": name, "description": description or "",
                "visibility": "private" if private else "public",
                "initialize_with_readme": True}
        if path and path != name:
            body["path"] = path
        if owner:
            ns = self.resolve_namespace(api_token, owner)
            if not ns or not ns.get("id"):
                return {"error": f"所属空间不存在或无建仓权限: {owner}"}
            body["namespace_id"] = ns["id"]
        try:
            info = self._http("POST", "/projects", api_token, body)
        except Exception as e:  # noqa: BLE001
            # 上游拒绝（token 无 create 权限 / 重名 / 组禁建）必须落 error dict：
            # 抛异常会被路由当成 500，用户看不到原因。
            return {"error": f"上游拒绝建仓: {e}"}
        # Issue #3580（2026-11-20 实锤）：GitLab 建仓响应没有 full_name/owner
        # （它叫 path_with_namespace，owner 在 namespace.full_path）。归一细节归
        # `kernel.vcs.repo_ref` 的 GitLabRepoRefMapper（按卡 dialect 注册），这里
        # 只调通用钩子——空 full_name 会让 to_repo_key("") 写出空 repo_key，托管
        # 「成功」而回调一律 404 repository not managed。
        return self.normalize_created_repo(info)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        # Issue #3580 复审：实现收口到 kernel.vcs_gitlab_hooks（与托管流程同源）
        from src.kernel.vcs_gitlab_hooks import find_hook

        try:
            return find_hook(self._card.api_base, api_token, owner, repo, webhook_url)
        except Exception as e:
            log.warning(f"gitlab find_existing_webhook failed {owner}/{repo}: {e}")
            return None

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        # Issue #3580/R5：分页取全（100/页翻到底），不再只取第一页。
        projects: list = []
        page = 1
        while True:
            batch = self._http(
                "GET", f"/projects?membership=true&per_page=100&page={page}", api_token)
            if not isinstance(batch, list):
                break
            projects.extend(batch)
            if len(batch) < 100:
                break
            page += 1
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
        # Issue #3580 复审：实现收口到 kernel.vcs_gitlab_hooks（与托管流程同源）；
        # GitLab 的 token 只能是共享密钥，缺省即报错、绝不复用 api_token。
        from src.kernel.vcs_gitlab_hooks import setup_hook

        return setup_hook(self._card.api_base, api_token, owner, repo, webhook_url, webhook_secret)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        # Issue #3580 复审：收口到 kernel.vcs_gitlab_hooks；404 幂等（返回 True）
        from src.kernel.vcs_gitlab_hooks import remove_hook

        try:
            return remove_hook(self._card.api_base, api_token, owner, repo, hook_id)
        except Exception as e:
            log.warning(f"gitlab remove_webhook failed {owner}/{repo} hook={hook_id}: {e}")
            return False
