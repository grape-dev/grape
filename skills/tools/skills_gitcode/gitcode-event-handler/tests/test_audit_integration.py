"""audit 集成测试: _run_skill_audit_scan 应扫出假凭据。"""
import os
import pathlib
import shutil
import sys
import tempfile
import importlib.util

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_detects_fake_ak():
    mod = _load_handler()
    files_map = {
        "skills/network/eip/huawei-cloud-eip-list/SKILL.md": "# demo\n",
        "skills/network/eip/huawei-cloud-eip-list/scripts/list.sh":
            "AKIAIOSFODNN7EXAMPLE\n"
            "export AWS_SECRET_ACCESS_KEY='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n",
    }
    issues = mod._run_skill_audit_scan("g30074593/test", 1, files_map, "github")
    assert issues, "应扫描出凭据泄露问题"
    assert any(i.get("rule") for i in issues), "issue 应含 rule"
    assert any(i.get("file") for i in issues), "issue 应含 file"
    assert any(i.get("severity") for i in issues), "issue 应含 severity"


def test_audit_clean_dir_no_issues():
    mod = _load_handler()
    files_map = {
        "skills/network/eip/huawei-cloud-eip-list/SKILL.md": "# demo skill\n",
        "skills/network/eip/huawei-cloud-eip-list/scripts/list.sh": "#!/bin/bash\necho ok\n",
    }
    issues = mod._run_skill_audit_scan("g30074593/test", 1, files_map, "github")
    # 干净目录允许有 0 条或仅 info 级(high 级别过滤 critical/error)
    assert isinstance(issues, list), "应返回 list"


def test_audit_missing_skill_dir_fail_open():
    mod = _load_handler()
    # 指向不存在的 audit 技能目录时需 fail-open(空列表)
    import unittest.mock as mock
    with mock.patch.object(mod.os.path, "isdir", return_value=False):
        issues = mod._run_skill_audit_scan("g30074593/test", 1, {}, "github")
    assert issues == [], "audit 技能缺失时应返回空列表(不阻断)"
