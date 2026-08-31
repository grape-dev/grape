"""Tests for the ClawHub publish manifest."""
from gitsync.manifest import Manifest


def test_manifest_add_and_save_load(tmp_path):
    path = str(tmp_path / "manifest.json")
    backup = str(tmp_path / "manifest.backup.json")
    m = Manifest(path, backup)
    m.add_version("skill-a", "1.0.0", "in_review")
    m.add_version("skill-a", "1.0.1", "published")
    m.save()

    m2 = Manifest(path, backup)
    m2.load()
    assert m2.highest_version("skill-a") == "1.0.1"
    assert len(m2.versions("skill-a")) == 2
    assert m2.versions("skill-a")[0]["status"] == "in_review"


def test_manifest_recovers_from_backup(tmp_path):
    path = str(tmp_path / "manifest.json")
    backup = str(tmp_path / "manifest.backup.json")
    m = Manifest(path, backup)
    m.add_version("skill-b", "2.0.0", "published")
    m.save()

    # primary lost, backup remains
    import os
    os.unlink(path)
    m2 = Manifest(path, backup)
    m2.load()
    assert m2.highest_version("skill-b") == "2.0.0"


def test_manifest_empty():
    m = Manifest("/nonexistent/manifest.json", "")
    m.load()
    assert m.highest_version("whatever") is None
