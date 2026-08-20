#!/usr/bin/env python3
"""规范检查脚本——脚本化校验，不靠 LLM 判断。

用法: python3 spec_check.py <skill_dir>
输出: 逐项 pass/fail（exit 0=全过 / 1=有失败）
"""
import os
import re
import sys


def _read_skill_md(skill_dir):
    """读取 SKILL.md 内容，返回 (content, frontmatter, body, exists)。
    只读一次，供所有检查项复用。
    """
    md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(md_path):
        return "", "", "", False
    content = open(md_path, encoding="utf-8", errors="ignore").read()
    if content.startswith("---") and content.count("---") >= 2:
        parts = content.split("---", 2)
        fm = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""
    else:
        fm = ""
        body = content
    return content, fm, body, True


def check_frontmatter(skill_dir: str) -> list:
    """Frontmatter 规范检查——合并 name/description/triggers/version/tags/naming。

    只解析一次 frontmatter，覆盖：
    - name/description/triggers 必填
    - name 小写连字符格式
    - name 与目录名一致性
    - version 语义化版本号格式
    - tags ≤5 个
    - 命名规范（huawei-cloud- 前缀建议）
    """
    problems = []
    content, fm, body, exists = _read_skill_md(skill_dir)
    if not exists:
        return ["SKILL.md 缺失"]
    if not content.startswith("---"):
        problems.append("frontmatter 缺失（SKILL.md 必须以 --- 开头）")
        return problems

    # name/description 必填
    for field in ("name", "description"):
        if not re.search(rf"^{field}:", fm, re.M):
            problems.append(f"frontmatter 缺 {field}")

    # triggers 检查
    if "triggers" not in fm.lower() and "triggers include" not in content[:500].lower():
        problems.append("缺 triggers（触发词）")

    # name 格式 + 目录名一致性
    m = re.search(r"^name:\s*([^\n]+)", fm, re.M)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            problems.append(f"name 格式错（须小写连字符）: {name}")
        dir_name = os.path.basename(skill_dir.rstrip("/"))
        if name != dir_name:
            problems.append(f"frontmatter name '{name}' 不匹配目录名 '{dir_name}'")
        # 命名规范建议
        if not dir_name.startswith("huawei-cloud-"):
            problems.append(f"命名规范：skill 目录名 '{dir_name}' 建议以 huawei-cloud- 开头")

    # version 格式
    m = re.search(r"^version:\s*([^\n]+)", fm, re.M)
    if m and not re.match(r"^\d+\.\d+\.\d+$", m.group(1).strip()):
        problems.append(f"version 格式错（须 x.y.z）: {m.group(1).strip()}")

    # tags ≤5
    if re.search(r"^tags:", fm, re.M):
        m_inline = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.M)
        if m_inline:
            count = len([t for t in m_inline.group(1).split(",") if t.strip()])
        else:
            tags_block = re.search(r"^tags:\s*\n((?:\s+-\s+.+\n?)+)", fm, re.M)
            if tags_block:
                count = len([t for t in tags_block.group(1).strip().split("\n") if t.strip().startswith("-")])
            else:
                count = 0
        if count > 5:
            problems.append(f"tags {count} 个超限（≤5 个）")

    return problems


def check_structure(skill_dir: str) -> list:
    """文件结构检查——合并目录/文件存在性检查。

    覆盖：
    - scripts/ 目录存在且有可执行 .py 脚本
    - SKILL.md 存在（frontmatter 检查也查，但这里作为结构检查的一部分）
    - references/ 目录存在
    - references/iam-policies.md 存在
    - references/cli-installation-guide.md（CLI 模式时）
    - templates/test-vars.json 推荐存在
    - 文件扩展名白名单
    """
    problems = []

    # scripts/ 目录 + 可执行性
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        problems.append("scripts/ 目录缺失")
    else:
        py_files = [f for f in os.listdir(scripts_dir) if f.endswith(".py")]
        if not py_files:
            problems.append("scripts/ 无 .py 脚本")
        for f in py_files:
            p = os.path.join(scripts_dir, f)
            if not os.access(p, os.X_OK):
                problems.append(f"scripts/{f} 不可执行（缺 +x）")

    # references/ 目录
    refs_dir = os.path.join(skill_dir, "references")
    if not os.path.isdir(refs_dir):
        problems.append("references/ 目录缺失（参考文档目录）")

    # references/iam-policies.md
    iam_path = os.path.join(skill_dir, "references", "iam-policies.md")
    if not os.path.exists(iam_path):
        problems.append("references/iam-policies.md 缺失（华为云 skill 必须有 IAM 权限说明）")

    # CLI 模式时检查 cli-installation-guide.md
    content, fm, body, exists = _read_skill_md(skill_dir)
    if exists and re.search(r'hcloud\s+', content):
        cli_guide = os.path.join(skill_dir, "references", "cli-installation-guide.md")
        if not os.path.exists(cli_guide):
            problems.append("references/cli-installation-guide.md 缺失（CLI 模式的 skill 必须有 CLI 安装指南）")

    # templates/test-vars.json 推荐存在
    test_vars = os.path.join(skill_dir, "templates", "test-vars.json")
    if not os.path.exists(test_vars):
        problems.append("templates/test-vars.json 缺失（推荐：测试用例文件）")

    # 文件扩展名白名单
    allowed = {
        ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
        ".js", ".ts", ".py", ".sh", ".go", ".rs", ".java",
        ".sql", ".csv", ".ini", ".cfg", ".conf", ".xml",
        ".html", ".css", ".svg",
    }
    for root, dirs, files in os.walk(skill_dir):
        for f in files:
            _, ext = os.path.splitext(f)
            ext = ext.lower()
            if ext and ext not in allowed:
                problems.append(f"文件 {f} 扩展名 {ext} 不在白名单内")
            elif not ext and not f.startswith("."):
                problems.append(f"文件 {f} 无扩展名（不在白名单内）")

    return problems


