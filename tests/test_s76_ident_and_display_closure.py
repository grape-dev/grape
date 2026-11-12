"""S76 用例（D51，先测后改）：族B ident int() 崩溃点全收 + 展示名/链接卡派生。

枚举来源（D50 方法论第三轮）：族B 过滤后 15 处真 ident 崩溃点（变量名各
异：_n/_num3/_ce_num/_sub_iss_num/issue_number/num/parent_issue_number），
gitee 字符串 ident 一到即 ValueError；族A 分类后 4 处展示错派
（chat/handler 的 _pName 二分显示 "GitCode"；issues 的 _issue_host
issue_url 指 gitcode.com）。
"""
import re

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
import json


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ------------------------------------------------ 守卫：ident int() 崩溃点清零

_IDENT_PATTERNS = [
    r"int\(_n\)", r"int\(_num3\)", r"int\(_ce_num\)", r"int\(_sub_iss_num\)",
    r"int\(issue_number\)", r"int\(num\)", r"int\(parent_issue_number\)",
    r"int\(_iss\)",
]

_IDENT_FILES = [
    "src/api/event_pipeline.py",
    "src/api/issues_center.py",
    "src/api/patrol.py",
    "src/api/patrol_compensate.py",
    "src/api/session_store.py",
    "src/domains/runtime/routes/webhook.py",
    "src/domains/mgmt/routes/autopilot.py",
    "src/domains/runtime/routes/tasks.py",
    "src/domains/runtime/routes/issues.py",
    "src/domains/runtime/routes/chat.py",  # _iss 类
]


def test_s76_no_ident_int_crash_sites():
    offenders = []
    for f in _IDENT_FILES:
        src = open(f, encoding="utf-8").read()
        for pat in _IDENT_PATTERNS:
            if pat == r"int\(issue_number\)":
                # webhook/routes 展示响应里的 number 原生化后允许 str；
                # 任何裸 int(issue_number) 均为崩溃点
                for m in re.finditer(pat, src):
                    offenders.append(f"{f}: {m.group(0)}")
            else:
                for m in re.finditer(pat, src):
                    offenders.append(f"{f}: {m.group(0)}")
    assert not offenders, f"ident int() crash sites remain: {offenders}"


# ------------------------------------------------ 展示名/链接卡派生

def test_s76_display_name_card_derived(gitee_card):
    from src.domains.runtime.vcs.platform_card import display_name_for_repo_key, host_for_repo_key
    assert display_name_for_repo_key("gitee:acme/web") == "Gitee"
    assert display_name_for_repo_key("github:acme/web") == "GitHub"
    assert display_name_for_repo_key("gitcode:acme/web") == "GitCode"
    assert display_name_for_repo_key("acme/web") == "GitCode"  # 裸键存量语义
    assert host_for_repo_key("gitee:acme/web") == "gitee.com"
    assert host_for_repo_key("github:acme/web") == "github.com"
    assert host_for_repo_key("acme/web") == "gitcode.com"


def test_s76_display_sites_use_helper():
    """chat/handler/issues 展示点必须走卡派生助手（原二分显示 GitCode/
    链接指 gitcode.com）。"""
    for f in ("src/domains/runtime/routes/chat.py", "src/domains/runtime/im/handler_async.py",
              "src/domains/runtime/routes/issues.py"):
        src = open(f, encoding="utf-8").read()
        assert '"GitHub" if' not in src, f"{f} 仍有平台名二分字面量"
    src = open("src/domains/runtime/routes/issues.py", encoding="utf-8").read()
    assert '_issue_host = "github.com"' not in src, "issues.py issue_url 仍硬编码 host"
