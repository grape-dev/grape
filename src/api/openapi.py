"""OpenAPI 定制渲染（Issue-45，M1）。

- 注入三通道 securitySchemes（Bearer / PRIVATE-TOKEN / access_token）。
- **只呈现对外安全路由**（依赖链含 `get_current_user_or_api_key`）：
  管理类/会话认证（`get_current_user`）与无认证路由不进契约，
  避免内部端点经 `/docs` / `/openapi.json` 暴露（issue-45-openapi-api-design.md §3.3/§六）。
- 对外 operation 的 `operation_id` 由 routes 侧显式声明（契约友好，CLI 按 tag+动词命名）。
- 版本号由 FastAPI `app.version` 承载（2.1.0），bump 触发 CLI 自动拉新。
"""
from datetime import datetime, timezone
import json
import os
import re

from fastapi.openapi.utils import get_openapi

# Issue #2237（#1302 移植）：契约侧本地命令清单单一事实源——经 /openapi.json
# info["x-grape-local-cli-commands"] 发布，供 grape-cli 反向消费渲染 [local]。
from src.kernel.help_docs_sync import KNOWN_LOCAL_CLI_COMMANDS
from fastapi.routing import APIRoute

from src.api.contract_versioning import build_changelog, load_contract_baseline

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

# FastAPI 路由模板 `/contents/{path:path}` → OpenAPI 路径 `/contents/{path}`
_PATH_PARAM_CONV = re.compile(r"\{(\w+):\w+\}")


def is_http_method(method: str) -> bool:
    """HTTP 方法白名单判定（openapi 渲染 / 登记清单校验 / 回归测试共用口径）。

    QA 复审判断项(d)：_METHODS（7 种，含 head/options）与测试侧 5 种白名单口径曾
    不一致——共用本判定，防 method 合法性口径漂移。
    """
    return str(method or "").lower() in _METHODS

# FastAPI 路由模板 `/contents/{path:path}` → OpenAPI 路径 `/contents/{path}`
_PATH_PARAM_CONV = re.compile(r"\{(\w+):\w+\}")



def _openapi_path(path: str) -> str:
    return _PATH_PARAM_CONV.sub(lambda m: "{" + m.group(1) + "}", path)

_SECURITY_SCHEMES = {
    "apiKey":      {"type": "http", "scheme": "bearer"},
    "privateKey":  {"type": "apiKey", "in": "header", "name": "PRIVATE-TOKEN"},
    "accessToken": {"type": "apiKey", "in": "query", "name": "access_token"},
}


def _iter_deps(dependant):
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep
        yield from _iter_deps(dep)


def _iter_routes(route):
    """递归产出 APIRoute（兼容 fastapi>=0.140 的惰性 `_IncludedRouter` 包装：

    新版 fastapi `include_router` 以 `original_router.routes` 惰性挂载，
    `app.routes` 里是 `_IncludedRouter` 而非 APIRoute，需展开后做对外判定。
    """
    if isinstance(route, APIRoute):
        yield route
        return
    orig = getattr(route, "original_router", None)
    if orig is not None:
        for r in getattr(orig, "routes", []) or []:
            yield from _iter_routes(r)


# 视为「对外安全路由」的认证依赖名（契约收录范围）
_PUBLIC_AUTH_DEPS = frozenset({
    "get_current_user_or_api_key",
    # Issue #3855：官方技能改名/详情同时服务普通用户与管理台 JWT（admin_accounts）。
    "get_current_user_or_api_key_or_admin",
    "get_user_or_none",  # a2a 软认证（JWT/session/api key 任一；None 交给内部 bot 鉴权）
    # Issue #3396：角色标签写权限收紧依赖（平台管理员 JWT / 用户认证二选一，
    # session_token 体内拒绝）——挂载端点对外（PUT /runtimes/{id}/tags）
    "_resolve_user_or_platform_admin",
})