def check_one_pr_one_skill(skill_dir: str) -> list:
    """一 PR 一 Skill 校验：skills/ 目录下只能有一个 skill 子目录改动。
    """
    problems = []
    parent = os.path.dirname(skill_dir.rstrip("/"))
    if os.path.basename(parent) != "skills":
        return problems
    siblings = [d for d in os.listdir(parent)
                if os.path.isdir(os.path.join(parent, d))
                and not d.startswith(".")
                and d != os.path.basename(skill_dir.rstrip("/"))]
    if siblings:
        problems.append(f"skills/ 下有多个 skill 目录：{[os.path.basename(skill_dir.rstrip('/'))] + siblings}——一个 PR 只应改一个 skill")
    return problems


def check_content(skill_dir: str) -> list:
    """内容规范检查——合并 SKILL.md 正文相关检查。

    覆盖：
    - 必填段落（Overview/Prerequisites/Workflow/Core Commands/Parameter Confirmation/Reference Documents）
    - 跨 skill 引用（../xxx/SKILL.md 或 .agents/skills/）
    - CLI 命令格式（Service 大写/Operation PascalCase/--cli-region）
    - references 文件 kebab-case 命名
    - Markdown 风格（MD013/MD036/MD031）
    - 包大小限制（SKILL.md ≤500 行 / 目录 ≤5MB / 文件数 ≤30）
    """
    problems = []
    content, fm, body, exists = _read_skill_md(skill_dir)
    if not exists:
        return problems

    # 必填段落
    required_sections = [
        ("Overview|概述", "Overview / 概述"),
        ("Prerequisites|前置条件|前置", "Prerequisites / 前置条件"),
        ("Workflow|工作流|流程", "Workflow / 工作流"),
        ("Core Commands|核心命令", "Core Commands / 核心命令"),
        ("Parameter Confirmation|参数确认|参数表", "Parameter Confirmation / 参数确认"),
        ("Reference Documents|参考文档|## 参考", "Reference Documents / 参考文档"),
    ]
    for pattern, label in required_sections:
        if not re.search(pattern, content, re.I):
            problems.append(f"SKILL.md 缺必填段落: {label}")

    # 跨 skill 引用
    if re.search(r'\.\./[^[:space:]`]+/(SKILL\.md|scripts/)', content):
        problems.append("SKILL.md 包含跨目录 skill 路径引用（../xxx/SKILL.md）——skill 应自包含")
    if re.search(r'\.agents/skills/[^\s`]+', content):
        problems.append("SKILL.md 包含 .agents/skills/ 路径引用——skill 应自包含")

    # CLI 命令格式
    commands = re.findall(r'hcloud\s+([A-Za-z]+)\s+([A-Za-z]+)', content)
    for service, operation in commands:
        if service.startswith("<") or service.startswith("{"):
            continue
        if not service[0].isupper():
            problems.append(f"CLI 命令 Service '{service}' 应首字母大写")
        if not operation[0].isupper():
            problems.append(f"CLI 命令 Operation '{operation}' 应 PascalCase")
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        if re.search(r'hcloud\s+[A-Za-z]+\s+[A-Za-z]+', line):
            if "--help" not in line and "<" not in line and "{" not in line:
                if "--cli-region" not in line:
                    problems.append(f"CLI 命令（行 {i}）缺 --cli-region 参数")

    # references kebab-case
    refs_dir = os.path.join(skill_dir, "references")
    if os.path.isdir(refs_dir):
        for f in os.listdir(refs_dir):
            if f.endswith(".md"):
                name = f[:-3]
                if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
                    problems.append(f"references 文件 '{f}' 不符合 kebab-case 命名")

    # Markdown 风格
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        in_code_block = stripped.startswith("```") or stripped.startswith("|")
        if not in_code_block and len(line.rstrip("\n")) > 200 and not stripped.startswith("http"):
            problems.append(f"MD013 行 {i}: 行长度 {len(line.rstrip())} 超限（≤200 字符）")
        if re.match(r"^\*\*[^*]+\*\*$", stripped) and not stripped.startswith("|"):
            problems.append(f"MD036 行 {i}: 加粗文本当标题，改用 ### 标题")
    prev_blank = True
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            if not prev_blank and i > 1:
                problems.append(f"MD031 行 {i}: 代码块前缺空行")
        prev_blank = line.strip() == ""

    # 包大小限制
    md_path = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(md_path):
        with open(md_path, encoding="utf-8", errors="ignore") as f:
            md_lines = sum(1 for _ in f)
        if md_lines > 500:
            problems.append(f"SKILL.md {md_lines} 行超限（≤500 行，超限拆分到 references/）")
    total_size = 0
    file_count = 0
    for root, dirs, files in os.walk(skill_dir):
        for f in files:
            fp = os.path.join(root, f)
            total_size += os.path.getsize(fp)
            file_count += 1
    if total_size > 5 * 1024 * 1024:
        problems.append(f"skill 目录 {total_size // 1024}KB 超限（≤5MB）")
    if file_count > 30:
        problems.append(f"文件数 {file_count} 超限（≤30）")

    return problems


