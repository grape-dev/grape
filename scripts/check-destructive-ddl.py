#!/usr/bin/env python3
"""Issue #536 · 危险 DDL CI/pre-commit 卡点。

规则：db_setup.py diff 中**新出现的** DROP/RENAME/MODIFY COLUMN 语句必须出现在
_cleanup 声明块内——即满足以下任一：
  a) 出现在 `_destructive_ddl(...)` 调用的参数行内（唯一入口）；
  b) 出现在 kind="cleanup" 的注册表声明行附近（register_migration(..., "cleanup", ...)）。

检查范围：仅 `git diff origin/<base>...HEAD -- src/api/db_setup.py` 的**新增行**
（禁止全历史扫描——历史内联语句是 legacy-stamp 打包内容，不在本次整改范围）。

base 解析（QA QA-Impt 修复）：
  1. 命令行参数（显式传目标分支，CI 推荐）；
  2. 环境变量 CI_MERGE_REQUEST_TARGET_BRANCH_NAME / GRAPE_WORK_BRANCH；
  3. 当前分支 upstream；
  4. 回退 main。
  base 不可解析（origin/<base> 与本地 <base> 均不存在）→ **FAIL（退出码 2）**，
  不允许 fail-open 放行（否则非法 DDL 静默通过 CI）。

用法：
  python3 scripts/check-destructive-ddl.py            # 自动探测 base
  python3 scripts/check-destructive-ddl.py local-liufei
                                                      # 显式指定 base 分支

退出码：0 = 通过；1 = 发现未收口的新增危险 DDL；2 = 环境/用法错误（含 base 不可解析）。
pre-commit 接入（仓库暂无 .pre-commit-config.yaml，本地 hook 手动安装）：
  ln -sf ../../scripts/check-destructive-ddl.py .git/hooks/pre-commit
"""
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TARGET_FILE = "src/api/db_setup.py"

# 危险 DDL 形态（大小写不敏感；行内匹配）
_DESTRUCTIVE_RE = re.compile(
    r"\b(DROP\s+COLUMN|RENAME\s+COLUMN|MODIFY\s+COLUMN)\b", re.IGNORECASE)
# 收口形态 a：_destructive_ddl( 调用行
_COLLECTED_RE = re.compile(r"_destructive_ddl\s*\(", re.IGNORECASE)
# 收口形态 b：cleanup 声明（注册表登记行）
_CLEANUP_DECL_RE = re.compile(
    r"register_migration\s*\(|kind\s*=\s*[\"']cleanup[\"']|[\"']cleanup[\"']\s*,",
    re.IGNORECASE)
# 探测逻辑行（'DROP COLUMN' in sql 之类的成员判断，非 DDL 语句本身）
_MEMBERSHIP_RE = re.compile(
    r"[\"'](DROP|RENAME|MODIFY)\s+COLUMN[\"']\s+in\b", re.IGNORECASE)


def _paren_balance(line: str) -> int:
    return line.count("(") - line.count(")")


def _classify_added_lines(added):
    """按序状态机解析新增行，返回未收口的危险 DDL 行。

    跳过：注释行、docstring 内行、_destructive_ddl/register_migration 调用的
    括号延续行（多行调用形态）、探测逻辑行（'DROP COLUMN' in sql）。
    """
    violations = []
    collected_depth = 0
    in_docstring = False
    for ln in added:
        stripped = ln.strip()
        if collected_depth > 0:
            collected_depth = max(collected_depth + _paren_balance(ln), 0)
            continue
        if in_docstring:
            if (ln.count('"""') + ln.count("'''")) % 2 == 1:
                in_docstring = False
            continue
        if stripped.startswith("#"):
            continue
        if (ln.count('"""') + ln.count("'''")) % 2 == 1:
            in_docstring = True
            continue
        if _MEMBERSHIP_RE.search(ln):
            continue
        # 收口形态行先登记括号深度（开括号行本身可不含 DDL 关键词）
        if _COLLECTED_RE.search(ln) or _CLEANUP_DECL_RE.search(ln):
            collected_depth = max(_paren_balance(ln), 0)
            continue
        if not _DESTRUCTIVE_RE.search(ln):
            continue
        violations.append(stripped)
    return violations


def _git(*args):
    out = subprocess.run(["git", "-C", REPO_ROOT, *args],
                         capture_output=True, text=True)
    return out.returncode, out.stdout


def _detect_base() -> str:
    """解析 base 分支：显式参数 > CI 环境变量 > upstream > main。"""
    for env_name in ("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "GRAPE_WORK_BRANCH"):
        val = os.environ.get(env_name, "").strip()
        if val and val != "HEAD":
            return val
    rc, up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc == 0 and up.strip() and up.strip() != "HEAD":
        return up.strip()
    return "main"


def _resolve_base(argv) -> str:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    return _detect_base()


def _added_lines(base: str):
    """diff 中目标文件的新增行（带 + 前缀去除）。

    返回 (lines, error)。base 无法解析（origin/<base> 与本地 <base> 均不存在）
    时 error 非空——调用方必须 FAIL（QA-Impt：不允许 fail-open 放行）。
    """
    rc, out = _git("diff", f"origin/{base}...HEAD", "--", TARGET_FILE,
                   "--unified=0")
    if rc != 0:
        # origin/<base> 可能不存在（未 fetch），尝试本地分支引用
        rc, out = _git("diff", f"{base}...HEAD", "--", TARGET_FILE, "--unified=0")
        if rc != 0:
            return [], (f"无法 diff base={base}（origin/{base} 与本地 {base} 均不存在）。"
                        f"CI 必须传入目标分支（参数 / CI_MERGE_REQUEST_TARGET_BRANCH_NAME / "
                        f"GRAPE_WORK_BRANCH），base 不可解析按 FAIL 处理，不做 fail-open。")
    lines = []
    for ln in out.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            lines.append(ln[1:])
    return lines, None


def main(argv):
    base = _resolve_base(argv)
    added, err = _added_lines(base)
    if err:
        print(f"[check-destructive-ddl] FAIL：{err}")
        return 2
    if not added:
        print(f"[check-destructive-ddl] PASS（base={base}，无新增行）")
        return 0

    violations = _classify_added_lines(added)

    if violations:
        print("[check-destructive-ddl] FAIL：以下新增的破坏性 DDL 未走 _destructive_ddl()"
              " 收口或 cleanup 声明块：")
        for v in violations:
            print(f"  + {v}")
        print("修复方式：改经 _destructive_ddl(c, <ddl>, kind_hint=\"cleanup\") 执行，"
              "或以 register_migration(..., kind=\"cleanup\", ...) 登记。")
        return 1
    print(f"[check-destructive-ddl] PASS（base={base}，新增危险 DDL 均已收口）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))