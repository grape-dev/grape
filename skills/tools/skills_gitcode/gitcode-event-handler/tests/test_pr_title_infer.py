"""PR 标题推断 add/update 判定测试(2026-08-18 用户要求, PR #24 实测)。

原规则只看变更文件 status(有 added 无 modified -> add): 给**已有 skill**
加文件(变更全是 added)被误判为 "add skill:"。修复后以仓库现状为准:
- 仓库已存在该 skill 目录 -> update(无论变更 added/modified)
- 仓库无该目录(新增 skill) -> 有 added 无 modified -> add, 否则 -> update
- 仓库查询失败 -> 回退 status 判定(fail-open)
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_files(files):
    return [{"filename": fn, "status": st, "additions": 0, "deletions": 0}
            for fn, st in files]


def _infer(mod, files, dir_exists=True):
    with mock.patch.object(mod, "gitcode_get", return_value=files):
        with mock.patch.object(mod, "_repo_dir_exists", return_value=dir_exists) as rde:
            r = mod._infer_pr_title("r/p", 24, "旧标题")
            # 2026-08-18 二次修复: 查仓库必须带 skills/ 前缀(skill_files 是
            # 去前缀路径, PR #25 实测无前缀查询恒 False 导致仍误判 add)
            if r and r.startswith(("add", "update")):
                assert rde.call_args[0][1].startswith("skills/"), rde.call_args
            return r


def test_existing_skill_all_added_is_update():
    # PR #24 场景: 已有 skill + 变更全 added -> update(修复核心)
    mod = _load_handler()
    files = _mk_files([
        ("skills/storage/rds/huawei-cloud-rds-smart-service/scripts/test.py", "added")])
    assert _infer(mod, files, dir_exists=True) == \
        "update skill: storage/rds/huawei-cloud-rds-smart-service"


def test_new_skill_all_added_is_add():
    # 仓库无该 skill + 全 added -> add
    mod = _load_handler()
    files = _mk_files([
        ("skills/network/ops/huawei-cloud-vpc-ip-group-create/SKILL.md", "added"),
        ("skills/network/ops/huawei-cloud-vpc-ip-group-create/scripts/a.py", "added")])
    assert _infer(mod, files, dir_exists=False) == \
        "add skill: network/ops/huawei-cloud-vpc-ip-group-create"


def test_new_skill_with_modified_is_update():
    # 仓库无该 skill 但有 modified -> update(旧规则保持)
    mod = _load_handler()
    files = _mk_files([
        ("skills/network/ops/huawei-cloud-vpc-list/SKILL.md", "modified")])
    assert _infer(mod, files, dir_exists=False) == \
        "update skill: network/ops/huawei-cloud-vpc-list"


def test_existing_skill_query_failure_falls_back_to_status():
    # 仓库查询失败(None) -> 回退 status 判定: 全 added -> add
    # (fail-open: 查询不可用时按变更文件推断)
    mod = _load_handler()
    files = _mk_files([
        ("skills/storage/rds/huawei-cloud-rds-smart-service/scripts/test.py", "added")])
    assert _infer(mod, files, dir_exists=None) == \
        "add skill: storage/rds/huawei-cloud-rds-smart-service"