# session 强制但依赖链不可见（体内 `_repo_token`/`_repo_platform_token` 判 auth_type）的 operation_id。
# 新增此类端点须在此登记（或纳入顺带路径规则），`x-token-scope` 才不会被误标为 dual（见
# docs/weekly-03/openapi-x-token-scope-design.md §3.2 强制机制）。
_SESSION_ONLY_OPERATIONS = frozenset({
    # git-repo 传输层（git_repo_api.py，体内 _repo_token 强制 session）
    "gitrepo_repo", "gitrepo_branches", "gitrepo_commits", "gitrepo_tree",
    "gitrepo_contents", "gitrepo_contents_root", "gitrepo_labels_list", "gitrepo_labels_create",
    "gitrepo_milestones", "gitrepo_issues_list", "gitrepo_issues_create",
    "gitrepo_issues_get", "gitrepo_issues_update", "gitrepo_issues_comments_list",
    "gitrepo_issues_comments_create", "gitrepo_pulls_list", "gitrepo_pulls_create",
    "gitrepo_pulls_get", "gitrepo_pulls_update", "gitrepo_pulls_merge",
    "gitrepo_pulls_comments_list", "gitrepo_pulls_comments_create", "gitrepo_pulls_files",
    "gitrepo_pulls_comments_update", "gitrepo_pulls_comments_delete",
    # CLI 本地 git 凭据/工作区（repos.py，体内 _repo_platform_token 强制 session）
    "repos_git_credential", "repos_workspace",
    # Agent 执行期动作（tasks.py / issues.py，体内 _require_session_repo_token / auth_type 强制 session）
    "tasks_current", "tasks_notify",
    # awaiting-input / pending-review 归属 issues（等用户双态），session_only 显式登记
    "issues_awaiting-input", "issues_pending-review", "issues_review-pass", "issues_review-reject",
})

# session_only 的路径前缀规则（覆盖表补充；新增 git-repo 端点只要进此命名空间即自动标注）
_SESSION_ONLY_PATH_PREFIXES = ("/api/git-repo/",)


def route_is_public(route) -> bool:
    """路由是否对外：依赖链中含对外认证依赖即收录进契约。"""
    if not isinstance(route, APIRoute):
        return False
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    return bool(names & _PUBLIC_AUTH_DEPS)


def token_scope(route) -> str | None:
    """推导对外路由的 token 作用域（`x-token-scope`）：

    - 依赖链命中 `require_no_session_token` / `require_no_session_token_or_admin`
      → user_only（D 类管理端点；后者额外放行管理台 JWT，Issue #3855）
    - operation_id 在 `_SESSION_ONLY_OPERATIONS` 或路径前缀命中
      `_SESSION_ONLY_PATH_PREFIXES`                          → session_only（git-repo 传输/凭据）
    - 依赖链命中 `get_current_user_or_api_key` / `get_current_user_or_api_key_or_admin`
      / `get_user_or_none`                                   → dual
    - 其余（依赖链无对外认证）                               → None（不进契约或未知）
    """
    if not isinstance(route, APIRoute):
        return None
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    op_id = getattr(route, "operation_id", "") or ""
    path = getattr(route, "path", "") or ""
    if op_id in _SESSION_ONLY_OPERATIONS or path.startswith(_SESSION_ONLY_PATH_PREFIXES):
        return "session_only"
    if "require_no_session_token" in names or "require_no_session_token_or_admin" in names:
        return "user_only"
    # Issue #3396：写权限收紧依赖同样体内拒绝 session_token（账户/平台管理语义）→ user_only
    if "_resolve_user_or_platform_admin" in names:
        return "user_only"
    if names & _PUBLIC_AUTH_DEPS:
        return "dual"
    return None


def _event_only_openapi_paths(prefixes=("/api/a2a",)) -> dict:
    """把只注册在 Event 进程的路由并入契约（仅 schema，不注册真实路由）。

    Issue #9427（flask-blog 流水线停在 issue 节点）根因：/api/a2a 在 #699 Phase 4
    四进程拆分后只挂在 Event 进程（:38082，nginx 也把 /api/a2a 路由到 Event），
    而 CLI 是按 Mgmt 的 /openapi.json 生成命令的 —— 契约里没有 /api/a2a，
    `grape a2a handoff` 命令就不存在，agent 无法交办下一个节点，流水线直接停住
    （agent 结构化产出里写着「契约 2.2.0 无 a2a/tasks 命令」）。

    这里用同一个 router 在一个临时 app 上生成 schema，按前缀挑出 a2a 条目补进契约。
    只影响 /openapi.json 内容，Mgmt 进程不会因此新增真实端点。
    """
    try:
        from fastapi import FastAPI

        from src.domains.runtime.routes import webhook
    except Exception:  # pragma: no cover - 契约合并失败不应影响 /openapi.json
        return {}

    sub = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    sub.include_router(webhook.router)
    sub_schema = get_openapi(title=sub.title, version=sub.version, routes=sub.routes)

    merged: dict = {}
    for path, methods in (sub_schema.get("paths") or {}).items():
        if not any(path.startswith(p) for p in prefixes):
            continue
        ops = {m: op for m, op in methods.items() if m in _METHODS and isinstance(op, dict)}
        if not ops:
            continue
        for op in ops.values():
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
            # a2a 走 get_user_or_none 软认证 → dual（JWT/session/api key 任一）
            op.setdefault("x-token-scope", "dual")
        merged[path] = ops
    return merged


