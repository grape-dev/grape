"""skills.py OBS 分支集成测试（FakeStorage 内存实现驱动，见 obs-skill-storage-design.md §9）。

覆盖：上传（规范化重打包/版本递增/DB 回写）、详情（提取 + zip_url）、
转换（copyObject）、删除（对象清理）、模板（临时 URL 返回）。
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest  # noqa: E402
import sys as _sys

_is_win = _sys.platform == "win32"
from fastapi.testclient import TestClient  # noqa: E402
from src.api.main import app  # noqa: E402
from src.user import db as user_db  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402
from src.storage import StorageBackend  # noqa: E402
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    # D32 适配（同 test_auth_token.py OMIT-4）：鉴权链路（deps.get_current_user）只查
    # grape_accounts，旧 user_db.create_user 写死表 user_accounts 导致全用例 401。
    # 改用 create_grape_account；唯一后缀避免跨运行 UNIQUE 冲突。
    import uuid as _uuid
    from src.auth.grape_account_db import create_grape_account, get_grape_account_by_username
    _uname = f"test_obs_{_uuid.uuid4().hex[:8]}"
    _uid = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
    if not _uid:
        raise RuntimeError(f"create_grape_account({_uname}) failed")
    # grape_accounts 不再存平台 token（D32：平台字段移 external_identities），
    # 本文件用例不依赖 gitcode_api_token，无需回写。
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version as _get_tv
    _token = create_access_token(_uid["id"], _uid["username"], _get_tv(_uid["id"]))
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    # 清理本模块创建的技能记录
    with db_lock:
        db.execute("DELETE FROM skill WHERE owner_id IN (?, ?)",
                   (str(_uid["id"]), "testobs/repo1"))
        db.commit()


class FakeStorage(StorageBackend):
    """内存 OBS 模拟：记录对象/复制/删除/URL。"""

    def __init__(self):
        self.objects = {}
        self.copied = []
        self.deleted = []
        self.default_expires = 300

    def backend_name(self):
        return "obs"

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def save_stream(self, key, source):
        with open(source, "rb") as f:
            self.objects[key] = f.read()
        return key

    def read_bytes(self, key):
        return self.objects.get(key)

    def read_file(self, key, local_path):
        data = self.objects.get(key)
        if data is None:
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return True

    def exists(self, key):
        return key in self.objects

    def list_keys(self, prefix):
        return [k for k in self.objects if k.startswith(prefix)]

    def delete_object(self, key):
        if key in self.objects:
            self.objects.pop(key, None)
            self.deleted.append(key)
            return True
        return False

    def delete_prefix(self, prefix):
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            self.objects.pop(k, None)
        self.deleted.extend(keys)
        return len(keys)

    def temporary_url(self, key, expires=300):
        return f"https://obs.test/{key}?expires={expires}"

    def copy_object(self, src, dst):
        if src in self.objects:
            self.objects[dst] = self.objects[src]
            self.copied.append((src, dst))
            return True
        return False


@pytest.fixture(autouse=True)
def _obs_env(monkeypatch):
    """启用 OBS 分支：monkeypatch get_storage 返回 FakeStorage，并清理缓存状态。"""
    fake = FakeStorage()
    monkeypatch.setattr("src.storage.get_storage", lambda: fake)
    from src.storage import reset_storage
    reset_storage()
    # 清理本用例的测试技能记录
    with db_lock:
        db.execute("DELETE FROM skill WHERE skill_name LIKE 'obs-test%'")
        db.commit()
    yield fake


def _make_skill_zip(skill_name: str, with_script: bool = False, binary_extra: str = "",
                    version: "str | None" = "1.0.0") -> bytes:
    """构造技能 zip（SKILL.md + references/ + 可选文本脚本 / 二进制文件）。

    version=None 表示 SKILL.md Front Matter 不写 version 字段（#336：缺失默认 1.0.0）。
    """
    version_line = "" if version is None else f"version: {version}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md",
                    f"---\nname: {skill_name}\n{version_line}description: obs test skill\n---\n\n## 场景\n测试技能")
        zf.writestr(f"{skill_name}/references/design.md", "# Design\n参考文档")
        if with_script:
            # 模拟用户本地 chmod +x 后打包：zip 内 0755，验证权限贯穿链路
            _info = zipfile.ZipInfo(f"{skill_name}/scripts/run_code.py")
            _info.external_attr = 0o755 << 16
            zf.writestr(_info, "#!/usr/bin/env python3\nprint('ok')\n")
        if binary_extra:
            zf.writestr(f"{skill_name}/{binary_extra}", b"\x7fELF\x02\x01\x01\x00evil")
    return buf.getvalue()


def _skill_id(skill_name, scope="user"):
    """sqlite3 直连测试库查技能 id——不依赖 session_manager 模块（避免 test_session_manager
    等测试替换全局模块导致的顺序污染）。"""
    import sqlite3 as _sq
    dbp = os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db")
    conn = _sq.connect(dbp)
    try:
        row = conn.execute(
            "SELECT id FROM skill WHERE skill_name=? AND scope=? ORDER BY id LIMIT 1",
            (skill_name, scope)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _ensure_official_build_debug():
    """官方 build-debug 技能缺失时补插（test_repos_remove_cleanup 等会清空 skill 表）。"""
    if _skill_id("build-debug", "official"):
        return
    import sqlite3 as _sq
    dbp = os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db")
    conn = _sq.connect(dbp)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, is_active) "
            "VALUES ('build-debug','build-debug','官方调试技能','chat','build-debug','official','',1)")
        conn.commit()
    finally:
        conn.close()


class TestSkillsObs:
    @pytest.fixture(autouse=True)
    def _restore_clean_session_manager(self):
        """防御：test_session_manager 等既有测试会替换全局 session_manager 模块
        （指向 tmp 临时库），本文件用例运行时 import 会拿到污染模块导致
        "no such table: skill" → 每用例【执行前】恢复干净实例（DEVOPS_DB_PATH 库），
        执行后再恢复一次防止泄漏给后续文件。"""
        import importlib as _il
        import sys as _sys

        def _restore():
            for _n in ("src.session_manager", "session_manager"):
                _sys.modules.pop(_n, None)
            try:
                _il.import_module("src.session_manager")
            except Exception:
                pass

        _restore()
        yield
        _restore()

    def test_upload_obs_creates_object_and_version(self, _obs_env):
        """上传 → OBS 固定 key 对象存在、DB 记录 key；再次上传 → 同 key 覆盖（对象数不变）。"""
        zip_bytes = _make_skill_zip("obs-test-upload")
        r = client.post("/api/skills/upload",
                        files={"file": ("obs-test-upload.zip", zip_bytes)},
                        data={"agent_type": "chat"}, headers=_headers)
        assert r.status_code == 200, r.text

        # DB 记录：file_path 为固定对象键（v2.5 字段职责分离）
        with db_lock:
            row = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-upload' AND scope='user'").fetchone()
        assert row, "DB 记录缺失"
        object_key = row[0]
        assert "/skills/custom/" in object_key and object_key.endswith("/obs-test-upload.zip")
        # OBS 对象存在且 zip 内根结构正确
        assert object_key in _obs_env.objects
        with zipfile.ZipFile(io.BytesIO(_obs_env.objects[object_key])) as zf:
            assert "obs-test-upload/SKILL.md" in zf.namelist()
        n_objects_after_first = len(_obs_env.objects)

        # 再次上传 → 同 key 原子覆盖（对象数不变，仍 1 个固定 key 对象）
        r2 = client.post("/api/skills/upload",
                         files={"file": ("obs-test-upload.zip", zip_bytes)},
                         data={"agent_type": "chat"}, headers=_headers)
        assert r2.status_code == 200, r2.text
        with db_lock:
            row2 = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-upload' AND scope='user'").fetchone()
        assert row2[0] == object_key, "重复上传应覆盖同一固定 key"
        assert len(_obs_env.objects) == n_objects_after_first, "覆盖不产生新对象"

    def test_upload_obs_rejects_binary_executables(self, _obs_env):
        """安全策略（validator FORBIDDEN_EXTS）：二进制可执行文件（.so/.exe）仍被拒绝。"""
        zip_bytes = _make_skill_zip("obs-test-script", binary_extra="scripts/libevil.so")
        r = client.post("/api/skills/upload",
                        files={"file": ("obs-test-script.zip", zip_bytes)},
                        data={"agent_type": "chat"}, headers=_headers)
        assert r.status_code == 400
        errs = r.json()["detail"].get("errors") or []
        assert any("可执行文件" in e for e in errs), r.text

    @pytest.mark.skipif(_is_win, reason="POSIX-only: 权限位 0755 在 Windows 解压重打包链路不保持")
    def test_upload_obs_allows_text_scripts(self, _obs_env):
        """官方规范 scripts/ 文本脚本（.py/.sh）放行 → OBS 上传成功 + zip 保留脚本。"""
        zip_bytes = _make_skill_zip("obs-script-ok", with_script=True)
        r = client.post("/api/skills/upload",
                        files={"file": ("obs-script-ok.zip", zip_bytes)},
                        data={"agent_type": "chat"}, headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-script-ok' AND scope='user'").fetchone()
        assert row and row[0]
        # zip 内脚本存在（权限位由打包链路保留，worker 物化恢复）
        from src.storage import get_skill_zip_bytes
        zdata = get_skill_zip_bytes(row[0])
        import io as _io, zipfile as _zf
        with _zf.ZipFile(_io.BytesIO(zdata)) as zf:
            names = zf.namelist()
        assert any("scripts/run_code.py" in n for n in names), names
        # 权限位贯穿链路：上传 zip 0755 → 服务端解压恢复 → OBS 重打包保留 0755
        import io as _io2, zipfile as _zf2
        with _zf2.ZipFile(_io2.BytesIO(zdata)) as zf2:
            target = next(i for i in zf2.infolist() if "run_code.py" in i.filename)
            assert (target.external_attr >> 16) == 0o755, oct(target.external_attr >> 16)

    def test_detail_is_obs_field(self, _obs_env):
        """详情返回 is_obs：官方未转 False（默认环境，按 id）。"""
        _ensure_official_build_debug()
        sid = _skill_id("build-debug", "official")
        assert sid, "build-debug 官方技能应存在"
        r = client.get(f"/api/skills/by-id/{sid}", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "is_obs" in d, d
        assert d["is_obs"] is False, d

    def test_download_obs_returns_temp_url(self, _obs_env):
        """下载技能包：OBS 分支返回 {url, expires_in} 临时签名 URL（点击动态签发，按 id）。"""
        zip_bytes = _make_skill_zip("obs-test-dl")
        r = client.post("/api/skills/upload",
                        files={"file": ("obs-test-dl.zip", zip_bytes)},
                        data={"agent_type": "chat"}, headers=_headers)
        assert r.status_code == 200, r.text
        sid = _skill_id("obs-test-dl")
        assert sid, "上传后应查到技能 id"
        r = client.get(f"/api/skills/by-id/{sid}/download", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("url", "").startswith("https://"), d
        assert d.get("expires_in", 0) > 0, d
        assert "/obs-test-dl/" in d["url"], d["url"]

    def test_download_official_rejected(self, _obs_env):
        """官方技能为部署内置物（未转集群），下载接口拒绝（400，按 id）。"""
        _ensure_official_build_debug()
        sid = _skill_id("build-debug", "official")
        assert sid, "build-debug 官方技能应存在"
        r = client.get(f"/api/skills/by-id/{sid}/download", headers=_headers)
        assert r.status_code == 400, r.text
        assert "官方技能" in r.json()["detail"]["message"]

    def test_detail_obs_returns_content_and_zip_url(self, _obs_env):
        """详情（按 id）→ content 提取自 zip、zip_url 非空、版本号、id 字段。"""
        zip_bytes = _make_skill_zip("obs-test-detail")
        client.post("/api/skills/upload",
                    files={"file": ("obs-test-detail.zip", zip_bytes)},
                    data={"agent_type": "chat"}, headers=_headers)
        sid = _skill_id("obs-test-detail")
        assert sid, "上传后应查到技能 id"
        r = client.get(f"/api/skills/by-id/{sid}", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        assert "## 场景" in data["content"]
        assert data["zip_url"].startswith("https://obs.test/")
        assert data["id"] == sid

    def test_convert_obs_copies_object(self, _obs_env):
        """转换 → OBS copyObject 到 repo 前缀 + DB repo 记录。"""
        zip_bytes = _make_skill_zip("obs-test-convert")
        client.post("/api/skills/upload",
                    files={"file": ("obs-test-convert.zip", zip_bytes)},
                    data={"agent_type": "chat"}, headers=_headers)
        r = client.post("/api/skills/convert",
                        json={"skill_name": "obs-test-convert", "repo": "testobs/repo1"},
                        headers=_headers)
        assert r.status_code == 200, r.text
        assert len(_obs_env.copied) == 1
        src, dst = _obs_env.copied[0]
        assert "/skills/custom/" in src and "/skills/repo/testobs/repo1/" in dst
        assert dst in _obs_env.objects  # 目标对象存在
        with db_lock:
            row = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-convert' AND scope='repo' AND owner_id='testobs/repo1'").fetchone()
        assert row and row[0] == dst, f"repo 记录 file_path 应为目标固定 key, got {row}"

    def test_delete_obs_removes_object(self, _obs_env):
        """删除 → OBS 该技能所有版本对象删除（v1+v2）+ DB 记录删除。"""
        zip_bytes = _make_skill_zip("obs-test-delete")
        client.post("/api/skills/upload",
                    files={"file": ("obs-test-delete.zip", zip_bytes)},
                    data={"agent_type": "chat"}, headers=_headers)
        client.post("/api/skills/upload",  # 覆盖同一固定 key
                    files={"file": ("obs-test-delete.zip", zip_bytes)},
                    data={"agent_type": "chat"}, headers=_headers)
        with db_lock:
            keys = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-delete' AND scope='user'").fetchall()
        assert len(keys) == 1  # DB 仅一条记录（固定 key）
        r = client.delete("/api/skills/obs-test-delete?scope=user", headers=_headers)
        assert r.status_code == 200, r.text
        # 固定 key 单对象删除（v2.4：每技能仅 1 对象）
        assert len(_obs_env.deleted) == 1, f"deleted={_obs_env.deleted}"
        assert _obs_env.deleted[0] == keys[0][0], f"deleted={_obs_env.deleted}"
        with db_lock:
            row = db.execute("SELECT COUNT(*) FROM skill WHERE skill_name='obs-test-delete' AND scope='user'").fetchone()
        assert row[0] == 0

    def test_template_obs_returns_url(self, _obs_env):
        """模板下载（OBS 模式）→ 返回 {url, expires_in}。"""
        r = client.get("/api/skills/template", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["url"].startswith("https://obs.test/")
        assert data["expires_in"] == 300

    # ===== #336 version 格式放宽：1~3 段数字 + v 前缀均通过，非法仍 400 =====
    # 注：断言走 API 响应 + app 的 db 句柄（同既有上传用例模式），
    # 不依赖 _skill_id 的相对路径直连（该路径在整文件运行时受 conftest 删库重建影响）。

    def _upload_user(self, skill_name, version):
        zip_bytes = _make_skill_zip(skill_name, version=version)
        return client.post("/api/skills/upload",
                           files={"file": (f"{skill_name}.zip", zip_bytes)},
                           data={"agent_type": "chat"}, headers=_headers)

    def test_upload_version_single_segment_user_scope(self, _obs_env):
        """version: 1（单段数字）user scope 上传成功（#336 核心场景）。"""
        r = self._upload_user("obs-test-ver-1", "1")
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-ver-1' AND scope='user'").fetchone()
        assert row and row[0], "DB 记录缺失"

    def test_upload_version_single_segment_repo_scope(self, _obs_env):
        """version: 1（单段数字）repo scope 上传成功（#336 核心场景）。"""
        zip_bytes = _make_skill_zip("obs-test-ver-repo", version="1")
        r = client.post("/api/skills/upload",
                        files={"file": ("obs-test-ver-repo.zip", zip_bytes)},
                        data={"agent_type": "chat", "scope": "repo", "repo": "testobs/repo1"},
                        headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.execute(
                "SELECT file_path FROM skill WHERE skill_name='obs-test-ver-repo' AND scope='repo'").fetchone()
        assert row and row[0], "DB 记录缺失"

    @pytest.mark.parametrize("version", ["1", "1.2", "1.2.3", "v1.2.3", "v1.2", "v1"])
    def test_upload_version_valid_formats(self, _obs_env, version):
        """合法格式矩阵：1~3 段数字 + v 前缀均上传成功。"""
        r = self._upload_user("obs-test-ver-ok", version)
        assert r.status_code == 200, f"version={version} 应通过: {r.text}"

    def test_upload_version_missing_defaults(self, _obs_env):
        """缺失 version 字段：不报错（默认 1.0.0 现状保持）。"""
        r = self._upload_user("obs-test-ver-miss", None)
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("version", ["abc", "1.2.3.4", "1.x", "x1", "1..2"])
    def test_upload_version_invalid_formats_rejected(self, _obs_env, version):
        """非法格式矩阵：非数字 / 超三段 / 非数字段 / 空段仍 400 且 errors 含明确提示。

        注：不测 "1."——YAML 会将其解析为浮点 1.0（合法），str() 后 "1.0" 通过校验，
        属 YAML 语义而非校验层缺陷。
        """
        r = self._upload_user("obs-test-ver-bad", version)
        assert r.status_code == 400, f"version={version} 应被拒绝"
        errs = r.json()["detail"].get("errors") or []
        assert any("version" in e for e in errs), f"version={version} errors 应含 version 提示: {errs}"
