"""Issue #448：子技能三端点支持 OBS 存储的 user/repo 技能（references/ zip 提取）。

覆盖（对齐 issue 验收标准）：
- user 技能（OBS 存储、zip 含 references/sub1/SKILL.md）列表/详情/package-sub 均返回 sub1
- official 技能本地目录优先（有对象键也不走 zip，行为不变）
- 磁盘 zip 缓存命中时不回源 OBS（storage.read_bytes 计数不变）
- 存储未启用（storage=None）但有对象键 → 503；无对象键（历史数据）→ 沿用 404/空列表
- sub_name 路径穿越（../）→ 404（entry 精确匹配天然免疫）

Mock 策略：DB 用共享测试 SQLite（本用例自建行、自清理）；存储用内存 FakeStorage
（带 read_bytes 计数），不触真实 OBS；zip 磁盘缓存在 fixture 中按对象键失效隔离。
端点为同步函数，直接调用传 user= 满足 Depends 默认值。
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from src.api.routes.skills import (  # noqa: E402
    list_sub_skills, read_sub_skill, package_sub_skill)
from src.session_manager import db, db_lock  # noqa: E402
from src.storage import StorageBackend, zip_cache_invalidate  # noqa: E402

SKILL = "obs-sub-test-skill"
OBJECT_KEY = "skills/user/1001/obs-sub-test-skill.zip"


class FakeStorage(StorageBackend):
    """内存 OBS 模拟：read_bytes 带计数（断言缓存命中不回源）。"""

    def __init__(self):
        self.objects = {}
        self.read_count = 0

    def backend_name(self):
        return "obs"

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def read_bytes(self, key):
        self.read_count += 1
        return self.objects.get(key)

    def exists(self, key):
        return key in self.objects

    def list_keys(self, prefix):
        return [k for k in self.objects if k.startswith(prefix)]

    def delete_object(self, key):
        return self.objects.pop(key, None) is not None

    def temporary_url(self, key, expires=300):
        return f"https://obs.test/{key}"

    def copy_object(self, src, dst):
        if src in self.objects:
            self.objects[dst] = self.objects[src]
            return True
        return False

    def save_stream(self, key, source):
        return key

    def read_file(self, key, local_path):
        return False

    def delete_prefix(self, prefix):
        return 0


def _make_pkg_zip() -> bytes:
    """构造带子技能的技能包 zip：references/sub1、sub2，sub1 带二级 references 附件。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{SKILL}/SKILL.md",
                    f"---\nname: {SKILL}\ndescription: pkg\n---\n\n## 主技能")
        zf.writestr(f"{SKILL}/references/sub1/SKILL.md",
                    "---\nname: 子技能一\ndescription: sub one\n---\n\nsub1 content")
        zf.writestr(f"{SKILL}/references/sub1/references/extra.md", "extra doc")
        zf.writestr(f"{SKILL}/references/sub1/references/nested/deep.md", "not top-level")
        zf.writestr(f"{SKILL}/references/sub2/SKILL.md",
                    "---\nname: sub-two\ndescription: sub two\n---\n\nsub2 content")
    return buf.getvalue()


def _insert_skill(skill_name, scope, owner_id, skill_path, file_path):
    """插入测试技能行，返回 id。"""
    with db_lock:
        db.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type,"
            " skill_path, file_path, scope, owner_id, is_active)"
            " VALUES (?,?,?,?,?,?,?,?,1)",
            (skill_name, "", "", "", skill_path, file_path, scope, owner_id))
        db.commit()
        row = db.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=?",
                         (skill_name, scope, owner_id)).fetchone()
    return row[0]


@pytest.fixture()
def obs_env(monkeypatch):
    """FakeStorage + 干净的测试技能行 + zip 磁盘缓存失效。"""
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'"
                   " OR (skill_name='huawei-cloud' AND owner_id='issue-448-test')")
        db.commit()
    zip_cache_invalidate(OBJECT_KEY)
    fake = FakeStorage()
    fake.objects[OBJECT_KEY] = _make_pkg_zip()
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: fake)
    yield fake
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'")
        db.commit()
    zip_cache_invalidate(OBJECT_KEY)


def test_list_sub_skills_from_obs(obs_env):
    """UC1：user 技能（OBS 存储）子技能列表返回 sub1/sub2，含 frontmatter 解析与文件计数。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = list_sub_skills(sid, user={"id": "1001"})
    names = [s["name"] for s in result["sub_skills"]]
    assert names == ["sub1", "sub2"], f"unexpected subs: {result}"
    sub1 = result["sub_skills"][0]
    assert sub1["display_name"] == "子技能一"
    assert sub1["description"] == "sub one"
    # sub1: SKILL.md + references/extra.md + references/nested/deep.md = 3
    assert sub1["file_count"] == 3, f"file_count wrong: {sub1}"


def test_read_sub_skill_from_obs(obs_env):
    """UC2：user 技能子技能详情返回 content + 二级 references 顶层文件（各截 2000 字符）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = read_sub_skill(sid, "sub1", user={"id": "1001"})
    assert "sub1 content" in result["content"]
    refs = {r["name"]: r["content"] for r in result["references"]}
    assert refs == {"extra.md": "extra doc"}, f"refs wrong: {refs}"  # nested/ 非顶层不读


