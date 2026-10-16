"""存储抽象层单测：skill_key/parse、zip 提取、磁盘/内存缓存、ObsStorage SDK 调用。

见 obs-skill-storage-design.md §9。
"""
import io
import os
import stat
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
os.environ["OBS_PREFIX"] = "grape/test"

import pytest  # noqa: E402

from src.storage import (  # noqa: E402
    skill_key, parse_skill_key, skill_tag_from_key, extract_skill_md, skill_md_content,
    zip_cache_get, zip_cache_put, zip_cache_invalidate, invalidate_skill_caches,
    mem_cache_invalidate, get_skill_zip_bytes, reset_storage,
)


class TestSkillKey:
    @pytest.fixture(autouse=True)
    def _obs_prefix(self, monkeypatch):
        # 稳定断言：显式固定 OBS_PREFIX（不依赖模块加载时序）
        import src.config as _cfg
        monkeypatch.setattr(_cfg, "OBS_PREFIX", "grape/test")

    def test_custom_key(self):
        k = skill_key("custom", "123", "hello", 2, rand8="a1b2c3d4")
        assert k == "grape/test/skills/custom/123/hello/hello.zip"

    def test_repo_key_two_level(self):
        k = skill_key("repo", "Jason7602/grape", "my-skill", 1, rand8="00001111")
        assert k == "grape/test/skills/repo/Jason7602/grape/my-skill/my-skill.zip"

    def test_official_key(self):
        k = skill_key("official", "", "ops", 1, rand8="ffffffff")
        assert k == "grape/test/skills/official/ops/ops.zip"

    def test_invalid_segments_rejected(self):
        with pytest.raises(ValueError):
            skill_key("custom", "123", "bad/name", 1)
        with pytest.raises(ValueError):
            skill_key("custom", "abc", "hello", 1)  # user_id 必须数字
        with pytest.raises(ValueError):
            skill_key("repo", "owner", "hello", 1)  # repo_key 必须 owner/repo
        with pytest.raises(ValueError):
            skill_key("badscope", "123", "hello", 1)

    def test_parse_roundtrip(self):
        for scope, owner, name in [
            ("custom", "123", "hello"),
            ("repo", "Jason7602/grape", "my-skill"),
            ("official", "", "ops"),
        ]:
            k = skill_key(scope, owner, name)
            info = parse_skill_key(k)
            assert info["scope"] == scope
            assert info["owner"] == owner
            assert info["skill_name"] == name
            assert info["version"] == 0  # v2.4 固定 key 无版本段


