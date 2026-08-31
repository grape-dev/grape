"""Tests for the map file."""
from gitsync.mapfile import MapFile


def test_map_append_and_query(tmp_path):
    path = str(tmp_path / "map.txt")
    m = MapFile(path)
    m.append([("sha-branch-1", "sha-remote-1", "sha-master-1")])
    assert m.branch_to_remote("sha-branch-1") == "sha-remote-1"
    assert m.remote_to_branch("sha-remote-1") == "sha-branch-1"
    assert m.branch_to_master("sha-branch-1") == "sha-master-1"
    assert m.master_to_branch("sha-master-1") == "sha-branch-1"


def test_map_append_incremental(tmp_path):
    path = str(tmp_path / "map.txt")
    m = MapFile(path)
    m.append([("b1", "r1", "m1")])
    m.append([("b2", "r2", "m2")])
    m2 = MapFile(path)  # reload from disk
    assert m2.branch_to_remote("b1") == "r1"
    assert m2.branch_to_remote("b2") == "r2"
    assert m2.has_branch("b1")
    assert not m2.has_branch("b3")


def test_map_empty(tmp_path):
    m = MapFile(str(tmp_path / "nope.txt"))
    assert m.branch_to_remote("x") is None
    assert not m.has_branch("x")
    assert not m.has_remote("x")
