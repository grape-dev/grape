"""仓库身份值对象与其「建仓响应 → RepoRef」对齐器（Issue #3580）。

问题形状：建仓接口按 provider 分派，但**各平台响应字段名不同**——github/gitcode/gitee
回 `full_name`/`owner`，GitLab 回 `path_with_namespace` 且 owner 藏在 `namespace.full_path`
里。路由层若按某一个平台的字段名取值，另一个平台就拿空串，`to_repo_key("")` 写出空
repo_key，托管记录从此无法被 webhook/同步链路定位（2026-11-20 实锤：托管「成功」但
回调一律 404 repository not managed）。

抽象：仓库身份收敛成一个不可变值对象 `RepoRef`，平台差异只体现在一个
`RepoRefMapper` 上；调用方（路由/托管用例）只认 `RepoRef`，不认平台字段名。
对齐器按平台卡 dialect 注册，**新平台只要声明卡 dialect（或补一个 mapper）**，
路由与托管链路零改动——与 D105-19「卡驱动分派」同一套口径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "RepoRef",
    "RepoRefError",
    "RepoRefMapper",
    "FlatRepoRefMapper",
    "GitLabRepoRefMapper",
    "mapper_for",
    "normalize_repo_ref",
    "normalize_repo_payload",
]


class RepoRefError(ValueError):
    """建仓响应里取不到仓库身份（缺 full_name/owner/name 的可用来源）。"""


@dataclass(frozen=True)
class RepoRef:
    """仓库身份（值对象）。字段一律派生自平台响应，不做平台分支。"""

    provider: str
    owner: str
    name: str
    full_name: str
    default_branch: str = ""
    web_url: str = ""
    native_id: str = ""

    @property
    def repo_key(self) -> str:
        """平台前缀仓储键（空 full_name → 空键，调用方据此 fail-closed）。"""
        from src.kernel.repo_key import to_repo_key

        return to_repo_key(self.full_name, provider=self.provider) if self.full_name else ""

    @property
    def is_complete(self) -> bool:
        return bool(self.full_name and self.owner and self.name)


class RepoRefMapper:
    """平台建仓响应 → RepoRef 的对齐器基类（子类只声明字段落点）。

    每个分量声明的是**候选路径列表**（tuple of paths，逐层下钻）——同一语义在不同
    平台/不同响应版本里键名不同（web_url vs html_url、owner 对象 vs namespace 对象），
    子类按需追加候选，不需要在调用方写 if。
    """

    dialect = ""

    full_name_paths: tuple[tuple[str, ...], ...] = (("full_name",),)
    owner_paths: tuple[tuple[str, ...], ...] = (("owner",),)
    owner_object_keys: tuple[str, ...] = ("login", "username", "name")
    name_paths: tuple[tuple[str, ...], ...] = (("name",), ("path",))
    default_branch_paths: tuple[tuple[str, ...], ...] = (("default_branch",),)
    web_url_paths: tuple[tuple[str, ...], ...] = (("web_url",), ("html_url",))
    native_id_paths: tuple[tuple[str, ...], ...] = (("id",),)

    @staticmethod
    def _dig(raw: Mapping[str, Any], path: tuple[str, ...]) -> tuple[Any, bool]:
        cur: Any = raw
        for key in path:
            if not isinstance(cur, Mapping):
                return "", False
            cur = cur.get(key)
        return (cur if cur is not None else ""), cur is not None and cur != ""

    def _text(self, raw: Mapping[str, Any], paths: tuple[tuple[str, ...], ...]) -> str:
        for path in paths:
            value, ok = self._dig(raw, path)
            if ok and not isinstance(value, Mapping):
                return str(value)
        return ""

    def _owner(self, raw: Mapping[str, Any]) -> str:
        for path in self.owner_paths:
            value, ok = self._dig(raw, path)
            if not ok:
                continue
            if isinstance(value, Mapping):
                for key in self.owner_object_keys:
                    if value.get(key):
                        return str(value[key])
                continue
            return str(value)
        return ""

    def to_ref(self, provider: str, raw: Mapping[str, Any]) -> RepoRef:
        full_name = self._text(raw, self.full_name_paths)
        owner = self._owner(raw)
        name = self._text(raw, self.name_paths)
        if not name and full_name:
            name = full_name.rpartition("/")[2]
        if not owner and full_name:
            owner = full_name.rpartition("/")[0]
        if not full_name and owner and name:
            full_name = f"{owner}/{name}"
        return RepoRef(
            provider=provider,
            owner=owner,
            name=name,
            full_name=full_name,
            default_branch=self._text(raw, self.default_branch_paths),
            web_url=self._text(raw, self.web_url_paths),
            native_id=self._text(raw, self.native_id_paths),
        )


class FlatRepoRefMapper(RepoRefMapper):
    """github / gitcode / gitee 系：平铺 full_name + owner（owner 可能是对象）。"""

    dialect = "github"


class GitLabRepoRefMapper(RepoRefMapper):
    """GitLab：path_with_namespace + namespace.full_path（owner 可为组/子组全路径）。"""

    dialect = "gitlab"
    full_name_paths = (("path_with_namespace",),)
    owner_paths = (("namespace", "full_path"), ("namespace", "path"))
    name_paths = (("path",), ("name",))
    owner_object_keys = ("full_path", "path")


#: dialect → mapper。新平台复用既有响应对齐器时只需在卡里声明 dialect。
_MAPPERS: dict[str, type[RepoRefMapper]] = {
    "github": FlatRepoRefMapper,
    "gitlab": GitLabRepoRefMapper,
    "": FlatRepoRefMapper,
}


def mapper_for(dialect: str) -> RepoRefMapper:
    return _MAPPERS.get((dialect or "").lower(), FlatRepoRefMapper)()


def normalize_repo_ref(provider: str, raw: Mapping[str, Any], dialect: str = "") -> RepoRef:
    """按方言把平台建仓响应归一成 RepoRef（唯一入口，调用方零平台分支）。"""
    if not isinstance(raw, Mapping):
        raise RepoRefError(f"{provider}: 建仓响应不是对象: {type(raw).__name__}")
    return mapper_for(dialect).to_ref(provider, raw)


def normalize_repo_payload(provider: str, raw: Mapping[str, Any], dialect: str = "") -> dict:
    """归一后回填 full_name/owner/name 三个共用键，**原始字段一律保留**。

    保留原字段是刻意的：对外契约（前端展示 web_url/default_branch、webhook 校验
    web_url 实例归属）仍在读平台原生键，这里只补缺口，不改造载荷。
    """
    ref = normalize_repo_ref(provider, raw, dialect)
    out = dict(raw)
    if ref.full_name:
        out.setdefault("full_name", ref.full_name)
        out["full_name"] = out["full_name"] or ref.full_name
    if ref.owner and not out.get("owner"):
        out["owner"] = ref.owner
    if ref.name and not out.get("name"):
        out["name"] = ref.name
    return out