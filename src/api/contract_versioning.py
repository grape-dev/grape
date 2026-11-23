"""契约版本号策略（Issue #2784）——单一事实源 + 递进判定。

## 策略（论文级口径见 CONTRIBUTING「契约版本号策略」章节）

1. **从现网递进，禁跳版**：新版本只能从现网版本 `X.Y.Z` 递进到
   `X.Y.(Z+1)`（patch）或 `X.(Y+1).0`（minor）；`2.2.0 -> 2.5.0` 这类跳版
   一律不允许（跳版号从未部署现网，却被 CLI 当成新契约，用户升级提醒指向
   一个不存在的版本）。
2. **预发布双轨（PEP 440）**：测试环境契约带 `aN` / `bN` / `rcN` 后缀，
   排序 `2.2.0 < 2.2.1a1 < 2.2.1b1 < 2.2.1rc1 < 2.2.1`；验证全绿 +
   发布审批后**去后缀转正**（同号正式版），转正后才是现网版本。
3. **单一来源**：所有进程入口（main / grape_server / event / worker /
   insight）、契约聚合（openapi_merge）与健康探针的契约版本号都取
   `CONTRACT_VERSION`，不再各自硬编码字面量——本单修复前仓库里同时存在
   `2.2.0`（5 处）与 `2.2.2`（2 处）两份互相漂移的版本号。

本模块只做纯字符串判定，不读文件、不读环境变量，方便被门禁/测试直接调用。
"""

from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

#: 当前测试线契约版本（未部署现网 → 预发布后缀；转正后改为 "2.3.0"）。
CONTRACT_VERSION = "2.3.0a1"

#: 现网（已发布）契约版本，来自 help/api/openapi-baseline.json 的发布快照。
PRODUCTION_VERSION = "2.2.0"

#: PEP 440 预发布标记，按 SemVer/PEP 440 的升降序排列。
PRERELEASE_LABELS = ("a", "b", "rc")

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")

#: 升序比对用：a < b < rc < 正式版
_LABEL_RANK = {"a": 0, "b": 1, "rc": 2, None: 3}

#: 现网发布快照（上次发布的正式契约）。它是**发布产物**，只在发布构建时刷新，
#: 不随测试线 bump 而改——否则「现网是什么」就无从判定。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BASELINE_PATH = os.path.join(_REPO_ROOT, "help", "api", "openapi-baseline.json")

#: 契约根 x-changelog 的必备键（CLI refresh --diff 按这些键打印摘要）
CHANGELOG_KEYS = ("version", "added", "removed", "deprecated")


class VersionInfo(NamedTuple):
    """解析后的契约版本：`2.3.0a1` -> (2, 3, 0, "a", 1)。"""

    major: int
    minor: int
    patch: int
    label: str | None
    num: int | None


def parse_version(text) -> VersionInfo | None:
    """解析 `X.Y.Z` / `X.Y.Z{a|b|rc}N` -> `VersionInfo`。

    不认识的形状（`2.3` / `v2.3.0` / `.dev1` / 空值）一律返回 None，
    由调用方决定报错文案——本函数不抛异常，便于批量校验。
    """
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return VersionInfo(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
        label=m.group(4),
        num=int(m.group(5)) if m.group(5) is not None else None,
    )


def is_prerelease(text) -> bool:
    """是否为预发布版本（带 a/b/rc 后缀）。不可解析按 False 处理。"""
    parsed = parse_version(text)
    return bool(parsed and parsed.label is not None)


def base_version(text):
    """去掉预发布后缀的正式版号：`2.3.0a1` -> `2.3.0`；不可解析返回 None。"""
    parsed = parse_version(text)
    if not parsed:
        return None
    return f"{parsed.major}.{parsed.minor}.{parsed.patch}"


def _sort_key(parsed: VersionInfo | None) -> tuple:
    if not parsed:
        return ()
    return (parsed.major, parsed.minor, parsed.patch,
            _LABEL_RANK.get(parsed.label, 3), parsed.num or 0)


def compare_versions(left, right) -> int:
    """按 PEP 440 语义比较：左 < 右返回 -1，相等 0，左 > 右 1。

    同号时预发布低于正式版（`2.2.1rc1 < 2.2.1`），预发布之间按
    `a < b < rc` 再按序号。不可解析的值排在最前。
    """
    key_l = _sort_key(parse_version(left))
    key_r = _sort_key(parse_version(right))
    if key_l == key_r:
        return 0
    return -1 if key_l < key_r else 1


def _successor_bases(current: VersionInfo):
    """现网版本的两个合法递进目标号：patch+1 与 minor+1。"""
    return [(current.major, current.minor, current.patch + 1),
            (current.major, current.minor + 1, 0)]


