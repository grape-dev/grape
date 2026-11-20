"""Issue #1934 回归 + #2081 扩全量：契约必须覆盖登记清单（单一事实源）全部端点。

CLI（grape-cli）是契约驱动的：`grape refresh` 拉取服务端契约（/static/openapi.json，
由 custom_openapi 渲染），registry.parse_operations 按 operationId 动态生成命令树：
  - operationId 首段 → 命令组：tasks_complete → `grape tasks complete`，
    a2a_handoff → `grape a2a handoff`；
  - operationId 含 "api_"（服务端 autogen 丑名）→ 命令被 CLI 过滤，命令组静默消失。

#1863/#1931/#1934 断链族的共同下游症状即「契约缺 operationId / operationId 变成
autogen 丑名 → CLI 刷新后 tasks/a2a 命令组 No such command」。

Issue #2081：原 5 条硬编码 REQUIRED_OPERATIONS 扩为 contracts/required_operations.json
登记清单（runbook/skill 引用面全量端点，含现有 5 条；抽取来源见清单 metadata）。
本文件从该清单派生，不再自带端点字典——重复定义被 test_registry_* 断言禁止（AC4）。
任一登记操作被删（operationId 缺失或 method/path 漂移）即红（AC1/AC2）。
"""
import json
import os

import pytest

from src.api.main import app
from src.api.openapi import custom_openapi, is_http_method, load_required_operations

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REGISTRY_PATH = os.path.join(_REPO_ROOT, "contracts", "required_operations.json")

# #2081 单一事实源：contracts/required_operations.json——技能 runbook/references 引用面
# + #1173 现有 5 条 + #897 核心工作流 7 路径，合并去重登记（operationId+method+path）。
REQUIRED_OPERATIONS = load_required_operations()

# #1173 原有 5 条 runbook 收尾/A2A 关键 operationId——登记清单不得丢失它们（#2081 AC1）。
LEGACY_FIVE = {
    "tasks_complete": ("/api/tasks/complete", "post"),
    "tasks_current": ("/api/tasks/current", "get"),
    "tasks_notify": ("/api/tasks/notify", "post"),
    "a2a_handoff": ("/api/a2a", "post"),
    "a2a_status": ("/api/a2a/status", "get"),
}

# grape-cli registry.GROUP_ALIAS 同口径：命令组名 = operationId 首段（gitrepo → git-repo）
_GROUP_ALIAS = {"gitrepo": "git-repo"}


@pytest.fixture(scope="module")
def schema():
    return custom_openapi(app)


def _iter_ops(s):
    for path, methods in (s.get("paths") or {}).items():
        for method, op in methods.items():
            if isinstance(op, dict) and op.get("operationId"):
                yield op["operationId"], path, method, op


def test_runbook_critical_operations_present(schema):
    """登记清单（runbook/skill 引用面全量）operationId 必须全部在契约且位置不漂移。"""
    seen = {oid: (path, method) for oid, path, method, _ in _iter_ops(schema)}
    missing = [oid for oid in REQUIRED_OPERATIONS if oid not in seen]
    assert not missing, (
        f"契约缺少登记清单 operationId {missing}——"
        "grape refresh 后 CLI 将缺对应命令组（#1934/#2081 断链面，"
        f"登记源 {_REGISTRY_PATH}"
    )
    for oid, (path, method) in REQUIRED_OPERATIONS.items():
        assert seen[oid] == (path, method), (
            f"{oid} 契约位置漂移：期望 {path}#{method}，实际 {seen[oid]}"
        )


def test_operationids_stay_cli_parseable(schema):
    """登记清单内 operationId 保持 CLI 可解析形状：首段=真实命令组、不含 "api_"、带动词段。

    registry.parse_operations 会静默过滤含 "api_" 的 operationId——一旦被改名成
    autogen 丑名（或丢失首段/首段组不渲染），命令组消失且无报错，必须在此拦下。
    """
    groups = set(schema.get("x-groups") or [])
    for oid, _path, _method, _op in _iter_ops(schema):
        if oid in REQUIRED_OPERATIONS:
            parts = oid.split("_")
            assert "api_" not in oid, f"{oid} 含 CLI 过滤词 api_，命令会被静默丢弃"
            assert len(parts) >= 2, f"{oid} 缺动词段，无法生成 `grape <group> <verb>`"
            group = _GROUP_ALIAS.get(parts[0], parts[0])
            assert group in groups, (
                f"{oid} 命令组 {group} 不在契约 x-groups，CLI 不渲染该命令组"
            )


def test_contract_paths_cover_registered_operations(schema):
    """契约路径必须包含登记清单全部 path（快照不完整/子集落盘的直接探针，#1931 覆写族）。"""
    paths = set((schema.get("paths") or {}).keys())
    for path, _ in REQUIRED_OPERATIONS.values():
        assert path in paths, f"契约缺路径 {path}（快照不完整，#1931/#1934 断链面）"


# ===================== #2081：登记清单自身完整性（AC1/AC4） =====================

def test_registry_covers_legacy_five():
    """AC1：#1173 原有 5 条 runbook 收尾/A2A 关键操作必须仍在登记清单中（含位置口径）。"""
    for oid, expected in LEGACY_FIVE.items():
        assert oid in REQUIRED_OPERATIONS, (
            f"登记清单丢失 #1173 关键操作 {oid}——扩全量不得丢既有登记"
        )
        assert REQUIRED_OPERATIONS[oid] == expected, (
            f"{oid} 登记口径漂移：期望 {expected}，实际 {REQUIRED_OPERATIONS[oid]}"
        )


def test_registry_file_is_wellformed_and_unique():
    """AC4：登记清单条目结构合法且无重复定义（operationId / method+path 双口径去重）。"""
    with open(_REGISTRY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    ops = data.get("operations")
    assert isinstance(ops, list) and ops, "登记清单 operations 必须为非空列表"
    seen_oids, seen_pairs = set(), set()
    for entry in ops:
        assert set(entry) >= {"operationId", "method", "path"}, f"条目缺字段: {entry!r}"
        oid, method, path = entry["operationId"], entry["method"], entry["path"]
        assert isinstance(oid, str) and oid and not oid.startswith("api_"), f"非法 operationId: {oid!r}"
        # QA 复审判断项(d)：method 白名单与渲染层 _METHODS（7 种，含 head/options）
        # 共用同一判定，防两处口径漂移
        assert is_http_method(method), f"非法 method: {method!r}"
        assert isinstance(path, str) and path.startswith("/"), f"非法 path: {path!r}"
        assert oid not in seen_oids, f"登记清单 operationId 重复定义: {oid}"
        assert (method, path) not in seen_pairs, f"登记清单 method+path 重复定义: {method} {path}"
        seen_oids.add(oid)
        seen_pairs.add((method, path))
    assert seen_oids == set(REQUIRED_OPERATIONS), "清单文件与加载器口径必须一致"
