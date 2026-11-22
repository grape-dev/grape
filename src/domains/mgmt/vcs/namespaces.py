"""「创建项目」目标上下文（应用服务）：provider 能力 + 可插拔缓存。

为什么单独一层：`list_create_spaces` 是平台事实（provider 的 NamespaceCapability），
缓存是横切关注点。把两者内联在路由函数里，会让「路由 = 参数整形 + 业务 + 缓存 +
失效」四件事耦合在一起；这里用**组合**（服务持有缓存组件）表达，路由只剩参数整形。

配置（需求方口径）：默认 1800s；meta `create_spaces_cache_ttl_seconds` > env
`GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS` > 默认；配置为 0 = 完全不走缓存。
缓存键含 token 指纹（不落明文）：换 token / 换账号立即 miss，不串用他人空间清单。
"""
from __future__ import annotations

import hashlib
import os
import time

from src.kernel.config import log
from src.kernel.ttl_cache import TTLCache

# 数值来源（复审 B2）：冷路径每次要打 2 次上游（/user 取平台登录名 + 组织清单），
# 2026-11-23 实测自建 worker → gitlab.com 单次 0.72–1.74s，合计 2–4s；1800s 缓存把
# 建仓/查重链的重复成本摊薄到可忽略，同时把「组织成员变化」的最长可见延迟限定在
# 30 分钟内（组织成员变更是低频运维动作，30 分钟内可见可接受）。
DEFAULT_TTL_SECONDS = 1800
# 30s：配置源（DB meta）自身的短缓存，避免每个请求打一次 DB。
_TTL_SOURCE_TTL = 30.0
_ttl_memo: tuple[float, int] = (0.0, DEFAULT_TTL_SECONDS)


def _meta_ttl() -> str:
    """meta 配置源（抽出来便于测试替换，也隔离 DB 依赖）。"""
    try:
        from src.domains.mgmt.user import db as _user_db

        return _user_db.get_meta_value("create_spaces_cache_ttl_seconds") or ""
    except Exception as e:  # noqa: BLE001 - 配置读失败不阻断建仓入口
        log.debug(f"create-targets: meta ttl read failed: {e}")
        return ""


def _resolve_ttl() -> int:
    """解析 TTL（秒）：meta > env > 默认；非法值按默认并留痕；负数按 0。"""
    global _ttl_memo
    now = time.time()
    if now - _ttl_memo[0] < _TTL_SOURCE_TTL:
        return _ttl_memo[1]
    raw = _meta_ttl()
    if str(raw).strip() == "":
        raw = os.environ.get("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", "")
    try:
        ttl = int(str(raw).strip()) if str(raw).strip() != "" else DEFAULT_TTL_SECONDS
    except (TypeError, ValueError):
        log.warning(f"create-targets: 非法缓存 TTL={raw!r}，按默认 {DEFAULT_TTL_SECONDS}s")
        ttl = DEFAULT_TTL_SECONDS
    ttl = max(0, ttl)
    _ttl_memo = (now, ttl)
    return ttl


def _token_fingerprint(api_token: str) -> str:
    return hashlib.sha256((api_token or "").encode()).hexdigest()[:16]


_CACHE = TTLCache("vcs_create_targets", _resolve_ttl)


def _collect(provider: str, api_token: str) -> dict:
    """实时拉取：个人登录名 + 可建仓空间清单（上游往返 3~4s）。

    空间清单以「创建项目组织页」同源的 user-namespaces 注册表兜底
    （2026-11-22）：适配器 list_create_spaces 只有 gitee/gitlab 实现，
    github/gitcode 在基类里恒返回 []，导致同一用户在「新建项目」能看到组织、
    在「一键创建」下拉里却只有个人空间。注册表本身四平台齐备
    （github /user/memberships/orgs、gitcode /users/orgs?admin=true），
    故：适配器有数据用它，为空则回落注册表——gitee/gitlab 行为不变。
    """
    from src.domains.mgmt.vcs import create_provider

    p = create_provider(provider)
    login = ""
    try:
        login = str(p.get_user_login(api_token) or "")
    except Exception as e:  # noqa: BLE001
        log.warning(f"create-targets: 取登录名失败 ({provider}): {e}")
    try:
        spaces = p.list_create_spaces(api_token) if api_token else []
    except Exception as e:  # noqa: BLE001
        log.warning(f"create-targets: 列空间失败 ({provider}): {e}")
        spaces = []
    if not spaces and api_token:
        try:
            from src.domains.mgmt.user import namespaces as _user_ns
            refs = _user_ns.list_namespaces(provider, api_token, {"login": login})
            spaces = [{"path": str(ref.path), "name": str(ref.name or ref.path),
                       "kind": str(ref.kind)} for ref in refs if str(ref.path or "")]
        except Exception as e:  # noqa: BLE001 - 兜底失败不阻断个人空间建仓
            log.warning(f"create-targets: 组织清单兜底失败 ({provider}): {e}")
    # 个人空间由前端固定渲染「个人账号（Personal）」一行；注册表会把个人
    # namespace 也列进来（path == login），这里剔除以免下拉出现重复项。
    if login:
        spaces = [s for s in spaces if str(s.get("path") or "") != login]
    return {"provider": provider, "personal_login": login, "spaces": spaces}


def create_targets(provider: str, api_token: str) -> dict:
    """建仓目标上下文（缓存命中直接返回；TTL<=0 时每次实时拉）。"""
    key = f"{provider}:{_token_fingerprint(api_token)}"
    return _CACHE.get_or_compute(key, lambda: _collect(provider, api_token))


def invalidate(provider: str = "") -> int:
    """清缓存（provider 空 = 全清）。建组/换绑后由调用方主动失效。"""
    return _CACHE.invalidate(f"{provider}:" if provider else "")


def stats() -> dict:
    """观测：命中率与条目数（运维排查「为什么列表没变」用）。"""
    return _CACHE.stats()