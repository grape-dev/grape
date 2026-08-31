"""降噪逻辑测试: _denoise_audit_issues 应过滤/降级误报, 保留真实安全问题。"""
import os
import pathlib
import tempfile
import importlib.util

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_tmp(files):
    """建临时目录并写入 {相对路径: 内容}。"""
    td = pathlib.Path(tempfile.mkdtemp(prefix="denoise_test_"))
    for fn, content in files.items():
        p = td / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return td


def test_doc_file_skillspector_downgraded():
    """a. 文档类文件(.txt)的 skillspector 命中 → 降为 warning。"""
    mod = _load_handler()
    td = _mk_tmp({"skill-gate-report.txt": "grep for eval(/exec(/os.system patterns; none found\n" * 40})
    issues = [{"severity": "error", "rule": "OH1", "file": "skill-gate-report.txt",
               "line": 19, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "降级而非移除"
    assert out[0]["severity"] == "warning", "应降为 warning"
    import shutil; shutil.rmtree(td, ignore_errors=True)


def test_e2_env_get_single_var_removed():
    """b. E2 按名读取单个环境变量 → 安全放行(移除)。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/list.py": "ak = os.environ.get('HUAWEI_ACCESS_KEY')\n" * 30})
    issues = [{"severity": "error", "rule": "E2", "file": "scripts/list.py",
               "line": 5, "message": "Env Variable Harvesting"}]
    out = mod._denoise_audit_issues(issues, td)
    assert out == [], "按名读取应放行"


def test_e2_env_items_iteration_kept():
    """b. E2 遍历全部环境变量(os.environ.items()) → 保留(真实风险)。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/leak.py": "for k, v in os.environ.items():\n    send(v)\n" * 20})
    issues = [{"severity": "error", "rule": "E2", "file": "scripts/leak.py",
               "line": 3, "message": "Env Variable Harvesting"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "遍历环境变量应保留"
    assert out[0]["severity"] == "error", "保持阻断"


def test_comment_line_removed():
    """c. 注释行命中 → 移除。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/x.py": "# eval is dangerous\n" * 10})
    issues = [{"severity": "error", "rule": "OH1", "file": "scripts/x.py",
               "line": 1, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert out == [], "注释行应移除"


def test_cred_rule_in_doc_kept():
    """gitleaks 凭据类命中在文档里也不降噪(硬编码凭据真实)。"""
    mod = _load_handler()
    td = _mk_tmp({"notes.txt": "AKIAIOSFODNN7EXAMPLE\n" * 20})
    issues = [{"severity": "error", "rule": "aws-access-token", "file": "notes.txt",
               "line": 1, "message": "AWS credentials"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "凭据类不降噪"
    assert out[0]["severity"] == "error", "保持阻断"


def test_code_file_skillspector_kept():
    """代码文件(.py)的 skillspector 真实命中 → 保留阻断。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/x.py": "eval(response)\n" * 10})
    issues = [{"severity": "error", "rule": "OH1", "file": "scripts/x.py",
               "line": 2, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "代码文件真实命中保留"
    assert out[0]["severity"] == "error", "保持阻断"


def test_oh1_subprocess_capture_output_removed():
    """d. OH1: subprocess.run(cmd, capture_output=True) 安全形态 → 放行。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/x.py": "proc = subprocess.run(cmd, capture_output=True, text=True)\n" * 10})
    issues = [{"severity": "error", "rule": "OH1", "file": "scripts/x.py",
               "line": 3, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert out == [], "capture_output=True 应放行"


def test_oh1_subprocess_shell_string_kept():
    """d. OH1: subprocess.run(shell 字符串, shell=True) 真实风险 → 保留。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/x.py": "subprocess.run(response, shell=True)\n" * 10})
    issues = [{"severity": "error", "rule": "OH1", "file": "scripts/x.py",
               "line": 2, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "shell=True 字符串执行应保留"
    assert out[0]["severity"] == "error", "保持阻断"


def test_oh1_eval_output_kept():
    """d. OH1: eval(output) 真实风险 → 保留(不是 subprocess 误伤)。"""
    mod = _load_handler()
    td = _mk_tmp({"scripts/x.py": "eval(response)\n" * 10})
    issues = [{"severity": "error", "rule": "OH1", "file": "scripts/x.py",
               "line": 1, "message": "Unvalidated Output Injection"}]
    out = mod._denoise_audit_issues(issues, td)
    assert len(out) == 1, "eval(output) 应保留"
    assert out[0]["severity"] == "error", "保持阻断"
