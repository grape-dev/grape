"""feedback-issue-body-redact-m1-design.md 验收测试（TDD 先红后绿）。

M-1：反馈→Issue 转换在公开仓库发布前对 fail_summary/description 做 IP/路径/凭据脱敏。

覆盖 R1-R8：
- R1 内网 IPv4 + 端口 → <redacted>
- R2 绝对路径（Unix / Windows）→ <redacted>
- R3 凭据（password=xxx / https://user:pass@host）→ 打码
- R4 幂等：redact_issue_text(redact_issue_text(x)) == redact_issue_text(x)
- R5 普通文案不被误伤（仅长度截断仍生效）
- R6 _build_issue_body 集成：含内网 IP 的 fail_summary 不进正文
- R7 submit_feedback 写侧落库前已打码
- R8 版本号/编号（1.2.3 非 IP）、CJK 文案不被误替换
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from unittest.mock import patch

from src.observability.redact_issue_body import redact_issue_text


# ---------- R1：内网 IPv4 + 端口 ----------

def test_r1_ip_with_port_redacted():
    s = "Connection refused to 192.168.1.10:38080, retry"
    out = redact_issue_text(s)
    assert "192.168.1.10" not in out
    assert "38080" not in out
    assert "<redacted>" in out


def test_r1_public_ip_redacted_too():
    # 全量 IPv4 兜底：公网 IP 同样打码（Node 汇报/日志里的真实地址都不可外泄）
    s = "probe http://203.0.113.5:8080/health failed"
    out = redact_issue_text(s)
    assert "203.0.113.5" not in out
    assert "<redacted>" in out


# ---------- R2：绝对路径 ----------

def test_r2_unix_abs_path_redacted():
    s = "File not found: /root/configs/server-config.json"
    out = redact_issue_text(s)
    assert "/root/configs" not in out
    assert "<redacted>" in out


def test_r2_windows_abs_path_redacted():
    s = "Open failed: C:\\Users\\admin\\secrets\\key.pem"
    out = redact_issue_text(s)
    assert "C:\\Users" not in out
    assert "<redacted>" in out


# ---------- R3：凭据 ----------

def test_r3_password_kv_redacted():
    s = "login failed password=abc123 for admin"
    out = redact_issue_text(s)
    assert "abc123" not in out
    # 保持 key= 前缀可读（logs.redact 语义）；值被抹掉
    assert "password=" in out or "=***" in out or "<redacted>" in out


def test_r3_url_userpass_redacted():
    s = "clone https://u:pw@gitcode.com/owner/repo.git failed"
    out = redact_issue_text(s)
    assert "u:pw@" not in out
    assert "pw@" not in out


def test_r3_token_prefix_redacted():
    s = "bad token grape_AbC123xYz when calling api"
    out = redact_issue_text(s)
    assert "grape_AbC123xYz" not in out


# ---------- R4：幂等 ----------

def test_r4_idempotent():
    s = "conn 192.168.1.10:8080, path /root/x, pass=abc"
    once = redact_issue_text(s)
    twice = redact_issue_text(once)
    assert twice == once


# ---------- R5：普通文案不误伤 + 长度截断 ----------

def test_r5_plain_text_unchanged():
    s = "需求方案已完成，开始编码；commit message 为修复文案。"
    assert redact_issue_text(s) == s


def test_r5_length_truncation_kept():
    s = "x" * 3000
    out = redact_issue_text(s, max_len=2000)
    assert len(out) == 2000


# ---------- R6：_build_issue_body 集成 ----------

def test_r6_build_issue_body_redacts_summary():
    from src.api.feedback_issue_converter import _build_issue_body
    row = {
        "id": 1, "repo_key": "gitcode:org/repo", "task_id": "t_x", "agent_type": "vod",
        "skill_name": "s", "skill_scope": "official", "event_seq": 9, "issue_number": "1",
        "description": "desc",
        "fail_summary": "FAIL conn 192.168.2.3:9999 err",
    }
    body = _build_issue_body(row)
    assert "192.168.2.3" not in body
    assert "9999" not in body
    assert "<redacted>" in body


def test_r6_build_issue_body_redacts_description():
    from src.api.feedback_issue_converter import _build_issue_body
    row = {
        "id": 2, "repo_key": "gitcode:org/repo", "task_id": "t_y", "agent_type": "coding",
        "skill_name": "s", "skill_scope": "official", "event_seq": 10, "issue_number": "2",
        "description": "该环境 /root/deploy/.env 读取失败",
        "fail_summary": "",
    }
    body = _build_issue_body(row)
    assert "/root/deploy" not in body
    assert "<redacted>" in body


# ---------- R7：submit_feedback 写侧落库前打码 ----------

def test_r7_submit_pre_redacts_fail_summary(monkeypatch):
    """submit_feedback 落库前对 fail_summary 先脱敏（防明文囤积库内）。

    用内存 DB 替换 feedback 模块的 db/db_lock，走真实 INSERT 捕获参数，
    避免 mock sqlite3.Connection.cursor（只读属性不可 patch）。
    """
    import sqlite3
    import threading
    from src.api.routes import feedback as fb_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    lock = threading.RLock()
    with lock:
        conn.execute(
            """CREATE TABLE user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id, username, category, title, description, page_url,
                repo_key, attachment_url, attachment_name, status,
                event_seq, task_id, issue_id, issue_number, agent_type,
                engine_info, fail_summary, source, skill_id, skill_name, skill_scope,
                feedback_kind
            )""")
        conn.commit()
    monkeypatch.setattr(fb_mod, "db", conn)
    monkeypatch.setattr(fb_mod, "db_lock", lock)

    class _Ctx:
        def get(self, k, d=None):
            m = {
                "repo_key": "gitcode:org/repo",
                "event_seq": 11,
                "issue_id": None, "issue_number": "3",
                "agent_type": "vod",
                "task": {"task_id": "t_z"},
                "skill": {"skill_id": 1, "skill_name": "s", "skill_scope": "official"},
                "fail_summary": "conn 10.0.0.2:443 failed",
                "engine": None,
            }
            return m.get(k, d)

    class _User:
        auth_type = "jwt"
        def get(self, k, d=None):
            return {}.get(k, d)

    class _Body:
        event_seq = 11
        repo_key = ""
        category = "bug"
        title = "t"
        description = "d"
        page_url = ""
        attachment_url = ""
        attachment_name = ""
        feedback_kind = ""

    from src.api.routes.feedback import submit_feedback

    from unittest.mock import patch
    with patch.object(fb_mod, "_collect_feedback_context", return_value=_Ctx()):
        submit_feedback(_Body(), _User())

    with lock:
        row = conn.execute("SELECT fail_summary FROM user_feedback WHERE event_seq=11").fetchone()
    assert row is not None, "row should be inserted"
    stored = row[0]
    assert "10.0.0.2" not in stored
    assert "<redacted>" in stored


# ---------- R8：版本号/编号/CJK 不被误替换 ----------

def test_r8_version_number_not_redacted():
    s = "pkg version 1.2.3; issue #42; rate 0.5"
    # 版本号 1.2.3 形态与 IP 类似但不足 4 段，不应被当 IP 打掉
    out = redact_issue_text(s)
    assert "1.2.3" in out
    assert "#42" in out


def test_r8_cjk_not_redacted():
    s = "需求：支持中文标题与描述；修复反馈闭环。"
    assert redact_issue_text(s) == s


# ---------- 边界：空串 ----------

def test_empty_input():
    assert redact_issue_text("") == ""
    assert redact_issue_text(None) == ""