"""D102-I：patrol ident 漏网第 16 处收口（S75/S76 族）——行为锁。

外部复核推翻此前误判：src/api/patrol.py:397（a2a 链 issue 终态判断）
裸调 _native_issue_number 无 import——S76 清扫 15 处后漏网的第 16 处。
纯数字编号巡检进入即 NameError（字符串 ident 被 isdigit 门槛提前返回，
故仅 int 平台——GitCode/GitHub 常态路径——触发）。
教训（memory 在册）：同名词双文件（src/api/patrol.py 实现本体 vs
src/domains/mgmt/routes/patrol.py 路由壳），判"代码不存在"必须全路径扫描。
"""
import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PATROL = _REPO_ROOT / "src" / "api" / "patrol.py"


def test_d102i_patrol_import_present():
    """patrol.py 若使用 _native_issue_number 则必须显式 import（防 NameError 回潮）。"""
    src = _PATROL.read_text(encoding="utf-8")
    if "_native_issue_number" not in src:
        return  # 未来移除该调用则守卫自然解除
    tree = ast.parse(src)
    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "src.domains.runtime.vcs.api_client"
        and any(a.name == "_native_issue_number" for a in n.names)
        for n in ast.walk(tree))
    assert imported, "patrol.py 使用 _native_issue_number 但未 import（S75 漏网回潮）"


def test_d102i_patrol_module_imports():
    """模块级 import 探针（NameError 在 import 期即暴露的兜底）。"""
    import src.api.patrol  # noqa: F401


def test_d102i_cache_version_bumped():
    """index.html 资源版本 ≥218（app.js 变更未 bump 的部署缓存问题防复发）。"""
    html = (_REPO_ROOT / "src" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    import re
    m = re.search(r"app\.js\?v=(\d+)", html)
    assert m, "index.html 缺 app.js 版本参数"
    assert int(m.group(1)) >= 218, f"app.js 版本号 {m.group(1)} < 218（D102-F 链接修复未对用户生效的缓存形态）"
