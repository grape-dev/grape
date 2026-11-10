"""Issue #1806：sweeper 折叠任务 A2A context 四要素守卫。

覆盖：
- 折叠任务判定单源：`sweeper-v2-<任意>-fold<数字>` 命中，其余不命中；
- 折叠任务空/缺项 context → 默认 400，错误体含 idempotency_key、缺失清单与 schema 示例；
- 折叠任务四要素齐全（文本 / 结构化 / 同行分隔符）→ 放行，入库 context 保留四要素；
- 配置开关降级 → 放行并产生含 task 标识与 idempotency_key 的结构化告警；
- 非折叠任务空 context 行为与幂等语义不变：同仓同键仍返回已有事件；
- 已存在 fold 事件同键重试仍返回已有事件（守卫位于幂等命中短路之后）。
"""
import json

import pytest

from src.api.a2a_context_guard import (
    FOLD_CONTEXT_ENFORCE_ENV,
    FOLD_CONTEXT_REQUIRED_FIELDS,
    is_fold_task,
    missing_fold_context_fields,
)

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

    def test_inline_separator_text_context_supported(self):
        context = (
            "source_pr: 754, target_pr: #620, "
            "target_files: src/api/routes/webhook.py, fold_reason: same file"
        )
        assert missing_fold_context_fields(context) == []

    def test_generic_aliases_do_not_satisfy_fields(self):
        context = "files: a.py\nreason: borrowed alias\n落点: b.py\n原因: 泛化键"
        assert missing_fold_context_fields(context) == list(FOLD_CONTEXT_REQUIRED_FIELDS)

    def test_empty_containers_treated_as_missing(self):
        context = {
            "source_pr": {},
            "target_pr": [],
            "target_files": {},
            "fold_reason": [],
        }
        assert missing_fold_context_fields(context) == list(FOLD_CONTEXT_REQUIRED_FIELDS)


class TestFoldTaskContextGuardEndpoint:
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

    def _client(self, repo):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._repo = repo
        repo.clean(REPO_KEY)
        repo.seed(REPO_KEY, "owner", "fold1806")
        return TestClient(app)

    def _event_count(self):
        return self._repo.count_events(REPO_KEY)

    def _stored_context(self, seq):
        event = self._repo.event(seq)
        payload = event.get("payload") or {}
        return str(payload.get("context") or "")

    def _assert_context_complete(self, seq):
        stored = self._stored_context(seq)
        for field in FOLD_CONTEXT_REQUIRED_FIELDS:
            assert field in stored, f"入库 context 缺少 {field}: {stored!r}"
        assert "754" in stored
        assert "#620" in stored
        assert "src/api/routes/webhook.py" in stored

    def test_fold_task_empty_context_rejected_400(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
        r = self._post(client, self._body(FOLD_KEY, ""))
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["idempotency_key"] == FOLD_KEY
        assert detail["missing_fields"] == list(FOLD_CONTEXT_REQUIRED_FIELDS)
        assert "source_pr:" in detail["context_schema_example"]
        assert self._event_count() == 0, "被拦截的折叠任务不得创建事件"

    def test_fold_task_missing_fields_rejected_400(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
        r = self._post(client, self._body(FOLD_KEY, "source_pr: 754\ntarget_pr: #620"))
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["missing_fields"] == ["target_files", "fold_reason"]
        assert self._event_count() == 0

    def test_fold_task_valid_text_context_allowed(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
        r = self._post(client, self._body(FOLD_KEY, VALID_CONTEXT))
        assert r.status_code == 200, r.text
        seq = r.json().get("seq", 0)
        assert seq > 0
        assert self._event_count() == 1
        self._assert_context_complete(seq)

    def test_fold_task_valid_structured_context_allowed(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
        context = {
            "source_pr": "754",
            "target_pr": "#620",
            "target_files": "src/api/routes/webhook.py",
            "fold_reason": "same file, continue on target PR",
        }
        r = self._post(client, self._body(FOLD_KEY, context))
        assert r.status_code == 200, r.text
        seq = r.json().get("seq", 0)
        assert seq > 0
        assert self._event_count() == 1
        self._assert_context_complete(seq)

    def test_fold_task_degraded_mode_allows_with_structured_warning(
            self, a2a_event_repo, monkeypatch, caplog):
        monkeypatch.setenv(FOLD_CONTEXT_ENFORCE_ENV, "0")
        client = self._client(a2a_event_repo)
        with caplog.at_level("WARNING"):
            r = self._post(client, self._body(FOLD_KEY, ""))
        assert r.status_code == 200, r.text
        assert r.json().get("seq", 0) > 0
        assert self._event_count() == 1
        assert FOLD_KEY in caplog.text
        assert "task-1806-fold" in caplog.text
        assert "a2a_fold_context_event" in caplog.text
        assert "degraded_allowed" in caplog.text

    def test_fold_task_rejection_emits_structured_warning(self, a2a_event_repo, caplog):
        client = self._client(a2a_event_repo)
        with caplog.at_level("WARNING"):
            r = self._post(client, self._body(FOLD_KEY, "source_pr: 754"))
        assert r.status_code == 400, r.text
        assert "a2a_fold_context_event" in caplog.text
        assert "rejected" in caplog.text
        event_line = next(
            line for line in caplog.text.splitlines() if "a2a_fold_context_event=" in line)
        payload = json.loads(event_line.split("a2a_fold_context_event=", 1)[1])
        assert payload["event"] == "a2a_fold_context_guard"
        assert payload["idempotency_key"] == FOLD_KEY
        assert payload["missing_fields"] == ["target_pr", "target_files", "fold_reason"]

    def test_existing_fold_event_idempotent_retry_not_rejected(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
        first = self._post(client, self._body(FOLD_KEY, VALID_CONTEXT))
        assert first.status_code == 200, first.text
        seq = first.json()["seq"]
        retry = self._post(client, self._body(FOLD_KEY, ""))
        assert retry.status_code == 200, retry.text
        assert retry.json().get("idempotent") is True
        assert retry.json()["seq"] == seq
        assert self._event_count() == 1

    def test_non_fold_task_empty_context_and_idempotency_unchanged(self, a2a_event_repo):
        client = self._client(a2a_event_repo)
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