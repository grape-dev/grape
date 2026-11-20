"""Issue #2081：contract_integrity() 以登记清单为同一基准（渲染层 fail-fast）。

Spec 实现路径 2 的第三处收敛：contracts/openapi_baseline.json + contract_integrity()
与门禁脚本/回归测试共用 contracts/required_operations.json。登记清单内任一操作被删
（operationId 缺失或 method/path 漂移）→ contract_integrity 判降级（complete=False
+ missing_operations 明细），custom_openapi 渲染层抛 ContractDegradedError 拒出契约，
失败信息含 operationId + method + path（AC2）。

测试 seams（公开接口）：
- S1 contract_integrity(schema)：完整契约报告通过（missing_operations 空）；
  删任一登记操作（S15「删端点」变异，全量参数化）→ 报告红且明细含三口径。
- S2 ContractDegradedError：错误信息列出 operationId + method + path。
- S3（QA 复审遗留2）load_required_operations：登记清单自我缩水防线——
  条数跌破下限即红（fail-closed，渲染层/门禁/测试三处同步拦截）。
"""
import copy
import json

import pytest

from src.api.main import app
from src.api import openapi as openapi_mod


@pytest.fixture(scope="module")
def schema():
    return openapi_mod.custom_openapi(app)


def _drop_operation(schema: dict, operation_id: str) -> dict:
    """返回删掉一个登记操作后的契约副本（不动缓存的原 schema）。"""
    required = openapi_mod.load_required_operations()
    path, method = required[operation_id]
    broken = copy.deepcopy(schema)
    del broken["paths"][path][method]
    if not broken["paths"][path]:
        del broken["paths"][path]
    return broken


def test_full_contract_integrity_reports_no_missing_operations(schema):
    """完整契约：完整性报告通过，登记清单零缺失。"""
    report = openapi_mod.contract_integrity(schema)
    assert report["complete"] is True
    assert report["missing_operations"] == []


@pytest.mark.parametrize("operation_id", sorted(openapi_mod.load_required_operations()))
def test_integrity_red_when_registered_operation_deleted(schema, operation_id):
    """AC2 变异验证：删任一登记操作 → 报告红且明细含 operationId+method+path。"""
    broken = _drop_operation(schema, operation_id)
    report = openapi_mod.contract_integrity(broken)
    assert report["complete"] is False, f"删除 {operation_id} 未被判降级"
    missing = {m["operationId"]: m for m in report["missing_operations"]}
    assert operation_id in missing, f"{operation_id} 不在 missing_operations 明细中"
    entry = missing[operation_id]
    path, method = openapi_mod.load_required_operations()[operation_id]
    assert (entry["path"], entry["method"]) == (path, method)


def test_degraded_error_message_names_operation_and_path(schema):
    """AC2：降级错误信息可定位——含 operationId + method + path（渲染层 fail-fast）。"""
    broken = _drop_operation(schema, "knowledge_stats")
    report = openapi_mod.contract_integrity(broken)
    assert not report["complete"]
    msg = str(openapi_mod.ContractDegradedError(report))
    assert "knowledge_stats" in msg, f"错误信息缺 operationId: {msg}"
    assert "/api/knowledge/stats" in msg, f"错误信息缺 path: {msg}"
    assert "GET" in msg, f"错误信息缺 method: {msg}"


# ===================== S3：登记清单自我缩水防线（QA 复审遗留2） =====================

def test_registry_shrink_below_floor_is_red(tmp_path, monkeypatch):
    """QA 复审遗留2：条数跌破下限，加载即红（fail-closed）。

    旧口径下直接从 required_operations.json 删条目门禁不拦（43/43 仍绿，静默降级）；
    下限常量落在代码（MIN_REQUIRED_OPERATIONS）——合法缩水须同 PR 改常量，
    代码 diff 即评审面，数据面单独缩水无处方绿。
    """
    data = json.load(open(openapi_mod.REQUIRED_OPERATIONS_PATH, encoding="utf-8"))
    floor = openapi_mod.MIN_REQUIRED_OPERATIONS
    assert len(data["operations"]) > floor, "现网清单应高于下限（前提自检）"
    data["operations"] = data["operations"][:floor - 1]
    shrunk = tmp_path / "required_operations.json"
    shrunk.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(openapi_mod, "REQUIRED_OPERATIONS_PATH", str(shrunk))
    monkeypatch.setattr(openapi_mod, "_required_operations_cache", None)
    with pytest.raises(RuntimeError, match="floor"):
        openapi_mod.load_required_operations()
