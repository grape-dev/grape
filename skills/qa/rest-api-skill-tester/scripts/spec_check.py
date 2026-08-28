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
    - name/description 必填
    - triggers 可选（缺失警告不失败；有 triggers 字段或 description 含 "Triggers include:" 即合规）
    - name 小写连字符格式
    - name 与目录名一致性
    - version 语义化版本号格式
    - tags ≤5 个
    - 命名规范（小写连字符，不强制特定前缀）
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

    # triggers 检查（可选：有 triggers 字段或 description 含 "Triggers include:" 即合规，缺失只警告）
    has_triggers = "triggers" in fm.lower() or "triggers include" in content[:500].lower()
    if not has_triggers:
        print("⚠️ 缺 triggers（触发词）——可选字段，建议补充以支持 ClawHub 搜索匹配")

    # name 格式 + 目录名一致性
    m = re.search(r"^name:\s*([^\n]+)", fm, re.M)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            problems.append(f"name 格式错（须小写连字符）: {name}")
        dir_name = os.path.basename(skill_dir.rstrip("/"))
        if name != dir_name:
            problems.append(f"frontmatter name '{name}' 不匹配目录名 '{dir_name}'")
        # 命名规范：skill 目录名应为小写连字符（不强制特定前缀）

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
    - scripts/ 目录或根目录入口文件存在（.py 或 .sh 均可）
    - SKILL.md 存在（frontmatter 检查也查，但这里作为结构检查的一部分）
    - references/ 目录存在
    - references/api-reference.md 可选（REST API skill 需要，工具 skill 不需要）
    - templates/test-vars.json 可选（REST API skill 需要，工具 skill 不需要）
    - 文件扩展名白名单
    """
    problems = []

    # 入口文件：scripts/ 目录或根目录（.py 或 .sh，排除 __init__.py / setup.py / conftest.py）
    scripts_dir = os.path.join(skill_dir, "scripts")
    entry_exts = (".py", ".sh")
    root_entry = [
        f for f in os.listdir(skill_dir)
        if f.endswith(entry_exts) and f not in ("__init__.py", "setup.py", "conftest.py")
    ]
    if not os.path.isdir(scripts_dir) and not root_entry:
        problems.append("无入口文件（scripts/*.py 或 scripts/*.sh 或根目录入口）")
    elif os.path.isdir(scripts_dir):
        entry_files = [f for f in os.listdir(scripts_dir) if f.endswith(entry_exts)]
        if not entry_files:
            problems.append("scripts/ 无入口文件（.py 或 .sh）")
        for f in entry_files:
            p = os.path.join(scripts_dir, f)
            if not os.access(p, os.X_OK):
                problems.append(f"scripts/{f} 不可执行（缺 +x）")

    # references/ 目录
    refs_dir = os.path.join(skill_dir, "references")
    if not os.path.isdir(refs_dir):
        problems.append("references/ 目录缺失（参考文档目录）")

    # references/api-reference.md（可选：REST API skill 需要，工具 skill 不需要）
    api_ref_path = os.path.join(skill_dir, "references", "api-reference.md")
    if not os.path.exists(api_ref_path):
        print("⚠️ references/api-reference.md 缺失——REST API skill 需要此文件，工具 skill 可省略")

    # templates/test-vars.json（可选：REST API skill 需要，工具 skill 不需要）
    test_vars = os.path.join(skill_dir, "templates", "test-vars.json")
    if not os.path.exists(test_vars):
        print("⚠️ templates/test-vars.json 缺失——REST API skill 需要此文件，工具 skill 可省略")

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

    本地 worktree 中可能存在多个已合并的 skill 目录（环境限制），
    此检查降级为警告——PR diff 实际只改一个目录即可。
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
        print(f"⚠️ skills/ 下有多个 skill 目录（{len(siblings)+1} 个）——worktree 环境限制，PR diff 实际只改一个目录即可")
    return problems


def check_content(skill_dir: str) -> list:
    """内容规范检查——合并 SKILL.md 正文相关检查。

    覆盖：
    - 必填段落（场景/知识/步骤）——对齐 references/skill-spec.md 规范
    - 可选段落（判断标准/输出规范/参考文档）——缺失警告不失败
    - 跨 skill 引用（../xxx/SKILL.md 或 .agents/skills/）
    - references 文件 kebab-case 命名
    - Markdown 风格（MD013/MD036/MD031）
    - 包大小限制（SKILL.md ≤500 行 / 目录 ≤5MB / 文件数 ≤50）
    """
    problems = []
    content, fm, body, exists = _read_skill_md(skill_dir)
    if not exists:
        return problems

    # 必填段落（缺失 = 失败）——段落名支持中英文别名
    required_sections = [
        ("场景|When to Use|Scenario", "场景 / When to Use"),
        ("知识|Knowledge", "知识 / Knowledge"),
        ("步骤|Steps|Workflow|工作流|流程", "步骤 / Steps"),
    ]
    for pattern, label in required_sections:
        if not re.search(pattern, content, re.I):
            problems.append(f"SKILL.md 缺必填段落: {label}")

    # 系统级 skill 必填段落（检测到"安全硬约束"段落 → 系统级 → 额外检查）
    is_system_skill = bool(re.search(r"安全硬约束|Security.*Constraint", content, re.I))
    if is_system_skill:
        system_required = [
            ("评论规范|Comment.*Rule", "评论规范"),
            ("^##.*Configuration|^##.*配置", "Configuration"),
            ("分析结论示例|Analysis.*Conclusion", "分析结论示例"),
            ("任务结束", "任务结束"),
        ]
        for pattern, label in system_required:
            if not re.search(pattern, content, re.I | re.M):
                problems.append(f"系统级 skill 缺必填段落: {label}")

    # 可选段落（缺失 = 警告，不失败）
    optional_sections = [
        ("判断标准|Verification", "判断标准 / Verification"),
        ("输出规范|Output Format", "输出规范 / Output Format"),
        ("参考文档|References|Reference Documents|## 参考", "参考文档 / References"),
    ]
    optional_warnings = []
    for pattern, label in optional_sections:
        if not re.search(pattern, content, re.I):
            optional_warnings.append(f"⚠️ SKILL.md 缺可选段落: {label}（不阻塞，建议补充）")

    # 打印可选段落警告（不加入 problems，不影响 exit code）
    for w in optional_warnings:
        print(w)

    # 跨 skill 引用
    if re.search(r'\.\./[^[:space:]`]+/(SKILL\.md|scripts/)', content):
        problems.append("SKILL.md 包含跨目录 skill 路径引用（../xxx/SKILL.md）——skill 应自包含")
    if re.search(r'\.agents/skills/[^\s`]+', content):
        problems.append("SKILL.md 包含 .agents/skills/ 路径引用——skill 应自包含")

    lines = content.split("\n")

    # references kebab-case
    refs_dir = os.path.join(skill_dir, "references")
    if os.path.isdir(refs_dir):
        for f in os.listdir(refs_dir):
            if f.endswith(".md"):
                name = f[:-3]
                if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
                    problems.append(f"references 文件 '{f}' 不符合 kebab-case 命名")

    # Markdown 风格——用 in_code_block 状态跟踪，代码块内不检查 MD013/MD036
    in_code_block = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        # MD013: 行长度 ≤200（排除表格行和 URL 行）
        if not stripped.startswith("|") and len(line.rstrip("\n")) > 200 and not stripped.startswith("http"):
            problems.append(f"MD013 行 {i}: 行长度 {len(line.rstrip())} 超限（≤200 字符）")
        # MD036: 加粗文本当标题
        if re.match(r"^\*\*[^*]+\*\*$", stripped) and not stripped.startswith("|"):
            problems.append(f"MD036 行 {i}: 加粗文本当标题，改用 ### 标题")
    # MD031: 代码块开始标记前必须有空行（结束标记前不要求）
    in_code_block = False
    prev_blank = True
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code_block:
                # 代码块开始——检查前一行是否为空
                if not prev_blank and i > 1:
                    problems.append(f"MD031 行 {i}: 代码块前缺空行")
                in_code_block = True
            else:
                # 代码块结束——不检查前一行
                in_code_block = False
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
    if file_count > 50:
        problems.append(f"文件数 {file_count} 超限（≤50）")

    return problems


def check_no_mock(skill_dir: str) -> list:
    """Mock 模式检查——skill 脚本严禁包含 mock 模式/假数据。

    检查 scripts/*.py 和根目录入口 .py 中是否存在 --mock 参数、MOCK_DATA、mock 相关代码。
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
    # 扫描 scripts/ 目录 + 根目录入口 .py（排除 __init__.py / setup.py / conftest.py）
    py_files_to_check = []
    if os.path.isdir(scripts_dir):
        for f in os.listdir(scripts_dir):
            if f.endswith(".py") and f != "spec_check.py":
                py_files_to_check.append((os.path.join(scripts_dir, f), f"scripts/{f}"))
    for f in os.listdir(skill_dir):
        if f.endswith(".py") and f not in ("__init__.py", "setup.py", "conftest.py"):
            py_files_to_check.append((os.path.join(skill_dir, f), f))
    for p, label in py_files_to_check:
        content = open(p, encoding="utf-8", errors="ignore").read()
        for pattern, pat_label in mock_code_patterns:
            if re.search(pattern, content, re.I):
                problems.append(f"{label}: 包含 {pat_label}（skill 脚本严禁 mock 模式）")
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


def check_generation_quality(skill_dir: str) -> list:
    """生成质量检查——验证生成物符合 rest-api-skill-creator 规范。

    只检查生成物（有 references/api-reference.md 或 templates/test-vars.json 的 skill），
    工具 skill（creator/tester 自身，无这些文件）跳过。

    覆盖：
    - 系统级 skill Configuration 段落含配置指导（环境变量 + 项目知识 JSON 方式）
    - 安全硬约束不含 GitCode（生成的 skill 管理目标 API，不调 GitCode）
    - SKILL.md 和 api-reference.md 不含硬编码 IP 地址
    - 脚本用 requests 不用 urllib
    - requirements.txt 含 requests 声明
    - test-vars.json 字段完整性（name/command/method/category/description）
    """
    problems = []

    # 只检查生成物——有 api-reference.md 或 test-vars.json 的 skill
    api_ref_path = os.path.join(skill_dir, "references", "api-reference.md")
    test_vars_path = os.path.join(skill_dir, "templates", "test-vars.json")
    if not os.path.exists(api_ref_path) and not os.path.exists(test_vars_path):
        return problems  # 工具 skill（creator/tester 自身），跳过

    content, fm, body, exists = _read_skill_md(skill_dir)
    if not exists:
        return problems

    is_system_skill = bool(re.search(r"安全硬约束|Security.*Constraint", content, re.I))

    # 1. 系统级 skill Configuration 段落含配置指导
    if is_system_skill:
        if not re.search(r"配置指导", content, re.I):
            problems.append("系统级 skill Configuration 段落缺'配置指导'子段（环境变量方式 + 项目知识 JSON 方式）")

    # 2. 安全硬约束不含 GitCode（生成的 skill 管理目标 API）
    # 提取安全硬约束段落内容
    sec_match = re.search(r"##\s*安全硬约束(.*?)(?=##\s)", content, re.S | re.I)
    if sec_match:
        sec_content = sec_match.group(1)
        # 排除否定语境（"不调 GitCode" 等）
        if re.search(r"GitCode", sec_content, re.I) and not re.search(r"不.*GitCode|禁止.*GitCode", sec_content, re.I):
            problems.append("安全硬约束包含 GitCode 相关内容——生成的 skill 管理目标 API，不应包含 GitCode 约束")

    # 3. SKILL.md 不含硬编码 IP 地址（排除占位符和示例标注）
    # 匹配 http://数字.数字.数字.数字 或 https://数字.数字.数字.数字
    ip_pattern = r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    for i, line in enumerate(content.split("\n"), 1):
        # 跳过占位符行和示例标注行
        if re.search(r'\{base_url\}|占位符|示例|<host>|<port>', line, re.I):
            continue
        if re.search(ip_pattern, line):
            problems.append(f"SKILL.md 行 {i}: 疑似硬编码 IP 地址（应使用 {{base_url}} 占位符）")
            break

    # api-reference.md 也不含硬编码 IP
    api_ref_path = os.path.join(skill_dir, "references", "api-reference.md")
    if os.path.exists(api_ref_path):
        api_content = open(api_ref_path, encoding="utf-8", errors="ignore").read()
        for i, line in enumerate(api_content.split("\n"), 1):
            if re.search(r'\{base_url\}|占位符|示例|<host>|<port>', line, re.I):
                continue
            if re.search(ip_pattern, line):
                problems.append(f"references/api-reference.md 行 {i}: 疑似硬编码 IP 地址（应使用 {{base_url}} 占位符）")
                break

    # 4. 脚本用 requests 不用 urllib
    scripts_dir = os.path.join(skill_dir, "scripts")
    py_files = []
    if os.path.isdir(scripts_dir):
        py_files = [os.path.join(scripts_dir, f) for f in os.listdir(scripts_dir)
                     if f.endswith(".py") and f != "spec_check.py"]
    for p in py_files:
        script_content = open(p, encoding="utf-8", errors="ignore").read()
        if re.search(r'import\s+urllib', script_content):
            problems.append(f"scripts/{os.path.basename(p)}: 使用了 urllib（应使用 requests 库）")
        if not re.search(r'import\s+requests', script_content):
            problems.append(f"scripts/{os.path.basename(p)}: 未 import requests（脚本必须用 requests 库调 REST API）")

    # 5. requirements.txt 含 requests 声明
    req_path = os.path.join(skill_dir, "requirements.txt")
    if os.path.exists(req_path):
        req_content = open(req_path, encoding="utf-8", errors="ignore").read()
        if not re.search(r'^\s*requests\s*>=', req_content, re.M):
            problems.append("requirements.txt 未声明 requests>=2.20.0（脚本依赖 requests 库）")
        # 检查真实换行符（不是字面量 \n）
        if b"\\n" in open(req_path, "rb").read():
            problems.append("requirements.txt 包含字面量 \\n（应使用真实换行符）")
    else:
        # 没有 requirements.txt 但有 .py 脚本 → 需要声明依赖
        if py_files:
            problems.append("requirements.txt 缺失（脚本依赖 requests 库，需声明）")

    # 6. test-vars.json 字段完整性
    test_vars_path = os.path.join(skill_dir, "templates", "test-vars.json")
    if os.path.exists(test_vars_path):
        try:
            import json as _json
            tv = _json.loads(open(test_vars_path, encoding="utf-8").read())
            cases = tv.get("test_cases", [])
            required_fields = ["id", "name", "command", "method", "category", "description"]
            for i, tc in enumerate(cases):
                missing = [f for f in required_fields if f not in tc]
                if missing:
                    problems.append(f"test-vars.json 用例 {i+1} 缺字段: {', '.join(missing)}（必须含 id/name/command/method/category/description）")
            # 检查不含敏感字段（base_url/cred_prefix/auth_type 不应出现在 test-vars.json）
            sensitive_fields = ["base_url", "cred_prefix", "auth_type", "base_url_env"]
            found_sensitive = [f for f in sensitive_fields if f in tv]
            if found_sensitive:
                problems.append(f"test-vars.json 含敏感字段: {', '.join(found_sensitive)}（base_url/cred_prefix/auth_type 不应写入，从环境变量/项目知识获取）")
        except Exception as e:
            problems.append(f"test-vars.json 解析失败: {e}")

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
        + check_generation_quality(skill_dir)
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
