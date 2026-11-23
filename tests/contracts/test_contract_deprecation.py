"""Issue #2837（等价 #2783）：端点弃用期机制（挂牌 → 观察一发布期 → 删除）契约门禁单测。

arch 形态落位（与 weekly 线 #1522 功能口径一致，代码路径按 domains 体系重写）：
- 数据面与门禁核 `src/domains/contracts/deprecation.py`（契约域，渲染层/门禁脚本/文档面共用）；
- 渲染层 `src/api/openapi.py` 调用域 markup（deprecated: true + x-deprecated-since
  + x-deprecated-replacement，挂牌态可从冻结发布快照机器读取）；
- 发布快照 `contracts/released/{version}.json`（release_index 单调、发布后不得回填），
  删侧观察期判定基准 = release_index 最大的已发布快照（不用 help/api/openapi-baseline.json
  ——该文件与删除同 PR 刷新，对删除天然失明）；
- 门禁判定矩阵：删除未挂牌端点 → 红；挂牌未满一发布期即删除 → 红；
  挂牌满一发布期后删除 → 绿；存量豁免白名单（pre_mechanism，只减不增）覆盖历史删除；
- 两条重定基通道（check-openapi-endpoints.py --update-baseline /
  regenerate_openapi.py --update-baseline --reason）写基线前调用同一校验函数，
  自由文本说明不得绕过观察期。

测试 seams（公开接口，一 seam 一测试）：
- S1 `src.domains.contracts.deprecation`：load_deprecated_endpoints / apply_deprecation_markup
- S2 `src.domains.contracts.deprecation`：check_deprecation_gate / load_released_snapshots /
  current_release_index / load_deprecation_exemptions（判定矩阵纯函数 + 数据面 fail-closed）
- S3 消费脚本 main(argv)：scripts/check-openapi-endpoints.py（CI 删侧判定 +
  --update-baseline 前置校验 + --require-baseline 缺发布快照即红）、
  scripts/regenerate_openapi.py（--update-baseline --reason 共用核 + --release-snapshot）
- S4 文档面：`src.kernel.help_docs_sync.load_doc_blocks` +
  `src.domains.contracts.deprecation.deprecated_doc_annotation_errors` + scripts/check-api-docs.py
"""
import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO)

from src.domains.contracts import deprecation as dep

_SCRIPT_CHECK = os.path.join(_REPO, "scripts", "check-openapi-endpoints.py")


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _registry_file(monkeypatch, tmp_path, entries):
    """构造挂牌登记表临时文件并指向域模块常量（monkeypatch 自动还原）。"""
    reg = tmp_path / "deprecated_endpoints.json"
    _write_json(reg, {
        "description": "test fixture",
        "entries": entries,
    })
    monkeypatch.setattr(dep, "DEPRECATED_ENDPOINTS_PATH", str(reg))
    return reg


def _paths(ops):
    """构造 OpenAPI paths：ops = [(path, method, operationId), ...]。"""
    paths = {}
    for path, method, oid in ops:
        paths.setdefault(path, {})[method] = {"operationId": oid}
    return paths


def _entry(oid="legacy_op", method="get", path="/api/legacy",
           since_release=1, since_date="2026-11-16",
           replacement="GET /api/legacy-v2", removal_target=None):
    e = {
        "operation_id": oid,
        "method": method,
        "path": path,
        "deprecated_since_release": since_release,
        "deprecated_since_date": since_date,
        "replacement": replacement,
    }
    if removal_target is not None:
        e["removal_target"] = removal_target
    return e


# ===================== S1：挂牌登记表 + 渲染层 markup =====================

