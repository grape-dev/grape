"""S75 用例（D50，先测后改）：全量枚举驱动的剩余确认洞收口。

本轮方法论升级：形状扫描从"抽样 grep"改为"全量枚举+逐条分类+计数对账"
（总命中=确认洞+基线预存+误报，数字闭环）。本文件锁定其中 11 组确认洞：
崩溃类（issues 列表/展示 int(number) ×3）、错误 token 类（chat×4、
repos 移除流、autopilot、scheduler）、功能拒掉类（管道 ISSUE_NUMBER
注入、cancelled 检查、SRE 回填、附件 prompt、IM 链接）、硬编码 URL
类（澄清评论 else=gitcode、fail_task 去重读评论 else=gitcode）。
"""
import re

import pytest


def _src(path):
    return open(path, encoding="utf-8").read()


# ------------------------------------------------ 崩溃类：issues 展示层

def test_s75_issues_routes_no_bare_int_number():
    """issues.py 展示层不得裸 int(number)（gitee 字符串 ident → 列表 500）。"""
    src = _src("src/domains/runtime/routes/issues.py")
    bare = [m.group(0) for m in re.finditer(r"int\(number\)", src)]
    assert not bare, f"bare int(number) remain: {bare}"


# ------------------------------------------------ 错误 token 类

def test_s75_chat_token_provider_derived():
    """chat.py 取用户 token 必须按 repo_key 派生（双平台绑定用户拿错 token
    注入 gitee 会话——D37 docstring 声称已修但 4 处实际未接）。"""
    src = _src("src/domains/runtime/routes/chat.py")
    assert "get_user_token_with_refresh" not in src, "chat.py 仍折叠 gitcode token"
    assert "get_user_token_for_repo" in src, "chat.py 未接 provider 派生"


def test_s75_autopilot_scheduler_repo_token_provider_aware():
    """autopilot/scheduler 取仓库 token 不得 github/else 二分折叠 gitcode
    （gitee 仓 autopilot 取空 token → 建单失败）。

    D101 方案二语义更新（W5）：scheduler 传输值退役——校验改
    resolve_repo_config_level（provider 感知的纯读级别检查，token 值
    传输移交 claim 现取注入）。锁的语义不变：token 获取必须 provider
    感知，不得二分折叠。
    """
    for f in ("src/domains/mgmt/routes/autopilot.py", "src/domains/runtime/engine/scheduler.py"):
        s = _src(f)
        assert "get_gitcode_token" not in s, f"{f} 仍 else 折叠 get_gitcode_token"
    # scheduler：provider 感知校验接缝（resolve_repo_config_level 内部走
    # 平台卡派生 provider——二分折叠的否决面由 token_service 承担）
    assert "resolve_repo_config_level" in _src("src/domains/runtime/engine/scheduler.py"), \
        "scheduler 未接 provider 感知校验（D101 W5 新接缝）"
    # autopilot：原断言保留（未在本轮改动面内）
    assert "get_repo_token_with_refresh" in _src("src/domains/mgmt/routes/autopilot.py"), \
        "autopilot 未接 provider 感知 token"


def test_s75_repos_remove_flow_token_derived():
    """repos.py 移除托管 bot 分支不得 else 折叠 gitcode token。"""
    src = _src("src/domains/mgmt/routes/repos.py")
    assert "get_user_token_with_refresh" not in src, "repos.py 仍有 gitcode 折叠 token 点"


# ------------------------------------------------ 功能拒掉类：isdigit 门槛

def test_s75_no_ident_isdigit_gates():
    """ident 处理不得 isdigit 门槛（gitee 字符串 ident 被功能性拒掉：
    ISSUE_NUMBER 不注入/cancelled 检查跳过/SRE 回填跳过/附件 prompt 跳过）。"""
    offenders = []
    for f, pats in (
        ("src/api/event_pipeline.py", [r"_issue_num\.isdigit\(\)"]),
        ("src/prompt_builder.py", [r"_issue_num\.isdigit\(\)"]),
        ("src/domains/runtime/engine/service.py", [r"_issue_num\.isdigit\(\)",
                                    r"_issue_id[^ ]*\.isdigit\(\)"]),
        ("src/domains/runtime/im/handler_async.py", [r"_iss\.isdigit\(\)"]),
    ):
        src = _src(f)
        for pat in pats:
            for m in re.finditer(pat, src):
                offenders.append(f"{f}: {m.group(0)}")
    assert not offenders, f"ident isdigit gates remain: {offenders}"


# ------------------------------------------------ 硬编码 URL 类

def test_s75_clarify_comment_no_gitcode_hardcode():
    """澄清评论 else 分支不得硬编码 gitcode.com URL（gitee 仓发到 gitcode
    → 404）——应走 VcsApiClient(provider)。"""
    src = _src("src/domains/runtime/routes/issues.py")
    hits = [l for l in src.splitlines()
            if "gitcode.com/api" in l and "comments" in l]
    assert not hits, f"clarify comment 仍硬编码 gitcode URL: {hits[:2]}"


def test_s75_fetch_comments_provider_aware():
    """_fetch_issue_comments 不得 else=gitcode 硬编码（gitee 仓 fail_task
    去重读评论失败 → 重复回帖）——GitHub 之外走卡客户端。"""
    src = _src("src/domains/runtime/engine/service.py")
    hits = [l for l in src.splitlines()
            if "gitcode.com/api" in l and "comments" in l]
    assert not hits, f"_fetch_issue_comments 仍硬编码: {hits[:2]}"


# ------------------------------------------------ 行为用例：chat token 派生

def test_s75_chat_gitee_token_derived():
    """行为锁：gitee 会话 chat 取 token 走 repo_key 派生（mock identity）。"""
    from src.api.deps import get_user_token_for_repo
    with _patch_identity({"gitee": "gt-1", "gitcode": ""}):
        tok = get_user_token_for_repo("gitee:acme/web", "u1", user_id=1)
    assert tok == "gt-1"


def _patch_identity(mapping):
    from unittest.mock import patch

    def _fake(username, provider):
        return mapping.get(provider, "")
    return patch("src.api.deps._get_oauth_token_from_identity", side_effect=_fake)
