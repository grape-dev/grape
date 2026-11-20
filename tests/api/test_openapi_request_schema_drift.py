"""契约副本 vs 实现模型的请求 schema 守卫（架构守护 C-2 的盲区补齐）。

背景：`scripts/check-openapi-endpoints.py` 只比对 paths/operationId/基线子集，
`test_write_openapi_contract_no_shrink` 只拦收缩——**请求字段增删两边都拦不住**。
#3469 就栽在这：`ChangeEmailBody` 实现加了可选 `password`/`old_email_code`，
两份契约副本仍只有 `new_email`/`code`，消费方按契约生成客户端必然吃 400
`NEED_SECOND_FACTOR` 且无从发现补参方式。
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_COPIES = (
    ROOT / "help" / "api" / "openapi-baseline.json",
    ROOT / "src" / "api" / "frontend" / "shell" / "openapi.json",
)


def _schema_name(path: Path, name: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["components"]["schemas"][name]


@pytest.mark.parametrize("copy_path", CONTRACT_COPIES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "model_name",
    ["ChangeEmailBody", "ChangeEmailSendCodeBody"],
)
def test_request_schema_matches_implementation(copy_path, model_name):
    from src.domains.mgmt.routes import grape_auth as routes

    model = getattr(routes, model_name)
    impl = model.model_json_schema()
    contract = _schema_name(copy_path, model_name)

    impl_fields = set(impl["properties"])
    contract_fields = set(contract["properties"])
    missing = impl_fields - contract_fields
    extra = contract_fields - impl_fields
    assert not missing, f"{copy_path.name}/{model_name} 契约缺字段：{sorted(missing)}"
    assert not extra, f"{copy_path.name}/{model_name} 契约多出实现没有的字段：{sorted(extra)}"

    # 必填口径必须一致（可选项不得在契约里被标成 required，反之亦然）
    assert set(contract.get("required") or []) == set(impl.get("required") or []), (
        f"{copy_path.name}/{model_name} required 不一致："
        f"contract={contract.get('required')} impl={impl.get('required')}"
    )


def test_change_email_second_factor_fields_are_contract_visible():
    """#3469 的二次因子字段必须能从契约里发现（C-2 的回归锁）。"""
    for copy_path in CONTRACT_COPIES:
        props = _schema_name(copy_path, "ChangeEmailBody")["properties"]
        assert "password" in props and "old_email_code" in props, copy_path.name
        required = _schema_name(copy_path, "ChangeEmailBody").get("required") or []
        assert set(required) == {"new_email", "code"}, copy_path.name