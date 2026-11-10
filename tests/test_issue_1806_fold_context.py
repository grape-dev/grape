"""Issue #1806：sweeper 折叠任务 A2A context 四要素守卫。

覆盖：
- 折叠任务判定单源：`sweeper-v2-<任意>-fold<数字>` 命中，其余不命中；
- 折叠任务空/缺项 context → 默认 400，错误体含 idempotency_key、缺失清单与 schema 示例；
- 折叠任务四要素齐全（文本 / 结构化）→ 放行并创建事件；
- 配置开关降级 → 放行并产生含 task 标识与 idempotency_key 的告警；
- 非折叠任务空 context 行为与幂等语义不变：同仓同键仍返回已有事件。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.api.a2a_context_guard import (  # noqa: E402
    FOLD_CONTEXT_ENFORCE_ENV,
    FOLD_CONTEXT_REQUIRED_FIELDS,
    is_fold_task,
    missing_fold_context_fields,
)
from src.session_manager import db, db_lock  # noqa: E402

REPO_FULL_NAME = "owner/fold1806"
REPO_KEY = f"gitcode:{REPO_FULL_NAME}"
FOLD_KEY = "sweeper-v2-pr754-fold620"

VALID_CONTEXT = (
    "source_pr: 754\n"
    "target_pr: #620\n"
    "target_files: src/api/routes/webhook.py\n"
    "fold_reason: 与目标 PR 修改同一文件且方向一致，追加到目标 PR"
)


class TestFoldTaskDetection:
    @pytest.mark.parametrize("key", [
        "sweeper-v2-pr754-fold620",
        "sweeper-v2-issue-1806-fold9",
        "sweeper-v2-a-b-c-fold123",
    ])
    def test_recognizes_fold_task(self, key):
        assert is_fold_task(key) is True

    @pytest.mark.parametrize("key", [
        "",
        "task-42:qa",
        "sweeper-v2-pr754",
        "sweeper-v2-pr754-fold",
        "sweeper-v3-pr754-fold620",
        "sweeper-v2-pr754-fold620x",
    ])
    def test_ignores_non_fold_task(self, key):
        assert is_fold_task(key) is False

    def test_missing_fields_for_empty_and_partial_context(self):
        assert missing_fold_context_fields("") == list(FOLD_CONTEXT_REQUIRED_FIELDS)
        assert missing_fold_context_fields("   \n\t ") == list(FOLD_CONTEXT_REQUIRED_FIELDS)
        assert missing_fold_context_fields("source_pr: 754") == [
            "target_pr", "target_files", "fold_reason"]
        assert missing_fold_context_fields(VALID_CONTEXT) == []

    def test_structured_context_supported(self):
        context = {
            "source_pr": 754,
            "target_pr": "#620",
            "target_files": ["src/api/routes/webhook.py"],
            "fold_reason": "same file",
        }
        assert missing_fold_context_fields(context) == []


class TestFoldTaskContextGuardEndpoint:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
            try:
                c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
            db.commit()

    def _seed(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO managed_repos "
                "(repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                (REPO_KEY, "owner", "fold1806", REPO_FULL_NAME))
            db.commit()

    def _body(self, key, context, issue_number="1806"):
        return {
            "agent": "coding",
            "idempotency_key": key,
            "payload": {
                "repository": REPO_FULL_NAME,
                "account": REPO_FULL_NAME,
                "issue_number": issue_number,
                "pr_number": "620",
                "task_id": "task-1806-fold",
                "context": context,
            },
        }

    def _post(self, client, body):
        return client.post("/api/a2a", json=body)

    def _event_count(self):
        with db_lock:
            return db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE event_type='a2a_event' AND repo_key=?",
                (REPO_KEY,)).fetchone()[0]

    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        return TestClient(app)

    def test_fold_task_empty_context_rejected_400(self):
        client = self._client()
        r = self._post(client, self._body(FOLD_KEY, ""))
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["idempotency_key"] == FOLD_KEY
        assert detail["missing_fields"] == list(FOLD_CONTEXT_REQUIRED_FIELDS)
        assert "source_pr:" in detail["context_schema_example"]
        assert self._event_count() == 0, "被拦截的折叠任务不得创建事件"

    def test_fold_task_missing_fields_rejected_400(self):
        client = self._client()
        r = self._post(client, self._body(FOLD_KEY, "source_pr: 754\ntarget_pr: #620"))
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["missing_fields"] == ["target_files", "fold_reason"]
        assert self._event_count() == 0

    def test_fold_task_valid_text_context_allowed(self):
        client = self._client()
        r = self._post(client, self._body(FOLD_KEY, VALID_CONTEXT))
        assert r.status_code == 200, r.text
        assert r.json().get("seq", 0) > 0
        assert self._event_count() == 1

    def test_fold_task_valid_structured_context_allowed(self):
        client = self._client()
        context = {
            "source_pr": "754",
            "target_pr": "#620",
            "target_files": "src/api/routes/webhook.py",
            "fold_reason": "same file, continue on target PR",
        }
        r = self._post(client, self._body(FOLD_KEY, context))
        assert r.status_code == 200, r.text
        assert r.json().get("seq", 0) > 0
        assert self._event_count() == 1

    def test_fold_task_degraded_mode_allows_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv(FOLD_CONTEXT_ENFORCE_ENV, "0")
        client = self._client()
        with caplog.at_level("WARNING"):
            r = self._post(client, self._body(FOLD_KEY, ""))
        assert r.status_code == 200, r.text
        assert r.json().get("seq", 0) > 0
        assert self._event_count() == 1
        assert FOLD_KEY in caplog.text
        assert "task-1806-fold" in caplog.text

    def test_non_fold_task_empty_context_and_idempotency_unchanged(self):
        client = self._client()
        key = "task-1806:nonfold"
        r1 = self._post(client, self._body(key, ""))
        assert r1.status_code == 200, r1.text
        seq1 = r1.json()["seq"]
        assert seq1 > 0
        r2 = self._post(client, self._body(key, ""))
        assert r2.status_code == 200, r2.text
        assert r2.json().get("idempotent") is True
        assert r2.json()["seq"] == seq1
        assert self._event_count() == 1, "同仓同键仍返回已有事件，不重复派发"