def test_registry_entry_marks_operation_deprecated(tmp_path, monkeypatch):
    """登记表条目 → 渲染契约 op 输出 deprecated + x-deprecated-since + x-deprecated-replacement。"""
    _registry_file(monkeypatch, tmp_path, [_entry()])
    schema = {"paths": _paths([
        ("/api/legacy", "get", "legacy_op"),
        ("/api/other", "get", "other_op"),
    ])}
    dep.apply_deprecation_markup(schema)
    op = schema["paths"]["/api/legacy"]["get"]
    assert op["deprecated"] is True
    assert op["x-deprecated-since"] == 1
    assert op["x-deprecated-replacement"] == "GET /api/legacy-v2"
    # 未挂牌 operation 不受影响（不得误标）
    assert "deprecated" not in schema["paths"]["/api/other"]["get"]


def test_registry_rejects_drifted_entry(tmp_path, monkeypatch):
    """登记表条目 path/method 与契约实际位置漂移 → fail-fast（防止误标别的 operation）。"""
    _registry_file(monkeypatch, tmp_path, [_entry(path="/api/elsewhere")])
    schema = {"paths": _paths([("/api/legacy", "get", "legacy_op")])}
    with pytest.raises(RuntimeError, match="legacy_op"):
        dep.apply_deprecation_markup(schema)


def test_registry_loader_fail_closed(tmp_path, monkeypatch):
    """登记表缺失/条目非法 → 显式报错（fail-closed），不静默跳过挂牌 markup。"""
    monkeypatch.setattr(dep, "DEPRECATED_ENDPOINTS_PATH",
                        str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="deprecated endpoints registry"):
        dep.load_deprecated_endpoints()

    _registry_file(monkeypatch, tmp_path, [_entry(since_release=0)])
    with pytest.raises(RuntimeError, match="deprecated_since_release"):
        dep.load_deprecated_endpoints()

    _registry_file(monkeypatch, tmp_path, [_entry(replacement="")])
    with pytest.raises(RuntimeError, match="replacement"):
        dep.load_deprecated_endpoints()


# ===================== S2：发布快照 + 共用门禁核（判定矩阵） =====================

def _snapshot_paths(mark=None):
    """构造最新已发布快照的 paths：legacy_op（可带挂牌标记）+ keep_op。"""
    snap = _paths([
        ("/api/legacy", "get", "legacy_op"),
        ("/api/keep", "get", "keep_op"),
    ])
    if mark is not None:
        snap["/api/legacy"]["get"]["deprecated"] = True
        snap["/api/legacy"]["get"]["x-deprecated-since"] = mark
        snap["/api/legacy"]["get"]["x-deprecated-replacement"] = "GET /api/keep"
    return snap


def _current_without_legacy():
    """当前契约：legacy_op 已被删除（keep_op 仍在）。"""
    return _paths([("/api/keep", "get", "keep_op")])


def test_gate_red_on_removal_without_registration():
    """判定矩阵①：删除未挂牌端点 → 红，错误信息含 operationId + method + path。"""
    errors = dep.check_deprecation_gate(
        _current_without_legacy(), _snapshot_paths(),
        current_release_index=2, registry={}, exemptions={})
    assert len(errors) == 1, errors
    msg = errors[0]
    assert "legacy_op" in msg and "GET" in msg and "/api/legacy" in msg, msg
    assert "deprecat" in msg.lower(), f"错误信息须指向挂牌纪律: {msg}"


def test_gate_red_on_removal_before_window_elapsed():
    """判定矩阵②：挂牌未满一发布期即删除 → 红（登记表挂牌但快照无标记 = 同 PR 挂牌即删）。"""
    registry = {"legacy_op": _entry(since_release=2)}
    errors = dep.check_deprecation_gate(
        _current_without_legacy(), _snapshot_paths(),
        current_release_index=2, registry=registry, exemptions={})
    assert errors, "挂牌未满一发布期即删除必须变红"
    msg = errors[0]
    assert "legacy_op" in msg, msg
    assert "2" in msg, f"错误信息须含挂牌起版（release_index）: {msg}"
    assert "observation" in msg.lower() or "观察" in msg, msg