def test_package_sub_from_obs(obs_env):
    """UC3：package-sub 端点返回与 detail 同构结果（type=package-sub）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    result = package_sub_skill(sid, "sub1", user={"id": "1001"})
    assert result["type"] == "package-sub"
    assert result["display_name"] == "子技能一"
    assert result["description"] == "sub one"
    assert "sub1 content" in result["content"]


def test_official_local_dir_priority(obs_env):
    """UC4（防回归）：official 技能本地目录优先——有对象键也不走 zip。

    官方技能本地 references/ 命中时不得读取 OBS（本用例把 zip 换成垃圾字节：
    若误走 zip 分支，list 结果为空/异常，即证未走本地）。
    """
    obs_env.objects[OBJECT_KEY] = b"not-a-zip"
    # 官方技能按 skill_name 解析本地目录（skill-description.json → skills/sre/app-release/huawei-cloud）。
    # owner_id 用独立哨兵值避开 UNIQUE(skill_name, scope, owner_id) 与真实 official 行冲突。
    sid = _insert_skill("huawei-cloud", "official", "issue-448-test",
                        "sre/app-release/huawei-cloud", OBJECT_KEY)
    result = list_sub_skills(sid, user={"id": "1"})
    assert result["sub_skills"], "official local sub-skills should be listed"
    assert obs_env.read_count == 0, "official local path must not read OBS"


def test_zip_cache_hit_no_refetch(obs_env):
    """UC5：磁盘 zip 缓存命中不回源——两次列表只触发一次 storage.read_bytes。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    list_sub_skills(sid, user={"id": "1001"})
    list_sub_skills(sid, user={"id": "1001"})
    assert obs_env.read_count == 1, f"expected 1 storage read, got {obs_env.read_count}"


def test_storage_disabled_503(monkeypatch):
    """UC6：有对象键但存储未启用 → 503（对齐 detail/convert 端点口径）。"""
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'")
        db.commit()
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: None)
    with pytest.raises(HTTPException) as exc:
        list_sub_skills(sid, user={"id": "1001"})
    assert exc.value.status_code == 503
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'")
        db.commit()


def test_legacy_user_skill_without_object_key_empty(monkeypatch):
    """UC7（边界）：历史 user 技能无对象键 → 沿用空列表口径（不 503、不 404）。"""
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'")
        db.commit()
    sid = _insert_skill("obs-sub-test-legacy", "user", "1001", "some/local/path", "")
    monkeypatch.setattr("src.storage.get_skill_storage", lambda: None)
    result = list_sub_skills(sid, user={"id": "1001"})
    assert result == {"skill_name": "obs-sub-test-legacy", "sub_skills": []}
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-sub-test%'")
        db.commit()


def test_sub_name_traversal_404(obs_env):
    """UC8：sub_name 路径穿越（../）→ 404，不读 zip 内主技能 SKILL.md。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        read_sub_skill(sid, "../" + SKILL, user={"id": "1001"})
    assert exc.value.status_code == 404


def test_sub_not_in_zip_404(obs_env):
    """UC9：zip 内不存在的子技能 → 404。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        read_sub_skill(sid, "nope", user={"id": "1001"})
    assert exc.value.status_code == 404


def test_official_read_and_package_sub_local_priority(obs_env):
    """UC4 补齐：read/package-sub 两个详情端点同样本地优先——zip 为垃圾字节也不读 OBS。"""
    obs_env.objects[OBJECT_KEY] = b"not-a-zip"
    sid = _insert_skill("huawei-cloud", "official", "issue-448-test",
                        "sre/app-release/huawei-cloud", OBJECT_KEY)
    # read 端点：本地 references/huawei-ecs/SKILL.md 命中
    result = read_sub_skill(sid, "huawei-ecs", user={"id": "1"})
    assert "description:" in result["content"], "official read_sub must come from local dir"
    # package-sub 端点：同一本地文件命中
    pkg = package_sub_skill(sid, "huawei-ecs", user={"id": "1"})
    assert pkg["type"] == "package-sub" and "description:" in pkg["content"]
    assert obs_env.read_count == 0, "official local path must not read OBS"


def test_package_sub_traversal_404(obs_env):
    """UC8 补齐：package-sub 端点 sub_name 穿越 → 404（entry 精确匹配免疫）。"""
    sid = _insert_skill(SKILL, "user", "1001", "skills/user/1001/x.zip", OBJECT_KEY)
    with pytest.raises(HTTPException) as exc:
        package_sub_skill(sid, "../" + SKILL, user={"id": "1001"})
    assert exc.value.status_code == 404
