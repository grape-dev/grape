"""守卫：仓库内任何被跟踪文件都不得包含 git 冲突标记（合并事故防线）。

背景：2026-11-12 weekly-11 合并时 `src/api/static/index.html` 的一处版本号冲突
未解就提交了，导致前端页面直接把 `<<<<<<< HEAD` 渲染出来。
pytest 只跑 Python，静态资源/模板不在覆盖范围，因此单独加这条全仓扫描。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "data", "dist"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".so", ".pyc", ".zip", ".gz"}

MARKERS = ("<<<<<<< ", ">>>>>>> ")


def _iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if SKIP_DIRS & set(p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        yield p


def test_no_conflict_markers_in_repo():
    bad = []
    for p in _iter_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            if line.startswith(MARKERS):
                bad.append(f"{p.relative_to(ROOT)}:{ln}: {line[:60]}")
                break
    assert not bad, "发现未解的 git 冲突标记：\n" + "\n".join(bad[:20])