def test_gate_green_on_removal_after_full_window():
    """判定矩阵③：挂牌满一发布期后删除 → 绿（上一发布快照带机器可读挂牌标记）。"""
    # 快照（release_index=1）已带 x-deprecated-since=1 → 用户已见一版，观察期已满
    errors = dep.check_deprecation_gate(
        _current_without_legacy(), _snapshot_paths(mark=1),
        current_release_index=2, registry={}, exemptions={})
    assert errors == [], errors


def test_gate_green_on_exempted_removal():
    """判定矩阵④：存量豁免白名单（pre_mechanism）覆盖的历史删除 → 绿。"""
    exemptions = {"legacy_op": {"operation_id": "legacy_op", "basis": "pre_mechanism"}}
    errors = dep.check_deprecation_gate(
        _current_without_legacy(), _snapshot_paths(),
        current_release_index=2, registry={}, exemptions=exemptions)
    assert errors == [], errors


def test_gate_red_on_orphaned_registry_entry():
    """登记表条目在当前契约与最新快照均不在场 → 红（事后补挂/笔误/删除后忘清理）。"""
    registry = {"legacy_op": _entry(since_release=1)}
    errors = dep.check_deprecation_gate(
        _current_without_legacy(),
        _paths([("/api/keep", "get", "keep_op")]),  # 快照也无 legacy_op
        current_release_index=2, registry=registry, exemptions={})
    assert errors, "孤儿登记条目必须变红（防事后补挂绕过观察期）"
    assert "legacy_op" in errors[0]


def test_gate_ignores_kept_endpoints_and_additions():
    """边界：未删除端点与新增端点不触发删侧判定（增侧走基线刷新纪律）。"""
    current = _paths([
        ("/api/legacy", "get", "legacy_op"),  # 保留端点（含已挂牌者）不触发删侧判定
        ("/api/keep", "get", "keep_op"),
        ("/api/new", "post", "new_op"),  # 新增侧不归本门禁管
    ])
    errors = dep.check_deprecation_gate(
        current, _snapshot_paths(),
        current_release_index=2, registry={}, exemptions={})
    assert errors == [], errors


# ---------- 发布快照数据面 ----------

def _released_dir(monkeypatch, tmp_path, snapshots):
    d = tmp_path / "released"
    d.mkdir()
    for snap in snapshots:
        _write_json(d / f"{snap['version']}.json", snap)
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(d))
    return d


def test_released_snapshot_latest_and_current_index(tmp_path, monkeypatch):
    """快照加载：latest = release_index 最大者；current_release_index = 最大 + 1。"""
    _released_dir(monkeypatch, tmp_path, [
        {"release_index": 1, "version": "2.2.0", "paths": {}},
        {"release_index": 3, "version": "2.4.0", "paths": {}},
        {"release_index": 2, "version": "2.3.0", "paths": {}},
    ])
    latest = dep.latest_released_snapshot()
    assert latest["release_index"] == 3 and latest["version"] == "2.4.0"
    assert dep.current_release_index() == 4


def test_released_snapshot_missing_dir_defaults(tmp_path, monkeypatch):
    """无已发布快照：latest=None（由 --require-baseline 报红），current_release_index=1。"""
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(tmp_path / "nope"))
    assert dep.latest_released_snapshot() is None
    assert dep.current_release_index() == 1


def test_released_snapshot_rejects_duplicate_index(tmp_path, monkeypatch):
    """release_index 必须唯一单调（重复序号 = 回填尝试）→ fail-closed。"""
    _released_dir(monkeypatch, tmp_path, [
        {"release_index": 1, "version": "2.2.0", "paths": {}},
        {"release_index": 1, "version": "2.2.0-hotfix", "paths": {}},
    ])
    with pytest.raises(RuntimeError, match="release_index"):
        dep.load_released_snapshots()


# ---------- 豁免白名单数据面（只减不增） ----------

