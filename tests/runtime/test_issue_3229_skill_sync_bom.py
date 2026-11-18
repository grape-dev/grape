"""Issue #3229：skill_sync BOM 归一化与 hash 幂等回归测试。

覆盖：
1. `_normalize_skill_dir_bom` 剥离文本文件（至少 SKILL.md）首部 BOM，非文本文件不动，幂等。
2. `_compute_skill_hash` 对 BOM 不敏感：带 BOM 与不带 BOM 的同一技能目录 hash 相同。
3. `_pack_skill_zip` 打包产物不含 BOM 字节。
4. `sync_official_skills_to_obs` 归一化后第二轮命中 skip，不重复上传（hash 幂等）。
"""
import asyncio
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.storage.skill_sync import (
    _compute_skill_hash,
    _normalize_skill_dir_bom,
    _pack_skill_zip,
    sync_official_skills_to_obs,
)

BOM = b"\xef\xbb\xbf"
SKILL_MD = b"---\nname: demo-skill\nversion: 1.0.0\n---\n\n## Body\n"


def _make_skill_dir(base, with_bom):
    skill_dir = base / "demo-skill"
    skill_dir.mkdir(parents=True)
    prefix = BOM if with_bom else b""
    (skill_dir / "SKILL.md").write_bytes(prefix + SKILL_MD)
    (skill_dir / "notes.txt").write_bytes(prefix + b"notes\n")
    (skill_dir / "logo.png").write_bytes(b"\x89PNG\r\n")
    return skill_dir


def test_normalize_strips_bom_from_text_files_only(tmp_path):
    skill_dir = _make_skill_dir(tmp_path, with_bom=True)
    (skill_dir / "asset.bin").write_bytes(BOM + b"\x00\x01")

    changed = _normalize_skill_dir_bom(str(skill_dir))
    assert changed == 2, f"expected SKILL.md + notes.txt rewritten, got {changed}"

    assert (skill_dir / "SKILL.md").read_bytes().startswith(b"---\n")
    assert (skill_dir / "notes.txt").read_bytes() == b"notes\n"
    assert (skill_dir / "logo.png").read_bytes() == b"\x89PNG\r\n"
    assert (skill_dir / "asset.bin").read_bytes() == BOM + b"\x00\x01"

    # 幂等：第二轮无改写
    assert _normalize_skill_dir_bom(str(skill_dir)) == 0


def test_compute_skill_hash_is_bom_insensitive(tmp_path):
    with_bom = _make_skill_dir(tmp_path / "a", with_bom=True)
    without_bom = _make_skill_dir(tmp_path / "b", with_bom=False)

    assert _compute_skill_hash(str(with_bom)) == _compute_skill_hash(str(without_bom))

    _normalize_skill_dir_bom(str(with_bom))
    assert _compute_skill_hash(str(with_bom)) == _compute_skill_hash(str(without_bom))


def test_pack_skill_zip_has_no_bom_after_normalize(tmp_path):
    skill_dir = _make_skill_dir(tmp_path, with_bom=True)
    _normalize_skill_dir_bom(str(skill_dir))

    data = _pack_skill_zip(str(skill_dir))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}

    assert contents["demo-skill/SKILL.md"].startswith(b"---\n")
    assert not contents["demo-skill/SKILL.md"].startswith(BOM)
    assert contents["demo-skill/notes.txt"] == b"notes\n"
    assert contents["demo-skill/logo.png"].startswith(b"\x89PNG")


class _FakeStorage:
    def __init__(self):
        self.objects = {}

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key


def test_obs_sync_bom_idempotent(tmp_path, monkeypatch):
    """首轮归一化并上传无 BOM zip；第二轮 hash 相同命中 skip，不再上传。"""
    skills_dir = tmp_path / "skills"
    config_dir = tmp_path / "configs"
    skills_dir.mkdir()
    config_dir.mkdir()
    _make_skill_dir(skills_dir, with_bom=True)

    (config_dir / "skill-description.json").write_text(
        json.dumps({"demo-skill": {"path": "demo-skill", "name": "demo-skill"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.domains.runtime.storage.skill_sync.SKILLS_DIR", str(skills_dir))
    monkeypatch.setattr("src.domains.runtime.storage.skill_sync.CONFIG_DIR", str(config_dir))

    fake = _FakeStorage()
    monkeypatch.setattr("src.domains.runtime.storage.get_storage", lambda: fake)
    monkeypatch.setattr("src.domains.runtime.storage.zip_cache_put", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.storage.mem_cache_invalidate", lambda *a, **k: None)

    state = {"hash": "", "path": ""}
    monkeypatch.setattr(
        "src.domains.mgmt.user.db.get_skill_content_hash",
        lambda skill_name, scope="official": (1, state["hash"], state["path"]),
    )

    def _update_skill_content_hash(skill_id, content_hash, file_path=""):
        state["hash"] = content_hash
        state["path"] = file_path or state["path"]
        return True

    monkeypatch.setattr(
        "src.domains.mgmt.user.db.update_skill_content_hash",
        _update_skill_content_hash,
    )

    first = asyncio.run(sync_official_skills_to_obs())
    assert first["added"] == 1, first
    assert len(fake.objects) == 1, fake.objects

    object_key = next(iter(fake.objects))
    with zipfile.ZipFile(io.BytesIO(fake.objects[object_key])) as zf:
        skill_md = zf.read("demo-skill/SKILL.md")
    assert skill_md.startswith(b"---\n"), "OBS zip SKILL.md must not carry BOM"

    second = asyncio.run(sync_official_skills_to_obs())
    assert second["skipped"] == 1, second
    assert second["added"] == 0 and second["updated"] == 0, second
    assert len(fake.objects) == 1, "second sync must not re-upload"
