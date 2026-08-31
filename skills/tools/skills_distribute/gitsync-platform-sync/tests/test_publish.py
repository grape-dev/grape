"""Tests for the publish command's self-healing version logic."""
from unittest import mock

from gitsync.commands.publish import _publish_with_self_heal
from gitsync.manifest import Manifest
from gitsync.semver import bump


def _make_skill(tmp_path, name="my-skill"):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")
    return type("Skill", (), {"folder": d, "name": name, "relative_path": str(d)})()


def test_new_skill_publishes_100(tmp_path):
    skill = _make_skill(tmp_path)
    m = Manifest(str(tmp_path / "m.json"), "")
    with mock.patch("gitsync.commands.publish.skill_publish",
                    return_value={"ok": True}) as pub:
        with mock.patch("gitsync.clawhub.skill_probe",
                        return_value={"status": "new"}):
            v = _publish_with_self_heal(skill, m, "")
    assert v == "1.0.0"
    assert m.highest_version("my-skill") == "1.0.0"
    assert m.versions("my-skill")[0]["status"] == "in_review"
    pub.assert_called_once_with(str(skill.folder), "1.0.0", "")


def test_manifest_loss_self_heals_via_probe(tmp_path):
    """When the manifest is lost, probing ClawHub detects the content is already
    published and rebuilds the entry without emitting a new version."""
    skill = _make_skill(tmp_path)
    m = Manifest(str(tmp_path / "m.json"), "")  # empty manifest (lost)
    probe_result = {
        "status": "unchanged",
        "version": "3.2.1",
        "fingerprint": "deadbeef",
    }
    with mock.patch("gitsync.clawhub.skill_probe",
                    return_value=probe_result) as probe:
        with mock.patch("gitsync.commands.publish.skill_publish") as pub:
            v = _publish_with_self_heal(skill, m, "")
    assert v is None  # skipped, no new version
    pub.assert_not_called()  # no publish happened
    probe.assert_called_once_with(str(skill.folder), "")
    # manifest rebuilt from ClawHub's reported version
    assert m.highest_version("my-skill") == "3.2.1"
    assert m.content_hash("my-skill")  # fingerprint recorded


def test_self_heal_on_conflict(tmp_path):
    skill = _make_skill(tmp_path)
    m = Manifest(str(tmp_path / "m.json"), "")
    m.add_version("my-skill", "1.0.0", "published")
    calls = []
    def fake_publish(folder, version, config_path):
        calls.append(version)
        if version == "1.0.1":
            return {"ok": True}
        return {"error": f"Version {version} already exists. Increment the version number and try again.",
                "returncode": 1}
    with mock.patch("gitsync.commands.publish.skill_publish",
                    side_effect=fake_publish) as pub:
        with mock.patch("gitsync.clawhub.skill_probe",
                        return_value={"status": "update"}):
            v = _publish_with_self_heal(skill, m, "")
    assert v == "1.0.1"
    assert calls == ["1.0.1"]
    assert m.highest_version("my-skill") == "1.0.1"


def test_self_heal_bumps_multiple(tmp_path):
    skill = _make_skill(tmp_path)
    m = Manifest(str(tmp_path / "m.json"), "")
    m.add_version("my-skill", "1.0.0", "published")
    m.add_version("my-skill", "1.0.1", "in_review")
    def fake_publish(folder, version, config_path):
        if version == "1.0.2":
            return {"ok": True}
        return {"error": f"Version {version} already exists.", "returncode": 1}
    with mock.patch("gitsync.commands.publish.skill_publish", side_effect=fake_publish):
        with mock.patch("gitsync.clawhub.skill_probe",
                        return_value={"status": "update"}):
            v = _publish_with_self_heal(skill, m, "")
    assert v == "1.0.2"