def test_exemptions_loader_enforces_shrink_only_cap(tmp_path, monkeypatch):
    """豁免白名单只减不增：条目数超上限 → fail-closed（扩容须同 PR 显式上调常量评审）。"""
    cap = dep.MAX_DEPRECATION_EXEMPTIONS
    exempt = tmp_path / "deprecated_exemptions.json"
    _write_json(exempt, {
        "description": "test",
        "basis": "pre_mechanism",
        "entries": [
            {"operation_id": f"op_{i}", "method": "get", "path": f"/api/op-{i}",
             "basis": "pre_mechanism"}
            for i in range(cap + 1)
        ],
    })
    monkeypatch.setattr(dep, "DEPRECATED_EXEMPTIONS_PATH", str(exempt))
    with pytest.raises(RuntimeError, match="shrink"):
        dep.load_deprecation_exemptions()


# ===================== S3：真实数据面（豁免清单 + 冻结发布快照 + 冻结通道） =====================

# weekly 线 #1522（#2783）在 2.5.0 认作「已下架」的 19 个 operation：arch 线此刻
# 仍在契约中（实测），因此本线没有 pre_mechanism 存量，豁免清单必须为空。
_WEEKLY_REMOVED_OPS = [
    ("get", "/api/dashboard/efficiency"),
    ("post", "/api/chat/im/wechat/login/start"),
    ("get", "/api/im/installations"),
    ("post", "/api/pipeline/handoff"),
    ("get", "/api/runtime/tasks/{task_id}/detail"),
    ("post", "/api/scenes/{scene_id}/pin"),
]


def test_real_exemption_list_is_empty_and_cap_pinned_on_arch_line():
    """仓库 checked-in 豁免白名单：arch 线机制自始生效，无 pre_mechanism 存量 →
    entries 为空且上限钉死为 0（任何新豁免须同 PR 显式上调常量并过评审）。"""
    exemptions = dep.load_deprecation_exemptions()
    assert exemptions == {}, "arch 线无 pre_mechanism 存量，豁免清单须为空"
    assert dep.MAX_DEPRECATION_EXEMPTIONS == 0
    # weekly 线「已下架」的端点在本线仍在契约中 → 不构成历史删除豁免面
    from src.api.openapi import custom_openapi
    from src.api.main import app
    current = custom_openapi(app).get("paths") or {}
    still_present = [
        f"{m.upper()} {p}" for m, p in _WEEKLY_REMOVED_OPS
        if isinstance((current.get(p) or {}).get(m), dict)
    ]
    assert len(still_present) == len(_WEEKLY_REMOVED_OPS), still_present


def test_gate_green_on_real_contract_vs_frozen_snapshot():
    """e2e：当前真实契约 vs checked-in 冻结快照 → 无未挂牌删除，门禁绿。"""
    snapshot = dep.latest_released_snapshot()
    assert snapshot is not None, "contracts/released/ 必须随仓携带冻结发布快照"
    assert snapshot["release_index"] == 1
    from src.api.openapi import custom_openapi
    from src.api.main import app
    current = custom_openapi(app).get("paths") or {}
    errors = dep.check_deprecation_gate(
        current, snapshot["paths"], dep.current_release_index())
    assert errors == [], errors


def test_write_released_snapshot_freezes_next_index_and_marks(tmp_path, monkeypatch):
    """冻结函数：release_index = 最大已有 + 1；挂牌标记机器可读地保留进快照。"""
    d = tmp_path / "released"
    d.mkdir()
    _write_json(d / "2.2.0.json", {"release_index": 1, "version": "2.2.0", "paths": {}})
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(d))
    schema = {
        "info": {"version": "2.3.0"},
        "paths": {
            "/api/legacy": {"get": {
                "operationId": "legacy_op", "description": "heavy", "responses": {},
                "deprecated": True, "x-deprecated-since": 2,
                "x-deprecated-replacement": "GET /api/keep",
            }},
            "/api/keep": {"get": {"operationId": "keep_op", "description": "heavy"}},
        },
    }
    snap = dep.write_released_snapshot(schema)
    assert snap["release_index"] == 2 and snap["version"] == "2.3.0"
    written = json.loads((d / "2.3.0.json").read_text(encoding="utf-8"))
    assert written["release_index"] == 2
    marked = written["paths"]["/api/legacy"]["get"]
    assert marked["deprecated"] is True
    assert marked["x-deprecated-since"] == 2
    assert marked["x-deprecated-replacement"] == "GET /api/keep"
    assert "responses" not in marked, "快照只冻结门禁所需面（精简），不复制重型 schema"
    assert written["paths"]["/api/keep"]["get"] == {"operationId": "keep_op"}


