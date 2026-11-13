#!/usr/bin/env python3
"""Issue #536 · 危险 DDL CI/pre-commit 卡点。

规则：db_setup.py diff 中**新出现的** DROP/RENAME/MODIFY COLUMN 语句必须出现在
_cleanup 声明块内——即满足以下任一：
  a) 出现在 `_destructive_ddl(...)` 调用的参数行内（唯一入口）；
  b) 出现在 kind="cleanup" 的注册表声明行附近（register_migration(..., "cleanup", ...)）。

检查范围：`git diff origin/<base>...HEAD` 中 src/api/db_setup.py 与 scripts/migrations/*.py 的**新增行**
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
  python3 scripts/check-destructive-ddl.py weekly-09
                                                      # 显式指定 base 分支

退出码：0 = 通过；1 = 发现未收口的新增危险 DDL；2 = 环境/用法错误（含 base 不可解析）。
pre-commit 接入（仓库暂无 .pre-commit-config.yaml，本地 hook 手动安装）：
  ln -sf ../../scripts/check-destructive-ddl.py .git/hooks/pre-commit
"""
import glob
import io
import os
import re
import subprocess
import sys
import tokenize

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TARGET_FILES = ["src/api/db_setup.py"] + sorted(
    os.path.relpath(p, REPO_ROOT)
    for p in glob.glob(os.path.join(REPO_ROOT, "scripts", "migrations", "*.py"))
)

# 危险 DDL 形态（大小写不敏感；行内匹配）
# 列级 + 表级都要覆盖：表级形态（DROP/RENAME TABLE、ALTER ... RENAME TO）一旦漏掉，
# 把重建迁移回退成裸 DDL 时 CI 仍全绿（评审 G-1）。
_DESTRUCTIVE_RE = re.compile(
    r"\b(DROP\s+COLUMN|RENAME\s+COLUMN|MODIFY\s+COLUMN|"
    r"DROP\s+TABLE|RENAME\s+TABLE|TRUNCATE\s+TABLE)\b"
    r"|\bALTER\s+TABLE\s+\S+\s+RENAME\s+TO\b", re.IGNORECASE)
# 收口形态 a：_destructive_ddl( 调用行
_COLLECTED_RE = re.compile(r"_destructive_ddl\s*\(", re.IGNORECASE)
# 收口形态 b：cleanup 声明（注册表登记行）
_CLEANUP_DECL_RE = re.compile(
    r"register_migration\s*\(|kind\s*=\s*[\"']cleanup[\"']|[\"']cleanup[\"']\s*,",
    re.IGNORECASE)
# 探测逻辑行（'DROP COLUMN' in sql 之类的成员判断，非 DDL 语句本身）
_MEMBERSHIP_RE = re.compile(
    r"[\"'](DROP|RENAME|MODIFY|TRUNCATE)\s+(COLUMN|TABLE)[\"']\s+in\b",
    re.IGNORECASE)


def _paren_balance(line: str) -> int:
    return line.count("(") - line.count(")")


def _hidden_lines(path: str) -> set:
    """文件里属于 STRING / COMMENT 令牌的行号（1 基）。

    按令牌判断而不是「三引号出现次数奇偶」——后者在 diff hunk 从 docstring 中间开始
    时状态是错的（hunk 里看不到开头的 `\"\"\"`），把 docstring 正文当代码扫，
    实测误报 db_setup.py 里一句 `ALTER TABLE ... RENAME TO` 说明文字（2026-11-13）。
    """
    rc, src = _git("show", f"HEAD:{path}")
    if rc != 0:
        return set()
    hidden = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                hidden.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass  # 语法不完整时退化为只跳过显式注释行，不误判为通过
    return hidden


def _classify_added_lines(added):
    """按序状态机解析新增行，返回未收口的危险 DDL 行。

    added 项可为 (file, lineno, text) / (file, text) / text：
    - 带 lineno 时用 tokenize 精确屏蔽字符串与注释行（真实 diff 路径）；
    - 不带 lineno（单元测试/旧调用点）时退回「三引号奇偶」启发式。
    lineno < 0 表示 hunk 边界（括号深度不可跨 hunk 推断，遇到就归零）。
    """
    violations = []
    collected_depth = 0
    hidden_cache = {}
    in_docstring = False
    for item in added:
        if isinstance(item, str):
            path, lineno, ln = "", None, item
        elif len(item) == 2:
            path, ln = item
            lineno = None
        else:
            path, lineno, ln = item
        if lineno is not None and lineno < 0:
            collected_depth = 0
            continue
        stripped = ln.strip()
        tri = (ln.count('"""') + ln.count("'''")) % 2 == 1
        if collected_depth > 0:
            collected_depth = max(collected_depth + _paren_balance(ln), 0)
            continue
        if lineno is None:
            if in_docstring:
                if tri:
                    in_docstring = False
                continue
            if stripped.startswith("#"):
                continue
            if tri:
                in_docstring = True
                continue
        else:
            if path not in hidden_cache:
                hidden_cache[path] = _hidden_lines(path)
            if lineno in hidden_cache[path] or stripped.startswith("#"):
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
    rc, out = _git("diff", f"origin/{base}...HEAD", "--", *TARGET_FILES,
                   "--unified=0")
    if rc != 0:
        # origin/<base> 可能不存在（未 fetch），尝试本地分支引用
        rc, out = _git("diff", f"{base}...HEAD", "--", *TARGET_FILES, "--unified=0")
        if rc != 0:
            return [], (f"无法 diff base={base}（origin/{base} 与本地 {base} 均不存在）。"
                        f"CI 必须传入目标分支（参数 / CI_MERGE_REQUEST_TARGET_BRANCH_NAME / "
                        f"GRAPE_WORK_BRANCH），base 不可解析按 FAIL 处理，不做 fail-open。")
    lines = []
    cur_file = ""
    new_lineno = 0
    for ln in out.splitlines():
        if ln.startswith("+++ b/"):
            cur_file = ln[len("+++ b/"):].strip()
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,\d+)?", ln)
            new_lineno = int(m.group(1)) if m else 0
            # hunk 边界：括号深度/文档状态不能跨 hunk 继承（-1 为哨兵行号）
            lines.append((cur_file, -1, ln))
        elif ln.startswith("+") and not ln.startswith("+++"):
            lines.append((cur_file, new_lineno, ln[1:]))
            new_lineno += 1
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

    # QA 复审 M-2：scripts/migrations/ 是一次性迁移脚本——受控通道（常量驱动的
    # 表/列名、逐文件人工评审），其中的危险 DDL 不适用 db_setup 的
    # _destructive_ddl/cleanup 收口形态；单独计数报告，不判违规。
    # 归一成 (file, lineno, text)：兼容测试/旧调用点给出的 (file, text) 与裸文本。
    norm = []
    for t in added:
        if isinstance(t, str):
            norm.append(("", None, t))
        elif len(t) == 2:
            norm.append((t[0], None, t[1]))
        else:
            norm.append(tuple(t))
    added = norm
    migration_ddl = [(f, l) for f, _n, l in added
                     if f.startswith("scripts/migrations/") and _DESTRUCTIVE_RE.search(l)]
    added = [t for t in added if not t[0].startswith("scripts/migrations/")]

    violations = _classify_added_lines(added)

    if migration_ddl:
        print(f"[check-destructive-ddl] INFO：scripts/migrations/ 受控通道内新增危险 DDL "
              f"{len(migration_ddl)} 行（一次性迁移脚本，逐文件评审）：")
        for f, l in migration_ddl:
            print(f"  [{f}] + {l.strip()[:100]}")
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