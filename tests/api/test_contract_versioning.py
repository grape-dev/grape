"""契约版本号策略（Issue #2784）——纯函数 seam。

公开契约：`src/api/contract_versioning.py` 暴露的解析 / 排序 / 递进校验，
是「从现网递进（禁跳版）+ a/b/rc 预发布双轨」的唯一判定实现。
本文件只测这些函数的对外行为（输入 -> 输出），不测内部实现细节。
"""

import pytest

from src.api import contract_versioning as cv


# --------------------------------------------------------------------------
# S1-a 解析：只接受 X.Y.Z 与 X.Y.Z{a|b|rc}N
# --------------------------------------------------------------------------

def test_parse_version_accepts_release_and_prerelease():
    assert cv.parse_version("2.2.0") == (2, 2, 0, None, None)
    assert cv.parse_version("2.3.0a1") == (2, 3, 0, "a", 1)
    assert cv.parse_version("2.2.1b2") == (2, 2, 1, "b", 2)
    assert cv.parse_version("2.2.1rc3") == (2, 2, 1, "rc", 3)


@pytest.mark.parametrize("bad", ["2.3", "v2.3.0", "2.3.0.dev1", "2.3.0-alpha1",
                                 "2.3.0a", "2.3.0a0x", "", None])
def test_parse_version_rejects_out_of_policy_shapes(bad):
    assert cv.parse_version(bad) is None


def test_is_prerelease_and_base_version():
    assert cv.is_prerelease("2.3.0a1") is True
    assert cv.is_prerelease("2.3.0") is False
    assert cv.base_version("2.3.0a1") == "2.3.0"
    assert cv.base_version("2.3.0") == "2.3.0"
    assert cv.base_version("nope") is None


# --------------------------------------------------------------------------
# S1-b PEP 440 排序（预发布必须低于同号正式版）
# --------------------------------------------------------------------------

def test_compare_versions_pep440_chain():
    chain = ["2.2.0", "2.2.1a1", "2.2.1b1", "2.2.1rc1", "2.2.1", "2.3.0a1", "2.3.0"]
    for left, right in zip(chain, chain[1:]):
        assert cv.compare_versions(left, right) == -1, f"{left} 应低于 {right}"
        assert cv.compare_versions(right, left) == 1
    assert cv.compare_versions("2.3.0a1", "2.3.0a1") == 0


def test_compare_versions_is_total_for_prerelease_numbers():
    assert cv.compare_versions("2.3.0a1", "2.3.0a2") == -1
    assert cv.compare_versions("2.3.0rc1", "2.3.0rc1") == 0


# --------------------------------------------------------------------------
# S1-c 合法递进步：只允许 patch+1 / minor+1（可带 a/b/rc 后缀），禁跳版
# --------------------------------------------------------------------------

def test_legal_successors_from_production():
    assert cv.legal_successors("2.2.0") == [
        "2.2.1a1", "2.2.1b1", "2.2.1rc1", "2.2.1",
        "2.3.0a1", "2.3.0b1", "2.3.0rc1", "2.3.0",
    ]


def test_legal_successors_never_contains_skipped_versions():
    successors = cv.legal_successors("2.2.0")
    for skipped in ("2.4.0", "2.5.0", "3.0.0", "2.2.2"):
        assert skipped not in successors


def test_legal_successors_from_prerelease_points_at_its_base():
    # 预发布仍在自己的目标号上流转：2.3.0a1 -> 2.3.0b1 / 2.3.0rc1 / 2.3.0
    assert cv.legal_successors("2.3.0a1") == ["2.3.0b1", "2.3.0rc1", "2.3.0"]


# --------------------------------------------------------------------------
# S1-d 递进校验：返回可执行的中文错误（空列表 = 合法）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("candidate", ["2.2.1a1", "2.2.1", "2.3.0a1", "2.3.0"])
def test_check_successor_accepts_legal_increment(candidate):
    assert cv.check_successor("2.2.0", candidate) == []


def test_check_successor_blocks_skipped_version():
    errors = cv.check_successor("2.2.0", "2.5.0")
    assert errors and "跳版" in errors[0]
    assert "2.2.1" in errors[0] and "2.3.0" in errors[0]


def test_check_successor_blocks_same_version_and_downgrade():
    assert any("未递进" in e for e in cv.check_successor("2.2.0", "2.2.0"))
    assert any("回退" in e for e in cv.check_successor("2.2.0", "2.1.9"))


def test_check_successor_reports_unparseable_input():
    assert any("无法解析" in e for e in cv.check_successor("2.2.0", "2.5"))
    assert any("无法解析" in e for e in cv.check_successor("2.5", "2.5.0"))


# --------------------------------------------------------------------------
# S1-e 仓库当前契约版本必须自证合规：预发布 + 对现网合法递进
# --------------------------------------------------------------------------

def test_module_contract_version_is_legal_prerelease_of_production():
    assert cv.CONTRACT_VERSION == "2.3.0a1"
    assert cv.PRODUCTION_VERSION == "2.2.0"
    assert cv.is_prerelease(cv.CONTRACT_VERSION) is True
    assert cv.check_successor(cv.PRODUCTION_VERSION, cv.CONTRACT_VERSION) == []