def test_regenerate_release_snapshot_freezes_current_contract(tmp_path, monkeypatch):
    """regenerate_openapi.py --release-snapshot：渲染当前契约并冻结为下一 release_index。"""
    regen_spec = importlib.util.spec_from_file_location(
        "regenerate_openapi_2837_snapshot",
        os.path.join(_REPO, "scripts", "regenerate_openapi.py"))
    regen = importlib.util.module_from_spec(regen_spec)
    regen_spec.loader.exec_module(regen)
    d = tmp_path / "released"
    d.mkdir()
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(d))
    rc = regen.main(["--release-snapshot"])
    assert rc == 0
    version = regen.custom_openapi(regen.app)["info"]["version"]
    files = sorted(p.name for p in d.iterdir())
    assert files == [f"{version}.json"], files
    written = json.loads((d / f"{version}.json").read_text(encoding="utf-8"))
    assert written["release_index"] == 1
    assert len(written["paths"]) == len(regen.custom_openapi(regen.app)["paths"])


# ===================== S3a：check-openapi-endpoints.py 接线（脚本 main(argv) seam） =====================

_CORE_EPS = [
    "/api/a2a", "/api/a2a/status", "/api/tasks/complete", "/api/tasks/current",
    "/api/tasks/notify", "/api/runtime/tasks/{task_id}/status",
    "/api/runtime/tasks/{task_id}/cancel",
]


def _with_core(paths):
    """补核心工作流端点，避免既有 core 校验干扰本组删侧判定用例。"""
    merged = dict(paths)
    for i, ep in enumerate(_CORE_EPS):
        merged.setdefault(ep, {}).setdefault("get", {"operationId": f"core_probe_{i}"})
    return merged


def _released_dir_with(monkeypatch, snapshot_paths, release_index=1,
                       version="2.2.0", where=None):
    d = where / "released"
    d.mkdir(exist_ok=True)
    _write_json(d / f"{version}.json", {
        "release_index": release_index, "version": version, "paths": snapshot_paths})
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(d))
    return d


def _load_check_script():
    spec = importlib.util.spec_from_file_location(
        "check_openapi_endpoints_2837", _SCRIPT_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_check_script(monkeypatch, current, tmp_path, released=None, baseline=None,
                      argv=(), released_paths=None, baseline_ops=None):
    """装载门禁脚本并运行 main(argv)，合成当前契约 + 隔离基线/发布快照数据面。"""
    coe = _load_check_script()
    current = _with_core(current)
    monkeypatch.setattr(coe, "build_current_contract",
                        lambda: {"info": {"version": "2.2.0"}, "paths": current})
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR",
                        str(tmp_path / "released-empty"))
    if released is not None:  # (release_index, version)
        _released_dir_with(monkeypatch, released_paths, released[0], released[1],
                           where=tmp_path)
    copy = tmp_path / "contract-copy.json"
    _write_json(copy, {"paths": current})
    monkeypatch.setattr(coe, "CONTRACT_COPY", str(copy))
    baseline_file = tmp_path / "baseline.json"
    baseline_ops = baseline_ops if baseline_ops is not None else current
    _write_json(baseline_file, {"info": {"version": "2.2.0"}, "paths": baseline_ops})
    return coe.main(["--baseline", str(baseline_file), *argv])


