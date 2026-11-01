# -*- coding: utf-8 -*-
"""Issue #630：技能物化完整性校验门禁 + /api/skills/by-id name→sid 解析测试。

验证点：
  1. verify_skill_materializable：
     - 技能未配置 → 恒通过（不误伤，Spec 风险表）
     - 空壳技能行（file_path 空 + 本地目录无 SKILL.md，即 sid=3276 形态）→ 阻断
     - 本地目录完整（SKILL.md + references）→ 通过
     - OBS 对象键可读 → 通过（worker 按 sid 整包拉取）
     - 名称不可解析且无官方内置包 → 阻断；有官方内置包 → 通过（兜底）
  2. GET /api/skills/by-id/<技能名>：
     - 用户级技能按名称查询 200（不再 422）
     - 未知名 404；数字 id 路径行为不变
"""
import os
import uuid

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock
from src.runtime.service import verify_skill_materializable

from src.auth.password import hash_password as _hash_pw

client = TestClient(app)


def _mk_skill_row(scope="user", owner="", sname="", file_path="", skill_path=""):
    """直接插 skill 表行（绕开 create_skill 的目录副作用）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "file_path, scope, owner_id, is_active) VALUES (?,?,?,?,?,?,?,?,1)",
            (sname, sname, "desc", "vod", skill_path or sname, file_path, scope, owner))
        db.commit()
        return c.lastrowid


class TestVerifySkillMaterializable:
    def test_no_skill_configured_passes(self):
        ok, reason, sid = verify_skill_materializable("")
        assert ok and sid == 0

    def test_empty_shell_row_blocked(self):
        """空壳技能行（legacy 注册、无 file_path、本地目录空）→ 门禁阻断。"""
        sname = f"shell630_{uuid.uuid4().hex[:6]}"
        _dir = os.path.join(user_db.SKILL_DIR, "8", sname)
        os.makedirs(_dir, exist_ok=True)
        try:
            sid = _mk_skill_row(scope="user", owner="8", sname=sname)
            ok, reason, _ = verify_skill_materializable(sname, skill_id=sid)
            assert not ok, "空壳技能行必须阻断"
            assert "materialization source missing" in reason
        finally:
            os.rmdir(_dir)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()

    def test_local_complete_dir_passes(self):
        """本地目录完整（SKILL.md + references）→ 通过。"""
        sname = f"local630_{uuid.uuid4().hex[:6]}"
        _dir = os.path.join(user_db.SKILL_DIR, "9", sname)
        os.makedirs(os.path.join(_dir, "references"), exist_ok=True)
        with open(os.path.join(_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\n---\nsee references/run.md\n" % sname)
        with open(os.path.join(_dir, "references", "run.md"), "w", encoding="utf-8") as f:
            f.write("# run")
        try:
            sid = _mk_skill_row(scope="user", owner="9", sname=sname)
            ok, reason, _ = verify_skill_materializable(sname, skill_id=sid)
            assert ok, reason
            assert "local skill dir ok" in reason
        finally:
            import shutil
            shutil.rmtree(_dir, ignore_errors=True)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()

    def test_obs_object_key_passes(self):
        """file_path 有 OBS 键且存储可读 → 通过（按 sid 整包拉取路径）。"""
        sname = f"obs630_{uuid.uuid4().hex[:6]}"
        sid = _mk_skill_row(scope="user", owner="8", sname=sname,
                            file_path="skills/custom/8/%s.zip" % sname)
        try:
            class _FakeStorage:
                def read_bytes(self, key):
                    return b"zip"

            with patch("src.storage.get_skill_storage", return_value=_FakeStorage()):
                ok, reason, _ = verify_skill_materializable(sname, skill_id=sid)
            assert ok and "obs object ok" in reason, reason
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()

    def test_unresolvable_skill_without_official_blocked(self):
        ok, reason, sid = verify_skill_materializable("no_such_skill_630xyz")
        assert not ok and sid == 0
        assert "unresolvable" in reason

    def test_unresolvable_with_official_bundle_passes(self):
        """sid=0 但官方内置包存在 → 放行（worker official fallback 完整物化）。"""
        with patch("src.config.resolve_official_skill_path", return_value="/fake/SKILL.md"), \
             patch("os.path.isfile", return_value=True):
            ok, _reason, sid = verify_skill_materializable("some_official_like_630")
        assert ok and sid == 0

    def test_storage_unavailable_with_filepath_and_no_local_dir_blocked(self):
        """PR #797 审查修复：storage 不可用 + file_path 有值 + 本地目录缺失 → 阻断。

        原 fail-open 缺陷：get_skill_storage 返回 None 时直接放行且 reason 误标
        "obs object ok"，worker 端拉取会静默失败。
        """
        sname = f"stfail630_{uuid.uuid4().hex[:6]}"
        sid = _mk_skill_row(scope="user", owner="8", sname=sname,
                            file_path="skills/custom/8/%s.zip" % sname)
        try:
            with patch("src.storage.get_skill_storage", return_value=None):
                ok, reason, _ = verify_skill_materializable(sname, skill_id=sid)
            assert not ok, "storage 不可用时不得 fail-open 放行"
            assert "storage unavailable" in reason, reason
            assert "obs object ok" not in reason, reason
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()

    def test_storage_unavailable_with_complete_local_dir_passes(self):
        """storage 不可用但本地目录完整（SKILL.md + references）→ 放行（不误伤可物化技能）。"""
        sname = f"stlocal630_{uuid.uuid4().hex[:6]}"
        _dir = os.path.join(user_db.SKILL_DIR, "9", sname)
        os.makedirs(os.path.join(_dir, "references"), exist_ok=True)
        with open(os.path.join(_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\n---\nsee references/run.md\n" % sname)
        with open(os.path.join(_dir, "references", "run.md"), "w", encoding="utf-8") as f:
            f.write("# run")
        try:
            sid = _mk_skill_row(scope="user", owner="9", sname=sname,
                                file_path="skills/custom/9/%s.zip" % sname)
            with patch("src.storage.get_skill_storage", return_value=None):
                ok, reason, _ = verify_skill_materializable(sname, skill_id=sid)
            assert ok, reason
            assert "local skill dir ok" in reason
        finally:
            import shutil
            shutil.rmtree(_dir, ignore_errors=True)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()


class TestSkillsByIdNameResolution:
    """GET /api/skills/by-id/{name}：name→sid 解析（Issue #630 反馈②，422→200）。"""

    def _headers(self, uid, uname):
        from src.api.jwt_utils import create_access_token
        return {"Authorization": "Bearer " + create_access_token(uid, uname, 0)}

    def test_by_name_user_scope_returns_200(self):
        """用户级技能按名称查询详情（此前 422：路径参数 int 解析失败）。"""
        uname = f"u630_{uuid.uuid4().hex[:6]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                (uname, None, _hash_pw("pw630"), uname))
            db.commit()
            uid = c.lastrowid
        sname = f"byid630_{uuid.uuid4().hex[:6]}"
        sid = _mk_skill_row(scope="user", owner=str(uid), sname=sname,
                            file_path=f"skills/custom/{uid}/{sname}.zip")
        try:
            class _FakeStorage:
                backend_name = "fake"
                def temporary_url(self, key):
                    return ""

            with patch("src.storage.get_skill_storage", return_value=_FakeStorage()), \
                 patch("src.storage.skill_md_content", return_value="# skill md"):
                r = client.get(f"/api/skills/by-id/{sname}", headers=self._headers(uid, uname))
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            body = r.json()
            assert body.get("id") == sid
            assert body.get("name") == sname
            # 数字 id 路径行为不变
            with patch("src.storage.get_skill_storage", return_value=_FakeStorage()), \
                 patch("src.storage.skill_md_content", return_value="# skill md"):
                r2 = client.get(f"/api/skills/by-id/{sid}", headers=self._headers(uid, uname))
            assert r2.status_code == 200 and r2.json().get("id") == sid
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
                db.commit()

    def test_by_name_unknown_returns_404_not_422(self):
        uname = f"u630b_{uuid.uuid4().hex[:6]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                (uname, None, _hash_pw("pw630"), uname))
            db.commit()
            uid = c.lastrowid
        try:
            r = client.get("/api/skills/by-id/no-such-skill-630xyz",
                           headers=self._headers(uid, uname))
            assert r.status_code == 404, f"expect 404, got {r.status_code}: {r.text[:200]}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
                db.commit()

    def test_by_name_other_users_skill_not_visible(self):
        """user scope 隔离：他人技能按名称解析不到（404，不越权）；数字 id 路径 403。"""
        uname = f"u630c_{uuid.uuid4().hex[:6]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                (uname, None, _hash_pw("pw630"), uname))
            db.commit()
            uid = c.lastrowid
        sname = f"priv630_{uuid.uuid4().hex[:6]}"
        sid = _mk_skill_row(scope="user", owner="999999", sname=sname)
        try:
            # 名称解析走调用者 user scope（owner 不匹配）→ 不泄露他人技能（404）
            r = client.get(f"/api/skills/by-id/{sname}", headers=self._headers(uid, uname))
            assert r.status_code == 404, f"expect 404, got {r.status_code}"
            # 数字 id 直查 → 归属校验 403（既有行为回归）
            r2 = client.get(f"/api/skills/by-id/{sid}", headers=self._headers(uid, uname))
            assert r2.status_code == 403, f"expect 403, got {r2.status_code}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
                db.commit()