def check_no_mock(skill_dir: str) -> list:
    """Mock 模式检查——skill 脚本严禁包含 mock 模式/假数据。

    检查 scripts/*.py 中是否存在 --mock 参数、MOCK_DATA、mock 相关代码。
    SKILL.md 中的检查排除否定语境（"不使用 mock"/"严禁 mock"/"no mock" 等）。
    """
    problems = []
    scripts_dir = os.path.join(skill_dir, "scripts")
    # mock 代码模式（用于检查 .py 脚本）
    mock_code_patterns = [
        (r'--mock\b', "--mock 参数"),
        (r'MOCK_DATA', "MOCK_DATA 常量"),
        (r'def\s+mock_(list|data|output|get|fetch|create|run)\s*\(', "mock 数据生成函数"),
        (r'fake_data|mock_data|simulated_data', "假数据变量"),
    ]
    # SKILL.md 检查模式——排除否定语境
    mock_md_patterns = [
        (r'--mock\b', "--mock 参数"),
        (r'MOCK_DATA', "MOCK_DATA 常量"),
        (r'fake_data|simulated_data', "假数据变量"),
    ]
    # 否定语境关键词——出现这些词时跳过该行的 mock 检查
    negate_patterns = [
        r'不使用\s*mock', r'严禁\s*mock', r'禁止\s*mock',
        r'no\s+mock', r'without\s+mock', r'不包含\s*mock',
        r'should\s+not.*mock', r'must\s+not.*mock',
    ]
    # 检查 Python 脚本（严格检查，不排除否定语境）
    if os.path.isdir(scripts_dir):
        for f in os.listdir(scripts_dir):
            if not f.endswith(".py"):
                continue
            p = os.path.join(scripts_dir, f)
            content = open(p, encoding="utf-8", errors="ignore").read()
            for pattern, label in mock_code_patterns:
                if re.search(pattern, content, re.I):
                    problems.append(f"scripts/{f}: 包含 {label}（skill 脚本严禁 mock 模式）")
                    break
    # 检查 SKILL.md（排除否定语境）
    md_path = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(md_path):
        content = open(md_path, encoding="utf-8", errors="ignore").read()
        lines = content.split("\n")
        for i, line in enumerate(lines):
            # 跳过否定语境行
            if any(re.search(np, line, re.I) for np in negate_patterns):
                continue
            for pattern, label in mock_md_patterns:
                if re.search(pattern, line, re.I):
                    problems.append(f"SKILL.md 行 {i+1}: 包含 {label}（skill 严禁 mock 模式）")
                    break
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python3 spec_check.py <skill_dir>")
        return 2
    skill_dir = sys.argv[1]
    if not os.path.isdir(skill_dir):
        print(f"🔴 目录不存在: {skill_dir}")
        return 1
    all_problems = (
        check_frontmatter(skill_dir)
        + check_structure(skill_dir)
        + check_one_pr_one_skill(skill_dir)
        + check_content(skill_dir)
        + check_no_mock(skill_dir)
    )
    if all_problems:
        print("🔴 规范检查失败:")
        for p in all_problems:
            print(f"  - {p}")
        return 1
    print("✅ 规范检查通过（frontmatter 完整 / 文件结构 OK）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