def test_ci_mode_red_on_unregistered_removal_vs_released_snapshot(
        tmp_path, monkeypatch, capsys):
    """CI 默认模式：发布快照有、当前契约无、未挂牌 → 红（错误含 operationId/method/path）。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    rc = _run_check_script(monkeypatch, current, tmp_path,
                           released=(1, "2.2.0"), released_paths=snapshot,
                           argv=["--require-baseline"])
    assert rc == 1, "删除未挂牌端点必须 CI 红"
    err = capsys.readouterr().err
    assert "legacy_op" in err and "GET" in err and "/api/legacy" in err, err


def test_ci_mode_green_on_registered_removal_after_window(
        tmp_path, monkeypatch, capsys):
    """CI 默认模式：挂牌满一发布期（快照带机器可读标记）后删除 → 绿。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    snapshot["/api/legacy"]["get"].update({
        "deprecated": True, "x-deprecated-since": 1,
        "x-deprecated-replacement": "GET /api/keep"})
    rc = _run_check_script(monkeypatch, current, tmp_path,
                           released=(1, "2.2.0"), released_paths=snapshot,
                           argv=["--require-baseline"])
    assert rc == 0, capsys.readouterr().err


def test_update_baseline_change_note_cannot_bypass_observation_window(
        tmp_path, monkeypatch, capsys):
    """重定基通道：--update-baseline --change-note 无法用自由文本绕过观察期。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    rc = _run_check_script(
        monkeypatch, current, tmp_path,
        released=(1, "2.2.0"), released_paths=snapshot,
        baseline_ops=snapshot,
        argv=["--update-baseline", "--change-note",
              "review note: intentionally removed, trust me"])
    assert rc == 1, "自由文本说明不得成为观察期的第二条放行口"
    err = capsys.readouterr().err
    assert "legacy_op" in err, err
    # 基线不得被改写（写基线前先行校验）
    written = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
    assert "/api/legacy" in written["paths"], "基线在门禁红时不得被刷新吸收删除"


def test_update_baseline_green_after_window_with_note(
        tmp_path, monkeypatch, capsys):
    """重定基通道放行路径：挂牌满期删除 + --change-note → 刷新成功。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    snapshot["/api/legacy"]["get"].update({
        "deprecated": True, "x-deprecated-since": 1,
        "x-deprecated-replacement": "GET /api/keep"})
    rc = _run_check_script(
        monkeypatch, current, tmp_path,
        released=(1, "2.2.0"), released_paths=snapshot,
        baseline_ops=snapshot,
        argv=["--update-baseline", "--change-note", "remove legacy_op after window"])
    assert rc == 0, capsys.readouterr().err
    written = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
    assert "/api/legacy" not in written["paths"], "满期删除后基线应被刷新"


def test_require_baseline_red_on_missing_released_snapshot(
        tmp_path, monkeypatch, capsys):
    """--require-baseline：缺发布快照即红（观察期基准不得静默缺失）。"""
    rc = _run_check_script(monkeypatch, _paths([("/api/keep", "get", "keep_op")]),
                           tmp_path, argv=["--require-baseline"])
    assert rc == 1, "缺已发布快照必须红"
    err = capsys.readouterr().err
    assert "released" in err and "snapshot" in err, err


def test_check_script_e2e_green_on_real_contract_and_snapshot(capsys):
    """e2e：真实渲染契约 + 真实 checked-in 冻结快照 → 门禁全绿。"""
    coe = _load_check_script()
    rc = coe.main(["--require-baseline"])
    assert rc == 0, capsys.readouterr().err


# ===================== S3b：regenerate_openapi.py --update-baseline 双通道统一 =====================

def _load_regen_module():
    regen_spec = importlib.util.spec_from_file_location(
        "regenerate_openapi_2837_gate",
        os.path.join(_REPO, "scripts", "regenerate_openapi.py"))
    mod = importlib.util.module_from_spec(regen_spec)
    regen_spec.loader.exec_module(mod)
    return mod


