"""Issue #3713 用例共用假环境：一份 rc 行 + 策略链要用的端口假件。

为什么要单独抽出来：本 Issue 的判据是「**同一份数据、两个入口同一个答案**」。
两个入口（换券路由 `_repo_platform_token` / 建树链 `get_task_token`）如果各自
mock 各说各话，用例就永远碰不到分叉点（评审 #202773482 判据）。
所以这里只装一份世界（`CredWorld`），两个入口都读它。

装法说明（踩过的坑）：`src.kernel.ports.*` 是 PEP 562 转发模块，未绑定实现时
`getattr` 直接抛 `PortNotBound`——用它当 `monkeypatch.setattr` 的目标会连
`getattr(..., 默认值)` 都炸。所以假件按端口契约**绑定**（`tokens/repos/accounts`
三个端口），测试结束由 `clear_ports()` 解绑回到「未绑定」基线。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class FakeProvider:
    """够用即可的平台实现：只实现策略链真正会调用的方法（零网络）。"""

    def __init__(self, user_tokens: dict | None = None, provider_name: str = "gitee"):
        self._users = dict(user_tokens or {})
        self._name = provider_name

    @property
    def provider_name(self) -> str:
        return self._name

    def token_storage_field(self) -> str:
        return f"bot_{self._name}_token"

    def webhook_token_field(self) -> str:
        return f"webhook_{self._name}_token"

    def session_token_field(self) -> str:
        return f"repo_{self._name}_token"

    def global_bot_meta_key(self) -> str:
        return f"global_bot_{self._name}_token"

    def global_bot_status_meta_key(self) -> str:
        return f"global_bot_{self._name}_token_status"

    def pat_create_url(self) -> str:
        return ""

    def fetch_user_token(self, username: str, user_id) -> str:
        return self._users.get(str(user_id), "")

    def verify_token(self, token: str) -> dict:
        raise RuntimeError("fake provider: verify_token 不联网")


@dataclass
class CredWorld:
    """一份仓库凭据世界（字段语义与生产数据一一对应）。"""

    rc_value: str | None = None        # None = 无 rc 行；"" = 行存在但值被清空
    rc_undecryptable: bool = False     # 行存在但密文解不开
    rc_row_id: str = "cred-1"
    bot_status: str = ""               # managed_repos.bot_token_status（唯一过期来源）
    legacy_column: str = ""            # managed_repos.bot_<provider>_token 存量列
    global_bot: str = ""               # 全局 bot token
    global_status: str = ""            # 全局 bot 过期标记
    host_user_id: str = ""             # 非空 = path-B（归属者托管）
    host_username: str = "owner-login"
    user_tokens: dict = field(default_factory=dict)   # user_id → 用户 PAT/OAuth


class FakePorts:
    """三个端口的假实现：全部读写同一份 CredWorld。"""

    def __init__(self, world: CredWorld):
        self._w = world

    # ── repos 端口 ──────────────────────────────────────────────────────
    def repo_bot_token_status_any(self, repo_key):
        return self._w.bot_status

    def set_repo_bot_token_status_any(self, repo_key, value):
        self._w.bot_status = value

    def repo_bot_username_any(self, repo_key):
        return self._w.host_username

    def repo_bot_token_from_legacy_column_any(self, repo_key, field):
        return self._w.legacy_column

    # ── tokens 端口 ─────────────────────────────────────────────────────
    def get_repo_credential_meta(self, repo_key, cred_type):
        if self._w.rc_undecryptable:
            return {"value": "", "id": self._w.rc_row_id, "undecryptable": True}
        if self._w.rc_value is None:
            return None
        return {"value": self._w.rc_value, "id": self._w.rc_row_id,
                "undecryptable": False}

    def get_global_bot_token_for(self, provider_name):
        return self._w.global_bot

    def get_global_bot_usernames_for(self, provider_name):
        return [self._w.host_username] if self._w.host_username else []

    def get_meta_value(self, key):
        return self._w.global_status

    def set_meta_value(self, key, value):
        self._w.global_status = value

    def invalidate_global_bot_cache(self):
        return None

    def get_host_user_id(self, repo_key):
        return self._w.host_user_id

    def repo_secret_decrypt(self, raw):
        return raw

    def decrypt(self, raw):
        return raw

    # 凭据使用审计（N4）：假件只需吃掉，不写库
    def credential_usage_log_insert_many(self, items):
        return list(items or [])

    def credential_usage_log_prune(self, *args, **kwargs):
        return 0

    # ── accounts 端口 ───────────────────────────────────────────────────
    def get_grape_account_by_id(self, uid):
        if self._w.host_user_id and str(uid) == str(self._w.host_user_id):
            return {"id": self._w.host_user_id, "username": self._w.host_username}
        return None

    def is_identity_expired(self, uid, provider_name):
        return False

    def stored_access_token_any(self, uid, provider_name):
        return self._w.user_tokens.get(str(uid), "")


def snapshot_ports() -> dict:
    """记录当前端口绑定（含装配层绑定的 vcs_cards 等）。"""
    from src.kernel.ports._base import bound_names, get_binding

    return {name: get_binding(name) for name in bound_names()}


def restore_ports(snapshot: dict) -> None:
    """还原到快照基线。

    为什么不是 `clear_bindings()`（踩过的坑）：那是「全清」，会把装配层
    （`src/api/contracts_wiring.py`）绑的 `vcs_cards` 一并抹掉，于是**同一进程里
    排在后面的用例**再也造不出 provider（`PortNotBound: vcs_cards`），
    而单独跑那个文件却全绿 —— 典型的跨文件污染。这里只还原差异。
    """
    from src.kernel.ports._base import _bind_registry, bound_names, clear_bindings, get_binding

    current = {name: get_binding(name) for name in bound_names()}
    if current == snapshot:
        return
    clear_bindings()
    registry = _bind_registry()
    for name, impl in snapshot.items():
        registry.bind(name, impl, replace=True)


def install(monkeypatch, world: CredWorld) -> FakeProvider:
    """把 world 装进 TokenResolver 真实读路径（端口绑定 + provider 工厂 + 域名面）。"""
    from src.kernel.ports import accounts as accounts_port
    from src.kernel.ports import repos as repos_port
    from src.kernel.ports import tokens as tokens_port
    import src.kernel.token_resolver as tr
    import src.domains.mgmt.user.repo_manager as repo_manager
    import src.domains.mgmt.user.db as user_db
    import src.api.deps as deps
    import src.domains.mgmt.public as mgmt_public

    impl = FakePorts(world)
    repos_port.bind(impl, replace=True)
    tokens_port.bind(impl, replace=True)
    accounts_port.bind(impl, replace=True)

    provider = FakeProvider(world.user_tokens)
    monkeypatch.setattr(tr, "create_provider", lambda name: provider, raising=False)
    monkeypatch.setattr(tr, "create_provider_from_repo_key", lambda rk: provider,
                        raising=False)
    # gitee/gitlab 走 _repo_platform_token 的端口分支（_getters 空 → 端口分支）
    monkeypatch.setattr(repo_manager, "repo_token_getters",
                        lambda provider_name: {}, raising=False)
    # 换券路由的归属者/会话用户档（只在「无 rc 行」时才会走到）
    monkeypatch.setattr(deps, "get_user_token_for",
                        lambda username, user_id="", provider="gitee":
                        world.user_tokens.get(str(user_id), ""), raising=False)
    monkeypatch.setattr(user_db, "get_host_user_id", lambda rk: world.host_user_id,
                        raising=False)
    monkeypatch.setattr(user_db, "get_host_vcs_username", lambda rk: world.host_username,
                        raising=False)
    # 旧实现里换券路由**直接**调这个域名函数读 rc 行（正是本 Issue 要去掉的第二遍实现）；
    # 这里一并替掉，两个入口才读到同一份假数据。新实现只经端口读，不信这条补丁。
    monkeypatch.setattr(user_db, "get_repo_credential_meta",
                        impl.get_repo_credential_meta, raising=False)
    # 换券路由的过期判定也走域名面（`_bot_expired`）——同样是与策略链分叉的第二遍读。
    monkeypatch.setattr(mgmt_public, "repo_bot_token_status_any",
                        impl.repo_bot_token_status_any, raising=False)
    return provider