def _side_process_openapi_paths(router, prefixes) -> dict:
    """把只注册在其它进程（Event / Worker / Insight）的路由并入契约（仅 schema）。

    四进程拆分后，a2a 只在 Event、runtime 全量路由只在 Worker、/api/obs 只在 Insight，
    而 CLI 是按 Mgmt 的 /openapi.json 生成命令的 —— 不合并就会出现「端点在跑但命令
    不存在」（Issue #9427：agent 拿不到 a2a 命令，流水线停在 issue 节点）。

    这里只在临时 app 上生成 schema，按前缀挑出条目补进契约；Mgmt 进程不会因此
    新增真实端点，管理面「不被业务面 HTTP 调用」的约定不受影响。

    Args:
        router: 目标进程的 APIRouter
        prefixes: 需要并入的路径前缀
    """
    try:
        from fastapi import FastAPI
    except Exception:  # pragma: no cover
        return {}

    scopes: dict = {}
    for r in getattr(router, "routes", []) or []:
        if not isinstance(r, APIRoute):
            continue
        sc = token_scope(r)
        for m in (getattr(r, "methods", None) or set()):
            if m.lower() in _METHODS:
                scopes[(_openapi_path(getattr(r, "path", "")), m.lower())] = sc

    sub = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    try:
        sub.include_router(router)
        sub_schema = get_openapi(title=sub.title, version=sub.version, routes=sub.routes)
    except Exception:  # pragma: no cover - 契约合并失败不应影响 /openapi.json
        return {}, {}

    merged: dict = {}
    for path, methods in (sub_schema.get("paths") or {}).items():
        if not any(path.startswith(p) for p in prefixes):
            continue
        ops = {}
        for m, op in methods.items():
            if m not in _METHODS or not isinstance(op, dict):
                continue
            scope = scopes.get((path, m))
            if not scope:
                continue  # 非对外认证路由（内部/工具端点）不并入
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
            op.setdefault("x-token-scope", scope)
            ops[m] = op
        if ops:
            merged[path] = ops
    # 子 app 的 components.schemas 必须一起并入，否则 $ref 悬空（CLI/agent 解析不到 body 结构）
    sub_schemas = dict((sub_schema.get("components", {}) or {}).get("schemas") or {})
    return merged, sub_schemas


import pathlib as _pl
import subprocess  # x-source-commit git fallback (PR #1847 review M1)
_REPO_ROOT = str(_pl.Path(__file__).resolve().parents[2])
CONTRACT_BASELINE_PATH = os.path.join(_REPO_ROOT, "contracts", "openapi_baseline.json")


# ===================== Issue #1863：契约元信息 + 完整性基线 =====================

# 与 grape-cli registry.GROUP_ALIAS 对齐：命令组名 = operation_id 首段（gitrepo → git-repo）
_GROUP_ALIAS = {"gitrepo": "git-repo"}

_source_commit_cache: str | None = None