def _run_regen_update_baseline(monkeypatch, tmp_path, current_paths,
                               snapshot_paths, reason="trust me, removed on purpose"):
    """隔离数据面运行 regenerate_openapi.py --update-baseline --reason。

    - 当前契约：monkeypatch 渲染层返回合成 schema（不碰真实 app）
    - 发布快照：tmp contracts/released/（门禁基准）
    - write_contract_baseline：spy 记录是否被调用（写基线动作本身走 tmp 断言）
    """
    regen = _load_regen_module()
    monkeypatch.setattr(regen, "custom_openapi",
                        lambda app, check_baseline=True:
                        {"info": {"version": "2.2.0"}, "paths": current_paths})
    d = tmp_path / "released"
    d.mkdir()
    if snapshot_paths is not None:
        _write_json(d / "2.2.0.json", {
            "release_index": 1, "version": "2.2.0", "paths": snapshot_paths})
    monkeypatch.setattr(dep, "RELEASED_CONTRACTS_DIR", str(d))
    monkeypatch.setattr(dep, "DEPRECATED_ENDPOINTS_PATH",
                        str(tmp_path / "deprecated_endpoints.json"))
    _write_json(tmp_path / "deprecated_endpoints.json",
                {"description": "t", "entries": []})
    monkeypatch.setattr(dep, "DEPRECATED_EXEMPTIONS_PATH",
                        str(tmp_path / "deprecated_exemptions.json"))
    _write_json(tmp_path / "deprecated_exemptions.json",
                {"description": "t", "entries": []})
    calls = []
    monkeypatch.setattr(regen, "write_contract_baseline",
                        lambda schema, path=None, change_reason="":
                        calls.append(schema) or {
                            "endpoint_count": len(schema["paths"]),
                            "change_reason": change_reason})
    # 组级完整性基线同样隔离：写真实 contracts/openapi_baseline.json 会把仓库文件
    # 覆盖成合成 schema（QA G-5 新增的第二条基线通道）
    integrity_calls = []
    monkeypatch.setattr(regen, "write_contract_integrity_baseline",
                        lambda schema, change_reason="", path=None:
                        integrity_calls.append(schema) or {
                            "endpoint_count": len(schema["paths"]),
                            "groups": [],
                            "change_reason": change_reason})
    rc = regen.main(["--update-baseline", "--reason", reason])
    return rc, calls


def test_regenerate_reason_cannot_bypass_observation_window(
        tmp_path, monkeypatch, capsys):
    """重定基通道②：regenerate_openapi.py --update-baseline --reason 无法绕过观察期。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    rc, calls = _run_regen_update_baseline(monkeypatch, tmp_path, current, snapshot)
    assert rc != 0, "--reason 自由文本说明不得成为观察期的第二条放行口"
    assert not calls, "门禁红时不得写契约基线"
    err = capsys.readouterr().err
    assert "legacy_op" in err, f"失败信息须含被删操作: {err}"


def test_regenerate_update_baseline_red_on_same_change_deprecate_and_remove(
        tmp_path, monkeypatch, capsys):
    """重定基通道②判定矩阵②：同 PR 挂牌即删（登记表有、快照无标记）→ 红。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    rc, calls = _run_regen_update_baseline(monkeypatch, tmp_path, current, snapshot)
    assert rc != 0, "挂牌未满一发布期即删除必须红"
    assert not calls
    assert "legacy_op" in capsys.readouterr().err


