"""Issue #367: 同名 skill 解析 scope 优先级测试。

验证点：
  1. resolve_skill_id 查询顺序 user → repo → official（个人优先）；
  2. owner_id 强匹配：用户 A 不误命中用户 B 的个人 skill；
  3. user_id 缺失时按 repo → official 降级（等同现状）；
  4. 仅 official 存在时零回归；
  5. build_general_chat_prompt：skill_id 指向 user/repo scope 时注入真实绝对路径
     （同名 official 也不回退 {{SKILL_DIR}} 占位）；official scope 保持占位。
"""
import os
import uuid

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.user import db as udb
from src.prompt_builder import build_general_chat_prompt
from src.session_manager import db, db_lock


def _mk_user(prefix="sk367"):
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    u = udb.create_user(uname, "pw123")
    if not u:
        u = udb.get_user_by_username(uname)
    return u, uname


def _cleanup_user(uname):
    u = udb.get_user_by_username(uname)
    if u:
        udb.delete_user(u["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


def _insert_scope_skill(sname, scope, owner_id, agent_type="issue"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO skill (skill_name, agent_type, skill_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,1)",
            (sname, agent_type, sname, scope, owner_id))
        db.commit()
    row = _skill_row(sname, scope, owner_id)
    assert row, f"insert {scope} skill failed"
    return row["id"]


def _skill_row(sname, scope, owner_id):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1",
                  (sname, scope, owner_id))
        row = c.fetchone()
    return {"id": row[0]} if row else None


def _delete_skills(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


class TestResolveSkillIdPriority:
    def test_user_over_repo_and_official(self):
        """user+repo+official 三同名并存 → 命中 user（个人优先）。"""
        ua, a_name = _mk_user()
        sname = f"clash_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_r = _insert_scope_skill(sname, "repo", rk)
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            got = udb.resolve_skill_id(sname, repo_key=rk, user_id=ua["id"])
            assert got == sid_u, f"user 应最高优先: expect {sid_u}, got {got}"
            assert got not in (sid_o, sid_r)
        finally:
            _delete_skills(sname)
            _cleanup_user(a_name)

    def test_repo_over_official(self):
        """repo+official 同名 → 命中 repo。"""
        sname = f"clash_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_r = _insert_scope_skill(sname, "repo", rk)
            got = udb.resolve_skill_id(sname, repo_key=rk, user_id="")
            assert got == sid_r, f"repo 应优先于 official: expect {sid_r}, got {got}"
        finally:
            _delete_skills(sname)

    def test_official_only_no_regression(self):
        """仅 official 存在 → 命中 official（零回归）。"""
        sname = f"solo_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            assert udb.resolve_skill_id(sname) == sid_o
            assert udb.resolve_skill_id(sname, repo_key=rk, user_id="12345") == sid_o
        finally:
            _delete_skills(sname)

    def test_owner_strict_match(self):
        """用户 A 的 user_id 不命中用户 B 的个人 skill → 降级 official。"""
        ua, a_name = _mk_user("sk367a")
        ub, b_name = _mk_user("sk367b")
        sname = f"priv_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_b = _insert_scope_skill(sname, "user", str(ub["id"]))
            # A 触发：不能命中 B 的个人 skill，降级 official
            got = udb.resolve_skill_id(sname, repo_key=rk, user_id=ua["id"])
            assert got == sid_o, f"A 不应命中 B 的个人 skill: expect official {sid_o}, got {got}"
            # B 触发：命中自己的个人 skill
            assert udb.resolve_skill_id(sname, repo_key=rk, user_id=ub["id"]) == sid_b
        finally:
            _delete_skills(sname)
            _cleanup_user(a_name)
            _cleanup_user(b_name)

    def test_missing_user_id_degrades(self):
        """user_id 缺失 → repo → official 降级（等同现状）。"""
        sname = f"degr_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_r = _insert_scope_skill(sname, "repo", rk)
            assert udb.resolve_skill_id(sname, repo_key=rk) == sid_r
            assert udb.resolve_skill_id(sname) == sid_o
        finally:
            _delete_skills(sname)


class TestPromptSkillPathInjection:
    def test_user_scope_same_name_official_uses_real_path(self):
        """user scope skill 与 official 同名 → 注入真实绝对路径，非 {{SKILL_DIR}} 占位。"""
        ua, a_name = _mk_user()
        sname = f"dupe_{uuid.uuid4().hex[:6]}"
        try:
            _insert_scope_skill(sname, "official", "")
            udb.create_skill(ua["id"], sname, "issue")
            row = _skill_row(sname, "user", str(ua["id"]))
            sid_u = row["id"]
            prompt = build_general_chat_prompt(
                "hello", skill_name=sname, skill_id=sid_u)
            assert "## Current Skill:" in prompt
            assert "{{SKILL_DIR}}" not in prompt, \
                "user scope 同名官方时不得回退官方占位"
            assert "Skill file:" in prompt
            # 真实路径应含 custom/<owner_id>
            assert f"custom{os.sep}{ua['id']}" in prompt or f"custom/{ua['id']}" in prompt
        finally:
            _delete_skills(sname)
            _cleanup_user(a_name)

    def test_repo_scope_uses_real_path(self):
        """repo scope skill（无同名 official）→ 注入真实绝对路径。"""
        sname = f"rpo_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test367"
        try:
            sid_r = _insert_scope_skill(sname, "repo", rk)
            prompt = build_general_chat_prompt(
                "hello", skill_name=sname, skill_id=sid_r)
            assert "{{SKILL_DIR}}" not in prompt
            assert "Skill file:" in prompt
        finally:
            _delete_skills(sname)

    def test_official_scope_keeps_placeholder(self):
        """official scope skill_id → 保持 {{SKILL_DIR}} 官方占位（回归）。"""
        sname = f"offi_{uuid.uuid4().hex[:6]}"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            prompt = build_general_chat_prompt(
                "hello", skill_name=sname, skill_id=sid_o)
            assert "{{SKILL_DIR}}" in prompt
            assert "Skill file: {{SKILL_DIR}}" in prompt
        finally:
            _delete_skills(sname)