def legal_successors(current) -> list:
    """列出 `current` 之后所有合法的新版本号（按 PEP 440 升序）。

    - 现网正式版 `2.2.0` → `2.2.1{a1,b1,rc1,''}` + `2.3.0{a1,b1,rc1,''}`；
    - 预发布 `2.3.0a1` → 只在自己的目标号上流转（`2.3.0b1/rc1` 与正式 `2.3.0`），
      不再往前递进——预发布还没上现网，不该产生下一个版本号。
    """
    parsed = parse_version(current)
    if not parsed:
        return []
    base = f"{parsed.major}.{parsed.minor}.{parsed.patch}"
    if parsed.label is not None:
        later_labels = [lbl for lbl in PRERELEASE_LABELS
                        if PRERELEASE_LABELS.index(lbl) > PRERELEASE_LABELS.index(parsed.label)]
        return [f"{base}{lbl}1" for lbl in later_labels] + [base]
    result = []
    for base_major, base_minor, base_patch in _successor_bases(parsed):
        base = f"{base_major}.{base_minor}.{base_patch}"
        for lbl in PRERELEASE_LABELS:
            result.append(f"{base}{lbl}1")
        result.append(base)
    return result


def check_successor(current, candidate) -> list:
    """校验 `candidate` 是否为 `current` 的合法递进，返回错误文案列表（空 = 合法）。

    文案直接给可执行修复方向（哪个版本可以升、现网是什么），供护栏测试与
    CONTRIBUTING 引用——不是「版本不合法」这种无法处置的结论。
    """
    cur = parse_version(current)
    cand = parse_version(candidate)
    if cur is None:
        return [f"无法解析现网版本号 {current!r}：应形如 2.2.0（可带 a1/b1/rc1 后缀）"]
    if cand is None:
        return [f"无法解析候选版本号 {candidate!r}：应形如 2.3.0a1 / 2.3.0（不带 v 前缀）"]
    if compare_versions(candidate, current) == 0:
        return [f"版本未递进：候选 {candidate} 与现网 {current} 相同，bump 必须产生新版本号"]
    if compare_versions(candidate, current) < 0:
        return [f"版本回退：候选 {candidate} 低于现网 {current}，契约版本只允许向前递进"]
    if candidate in legal_successors(current):
        return []
    legal_base = [f"{ma}.{mi}.{pa}" for ma, mi, pa in _successor_bases(cur)]
    return [
        f"跳版违规：现网 {current} 只能递进到 "
        f"{legal_base[0]} 或 {legal_base[1]}（可带 a/b/rc 预发布后缀，如 2.3.0a1），"
        f"实际候选 {candidate} 跨过了中间的版本号——中间版本从未部署现网。"
    ]


def promote(prerelease):
    """预发布转正：`2.3.0a1` -> `2.3.0`（测试全绿 + 发布审批后执行）。"""
    base = base_version(prerelease)
    if base is None:
        raise ValueError(f"无法解析版本号 {prerelease!r}")
    return base


def load_contract_baseline(path=None) -> dict:
    """读取现网发布快照，返回 `{"paths": {...}, "meta": {...}}`。

    fail-soft：文件缺失 / 损坏（部署副本可能不带 help/）时返回空结构，
    契约照常渲染——`x-changelog` 只是变更通知，不该成为启动的硬依赖。
    """
    baseline_path = path or DEFAULT_BASELINE_PATH
    empty = {"paths": {}, "meta": {}}
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    paths = data.get("paths")
    if not isinstance(paths, dict):
        paths = {}
    meta = data.get("x-changelog") or data.get("info", {}).get("x-changelog") or {}
    if not isinstance(meta, dict):
        meta = {}
    return {"paths": paths, "meta": meta}


def _is_deprecated(ops) -> bool:
    if not isinstance(ops, dict):
        return False
    return any(isinstance(op, dict) and op.get("deprecated") is True for op in ops.values())


def build_changelog(current_paths, baseline_paths=None, version=None, meta=None) -> dict:
    """生成契约变更摘要（契约根 `x-changelog` 的内容）。

    `baseline_paths` 为 None 表示现网快照不可得：此时 `added`/`removed` 留空
    并置 `baseline_available=False`——把「不知道」明确说成「不知道」，
    而不是把全量端点当成新增（后者会让 CLI 每次 refresh 都报一堆假变更）。
    """
    changelog = {
        "version": version or CONTRACT_VERSION,
        "added": [],
        "removed": [],
        "deprecated": sorted(p for p, ops in (current_paths or {}).items() if _is_deprecated(ops)),
        "baseline_available": baseline_paths is not None,
    }
    meta = meta or {}
    for key in ("change_reason", "source_commit", "updated_at"):
        if meta.get(key):
            changelog[key] = meta[key]
    if baseline_paths is None:
        return changelog
    current = set((current_paths or {}).keys())
    baseline = set(baseline_paths.keys())
    changelog["added"] = sorted(current - baseline)
    changelog["removed"] = sorted(baseline - current)
    return changelog
