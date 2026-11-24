"""Issue #4239：调用方 i18n key 差集门禁（scripts/check-i18n-usage.py）单测。

覆盖三类缺口：命名空间前缀错位导致 t() 返回 key 原文、语言包缺词条只能靠
组件 DEFAULTS 兜底（英文环境显示中文）、以及正常命中词条不误报。
"""
import importlib.util
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check-i18n-usage.py"

spec = importlib.util.spec_from_file_location("check_i18n_usage", _SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
sys.modules["check_i18n_usage"] = gate
spec.loader.exec_module(gate)


_TASKS_PREFIXED = '''
<script>
function t(key, params) {
  const full = 'tasks.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : TASK_DEFAULTS[key] || key
}
function td(key, params) {
  const full = 'issues.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : ISSUE_DEFAULTS[key] || key
}
const TASK_DEFAULTS = {
  page_title: '需求中心',
  fallback_only: '仅默认表',
}
const ISSUE_DEFAULTS = {
  save_failed: '保存失败',
}
const a = t('pipeline.status_failed')
const b = t('fallback_only')
const c = td('save_failed')
const d = td('deploy_url_saved')
</script>
'''


def _write_locales(root: Path):
    for lang, values in (
        ("zh", {"pipeline": {"status_failed": "失败"}, "issues": {"deploy_url_saved": "已保存"}}),
        ("en", {"pipeline": {"status_failed": "Failed"}, "issues": {"deploy_url_saved": "Saved"}}),
    ):
        d = root / lang
        d.mkdir(parents=True, exist_ok=True)
        for mod, payload in values.items():
            (d / f"{mod}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_src(root: Path, body: str = _TASKS_PREFIXED):
    (root / "views").mkdir(parents=True, exist_ok=True)
    (root / "views" / "Sample.vue").write_text(body, encoding="utf-8")


def test_namespace_mismatch_flagged_as_leak(tmp_path):
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    _write_src(src)
    _write_locales(locales)

    findings = gate.scan_tree(src, locales)
    keys = {(kind, key) for _path, kind, key in findings}
    assert ("leak", "pipeline.status_failed") in keys


def test_defaults_only_missing_flag_as_fallback(tmp_path):
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    _write_src(src)
    _write_locales(locales)

    findings = gate.scan_tree(src, locales)
    kinds = {(kind, key) for _path, kind, key in findings}
    assert ("fallback", "fallback_only") in kinds


def test_correct_namespace_and_keys_not_flagged(tmp_path):
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    body = '''
<script>
function t(key) {
  return G.I18N.t(key)
}
const a = t('pipeline.status_failed')
const b = t('issues.deploy_url_saved')
</script>
'''
    _write_src(src, body)
    _write_locales(locales)

    assert gate.scan_tree(src, locales) == []


def test_main_exit_code_reflects_findings(tmp_path):
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    _write_src(src)
    _write_locales(locales)

    assert gate.main(["--src-dir", str(src), "--locales-dir", str(locales)]) == 1


def test_repository_is_clean():
    findings = gate.scan_tree(gate.DEFAULT_SRC, gate.DEFAULT_LOCALES)
    assert findings == []


_ARROW_BLOCK = '''
<script>
const t = (key, vars) => {
  const full = 'sandbox.llm.' + key
  const v = G.I18N.t(full, vars)
  return v && v !== full ? v : key
}
const a = t('missing_key')
</script>
'''

_ARROW_EXPR = '''
<script>
const t = key => G.I18N.t('sandbox.llm.' + key)
const a = t('missing_key')
</script>
'''


def test_arrow_wrapper_block_body_flagged(tmp_path):
    """箭头函数包装（块体）也必须解析出前缀，否则整文件静默跳过。"""
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    _write_src(src, _ARROW_BLOCK)
    _write_locales(locales)

    found = {(kind, key) for _path, kind, key in gate.scan_tree(src, locales)}
    assert ("leak", "missing_key") in found


def test_arrow_wrapper_expression_body_flagged(tmp_path):
    """`const t = key => G.I18N.t('ns.' + key)` 这种单表达式体同样要被识别。"""
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    _write_src(src, _ARROW_EXPR)
    _write_locales(locales)

    found = {(kind, key) for _path, kind, key in gate.scan_tree(src, locales)}
    assert ("leak", "missing_key") in found


def test_module_list_derived_from_locale_dir(tmp_path):
    """模块清单必须从 locales/<lang>/*.json 派生：旧硬编码表漏掉的 beta_apply 也要纳入。"""
    src = tmp_path / "src"
    locales = tmp_path / "locales"
    for lang in ("zh", "en"):
        d = locales / lang
        d.mkdir(parents=True, exist_ok=True)
        (d / "beta_apply.json").write_text(
            json.dumps({"form": {"title": "x"}}, ensure_ascii=False), encoding="utf-8")
    _write_src(src, "const a = I18N.t('beta_apply.form.title')\n")

    assert gate.scan_tree(src, locales) == []
