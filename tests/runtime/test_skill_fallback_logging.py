"""skill_materialize 降级兜底不得因日志名未定义而崩溃（事件 #9503 根因）。

现场：技能拉取全部失败 → 走 write_skill_fallback 写 FALLBACK.md 时，
`_logging` 只在模块顶部 try/except 的 except 分支里 import，正常路径下未定义 →
NameError: name '_logging' is not defined → 任务执行协程炸掉、任务停在 running →
每次 re-register 触发 recover-orphans，attempts 累加到 3 → "possible self-restart loop"。

回归：正常导入路径下 write_skill_fallback 必须成功写文件并返回 True。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine.skill_materialize import write_skill_fallback  # noqa: E402


def test_fallback_writes_without_name_error(tmp_path):
    assert write_skill_fallback(str(tmp_path), "vod-cli", reason="obs unreachable", stage="fetch") is True
    f = tmp_path / "vod-cli" / "FALLBACK.md"
    assert f.exists()
    assert "vod-cli" in f.read_text(encoding="utf-8")


def test_fallback_noop_when_skill_missing(tmp_path):
    assert write_skill_fallback(str(tmp_path), "", reason="x") is False


def test_fallback_idempotent_second_call(tmp_path):
    assert write_skill_fallback(str(tmp_path), "vod-cli") is True
    assert write_skill_fallback(str(tmp_path), "vod-cli") is True