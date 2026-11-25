"""Issue #3750：403 拒绝必须「可定位 + 可统计」——日志补 event/reason，并落一行拒绝事实。

现场：`grape-dev/grape` 1 小时 54 次 `Webhook rejected: token mismatch`，同窗口正常事件仍通过
（配置侧残留旧 secret）。webhook 不重放 → 被丢弃的 Push Hook 永久丢失且此前无任何速率信号。

契约（403 语义与响应体不变，只是把拒绝「说清楚 + 记下来」）：
  1. WARNING 带 `event=` / `reason=`（absent|mismatch）/ 两端指纹 / 来源 IP；
  2. 每次 403 落一条 `obs_webhook_rejects`（repo_key/provider/event/ip/指纹/reason）；
  3. 落库链路异常**不影响** 403 返回（响应体与状态码不变）；
  4. 404 unmanaged 同样带 `event=` / `ip=`（不再只能靠 request_id 反查）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.insight.observability import webhook_rejects as wr
from src.domains.runtime.routes import webhook as W

REPO = "grape-dev/grape"
REPO_KEY = f"gitcode:{REPO}"
EXPECTED = "expected-secret-3750"
WRONG = "stale-secret-3750"


def _clean():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM obs_webhook_rejects")
        db.commit()


def _rows(repo_key=REPO_KEY):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, provider, event, ip, token_fp, expected_fp, reason"
                  " FROM obs_webhook_rejects WHERE repo_key=?", (repo_key,))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


@pytest.fixture(autouse=True)
def _clean_ledger():
    _clean()
    yield
    wr.flush(timeout=2.0)
    _clean()


def _body():
    return json.dumps({"project": {"path_with_namespace": REPO}}).encode()


def _post(monkeypatch, token=WRONG):
    monkeypatch.setattr(W, "_expected_webhook_token", lambda repo, provider: EXPECTED)
    monkeypatch.setattr(W, "run_pipeline", lambda *a, **k: pytest.fail("must not dispatch"))
    headers = {"X-Gitcode-Token": token, "X-Gitcode-Event": "Push Hook"} if token else \
        {"X-Gitcode-Event": "Push Hook"}
    return TestClient(app).post("/webhook/gitcode", content=_body(), headers=headers)


def test_gitcode_403_logs_event_and_reason(monkeypatch, caplog):
    caplog.set_level("WARNING")
    r = _post(monkeypatch)
    assert r.status_code == 403
    assert r.json() == {"code": 403, "message": "invalid webhook token"}, "403 响应体不得改变"
    text = caplog.text
    assert "token mismatch" in text
    assert "event=Push Hook" in text
    assert "reason=mismatch" in text
    assert f"token_fp={W._token_fingerprint(WRONG)}" in text, "字段名须对齐 #3750 验收"
    assert f"expected_fp={W._token_fingerprint(EXPECTED)}" in text
    assert EXPECTED not in text and WRONG not in text


def test_gitcode_403_records_one_reject_row(monkeypatch):
    _post(monkeypatch)
    wr.flush(timeout=3.0)
    rows = _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["provider"] == "gitcode"
    assert row["event"] == "Push Hook"
    assert row["ip"], "来源 IP 必须落库（反查残留 hook 的依据之一）"
    assert row["reason"] == "mismatch"
    assert row["token_fp"] == wr.token_fingerprint(WRONG)
    assert row["expected_fp"] == wr.token_fingerprint(EXPECTED)


def test_gitcode_missing_token_reason_absent(monkeypatch):
    _post(monkeypatch, token="")
    wr.flush(timeout=3.0)
    row = _rows()[0]
    assert row["reason"] == "absent"
    assert row["token_fp"] == "absent"


def test_ledger_failure_does_not_change_403(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("src.domains.insight.public.record_webhook_reject", _boom)
    r = _post(monkeypatch)
    assert r.status_code == 403
    assert r.json() == {"code": 403, "message": "invalid webhook token"}


def test_gitcode_404_unmanaged_logs_event_and_ip(monkeypatch, caplog):
    monkeypatch.setattr(W, "_expected_webhook_token", lambda repo, provider: "")
    caplog.set_level("WARNING")
    r = TestClient(app).post("/webhook/gitcode", content=_body(),
                             headers={"X-Gitcode-Event": "Push Hook"})
    assert r.status_code == 404
    text = caplog.text
    assert "unmanaged" in text
    assert "event=Push Hook" in text
    assert "ip=" in text


def test_github_token_403_also_recorded(monkeypatch, caplog):
    monkeypatch.setattr(W, "_expected_webhook_token", lambda repo, provider: EXPECTED)
    monkeypatch.setattr(W, "run_pipeline", lambda *a, **k: pytest.fail("must not dispatch"))
    caplog.set_level("WARNING")
    body = json.dumps({"repository": {"full_name": REPO}}).encode()
    r = TestClient(app).post("/webhook/github", content=body,
                             headers={"X-GitHub-Token": WRONG, "X-GitHub-Event": "push"})
    assert r.status_code == 403
    assert "event=push" in caplog.text
    assert "reason=mismatch" in caplog.text
    assert "expected_fp=" in caplog.text and "token_fp=" in caplog.text
    wr.flush(timeout=3.0)
    rows = _rows(f"github:{REPO}")
    assert len(rows) == 1, rows
    assert rows[0]["provider"] == "github"
    assert rows[0]["event"] == "push"