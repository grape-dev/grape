"""本地仓库镜像层测试(2026-08-18 用户要求)。

镜像层: 审查/判重/分流读仓库内容改用本地 bare 镜像(替代 contents/commits
API), 消除 429 限流误报。并发 fetch 用 flock 排队等锁; 任何失败 fail-open
返回 None/空, 调用方回退原 API 逻辑。
"""
from unittest import mock

import gitcode_event_handler as h


def test_mirror_path_sanitizes():
    assert h._mirror_path("hd-skills-pre/skills-pre").endswith(
        "hd-skills-pre_skills-pre.git")
    # 仓库名里的 "/" 被替换, basename 无斜杠(避免路径穿越)
    import os
    assert os.path.basename(h._mirror_path("a/b")) == "a_b.git"


def test_ensure_mirror_no_repo():
    assert h._ensure_mirror("") is False
    assert h._ensure_mirror(None) is False


def test_ensure_mirror_ttl_throttle():
    # 进程内节流: TTL 内二次调用不重复 fetch
    with mock.patch.object(h, "_mirror_git", return_value=(0, "", "")) as mg:
        with mock.patch.object(h, "fcntl", mock.MagicMock()):
            with mock.patch.object(h, "MIRROR_FETCH_TTL", 60):
                h._MIRROR_ENSURE_CACHE.clear()
                assert h._ensure_mirror("r/x") is True
                assert h._ensure_mirror("r/x") is True
                # 首次 clone 1 次 git 调用; 第二次命中 TTL 不再调用
                assert mg.call_count == 1


def test_mirror_read_returns_content():
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git",
                               return_value=(0, "---\nname: x\n---", "")):
            assert h._mirror_read("r", "skills/x/SKILL.md").startswith("---")


def test_mirror_read_failure_returns_none():
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git", return_value=(128, "", "fatal")):
            assert h._mirror_read("r", "nope.md") is None


def test_mirror_dir_exists_semantics():
    # tree 类型 -> True; 路径不存在(空输出) -> False; 命令失败 -> None
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git",
                               return_value=(0, "040000 tree abc\tx", "")):
            assert h._mirror_dir_exists("r", "skills/x") is True
        with mock.patch.object(h, "_mirror_git", return_value=(0, "", "")):
            assert h._mirror_dir_exists("r", "skills/nope") is False
        with mock.patch.object(h, "_mirror_git", return_value=(128, "", "err")):
            assert h._mirror_dir_exists("r", "skills/x") is None


def test_mirror_dir_files():
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git",
                               return_value=(0, "a.md\0b/c.py\0", "")):
            assert h._mirror_dir_files("r", "skills/x") == {"a.md", "b/c.py"}
        with mock.patch.object(h, "_mirror_git", return_value=(0, "", "")):
            assert h._mirror_dir_files("r", "skills/nope") == set()
        with mock.patch.object(h, "_mirror_git", return_value=(128, "", "err")):
            assert h._mirror_dir_files("r", "skills/x") is None


def test_mirror_products():
    # skills/<cat>/<name> 与 skills/<cat>/<subcat>/<name> 都识别 skill 根
    out = "skills/ai/maas\0skills/ai/maas/huawei-cloud-maas-tokens-usage\0" \
          "skills/network/ops/huawei-cloud-vpc-list\0skills/network\0"
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git", return_value=(0, out, "")):
            p = h._mirror_products("r")
    assert p.get("huawei-cloud-maas-tokens-usage") == \
        "skills/ai/maas/huawei-cloud-maas-tokens-usage"
    assert p.get("huawei-cloud-vpc-list") == "skills/network/ops/huawei-cloud-vpc-list"
    assert "maas" not in p and "network" not in p  # 非 skill 根不收录


def test_mirror_commit_authors_and_bot():
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git",
                               return_value=(0, "skills_bot|skills_bot@gitcode.com\n"
                                                 "lccode|a@noreply.gitcode.com\n", "")):
            auth = h._mirror_commit_authors("r", "skills/x")
    assert auth == [("skills_bot", "skills_bot@gitcode.com"),
                    ("lccode", "a@noreply.gitcode.com")]
    assert h._mirror_author_is_bot("skills_bot", "skills_bot@gitcode.com") is True
    assert h._mirror_author_is_bot("lccode", "a@noreply.gitcode.com") is False
    assert h._mirror_author_is_bot("check_bot", "x@y") is True
    assert h._mirror_author_is_bot("dev2bot", "x@y") is True  # 含 bot 字样


def test_mirror_commit_authors_failure_returns_none():
    with mock.patch.object(h, "_ensure_mirror", return_value=True):
        with mock.patch.object(h, "_mirror_git", return_value=(128, "", "fatal")):
            assert h._mirror_commit_authors("r", "skills/x") is None