def _source_commit() -> str:
    """契约产物来源 commit：环境注入优先，git rev-parse 兜底，未知则 'unknown'。"""
    global _source_commit_cache
    if _source_commit_cache is None:
        v = os.environ.get("GRAPE_SOURCE_COMMIT") or os.environ.get("GIT_COMMIT")
        if not v:
            try:
                out = subprocess.run(
                    ["git", "-C", _REPO_ROOT, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    v = out.stdout.strip()
            except Exception:
                v = ""
        _source_commit_cache = v or "unknown"
    return _source_commit_cache


def contract_command_groups(schema: dict) -> set[str]:
    """契约命令组集合：按 grape-cli 命令树推导口径（operation_id 首段 + gitrepo 别名）。

    CLI 侧（registry.parse_operations）把含 "api_" 的自动生成丑名过滤出命令树，
    服务端基线校验沿用同一口径，保证「命令组」与用户可见命令一一对应。
    """
    groups: set[str] = set()
    for methods in (schema.get("paths") or {}).values():
        if not isinstance(methods, dict):
            continue
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            oid = op.get("operationId") or ""
            if not oid or "api_" in oid:
                continue
            head = oid.split("_")[0]
            groups.add(_GROUP_ALIAS.get(head, head))
    return groups


def attach_contract_meta(schema: dict) -> dict:
    """契约透出元信息（Issue #1863 ③）：x-endpoint-count / x-groups / x-source-commit / x-generated-at。

    - 一律走 OpenAPI 扩展位 x- 前缀（docs/weekly-03/openapi-x-token-scope-design.md 仓库口径，
      与 x-token-scope 同一先例）——严格校验器不忽略非 x- 根字段
    - x-endpoint-count = paths 数（与客户端 `grape refresh` 展示口径一致）
    - x-groups = 命令组清单（CLI 命令树口径，见 contract_command_groups）
    """
    schema["x-endpoint-count"] = len(schema.get("paths") or {})
    schema["x-groups"] = sorted(contract_command_groups(schema))
    schema["x-source-commit"] = _source_commit()
    schema["x-generated-at"] = datetime.now(timezone.utc).isoformat()
    return schema


class ContractDegradedError(RuntimeError):
    """契约降级（Issue #1863 ② / #2080 B）：命令组集合不一致或端点数偏离基线，fail-fast 拒绝出契约。

    `.report` 为 `contract_integrity()` 完整性报告（missing_groups / extra_groups /
    endpoint_count / baseline_endpoint_count 等）。
    """

    def __init__(self, report: dict):
        self.report = report
        parts = []
        missing = ", ".join(report["missing_groups"])
        if missing:
            parts.append(f"missing command groups [{missing}]")
        extra = ", ".join(report.get("extra_groups") or [])
        if extra:
            parts.append(f"unexpected command groups [{extra}]")
        # Issue #2081：登记操作缺失明细可定位（AC2：operationId + method + path）
        missing_ops = report.get("missing_operations") or []
        if missing_ops:
            parts.append("missing required operations [" + ", ".join(
                fmt_operation(m["operationId"], m["path"], m["method"])
                for m in missing_ops) + "]")
        parts.append(
            f"endpoint_count={report['endpoint_count']} "
            f"(baseline exact={report.get('baseline_endpoint_count')}, "
            f"baseline groups={len(report['baseline_groups'])})"
        )
        super().__init__(
            "OpenAPI contract degraded: " + "; ".join(parts) + ". "
            "Refusing to serve a contract that deviates from the baseline "
            "(see contracts/openapi_baseline.json and "
            "contracts/required_operations.json). For intentional changes, "
            "re-baseline: python scripts/regenerate_openapi.py "
            "--update-baseline --reason '<contract change note>'"
        )


_baseline_cache: dict | None = None


def load_contract_baseline() -> dict:
    """加载契约基线（Issue #1863 ①）：contracts/openapi_baseline.json，版本化入库。

    基线文件缺失/损坏均显式报错（fail-closed）——基线是完整性校验的前提，
    宁可显式失败也不能静默跳过校验后对外出降级契约。
    """
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache
    try:
        with open(CONTRACT_BASELINE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"contract baseline missing: {CONTRACT_BASELINE_PATH} — restore the "
            "versioned baseline, or regenerate after intentional endpoint changes: "
            "python scripts/regenerate_openapi.py --update-baseline "
            "--reason '<contract change note>'")
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"contract baseline corrupt: {CONTRACT_BASELINE_PATH}: {e}")
    if (not isinstance(data.get("groups"), list) or not data["groups"]
            or not all(isinstance(g, str) and g for g in data["groups"])
            or not isinstance(data.get("endpoint_count"), int)
            or not isinstance(data.get("min_endpoint_count"), int)):
        raise RuntimeError(
            f"contract baseline malformed (groups/endpoint_count/min_endpoint_count): "
            f"{CONTRACT_BASELINE_PATH}")
    _baseline_cache = data
    return data




# ===================== Issue #2081：必需操作登记清单（单一事实源） =====================

# contracts/required_operations.json：runbook/skill 引用面全量操作登记（含 #1173
# 的 5 条与 #897 的 runtime 2 条）。门禁脚本（scripts/check-openapi-endpoints.py）、
# 渲染层完整性校验（contract_integrity）与回归测试（tests/test_contract_command_groups_
# 1934.py）共用本清单——三处常量口径收敛为一处，任一登记操作被删即红。
REQUIRED_OPERATIONS_PATH = os.path.join(_REPO_ROOT, "contracts", "required_operations.json")

# QA 复审遗留2（PR 评论 200396669）：登记清单自我缩水防线——最小条数下限
# （对称 #1151 端点基线下限口径）。直接从清单删条目（数据面缩水）在旧口径下
# 门禁不拦（43/43 仍绿，评审漏看即静默降级）；下限落在代码常量，合法缩水须同 PR
# 显式改本常量（代码 diff 即评审面）。现网 44 条，下限留 4 条合法调整余量。
MIN_REQUIRED_OPERATIONS = 40

_required_operations_cache: dict | None = None


def load_required_operations() -> dict[str, tuple[str, str]]:
    """加载必需操作登记清单（Issue #2081 单一事实源），返回 {operationId: (path, method)}。

    清单缺失/损坏/条目非法/重复定义均显式报错（fail-closed）——清单是三类门禁的
    共同前提，宁可显式失败也不能静默跳过校验。删除登记项属契约变更，须
    `--update-baseline --change-note` + PR 评审说明，禁止静默下调。
    """
    global _required_operations_cache
    if _required_operations_cache is not None:
        return _required_operations_cache
    try:
        with open(REQUIRED_OPERATIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"required operations registry missing: {REQUIRED_OPERATIONS_PATH} — "
            "restore the versioned registry (Issue #2081 single source of truth)")
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"required operations registry corrupt: {REQUIRED_OPERATIONS_PATH}: {e}")
    ops_raw = data.get("operations")
    if not isinstance(ops_raw, list) or not ops_raw:
        raise RuntimeError(
            f"required operations registry malformed (operations): "
            f"{REQUIRED_OPERATIONS_PATH}")
    result: dict[str, tuple[str, str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for entry in ops_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"required operations registry malformed entry: {entry!r}")
        oid = entry.get("operationId")
        method = str(entry.get("method") or "").lower()
        path = entry.get("path")
        if (not isinstance(oid, str) or not oid or "api_" in oid
                or not isinstance(path, str) or not path.startswith("/")
                or not is_http_method(method)):
            raise RuntimeError(
                f"required operations registry malformed entry: {entry!r} "
                "(need operationId/method/path)")
        if oid in result or (method, path) in seen_pairs:
            raise RuntimeError(
                f"required operations registry duplicate entry: {oid} ({method} {path})")
        result[oid] = (path, method)
        seen_pairs.add((method, path))
    if len(result) < MIN_REQUIRED_OPERATIONS:
        # QA 复审遗留2：数据面缩水 fail-closed——渲染层/门禁/测试三处同步拦截
        raise RuntimeError(
            f"required operations registry shrank below floor: {len(result)} < "
            f"{MIN_REQUIRED_OPERATIONS} ({REQUIRED_OPERATIONS_PATH}) — intentional "
            "shrinkage must lower MIN_REQUIRED_OPERATIONS in the same reviewed "
            "change (silent registry shrinkage is a contract regression, "
            "QA review item 200396669)")
    _required_operations_cache = result
    return result


def operations_of(paths: dict) -> dict[str, tuple[str, str]]:
    """从契约 paths 提取 {operationId: (path, method)}（跳过无 operationId 条目）。

    QA 复审判断项(a)：渲染层 contract_integrity 与门禁脚本（scripts/check-openapi-
    endpoints.py）原本逐字双实现本映射——上移共享，与「单一事实源」目标对齐。
    """
    ops: dict[str, tuple[str, str]] = {}
    for path, methods in (paths or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if isinstance(op, dict) and op.get("operationId"):
                ops[op["operationId"]] = (path, method)
    return ops


def fmt_operation(oid: str, path: str, method: str) -> str:
    """操作三元组可定位格式（门禁失败信息 / 降级错误信息共用）："oid (METHOD path)"。

    QA 复审判断项(b)：该格式串曾在 openapi.py 与门禁脚本 5 处重复——提取共享。
    """
    return f"{oid} ({str(method).upper()} {path})"


def write_contract_integrity_baseline(schema: dict, change_reason: str,
                                      path: str | None = None) -> dict:
    """受控重定基通道：按当前渲染结果重写组级完整性基线（contracts/openapi_baseline.json）。

    QA G-5 补口：该基线此前无生成器覆盖，端点有意增删后只能手改 JSON，门禁长期红。
    纪律与原 `help/api/openapi-baseline.json` 一致——`change_reason` 必填留痕，
    只允许由 `scripts/regenerate_openapi.py --update-baseline --reason '<说明>'` 触发。
    """
    global _baseline_cache
    reason = " ".join((change_reason or "").split())
    if not reason:
        raise RuntimeError(
            "contract integrity baseline re-baseline requires a non-empty change reason "
            "(contracts/openapi_baseline.json is the completeness gate's reference; "
            "silent re-baseline turns the gate into decoration)")
    target = path or CONTRACT_BASELINE_PATH
    endpoint_count = len(schema.get("paths") or {})
    data = {
        "contract_version": ((schema.get("info") or {}).get("version") or ""),
        "endpoint_count": endpoint_count,
        "min_endpoint_count": int(endpoint_count * _BASELINE_FLOOR_RATIO),
        "groups": sorted(contract_command_groups(schema)),
        "change_reason": reason,
        "source_commit": _source_commit(),
    }
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _baseline_cache = None
    return data


def contract_integrity(schema: dict) -> dict:
    """完整性校验（Issue #1863 ① / #2080 B + #2081 清单接入）：命令组集合一致 + 端点数与基线精确相等 + 登记清单全量在场。

    - 命令组集合双向钉死：整组缺失（baseline - 当前）或新增（当前 - baseline）均判不完整
    - 端点数精确相等：任何方向偏离（含组内零散增删端点）均判不完整——
      有意增删端点走 `scripts/regenerate_openapi.py --update-baseline --reason`
      受控重定基通道（#2080：防同号不同容契约静默漏过下限式校验）
    - Issue #2081：登记清单（contracts/required_operations.json）内任一操作缺失
      （operationId 消失或 method/path 漂移）→ 判不完整——runbook/skill 引用面全量，
      缺项即红（S15 删 7 端点全部漏检的补口）。
    - `min_endpoint_count` 仅作报告字段保留（旧下限口径的观测值，不再参与判定）
    """
    baseline = load_contract_baseline()
    required = load_required_operations()
    groups = contract_command_groups(schema)
    endpoint_count = len(schema.get("paths") or {})
    baseline_endpoint_count = int(baseline["endpoint_count"])
    min_endpoint_count = int(baseline["min_endpoint_count"])
    baseline_groups = set(baseline["groups"])
    missing_groups = sorted(baseline_groups - groups)
    extra_groups = sorted(groups - baseline_groups)
    seen = operations_of(schema.get("paths") or {})
    missing_operations = sorted(
        ({"operationId": oid, "method": method, "path": path}
         for oid, (path, method) in required.items()
         if seen.get(oid) != (path, method)),
        key=lambda m: m["operationId"])
    complete = (not missing_groups and not extra_groups
                and not missing_operations
                and endpoint_count == baseline_endpoint_count)
    return {
        "complete": complete,
        "endpoint_count": endpoint_count,
        "baseline_endpoint_count": baseline_endpoint_count,
        "min_endpoint_count": min_endpoint_count,
        "groups": sorted(groups),
        "baseline_groups": sorted(baseline_groups),
        "missing_groups": missing_groups,
        "extra_groups": extra_groups,
        "missing_operations": missing_operations,
    }


# min_endpoint_count = 观测值 × 该比例：仅作报告字段保留（旧下限口径的观测值，
# #2080 B 起不参与判定——判定口径为 endpoint_count 与基线精确相等，任何方向偏离均判
# 降级；有意增删端点须走 --update-baseline --reason 受控重定基）
_BASELINE_FLOOR_RATIO = 0.9




def custom_openapi(app, check_baseline: bool = True):
    """定制 /openapi.json 渲染：注入 securitySchemes + 只保留对外安全路由 + 逐操作 security。"""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = _SECURITY_SCHEMES
    # 契约 info 扩展（Issue #2237 #1302 条件 A）：发布 CLI 本地命令清单，值直接取自
    # KNOWN_LOCAL_CLI_COMMANDS（单一事实源）——禁止在此硬编码第二份清单。
    schema.setdefault("info", {})["x-grape-local-cli-commands"] = list(KNOWN_LOCAL_CLI_COMMANDS)

    public_ops = {
        (_openapi_path(r.path), m.lower(), token_scope(r))
        for route in app.routes for r in _iter_routes(route)
        if route_is_public(r)
        for m in (getattr(r, "methods", None) or set())
        if m.lower() in _METHODS
    }
    public_paths = {(path, m) for path, m, _ in public_ops}
    scopes = {(path, m): scope for path, m, scope in public_ops}
    paths = {}
    for path, methods in (schema.get("paths") or {}).items():
        ops = {
            m: op for m, op in methods.items()
            if m in _METHODS and isinstance(op, dict) and (path, m) in public_paths
        }
        if not ops:
            continue
        for m, op in ops.items():
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
            scope = scopes.get((path, m))
            if scope:
                op["x-token-scope"] = scope
        paths[path] = ops

    # 契约完整性（四进程模式）：下列路由只注册在各自进程，但 CLI/agent 按 Mgmt 的
    # /openapi.json 生成命令 —— 不并入就会出现「端点在跑但命令不存在」。
    #   · /api/a2a      → Event 进程（Issue #9427：交办命令缺失，流水线停在 issue 节点）
    #   · /api/runtime  → Worker 进程（runtime 全量：标签/别名/接管等用户态操作）
    #   · /api/obs      → Insight 进程（可观测性 L1 视图）
    # 仅合并 schema，Mgmt 进程不新增真实端点。
    def _merge_router(router, prefixes):
        _paths, _schemas = _side_process_openapi_paths(router, prefixes)
        for _name, _sch in _schemas.items():
            schema.setdefault("components", {}).setdefault("schemas", {}).setdefault(_name, _sch)
        for _p, _ops in _paths.items():
            existing = paths.get(_p) or {}
            for _m, _op in _ops.items():
                existing.setdefault(_m, _op)
            if existing:
                paths[_p] = existing

    try:
        from src.domains.runtime.routes import webhook as _a2a_mod
        _merge_router(_a2a_mod.router, ("/api/a2a",))
    except Exception:
        pass
    try:
        from src.domains.runtime.routes import runtime as _runtime_mod
        _merge_router(_runtime_mod.router, ("/api/runtime",))
    except Exception:
        pass
    try:
        from src.domains.insight.routes import observability as _obs_mod
        _merge_router(_obs_mod.router, ("/api/obs",))
    except Exception:
        pass

    schema["paths"] = paths
    # Issue #2784：契约根透出 x-changelog（版本 -> 新增/删除/弃用端点清单），
    # 供 CLI `refresh --diff` 直接读取变更摘要。基线取现网发布快照
    # （help/api/openapi-baseline.json），缺失/损坏时降级为空清单而非报错。
    # Issue #2784：x-changelog 基线取全量发布快照（help/api/openapi-baseline.json）；
    # 完整性门禁的组级基线（contracts/openapi_baseline.json）不含 paths，两口径分离。
    try:
        import json as _json
        _snap = _json.loads(open(os.path.join(_REPO_ROOT, "help/api/openapi-baseline.json"), encoding="utf-8").read())
        schema["x-changelog"] = build_changelog(
            paths, _snap.get("paths"), version=app.version, meta=_snap.get("info") or {})
    except Exception as _ce:
        schema["x-changelog"] = {"error": f"baseline snapshot unreadable: {_ce}"}
    # Issue #2837（等价 #2783）：挂牌登记表 → deprecated / x-deprecated-since /
    # x-deprecated-replacement（挂牌态随契约发布，可从冻结发布快照机器读取；
    # 登记漂移在契约域 fail-fast）。空登记表时本调用是 no-op。
    from src.domains.contracts.deprecation import apply_deprecation_markup
    apply_deprecation_markup(schema)
    attach_contract_meta(schema)  # Issue #1863 ③：元信息透出（x-endpoint-count/x-groups/x-source-commit/x-generated-at）
    # Issue #1863/#2080/#2081：契约完整性 fail-fast——必需操作缺失/端点数偏离基线即拒出契约。
    # check_baseline=False 供测试/局部渲染上下文跳过（weekly 口径）；生产默认 True。
    if not check_baseline:
        app.openapi_schema = schema
        return schema
    try:
        _report = contract_integrity(schema)
        _bad = _report.get("missing_groups") or _report.get("missing_operations") or _report.get("degraded")
        if _bad:
            raise ContractDegradedError(_report)
    except ContractDegradedError:
        raise
    except Exception as _e:
        raise RuntimeError(f"contract integrity check error: {_e}")
    app.openapi_schema = schema
    return schema