class TestPipelineEnqueueGate:
    """Issue #630：_enqueue_pipeline_task enqueue 前门禁——空壳技能阻断派发。"""

    def test_gate_blocks_empty_shell_skill(self, monkeypatch, tmp_path):
        from src.api import event_pipeline as ep

        sname = f"pipeshell630_{uuid.uuid4().hex[:6]}"
        _dir = os.path.join(user_db.SKILL_DIR, "8", sname)
        os.makedirs(_dir, exist_ok=True)  # 空目录（legacy 注册形态，sid=3276 同型）
        sid = _mk_skill_row(scope="user", owner="8", sname=sname)
        failed = []
        try:
            monkeypatch.setattr("src.api.event_archive.init_event_json", lambda *a, **k: None)
            monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **k: str(tmp_path))
            monkeypatch.setattr("src.api.session_store.get_or_create_agent_session",
                                lambda *a, **k: {"id": "sess-630"})
            monkeypatch.setattr(ep, "_fail_event",
                                lambda seq, msg, **k: failed.append((seq, msg)))
            _enqueued = []
            monkeypatch.setattr("src.runtime.service.enqueue_task",
                                lambda **k: _enqueued.append(k) or "t_never")
            ep._enqueue_pipeline_task(
                "gitcode:org/gate630", 9001, "vod", "opencode", sname,
                {"type": "a2a_event"}, sid, sname, 1, issue_id="#630", user_id=8)
            assert failed, "空壳技能必须触发 _fail_event 显式阻断"
            assert "技能物化校验未通过" in failed[0][1]
            assert not _enqueued, "门禁失败不得入队（不静默降级）"
        finally:
            os.rmdir(_dir)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE id=?", (sid,))
                db.commit()

    def test_gate_passes_configured_ok_skill(self, monkeypatch, tmp_path):
        """技能未配置（推荐模式）不阻断，正常入队（Spec 风险表：不误伤）。"""
        from src.api import event_pipeline as ep

        failed = []
        monkeypatch.setattr("src.api.event_archive.init_event_json", lambda *a, **k: None)
        monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **k: str(tmp_path))
        monkeypatch.setattr("src.api.session_store.get_or_create_agent_session",
                            lambda *a, **k: {"id": "sess-630"})
        monkeypatch.setattr(ep, "_fail_event",
                            lambda seq, msg, **k: failed.append((seq, msg)))
        monkeypatch.setattr("src.api.session_store.get_or_create_issue_session",
                            lambda *a, **k: {"id": "is-630"})
        monkeypatch.setattr("src.api.session_store.guard_issue_session_pointer",
                            lambda *a, **k: None)
        monkeypatch.setattr("src.api.event_store.update_event_config", lambda *a, **k: None)
        monkeypatch.setattr("src.runtime.instances.runtime_instance_name", lambda *a, **k: "opencode-1")
        monkeypatch.setattr("src.prompt_builder.build_devops_agent_prompt", lambda *a, **k: "prompt-630")
        monkeypatch.setattr("src.prompt_builder._resolve_server_url", lambda *a, **k: "http://srv")
        _enqueued = []
        monkeypatch.setattr("src.runtime.service.enqueue_task",
                            lambda **k: _enqueued.append(k) or "t_ok630")
        monkeypatch.setattr("src.runtime.service.collect_knowledge_files",
                            lambda *a, **k: {"project_info": {}, "meta": {}, "knowledge": {}})
        try:
            ep._enqueue_pipeline_task(
                "gitcode:org/gate630", 9002, "vod", "opencode", "",
                {"type": "a2a_event"}, 0, "", 1, issue_id="#630", user_id=8)
            assert not failed
            assert _enqueued, "未配置技能不得被门禁误阻断"
        finally:
            pass
