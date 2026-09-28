"""sync_official_skills_to_obs 回归测试（Issue #251 / PR #349 / Issue #254）。

Issue #254 增强：缓存命中时校验 content_hash 回源（skill_md_content）。

PR #349 修复了上传 OBS 后未失效两级缓存的 bug（skill_sync.py:188-191）：
    await loop.run_in_executor(None, storage.save_bytes, object_key, zip_data)
    zip_cache_put(object_key, zip_data)        # 磁盘 zip 缓存覆盖
    mem_cache_invalidate(skill_name)           # 内存 SKILL.md 缓存清除

本测试验证修复不被退化：
  1. 正向：上传新 zip 后 zip_cache_put + mem_cache_invalidate 各调用一次，
     且 zip_cache_put 的数据与 OBS 上传数据一致。
  2. 反向：hash 未变且 file_path 已存在时走 skip 分支，不调用缓存失效函数
     （防无条件调用的退化修复）。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest

from src.storage.skill_sync import sync_official_skills_to_obs, _compute_skill_hash
from src.storage import StorageBackend


class FakeStorage(StorageBackend):
    """内存 OBS 模拟（参照 tests/test_skills_obs.py 的 FakeStorage 实现）。"""

    def __init__(self):
        self.objects = {}
        self.copied = []
        self.deleted = []

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


class TestSyncOfficialSkillsToObs:
    """回归测试：验证 OBS 上传后两级缓存失效（Issue #251 / PR #349）。"""

    @pytest.fixture
    def skill_env(self, tmp_path, monkeypatch):
        """搭建隔离环境：临时 SKILLS_DIR + CONFIG_DIR + skill-description.json + skill 目录。

        monkeypatch skill_sync 模块级 CONFIG_DIR / SKILLS_DIR（模块级 from src.config import），
        使被测函数读取隔离的临时路径而非项目真实配置。
        """
        skills_dir = tmp_path / "skills"
        config_dir = tmp_path / "configs"
        skills_dir.mkdir()
        config_dir.mkdir()

        skill_name = "regression-test-skill"
        skill_path = skill_name
        skill_dir = skills_dir / skill_path
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: regression-test-skill\nversion: 1.0.0\n---\n\n"
            "## 场景\n回归测试用技能\n",
            encoding="utf-8",
        )

        desc = {
            skill_name: {
                "name": skill_name,
                "description": "regression test skill",
                "agent_type": "chat",
                "path": skill_path,
            }
        }
        (config_dir / "skill-description.json").write_text(
            json.dumps(desc, ensure_ascii=False), encoding="utf-8"
        )

        # 模块级 import：from src.config import SKILLS_DIR, CONFIG_DIR
        monkeypatch.setattr("src.storage.skill_sync.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("src.storage.skill_sync.SKILLS_DIR", str(skills_dir))

        return {
            "skill_name": skill_name,
            "skill_dir": str(skill_dir),
        }

    # ------------------------------------------------------------------
    # 正向：上传新 zip 后缓存失效函数被调用
    # ------------------------------------------------------------------
    def test_upload_invalidates_caches(self, skill_env, monkeypatch):
        """上传新 zip → zip_cache_put + mem_cache_invalidate 各调用一次，
        且 zip_cache_put 的数据与 OBS 上传数据完全一致。"""
        fake = FakeStorage()
        monkeypatch.setattr("src.storage.get_storage", lambda: fake)

        zip_calls = []

        def _track_zip_put(object_key, data, content_hash=""):
            zip_calls.append((object_key, data, content_hash))

        monkeypatch.setattr("src.storage.zip_cache_put", _track_zip_put)

        mem_calls = []

        def _track_mem_invalidate(skill_name):
            mem_calls.append(skill_name)

        monkeypatch.setattr("src.storage.mem_cache_invalidate", _track_mem_invalidate)

        # DB mock：返回旧 hash（与当前不同）+ 空 file_path → 进入上传分支
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (1, "old-different-hash", ""),
        )
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        # result 计数断言：db_hash 非空且 != current_hash → updated +1
        assert result["updated"] == 1, f"expected updated=1, got {result}"
        assert result["added"] == 0, result
        assert result["skipped"] == 0, result

        # OBS 上传断言：恰好 1 个对象，key 格式正确
        assert len(fake.objects) == 1, f"expected 1 OBS object, got {len(fake.objects)}"
        object_key = list(fake.objects.keys())[0]
        assert object_key.endswith(f"/{skill_env['skill_name']}/{skill_env['skill_name']}.zip"), \
            f"unexpected object_key: {object_key}"
        assert "/skills/official/" in object_key, object_key

        # zip_cache_put 断言：恰好调用 1 次，key 与 OBS 对象 key 一致，
        # data 与 OBS 上传数据完全一致（防止传空/旧数据的退化）
        assert len(zip_calls) == 1, f"zip_cache_put should be called once, calls={zip_calls}"
        zip_put_key, zip_put_data, zip_put_hash = zip_calls[0]
        # Issue #254: zip_cache_put should receive content_hash
        assert zip_put_hash, f"zip_cache_put should receive non-empty content_hash, got: {zip_put_hash}"
        assert zip_put_key == object_key, \
            f"zip_cache_put key mismatch: {zip_put_key} != {object_key}"
        assert zip_put_data == fake.objects[object_key], \
            "zip_cache_put data must match OBS uploaded data"

        # mem_cache_invalidate 断言：恰好调用 1 次，skill_name 正确
        assert len(mem_calls) == 1, \
            f"mem_cache_invalidate should be called once, calls={mem_calls}"
        assert mem_calls[0] == skill_env["skill_name"], \
            f"mem_cache_invalidate skill_name: {mem_calls[0]}"

    # ------------------------------------------------------------------
    # 反向：hash 未变且 file_path 已存在 → skip，不调用缓存失效函数
    # ------------------------------------------------------------------
    def test_skip_does_not_invalidate_caches(self, skill_env, monkeypatch):
        """hash 未变且 file_path 已存在时走 skip 分支，
        不调用 zip_cache_put / mem_cache_invalidate（防无条件调用的退化修复）。"""
        fake = FakeStorage()
        monkeypatch.setattr("src.storage.get_storage", lambda: fake)

        zip_calls = []

        def _track_zip_put(object_key, data, content_hash=""):
            zip_calls.append((object_key, data, content_hash))

        monkeypatch.setattr("src.storage.zip_cache_put", _track_zip_put)

        mem_calls = []

        def _track_mem_invalidate(skill_name):
            mem_calls.append(skill_name)

        monkeypatch.setattr("src.storage.mem_cache_invalidate", _track_mem_invalidate)

        # 计算真实 hash，让 db_hash == current_hash → 进入 skip 判断
        real_hash = _compute_skill_hash(skill_env["skill_dir"])
        assert real_hash, "skill hash should not be empty for non-empty skill dir"

        # DB mock：hash 相同 + file_path 非空 → skip（storage 非 None 且 db_file_path 非空）
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (1, real_hash, "grape/prod/skills/official/existing.zip"),
        )
        # update_skill_content_hash 不应被调用，但 mock 以防意外
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        # result 计数断言：skipped +1
        assert result["skipped"] == 1, f"expected skipped=1, got {result}"
        assert result["added"] == 0, result
        assert result["updated"] == 0, result

        # 缓存失效函数不应被调用（核心回归断言）
        assert len(zip_calls) == 0, \
            f"zip_cache_put must NOT be called on skip, calls={zip_calls}"
        assert len(mem_calls) == 0, \
            f"mem_cache_invalidate must NOT be called on skip, calls={mem_calls}"

        # OBS 不应有任何上传
        assert len(fake.objects) == 0, \
            f"OBS should have no objects on skip, got {len(fake.objects)}"

    # ------------------------------------------------------------------
    # 边界：skill-description.json 不存在 → 返回全 0
    # ------------------------------------------------------------------
    def test_json_not_found_returns_zeros(self, tmp_path, monkeypatch):
        """CONFIG_DIR 下无 skill-description.json → 返回全 0，不报错。"""
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        monkeypatch.setattr("src.storage.skill_sync.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("src.storage.skill_sync.SKILLS_DIR", str(skills_dir))

        result = asyncio.run(sync_official_skills_to_obs())
        assert result == {"added": 0, "updated": 0, "skipped": 0}, result

    # ------------------------------------------------------------------
    # 边界：skill 目录不存在 → 跳过该 skill
    # ------------------------------------------------------------------
    def test_skill_dir_not_found_skipped(self, tmp_path, monkeypatch):
        """skill-description.json 中有 skill 但本地目录不存在 → 跳过，返回全 0。"""
        config_dir = tmp_path / "configs"
        skills_dir = tmp_path / "skills"
        config_dir.mkdir()
        skills_dir.mkdir()

        (config_dir / "skill-description.json").write_text(
            json.dumps({"ghost-skill": {"path": "ghost-skill"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr("src.storage.skill_sync.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("src.storage.skill_sync.SKILLS_DIR", str(skills_dir))
        monkeypatch.setattr("src.storage.get_storage", lambda: FakeStorage())

        result = asyncio.run(sync_official_skills_to_obs())
        assert result == {"added": 0, "updated": 0, "skipped": 0}, result

    # ------------------------------------------------------------------
    # 边界：上传 OBS 抛异常 → catch 后 continue，不崩溃
    # ------------------------------------------------------------------
    def test_upload_exception_continues(self, skill_env, monkeypatch):
        """save_bytes 抛异常 → catch 后 continue，result 全 0，不调用缓存失效函数。"""
        class _BrokenStorage(FakeStorage):
            def save_bytes(self, key, data):
                raise RuntimeError("OBS simulated failure")

        monkeypatch.setattr("src.storage.get_storage", lambda: _BrokenStorage())

        zip_calls = []
        monkeypatch.setattr("src.storage.zip_cache_put",
                            lambda object_key, data, content_hash="": zip_calls.append((object_key, data, content_hash)))
        mem_calls = []
        monkeypatch.setattr("src.storage.mem_cache_invalidate",
                            lambda skill_name: mem_calls.append(skill_name))

        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (1, "old-hash", ""),
        )
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        assert result == {"added": 0, "updated": 0, "skipped": 0}, result
        assert len(zip_calls) == 0, f"zip_cache_put should not be called on upload failure: {zip_calls}"
        assert len(mem_calls) == 0, f"mem_cache_invalidate should not be called on upload failure: {mem_calls}"

    # ------------------------------------------------------------------
    # 边界：新 skill（DB 不存在，id=0）→ added +1
    # ------------------------------------------------------------------
    def test_new_skill_added_count(self, skill_env, monkeypatch):
        """DB 中不存在该 skill（id=0, db_hash=""）→ 上传后 added +1。"""
        fake = FakeStorage()
        monkeypatch.setattr("src.storage.get_storage", lambda: fake)
        monkeypatch.setattr("src.storage.zip_cache_put", lambda object_key, data, content_hash="": None)
        monkeypatch.setattr("src.storage.mem_cache_invalidate", lambda skill_name: None)

        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (0, "", ""),
        )
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        assert result["added"] == 1, f"expected added=1, got {result}"
        assert result["updated"] == 0, result
        assert result["skipped"] == 0, result
        assert len(fake.objects) == 1, "OBS should have 1 uploaded object"

    # ------------------------------------------------------------------
    # 边界：hash 相同但 file_path 为空 + storage 非 None → 上传补 file_path，updated +1
    # ------------------------------------------------------------------
    def test_file_path_synced_count(self, skill_env, monkeypatch):
        """hash 未变但 file_path 为空且 OBS 模式 → 不 skip，上传补 file_path，updated +1。"""
        fake = FakeStorage()
        monkeypatch.setattr("src.storage.get_storage", lambda: fake)
        monkeypatch.setattr("src.storage.zip_cache_put", lambda object_key, data, content_hash="": None)
        monkeypatch.setattr("src.storage.mem_cache_invalidate", lambda skill_name: None)

        real_hash = _compute_skill_hash(skill_env["skill_dir"])
        assert real_hash, "skill hash should not be empty"

        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (1, real_hash, ""),
        )
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        assert result["updated"] == 1, f"expected updated=1 (file_path synced), got {result}"
        assert result["added"] == 0, result
        assert result["skipped"] == 0, result
        assert len(fake.objects) == 1, "OBS should have 1 uploaded object"

    # ------------------------------------------------------------------
    # 边界：skill 目录不在 SKILLS_DIR 下，但 resolve_official_skill_path 能找到 → 正常处理
    # ------------------------------------------------------------------
    def test_skill_resolved_via_fallback_path(self, tmp_path, monkeypatch):
        """SKILLS_DIR 下无目录，但 resolve_official_skill_path 返回有效路径 → 正常上传。"""
        skills_dir = tmp_path / "skills"
        config_dir = tmp_path / "configs"
        fallback_dir = tmp_path / "fallback" / "regression-test-skill"
        skills_dir.mkdir()
        config_dir.mkdir()
        fallback_dir.mkdir(parents=True)
        (fallback_dir / "SKILL.md").write_text(
            "---\nname: regression-test-skill\n---\n## 场景\nfallback\n", encoding="utf-8")

        skill_name = "regression-test-skill"
        (config_dir / "skill-description.json").write_text(
            json.dumps({skill_name: {"path": skill_name, "agent_type": "chat"}}, ensure_ascii=False),
            encoding="utf-8")

        monkeypatch.setattr("src.storage.skill_sync.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("src.storage.skill_sync.SKILLS_DIR", str(skills_dir))
        # mock resolve 返回 fallback 目录下的 SKILL.md
        monkeypatch.setattr(
            "src.storage.skill_sync.resolve_official_skill_path",
            lambda name: str(fallback_dir / "SKILL.md") if name == skill_name else "",
        )

        fake = FakeStorage()
        monkeypatch.setattr("src.storage.get_storage", lambda: fake)
        monkeypatch.setattr("src.storage.zip_cache_put", lambda object_key, data, content_hash="": None)
        monkeypatch.setattr("src.storage.mem_cache_invalidate", lambda skill_name: None)
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda skill_name, scope="official": (0, "", ""),
        )
        monkeypatch.setattr(
            "src.user.db.update_skill_content_hash",
            lambda skill_id, content_hash, file_path="": True,
        )

        result = asyncio.run(sync_official_skills_to_obs())

        assert result["added"] == 1, f"expected added=1, got {result}"
        assert len(fake.objects) == 1, "OBS should have 1 uploaded object"



# ==================================================================
# Issue #254：skill_md_content 缓存命中校验 content_hash 回源
# ==================================================================

from src.storage import (
    skill_md_content, mem_cache_put, mem_cache_get, mem_cache_get_with_hash,
    mem_cache_invalidate, zip_cache_put, zip_cache_get, zip_cache_get_with_hash,
    zip_cache_invalidate, reset_storage, _mem_cache,
)


def _make_skill_zip_v1(skill_name: str) -> bytes:
    """打包 v1 版本的技能 zip。"""
    import io as _io, zipfile as _zipfile
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", "# v1 content\nold skill md")
    return buf.getvalue()


def _make_skill_zip_v2(skill_name: str) -> bytes:
    """打包 v2 版本的技能 zip（内容不同）。"""
    import io as _io, zipfile as _zipfile
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", "# v2 content\nnew skill md with updated content")
    return buf.getvalue()


class TestMemCacheHash:
    """Issue #254: mem_cache 存取 content_hash。"""

    def test_put_get_with_hash(self):
        """mem_cache_put 传入 content_hash → get_with_hash 返回 (content, hash)。"""
        mem_cache_invalidate("test-hash-skill")
        mem_cache_put("test-hash-skill", "content v1", "hash-v1")
        result = mem_cache_get_with_hash("test-hash-skill")
        assert result is not None
        content, cached_hash = result
        assert content == "content v1"
        assert cached_hash == "hash-v1"

    def test_put_without_hash_backward_compat(self):
        """mem_cache_put 不传 content_hash → hash 默认空串，get 仍返回 content（向后兼容）。"""
        mem_cache_invalidate("test-no-hash-skill")
        mem_cache_put("test-no-hash-skill", "content no hash")
        # 向后兼容：mem_cache_get 返回纯 content
        assert mem_cache_get("test-no-hash-skill") == "content no hash"
        # get_with_hash 返回 (content, "")
        result = mem_cache_get_with_hash("test-no-hash-skill")
        assert result is not None
        assert result[0] == "content no hash"
        assert result[1] == ""  # 无 hash

    def test_invalidate_clears_hash(self):
        """mem_cache_invalidate 清除后 get 返回 None。"""
        mem_cache_put("test-invalidate-skill", "content", "hash-abc")
        mem_cache_invalidate("test-invalidate-skill")
        assert mem_cache_get("test-invalidate-skill") is None
        assert mem_cache_get_with_hash("test-invalidate-skill") is None


class TestZipCacheHash:
    """Issue #254: zip_cache 存取 content_hash。"""

    def test_put_get_with_hash(self, tmp_path, monkeypatch):
        """zip_cache_put 传入 content_hash → get_with_hash 返回 (data, hash)。"""
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        from src.storage import skill_key
        k = skill_key("custom", "123", "hash-test-skill")
        zip_cache_put(k, b"zip-data-v1", "content-hash-v1")
        result = zip_cache_get_with_hash(k)
        assert result is not None
        data, cached_hash = result
        assert data == b"zip-data-v1"
        assert cached_hash == "content-hash-v1"

    def test_put_without_hash_backward_compat(self, tmp_path, monkeypatch):
        """zip_cache_put 不传 content_hash → hash 默认空串，get 仍返回纯 bytes（向后兼容）。"""
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        from src.storage import skill_key
        k = skill_key("custom", "123", "no-hash-skill")
        zip_cache_put(k, b"zip-data")
        # 向后兼容：zip_cache_get 返回纯 bytes
        assert zip_cache_get(k) == b"zip-data"
        # get_with_hash 返回 (data, "")
        result = zip_cache_get_with_hash(k)
        assert result is not None
        assert result[0] == b"zip-data"
        assert result[1] == ""

    def test_overwrite_updates_hash(self, tmp_path, monkeypatch):
        """同 key 覆盖写入 → hash 更新为新值。"""
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        from src.storage import skill_key
        k = skill_key("custom", "123", "overwrite-skill")
        zip_cache_put(k, b"v1-data", "hash-v1")
        zip_cache_put(k, b"v2-data", "hash-v2")
        result = zip_cache_get_with_hash(k)
        assert result is not None
        assert result[0] == b"v2-data"
        assert result[1] == "hash-v2"


class TestSkillMdContentHashVerification:
    """Issue #254: skill_md_content 缓存命中时校验 content_hash，不一致回源。"""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        """隔离缓存环境。"""
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        # 清理内存缓存
        mem_cache_invalidate("hash-verify-skill")
        # 重置 storage 单例
        reset_storage()
        yield
        mem_cache_invalidate("hash-verify-skill")

    def test_hash_match_returns_cached_fast_path(self, monkeypatch):
        """缓存命中 + DB hash 一致 → 走快路径，不回源 OBS。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 预填内存缓存（带 hash）
        mem_cache_put(skill_name, "cached content", "hash-v1")
        # DB hash 一致
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (1, "hash-v1", "file_path"),
        )
        # OBS 不应被调用
        class _NoCallStorage:
            def read_bytes(self, key):
                raise AssertionError("OBS read_bytes should NOT be called on hash match (fast path)")
        monkeypatch.setattr("src.storage.get_storage", lambda: _NoCallStorage())

        result = skill_md_content(object_key, skill_name)
        assert result == "cached content"

    def test_hash_mismatch_invalidates_and_refetches(self, monkeypatch):
        """缓存命中 + DB hash 不一致 → 失效缓存回源 OBS 获取最新内容。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 预填内存缓存（旧 hash）
        mem_cache_put(skill_name, "old content", "hash-old")
        # DB hash 不同（内容已更新）
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (1, "hash-new", "file_path"),
        )

        # OBS 回源返回新 zip
        new_zip = _make_skill_zip_v2(skill_name)
        class _RefetchStorage:
            def read_bytes(self, key):
                return new_zip
        monkeypatch.setattr("src.storage.get_storage", lambda: _RefetchStorage())

        result = skill_md_content(object_key, skill_name)
        assert "new skill md with updated content" in result, f"expected new content, got: {result}"
        # 旧缓存已失效，新内容已回填
        mem_result = mem_cache_get_with_hash(skill_name)
        assert mem_result is not None
        assert mem_result[1] == "hash-new"  # 回填时带最新 DB hash

    def test_zip_cache_hash_mismatch_refetches(self, monkeypatch):
        """磁盘缓存命中 + hash 不一致 → 失效磁盘缓存回源 OBS。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 清内存缓存（强制走磁盘路径）
        mem_cache_invalidate(skill_name)
        # 预填磁盘缓存（旧 zip + 旧 hash）
        old_zip = _make_skill_zip_v1(skill_name)
        zip_cache_put(object_key, old_zip, "hash-old")
        # DB hash 不同
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (1, "hash-new", "file_path"),
        )

        # OBS 回源返回新 zip
        new_zip = _make_skill_zip_v2(skill_name)
        class _RefetchStorage:
            def read_bytes(self, key):
                return new_zip
        monkeypatch.setattr("src.storage.get_storage", lambda: _RefetchStorage())

        result = skill_md_content(object_key, skill_name)
        assert "new skill md with updated content" in result, f"expected new content, got: {result}"

    def test_zip_cache_hash_match_uses_cache(self, monkeypatch):
        """磁盘缓存命中 + hash 一致 → 不回源 OBS。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 清内存缓存（强制走磁盘路径）
        mem_cache_invalidate(skill_name)
        # 预填磁盘缓存（zip + 匹配的 hash）
        zip_data = _make_skill_zip_v1(skill_name)
        zip_cache_put(object_key, zip_data, "hash-v1")
        # DB hash 一致
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (1, "hash-v1", "file_path"),
        )
        # OBS 不应被调用
        class _NoCallStorage:
            def read_bytes(self, key):
                raise AssertionError("OBS should NOT be called on zip cache hash match")
        monkeypatch.setattr("src.storage.get_storage", lambda: _NoCallStorage())

        result = skill_md_content(object_key, skill_name)
        assert "old skill md" in result, f"expected cached content, got: {result}"

    def test_no_hash_in_cache_skip_verification(self, monkeypatch):
        """缓存无 hash（旧数据）→ 不校验，直接返回缓存（向后兼容）。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 预填内存缓存（无 hash）
        mem_cache_put(skill_name, "legacy cached content")
        # OBS 不应被调用
        class _NoCallStorage:
            def read_bytes(self, key):
                raise AssertionError("OBS should NOT be called when cache has no hash (backward compat)")
        monkeypatch.setattr("src.storage.get_storage", lambda: _NoCallStorage())

        result = skill_md_content(object_key, skill_name)
        assert result == "legacy cached content"

    def test_db_hash_empty_skip_verification(self, monkeypatch):
        """DB hash 为空（技能未同步过）→ 不校验，走缓存快路径。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 预填内存缓存（带 hash）
        mem_cache_put(skill_name, "cached content", "some-hash")
        # DB hash 为空（技能不存在或未同步）
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (0, "", ""),
        )
        # OBS 不应被调用
        class _NoCallStorage:
            def read_bytes(self, key):
                raise AssertionError("OBS should NOT be called when DB hash is empty")
        monkeypatch.setattr("src.storage.get_storage", lambda: _NoCallStorage())

        result = skill_md_content(object_key, skill_name)
        assert result == "cached content"

    def test_refetch_updates_cache_with_db_hash(self, monkeypatch):
        """回源后缓存写入时带最新 DB hash（供后续命中校验）。"""
        skill_name = "hash-verify-skill"
        from src.storage import skill_key
        object_key = skill_key("official", "", skill_name)

        # 清所有缓存
        mem_cache_invalidate(skill_name)
        zip_cache_invalidate(object_key)
        # DB hash
        monkeypatch.setattr(
            "src.user.db.get_skill_content_hash",
            lambda sn, scope="official": (1, "db-hash-abc", "file_path"),
        )

        new_zip = _make_skill_zip_v2(skill_name)
        class _RefetchStorage:
            def read_bytes(self, key):
                return new_zip
        monkeypatch.setattr("src.storage.get_storage", lambda: _RefetchStorage())

        result = skill_md_content(object_key, skill_name)
        assert "new skill md" in result

        # 验证磁盘缓存带 hash
        zip_result = zip_cache_get_with_hash(object_key)
        assert zip_result is not None
        assert zip_result[1] == "db-hash-abc", f"zip cache should have DB hash, got: {zip_result[1]}"

        # 验证内存缓存带 hash
        mem_result = mem_cache_get_with_hash(skill_name)
        assert mem_result is not None
        assert mem_result[1] == "db-hash-abc", f"mem cache should have DB hash, got: {mem_result[1]}"