def _make_zip(skill_name: str, with_exec: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", "---\nname: x\n---\n\n# Hello")
        zf.writestr(f"{skill_name}/references/design.md", "# Design")
        zf.writestr(f"{skill_name}/assets/icon.bin", b"\x00\x01\x02")
        if with_exec:
            info = zipfile.ZipInfo(f"{skill_name}/scripts/run.sh")
            info.external_attr = (0o755) << 16
            zf.writestr(info, "#!/bin/sh\necho ok\n")
    return buf.getvalue()


class TestExtract:
    def test_extract_skill_md(self):
        md = extract_skill_md(_make_zip("hello"), "hello")
        assert md and "# Hello" in md

    def test_extract_case_insensitive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello/skill.md", "# lower case")
        assert "# lower case" in extract_skill_md(buf.getvalue(), "hello")

    def test_extract_missing_returns_none(self):
        assert extract_skill_md(_make_zip("hello"), "other") is None
        assert extract_skill_md(b"", "hello") is None

    def test_extract_fallback_single_mismatched_dir(self):
        """兜底 1：_template 等内置特殊技能，zip 内仅一个 */SKILL.md 且目录名 != skill_name → 采用该 entry。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("my-skill/SKILL.md", "# Template Skill")
        md = extract_skill_md(buf.getvalue(), "_template")
        assert md and "# Template Skill" in md

    def test_extract_no_fallback_for_regular_skill_mismatch(self):
        """兜底 1 不触发：常规技能目录名 != skill_name → 返回 None（保持精确匹配语义，Issue #355 QA）。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello/SKILL.md", "# Hello")
        assert extract_skill_md(buf.getvalue(), "other") is None

    def test_extract_fallback_multiple_skill_md_no_mismatch(self):
        """兜底 1 不触发：zip 内有多个 */SKILL.md → 不误匹配，返回 None。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("skill-a/SKILL.md", "# A")
            zf.writestr("skill-b/SKILL.md", "# B")
        assert extract_skill_md(buf.getvalue(), "_template") is None

    def test_extract_keeps_exec_bit_metadata(self):
        """zip 内脚本 external_attr 权限位可读（worker 物化时按此恢复）。"""
        zip_bytes = _make_zip("hello", with_exec=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            info = zf.getinfo("hello/scripts/run.sh")
            mode = info.external_attr >> 16
            assert mode & 0o111


class TestZipCache:
    def test_put_get(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBS_LOCAL_CACHE", str(tmp_path))
        from src.storage import ZIP_CACHE_DIR, ZIP_CACHE_INDEX
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        k = skill_key("custom", "123", "hello", 1)
        zip_cache_put(k, b"data-v1")
        assert zip_cache_get(k) == b"data-v1"
        # 覆盖写
        zip_cache_put(k, b"data-v1b")
        assert zip_cache_get(k) == b"data-v1b"

    def test_invalidate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBS_LOCAL_CACHE", str(tmp_path))
        from src.storage import ZIP_CACHE_DIR, ZIP_CACHE_INDEX
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        k = skill_key("custom", "123", "hello", 1)
        zip_cache_put(k, b"data")
        zip_cache_invalidate(k)
        assert zip_cache_get(k) is None

    def test_versions_capped(self, tmp_path, monkeypatch):
        # v2.4 固定 key：同技能同一 key，重复 put 覆盖，缓存仅 1 条（无版本累积）
        monkeypatch.setenv("OBS_LOCAL_CACHE", str(tmp_path))
        from src.storage import ZIP_CACHE_DIR, ZIP_CACHE_INDEX
        monkeypatch.setattr("src.storage.ZIP_CACHE_DIR", str(tmp_path / "zips"))
        monkeypatch.setattr("src.storage.ZIP_CACHE_INDEX", str(tmp_path / "zips" / "index.json"))
        key = skill_key("custom", "123", "hello")
        zip_cache_put(key, b"v1")
        zip_cache_put(key, b"v2")  # 同 key 覆盖
        assert zip_cache_get(key) == b"v2"
        # 同技能仅 1 条缓存条目（固定 key 覆盖语义）
        from src.storage import _zip_index
        idx = _zip_index()
        same_tag = [k for k in idx if idx[k].get("tag") == skill_tag_from_key(key)]
        assert len(same_tag) == 1, f"固定 key 覆盖后同技能缓存应仅 1 条: {idx}"


class TestObsStorageMock:
    @pytest.fixture(autouse=True)
    def _obs_prefix(self, monkeypatch):
        # 与实例 prefix 保持一致（不依赖模块加载时序）
        import src.config as _cfg
        monkeypatch.setattr(_cfg, "OBS_PREFIX", "grape/test")

    def test_sdk_call_contract(self, monkeypatch):
        """ObsStorage 调用 esdk-obs-python 的参数契约（bucket/对象键前缀）。"""
        import src.storage.obs as obs_mod

        calls = {}

        class FakeResp:
            status = 200

        class FakeClient:
            def __init__(self, **kwargs):
                calls["client_kwargs"] = kwargs

            def putObject(self, bucketName, objectKey, content, **kw):
                calls["put"] = (bucketName, objectKey, content)
                return FakeResp()

            def getObject(self, bucketName, objectKey, **kw):
                calls["get"] = (bucketName, objectKey, kw)
                return FakeResp()

            def createSignedUrl(self, method, bucketName=None, objectKey=None, **kw):
                calls["sign"] = (method, bucketName, objectKey, kw)

                class R(dict):
                    signedUrl = f"https://x/{objectKey}?sig=1"
                return R()

        monkeypatch.setattr(obs_mod, "ObsClient", FakeClient)
        s = obs_mod.ObsStorage(endpoint="https://obs.cn-north-4.myhuaweicloud.com",
                               bucket="obs-grape-dev-file", access_key="AK",
                               secret_key="SK", prefix="grape/test")
        k = skill_key("custom", "123", "hello", 1)
        s.save_bytes(k, b"data")
        # skill_key 产物已含前缀（与实例 prefix 一致），_full 幂等不重复拼接
        assert calls["put"] == ("obs-grape-dev-file", k, b"data")
        s.read_bytes(k)
        assert calls["get"][0] == "obs-grape-dev-file"
        assert calls["get"][1] == k
        assert calls["get"][2].get("loadStreamInMemory") is True
        url = s.temporary_url(k, expires=300)
        assert url.startswith("https://x/")
        assert calls["sign"][0] == "GET"
        assert calls["sign"][1] == "obs-grape-dev-file"
        assert calls["sign"][2] == k
        # 无前缀业务 key（如模板）仍拼一次前缀
        s.save_bytes("skills/template/skill-template.zip", b"t")
        assert calls["put"] == ("obs-grape-dev-file", "grape/test/skills/template/skill-template.zip", b"t")
