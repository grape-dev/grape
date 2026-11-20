"""端点弃用期机制（Issue #2837，等价 #2783；arch 形态落位 = 契约域数据面 + 门禁核）。

纪律：端点下线前先在 `contracts/deprecated_endpoints.json` 挂牌（渲染成
`deprecated: true` + `x-deprecated-since` + `x-deprecated-replacement`），
经一个发布期（发布快照 `release_index` 差 >= 1）后才允许从契约删除。

本模块是单一事实源，被三层共用：
- 渲染层 `src/api/openapi.py`：`apply_deprecation_markup`；
- 门禁脚本 `scripts/check-openapi-endpoints.py` / `scripts/regenerate_openapi.py`：
  `check_deprecation_gate`（两条重定基通道共用同一判定，自由文本说明不能绕过）；
- 文档面 `scripts/check-api-docs.py`：`deprecated_doc_annotation_errors`。

观察期基准 = `contracts/released/` 里 `release_index` 最大的冻结发布快照。不用
`help/api/openapi-baseline.json`——它与删除同 PR 刷新，对删除天然失明。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

DEPRECATED_ENDPOINTS_PATH = os.path.join(
    _REPO_ROOT, "contracts", "deprecated_endpoints.json")
DEPRECATED_EXEMPTIONS_PATH = os.path.join(
    _REPO_ROOT, "contracts", "deprecated_exemptions.json")
RELEASED_CONTRACTS_DIR = os.path.join(_REPO_ROOT, "contracts", "released")

# 存量豁免白名单上限：arch 线机制自始生效，没有「机制生效前已下架」的存量
# （weekly 线 #1522 的 19 条 operation 在本线仍在契约中），故上限钉死为 0——
# 任何新豁免都必须同 PR 显式上调本常量（代码 diff 即评审面）。
MAX_DEPRECATION_EXEMPTIONS = 0

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def is_http_method(method: str) -> bool:
    """是否 OpenAPI HTTP 方法（小写比较）。"""
    return str(method).lower() in _HTTP_METHODS


def operations_of(paths: dict) -> dict[str, tuple[str, str]]:
    """paths → {operation_id: (path, method)}（渲染层与门禁脚本共用映射口径）。"""
    ops: dict[str, tuple[str, str]] = {}
    for path, methods in (paths or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if not is_http_method(method) or not isinstance(op, dict):
                continue
            oid = op.get("operationId")
            if oid:
                ops[oid] = (path, method.lower())
    return ops


def fmt_operation(oid: str, path: str, method: str) -> str:
    """operation 三元组格式串（错误信息可定位口径）。"""
    return f"{oid} ({str(method).upper()} {path})"


def load_deprecated_endpoints(path: str | None = None) -> dict[str, dict]:
    """加载挂牌登记表，返回 {operation_id: entry}。

    条目字段：operation_id / method / path / deprecated_since_release（发布快照序号
    release_index，观察期判定口径，与版本号解耦）/ deprecated_since_date（报告字段）/
    replacement / removal_target（可选）。缺失/损坏/条目非法/重复定义均显式报错
    （fail-closed）——挂牌 markup 与观察期门禁共用本表，静默跳过 = 挂牌纪律失效。
    """
    target = path or DEPRECATED_ENDPOINTS_PATH
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"deprecated endpoints registry missing: {target} — restore the "
            "versioned deprecation registry (Issue #2837)")
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"deprecated endpoints registry corrupt: {target}: {e}")
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, list):
        raise RuntimeError(
            f"deprecated endpoints registry malformed (entries): {target}")
    result: dict[str, dict] = {}
    for entry in entries_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"deprecated endpoints registry malformed entry: {entry!r}")
        oid = entry.get("operation_id")
        method = str(entry.get("method") or "").lower()
        path_ = entry.get("path")
        since_release = entry.get("deprecated_since_release")
        since_date = entry.get("deprecated_since_date")
        replacement = entry.get("replacement")
        if (not isinstance(oid, str) or not oid or "api_" in oid
                or not isinstance(path_, str) or not path_.startswith("/")
                or not is_http_method(method)):
            raise RuntimeError(
                f"deprecated endpoints registry malformed entry: {entry!r} "
                "(need operation_id/method/path)")
        if (not isinstance(since_release, int) or isinstance(since_release, bool)
                or since_release < 1):
            raise RuntimeError(
                f"deprecated endpoints registry malformed entry: {entry!r} "
                "(deprecated_since_release must be a positive release_index int)")
        if not isinstance(replacement, str) or not replacement.strip():
            raise RuntimeError(
                f"deprecated endpoints registry malformed entry: {entry!r} "
                "(replacement is mandatory — a deprecated endpoint must point "
                "callers at its successor, Issue #2837 Spec)")
        if not isinstance(since_date, str) or not since_date.strip():
            raise RuntimeError(
                f"deprecated endpoints registry malformed entry: {entry!r} "
                "(deprecated_since_date is a mandatory report field)")
        if oid in result:
            raise RuntimeError(
                f"deprecated endpoints registry duplicate entry: {oid} ({method} {path_})")
        result[oid] = {
            "operation_id": oid,
            "method": method,
            "path": path_,
            "deprecated_since_release": since_release,
            "deprecated_since_date": since_date,
            "replacement": replacement,
        }
        if isinstance(entry.get("removal_target"), int):
            result[oid]["removal_target"] = entry["removal_target"]
    return result


def apply_deprecation_markup(schema: dict) -> dict:
    """按挂牌登记表给契约 operation 挂 deprecated 标记（渲染层）。

    已挂牌 operation 输出：
    - `deprecated: true`（OpenAPI 标准字段，客户端可机器读取）
    - `x-deprecated-since`：挂牌起发布快照序号 release_index（观察期判定口径）
    - `x-deprecated-replacement`：替代端点说明

    登记条目与契约实际位置漂移（operationId 在场但 method/path 不符）→ 显式报错，
    防止误标别的 operation（挂牌是承诺，位置漂移即承诺错位）。
    """
    registry = load_deprecated_endpoints()
    if not registry:
        return schema
    seen = operations_of(schema.get("paths") or {})
    for oid, entry in registry.items():
        actual = seen.get(oid)
        if actual is None:
            continue  # 已删除/未渲染的登记条目由观察期门禁（check_deprecation_gate）处置
        if actual != (entry["path"], entry["method"]):
            raise RuntimeError(
                f"deprecated endpoints registry entry drifted: {oid} registered as "
                f"{fmt_operation(oid, entry['path'], entry['method'])} but rendered at "
                f"{fmt_operation(oid, actual[0], actual[1])} — fix "
                f"{DEPRECATED_ENDPOINTS_PATH} (a deprecation promise must not mark "
                "the wrong operation)")
    for methods in (schema.get("paths") or {}).values():
        if not isinstance(methods, dict):
            continue
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            entry = registry.get(op.get("operationId") or "")
            if entry is None:
                continue
            op["deprecated"] = True
            op["x-deprecated-since"] = entry["deprecated_since_release"]
            op["x-deprecated-replacement"] = entry["replacement"]
    return schema


def load_deprecation_exemptions(path: str | None = None) -> dict[str, dict]:
    """加载存量豁免白名单，返回 {operation_id: entry}。

    只登记机制生效前已下架的历史端点（basis: pre_mechanism）——豁免是对历史存量的
    **一次性**豁免，不是新放行通道：条目数超过 MAX_DEPRECATION_EXEMPTIONS 即
    fail-closed，扩容必须同 PR 显式上调常量（代码 diff 即评审面）。
    缺失/损坏同样 fail-closed。
    """
    target = path or DEPRECATED_EXEMPTIONS_PATH
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"deprecation exemptions registry missing: {target} — restore the "
            "versioned pre-mechanism exemption list (Issue #2837)")
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"deprecation exemptions registry corrupt: {target}: {e}")
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, list):
        raise RuntimeError(
            f"deprecation exemptions registry malformed (entries): {target}")
    result: dict[str, dict] = {}
    for entry in entries_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"deprecation exemptions registry malformed entry: {entry!r}")
        oid = entry.get("operation_id")
        # 注意：豁免表不套用登记表的 "api_" operationId 禁令——历史存量端点的
        # FastAPI 自动生成名（如 im_*_api_im_*_get）合法入册；禁令只约束新挂牌。
        if not isinstance(oid, str) or not oid:
            raise RuntimeError(
                f"deprecation exemptions registry malformed entry: {entry!r} "
                "(need operation_id)")
        if oid in result:
            raise RuntimeError(
                f"deprecation exemptions registry duplicate entry: {oid}")
        result[oid] = entry
    if len(result) > MAX_DEPRECATION_EXEMPTIONS:
        raise RuntimeError(
            f"deprecation exemptions registry must shrink only: {len(result)} > "
            f"{MAX_DEPRECATION_EXEMPTIONS} ({target}) — pre-mechanism exemptions "
            "are a one-time allowance; new removals go through the deprecation "
            "registry and observation window instead (Issue #2837)")
    return result


def load_released_snapshots() -> list[dict]:
    """加载全部冻结发布快照（contracts/released/*.json），按 release_index 升序返回。

    每份快照：{release_index: int>=1, version: str, paths: {...}}。release_index
    必须唯一（重复 = 回填/篡改尝试，fail-closed）。发布后不得改写历史快照——
    删侧观察期判定的全部证据都在这些不可变文件里。
    """
    try:
        names = sorted(os.listdir(RELEASED_CONTRACTS_DIR))
    except FileNotFoundError:
        return []
    snapshots: list[dict] = []
    seen_index: set[int] = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        fpath = os.path.join(RELEASED_CONTRACTS_DIR, name)
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"released contract snapshot corrupt: {fpath}: {e}")
        idx = data.get("release_index")
        if (not isinstance(idx, int) or isinstance(idx, bool) or idx < 1
                or not isinstance(data.get("paths"), dict)
                or not isinstance(data.get("version"), str) or not data.get("version")):
            raise RuntimeError(
                f"released contract snapshot malformed: {fpath} "
                "(need release_index>=1 / version / paths)")
        if idx in seen_index:
            raise RuntimeError(
                f"released contract snapshot release_index duplicated: {idx} "
                f"({fpath}) — released snapshots are frozen and must never be "
                "backfilled or renumbered (Issue #2837)")
        seen_index.add(idx)
        snapshots.append(data)
    snapshots.sort(key=lambda s: s["release_index"])
    return snapshots


def latest_released_snapshot() -> dict | None:
    """release_index 最大的已发布快照（删侧观察期判定基准）；无快照返回 None。"""
    snapshots = load_released_snapshots()
    return snapshots[-1] if snapshots else None


def current_release_index() -> int:
    """正在开发的发布期序号 = 最新已发布快照 release_index + 1（无快照时为 1）。

    观察期口径（与版本号解耦）：挂牌起 release N（deprecated_since_release=N）的端点，
    须保留到 release N 发布，在 release N+1 开发期才允许删除（current - since >= 1）。
    """
    snapshots = load_released_snapshots()
    return (snapshots[-1]["release_index"] + 1) if snapshots else 1


def check_deprecation_gate(current_paths: dict, snapshot_paths: dict,
                           current_release_index: int,
                           registry: dict | None = None,
                           exemptions: dict | None = None) -> list[str]:
    """删侧观察期门禁（核心判定，渲染层/两条重定基通道共用）。

    输入：当前契约 paths、最新已发布快照 paths、当前发布期序号、挂牌登记表、豁免白名单。
    判定矩阵（AC）：
    - 快照有、当前无（删除现在发生）：
      * 快照 op 带 x-deprecated-since（挂牌已随上一发布期对用户可见）→ 绿；
      * 无标记但在挂牌登记表 → 红（挂牌必然未满一发布期：同 PR 挂牌即删，或
        登记起版早于快照却无标记 = 事后补挂，两种都被本分支拦住）；
      * 在豁免白名单（pre_mechanism）→ 绿；
      * 其余 → 红（未挂牌即删除），错误信息含 operationId + method + path。
    - 登记表孤儿条目（当前契约与最新快照均不在场）→ 红（事后补挂/笔误/删除后忘清理）。
    新增端点不归本门禁管（增侧走既有基线刷新纪律，两机制串联而非并列放行口）。
    """
    registry = registry if registry is not None else load_deprecated_endpoints()
    exemptions = (exemptions if exemptions is not None
                  else load_deprecation_exemptions())
    current_ops = operations_of(current_paths)
    snapshot_ops = operations_of(snapshot_paths)
    errors: list[str] = []
    for oid in sorted(set(snapshot_ops) - set(current_ops)):
        path, method = snapshot_ops[oid]
        snap_op = (snapshot_paths.get(path) or {}).get(method) or {}
        mark = snap_op.get("x-deprecated-since") if isinstance(snap_op, dict) else None
        if isinstance(mark, int) and not isinstance(mark, bool) and mark >= 1:
            continue  # 挂牌已随最新发布期可见，观察期已满（release_index 差 >= 1 由单调性保证）
        if oid in registry:
            since = registry[oid].get("deprecated_since_release")
            errors.append(
                f"endpoint removed before its observation window elapsed: "
                f"{fmt_operation(oid, path, method)} — deprecated in release_index="
                f"{since} (current release_index={current_release_index}); a "
                "deprecation must ship in at least one released contract before "
                "the endpoint may be removed (register in "
                "contracts/deprecated_endpoints.json, keep the endpoint in the "
                "next release, then remove; Issue #2837)")
        elif oid in exemptions:
            continue  # pre-mechanism 存量豁免（只减不增白名单）
        else:
            errors.append(
                f"endpoint removed without deprecation registration: "
                f"{fmt_operation(oid, path, method)} — deprecate it first "
                "(deprecated: true + replacement via contracts/"
                "deprecated_endpoints.json), ship one full release, then "
                "remove (Issue #2837挂牌→观察→删除纪律)")
    for oid in sorted(registry):
        if oid not in current_ops and oid not in snapshot_ops:
            entry = registry[oid]
            errors.append(
                f"deprecation registry entry orphaned: "
                f"{fmt_operation(oid, entry.get('path', '?'), entry.get('method', '?'))} "
                "— absent from both the current contract and the latest "
                "released snapshot; remove the entry together with the "
                "endpoint, or (pre-mechanism removals) list it in "
                "contracts/deprecated_exemptions.json instead of "
                "retroactively registering it (Issue #2837)")
    return errors


def write_released_snapshot(schema: dict, target_dir: str | None = None) -> dict:
    """冻结发布快照：contracts/released/{version}.json，release_index = 最大已有 + 1。

    发布时落盘、发布后不得回填（load_released_snapshots 校验 release_index 唯一）。
    快照只冻结门禁所需面（精简）：每 operation 保留 operationId + 挂牌标记
    （deprecated / x-deprecated-since / x-deprecated-replacement），不复制重型
    schema（responses/description 等）——观察期证据是「挂牌标记 + 端点在场性」，
    不是完整契约副本（help/api/openapi-baseline.json 已承担该职责）。
    """
    snapshots = load_released_snapshots()
    version = str((schema.get("info") or {}).get("version") or "").strip()
    if not version:
        raise RuntimeError("cannot freeze release snapshot: schema info.version missing")
    next_index = (snapshots[-1]["release_index"] + 1) if snapshots else 1
    paths_frozen: dict[str, dict] = {}
    for path, methods in (schema.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        frozen_methods = {}
        for method, op in methods.items():
            if not isinstance(op, dict) or not op.get("operationId"):
                continue
            keep = {"operationId": op["operationId"]}
            for key in ("deprecated", "x-deprecated-since", "x-deprecated-replacement"):
                if key in op:
                    keep[key] = op[key]
            frozen_methods[method] = keep
        if frozen_methods:
            paths_frozen[path] = frozen_methods
    snapshot = {
        "release_index": next_index,
        "version": version,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "paths": paths_frozen,
    }
    out_dir = target_dir or RELEASED_CONTRACTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{version}.json")
    if os.path.exists(out):
        raise RuntimeError(
            f"released contract snapshot already exists: {out} — released snapshots "
            "are frozen and must never be rewritten (Issue #2837)")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return snapshot


def deprecated_doc_annotation_errors(registry: dict | None = None,
                                     contract_ops: dict | None = None,
                                     doc_blocks: dict | None = None) -> list[str]:
    """挂牌态文档标注校验（scripts/check-api-docs.py 与 pytest 共用）。

    已挂牌（contracts/deprecated_endpoints.json）且仍在契约的 operation：
    - `help/api` 与 `docs/openapi` 两处端点块须同步标注 Deprecated；
    - 且须写明替代端点（挂牌登记表 replacement 中的 /api/... 路径）。
    边界分工（不与既有文档双向同步门禁互相误红）：
    - 已删除端点 → 残留文档块由文档面同步侧红；删除清理属删侧观察期门禁；
    - 文档块缺失 → 由文档面同步侧红，此处不重复报。

    `doc_blocks` 支持两种形状：`{(method, path): 正文}`（单来源，单测便利）与
    `{(method, path): {来源: 正文}}`（多来源，`src.kernel.help_docs_sync.load_doc_blocks`）。
    """
    if registry is None:
        registry = load_deprecated_endpoints()
    if not registry:
        return []
    from src.kernel.help_docs_sync import (load_contract_ops, load_doc_blocks,
                                           normalize_path)
    if contract_ops is None:
        contract_ops = load_contract_ops()
    if doc_blocks is None:
        doc_blocks = load_doc_blocks()
    errors: list[str] = []
    for oid in sorted(registry):
        entry = registry[oid]
        key = (str(entry.get("method") or "").lower(), normalize_path(entry["path"]))
        if key not in contract_ops:
            continue  # 已删除端点（文档面同步侧管辖）
        raw = doc_blocks.get(key)
        if raw is None:
            continue  # 文档块缺失由文档面同步侧红
        sources = {"": raw} if isinstance(raw, str) else raw
        replacement = str(entry.get("replacement") or "")
        match = re.search(r"(/api/[^\s，。;；)）]+)", replacement)
        for source, body in sources.items():
            label = f" [{source}]" if source else ""
            if "deprecated" not in body.lower():
                errors.append(
                    f"挂牌端点文档未标注 Deprecated{label}: "
                    f"{key[0].upper()} {key[1]} ({oid}) — deprecated endpoints "
                    "must carry a `Deprecated` note in their help/api AND "
                    "docs/openapi block pointing callers at the replacement "
                    "(Issue #2837挂牌→观察→删除)")
                continue
            if match and match.group(1) not in body:
                errors.append(
                    f"挂牌端点文档未写明替代端点{label}: "
                    f"{key[0].upper()} {key[1]} ({oid}) — replacement "
                    f"{match.group(1)} (from contracts/deprecated_endpoints.json) "
                    "must appear in the block (Issue #2837)")
    return errors