def test_regenerate_update_baseline_green_after_window(
        tmp_path, monkeypatch, capsys):
    """重定基通道②放行路径：挂牌满一发布期（快照带标记）后删除 + --reason → 写基线。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    snapshot = _paths([("/api/keep", "get", "keep_op"),
                       ("/api/legacy", "get", "legacy_op")])
    snapshot["/api/legacy"]["get"].update({
        "deprecated": True,
        "x-deprecated-since": 1, "x-deprecated-replacement": "GET /api/keep"})
    rc, calls = _run_regen_update_baseline(monkeypatch, tmp_path, current, snapshot)
    assert rc == 0, capsys.readouterr().err
    assert calls, "满期删除须正常重写契约基线"


def test_regenerate_update_baseline_passes_without_released_snapshot(
        tmp_path, monkeypatch):
    """边界：无已发布快照（首发引导期）→ 门禁跳过不阻断重定基（CI 侧由
    check-openapi-endpoints.py --require-baseline 拦缺失）。"""
    current = _paths([("/api/keep", "get", "keep_op")])
    rc, calls = _run_regen_update_baseline(
        monkeypatch, tmp_path, current, snapshot_paths=None)
    assert rc == 0
    assert calls, "引导期重定基须正常写基线（缺快照由 CI 门禁拦）"


# ===================== S4：check-api-docs.py 挂牌态文档标注联动 =====================

def test_docs_gate_red_when_deprecated_annotation_missing():
    """已挂牌且仍在契约的端点：文档块缺 Deprecated 标注 → 红（可定位到 method/path）。"""
    registry = {"legacy_op": _entry()}
    contract_ops = {("get", "/api/legacy"): "legacy_op"}
    doc_blocks = {("get", "/api/legacy"): {
        "help/api/01.md": "Deprecated：由 GET /api/legacy-v2 取代。",
        "docs/openapi/01.md": "## GET /api/legacy\n\n返回旧版数据。",
    }}
    errors = dep.deprecated_doc_annotation_errors(
        registry=registry, contract_ops=contract_ops, doc_blocks=doc_blocks)
    assert len(errors) == 1, errors
    msg = errors[0]
    assert "GET" in msg and "/api/legacy" in msg and "legacy_op" in msg, msg
    assert "Deprecated" in msg or "deprecated" in msg, msg
    assert "docs/openapi/01.md" in msg, f"须定位到未标注的文档来源: {msg}"


def test_docs_gate_red_when_replacement_not_documented():
    """文档块标了 Deprecated 但没写替代端点 → 红（挂牌承诺必须给替代指引）。"""
    registry = {"legacy_op": _entry()}
    contract_ops = {("get", "/api/legacy"): "legacy_op"}
    doc_blocks = {("get", "/api/legacy"): "Deprecated，请自行迁移。"}
    errors = dep.deprecated_doc_annotation_errors(
        registry=registry, contract_ops=contract_ops, doc_blocks=doc_blocks)
    assert errors, "缺替代端点说明必须红"
    assert "/api/legacy-v2" in errors[0], errors


def test_docs_gate_green_with_full_annotation():
    """文档块（help/api 与 docs/openapi 两处）含 Deprecated + 替代端点 → 绿。"""
    registry = {"legacy_op": _entry()}
    contract_ops = {("get", "/api/legacy"): "legacy_op"}
    doc_blocks = {("get", "/api/legacy"): {
        "help/api/01.md": "Deprecated：由 GET /api/legacy-v2 取代。",
        "docs/openapi/01.md": "Deprecated：由 GET /api/legacy-v2 取代。",
    }}
    assert dep.deprecated_doc_annotation_errors(
        registry=registry, contract_ops=contract_ops, doc_blocks=doc_blocks) == []


def test_docs_gate_skips_removed_and_missing_block_cases():
    """边界：已删除端点（文档残留由双向同步侧管）与文档块缺失不重复报。"""
    registry = {"legacy_op": _entry()}
    errors = dep.deprecated_doc_annotation_errors(
        registry=registry,
        contract_ops={("get", "/api/keep"): "keep_op"},  # legacy 已删除
        doc_blocks={})
    assert errors == []


def test_docs_gate_noop_on_empty_registry():
    """真实仓数据面：挂牌登记表为空（arch 线全契约无挂牌）→ 文档标注门禁空转不误红。"""
    assert dep.deprecated_doc_annotation_errors() == []


def test_check_api_docs_script_wires_deprecated_annotation(tmp_path, capsys):
    """脚本 seam：check-api-docs.py main() 接入挂牌态文档标注判定（空登记表 → 不误红）。"""
    spec = importlib.util.spec_from_file_location(
        "check_api_docs_2837",
        os.path.join(_REPO, "scripts", "check-api-docs.py"))
    cad = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cad)
    rc = cad.main(["--baseline",
                   os.path.join(_REPO, "help", "api", "openapi-baseline.json")])
    assert rc == 0, capsys.readouterr().err
