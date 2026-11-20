"""Issue #2695：issues pending-review 空/非法 issue_id 应返回明确 400（不再误导性 405）。

反馈来源（后台 id 7369）：runbook 把 GitCode issue number 当成 issue_id 传入，CLI 拼出
`/api/issues//pending-review`，服务端落到 method-mismatch 兜底，返回无详情 405，调用方无法自诊断。

测试 seam（公开 HTTP 接口）：`POST /api/issues/{issue_id}/pending-review`
- 空段 / 非数字 issue_id → 400 + `{"detail":{"code":400,"message":"invalid issue_id"}}`（平台信封）
- 合法（纯数字）id 行为不回归：由既有 tests/test_issue_pending_review.py 覆盖（本文件不重复）

seed/鉴权复用 test_issue_pending_review.py 的既有手法。
"""
import os
import re
import uuid

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

_uid = None


def _session_headers(repo_key="gitcode:org/test", agent_type="qa"):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"inv-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='invalid_id_user'")
        db.commit()
    _uid = grape_account_db.create_grape_account("invalid_id_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("invalid_id_user")
    user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test", True, True)


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='invalid_id_user'")
        db.commit()


def _assert_invalid_issue_id(r):
    assert r.status_code == 400, f"{r.request.url} -> {r.status_code} {r.text[:300]}"
    body = r.json()
    detail = body.get("detail", body)
    assert isinstance(detail, dict), detail
    assert detail.get("code") == 400
    assert detail.get("message") == "invalid issue_id"


def test_empty_issue_id_returns_400():
    """空 issue_id（`/api/issues//pending-review`）→ 400，不再 405。"""
    r = client.post("/api/issues//pending-review",
                    headers=_session_headers(agent_type="qa"),
                    json={"review_points": "x"})
    _assert_invalid_issue_id(r)


def test_non_numeric_issue_id_returns_400():
    """非数字 issue_id（传了 GitCode issue number 之外的脏值）→ 400 而非 405。"""
    r = client.post("/api/issues/abc/pending-review",
                    headers=_session_headers(agent_type="qa"),
                    json={"review_points": "x"})
    _assert_invalid_issue_id(r)


def test_numeric_like_but_invalid_forms_return_400():
    """其他非纯数字形态（小数/十六进制/带符号）同样 400。"""
    for bad in ("12.5", "0x1F", "+12", "1e3", "%23pr_1"):
        r = client.post(f"/api/issues/{bad}/pending-review",
                        headers=_session_headers(agent_type="qa"),
                        json={"review_points": "x"})
        _assert_invalid_issue_id(r)


# 平台统一信封：`{"detail":{"code":400,"message":"invalid issue_id"}}`（允许空白差异）
_DETAIL_ENVELOPE_RE = re.compile(
    r'\{\s*"detail"\s*:\s*\{\s*"code"\s*:\s*400\s*,\s*"message"\s*:\s*"invalid issue_id"\s*\}\s*\}')
# 反例：顶层 code/message（`(?<!:)` 排除 detail:{...} 的内层匹配）
_TOPLEVEL_ENVELOPE_RE = re.compile(
    r'(?<!:)\{\s*"code"\s*:\s*400\s*,\s*"message"\s*:\s*"invalid issue_id"\s*\}')


def test_api_doc_pins_invalid_issue_id_contract():
    """对外 API 文档必须按平台信封写明返回体，且把收敛后的行为限定在 POST（Issue #2695）。"""
    import pathlib
    doc = (pathlib.Path(__file__).parent.parent / "help" / "api" / "11-issues.md").read_text(encoding="utf-8")
    block = doc[doc.index("POST /api/issues/{issue_id}/pending-review"):]
    block = block[:block.find("\n### ")] if "\n### " in block else block

    assert "invalid issue_id" in block, "11-issues.md 的 pending-review 端点块未写 400 invalid issue_id"
    assert "不是 GitCode issue number" in block, "端点块须点明 issue_id 来源口径（内部 id，不是 GitCode 编号）"

    # 形状钉死：必须是平台统一信封 detail:{code,message}（QA 复审：曾误写成顶层 code/message）
    assert _DETAIL_ENVELOPE_RE.search(block), \
        '端点块返回体须写成 {"detail":{"code":400,"message":"invalid issue_id"}}'
    assert _TOPLEVEL_ENVELOPE_RE.search(block) is None, \
        "端点块不得把返回体写成顶层 {code,message}（与平台信封不符）"

    # 措辞范围：只有 POST 收敛为 400，GET/PUT/DELETE 空段行为不变
    assert "仅 POST" in block, "文档须限定「仅 POST」收敛为 400，避免被读成所有方法都变"
    assert "GET/PUT/DELETE" in block, "文档须声明 GET/PUT/DELETE 空段行为不变"
