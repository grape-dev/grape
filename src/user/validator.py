"""自定义技能校验 — YAML Front Matter、Markdown 章节、文件名检查

安全策略（2026-08 调整，见 obs-skill-storage-design.md §6.1）：
- 官方技能规范（阿里云/Anthropic/OpenAI）支持 scripts/ 文本脚本；
  opencode agent 本身具备执行 shell 命令能力，文本脚本不扩大风险边界。
- 因此放开文本脚本（.py/.sh/.js/.ts 等），仅禁止二进制可执行/平台原生
  文件（.exe/.so/.dll/.bin/.jar/.class/.bat），防原生代码注入。
"""
import re, os, yaml

# 二进制可执行/平台原生 —— 禁止上传（防原生代码注入）
FORBIDDEN_EXTS = {".exe", ".bat", ".jar", ".class", ".so", ".dll", ".bin", ".msi", ".apk"}
# 文本脚本（官方规范 scripts/ 目录支持，zip 化保留权限位 + worker 物化恢复）
SCRIPT_EXTS = {".py", ".sh", ".js", ".ts", ".pl", ".rb", ".go", ".rs"}
ALLOWED_REF_EXTS = {".md", ".txt", ".json", ".yaml", ".yml"}
REQUIRED_SECTIONS = ["## 场景", "## 知识", "## 步骤"]
PROJECT_SKILLS = {"pr", "issue", "implementation", "build-debug", "deploy-guard",
                  "incident-triage", "changelog-gen", "chat", "cloud-deploy", "im-notify", "agent"}


def parse_skill_frontmatter(skill_dir: str) -> dict:
    """解析 SKILL.md 的 YAML Front Matter，返回 {name, description, version}（缺失字段为空串）"""
    result = {"name": "", "description": "", "version": ""}
    skill_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(skill_path):
        return result
    try:
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        parts = content.split("---", 2)
        if len(parts) < 3:
            return result
        fm = yaml.safe_load(parts[1])
        if isinstance(fm, dict):
            result["name"] = str(fm.get("name", ""))
            result["description"] = str(fm.get("description", "")).strip()
            result["version"] = str(fm.get("version", ""))
    except Exception:
        pass
    return result


def validate_skill(upload_dir: str, skill_name: str) -> list[str]:
    """
    校验自定义技能，返回错误列表。空列表表示通过。
    与官方技能格式保持一致：只要求 SKILL.md 存在 + Front Matter 必填字段，
    不强制特定正文章节（三章节为可选约定）。
    """
    errors = []

    # 1. 检查 SKILL.md 是否存在
    skill_path = os.path.join(upload_dir, "SKILL.md")
    if not os.path.exists(skill_path):
        return ["缺少 SKILL.md 文件"]

    with open(skill_path, encoding="utf-8") as f:
        content = f.read()

    # 2. 解析 Front Matter
    parts = content.split("---", 2)
    if len(parts) < 3:
        errors.append("缺少 YAML Front Matter（--- 分隔）")
        return errors

    try:
        fm = yaml.safe_load(parts[1])
    except Exception as e:
        errors.append(f"Front Matter 解析失败: {e}")
        return errors

    if not isinstance(fm, dict):
        errors.append("Front Matter 必须是一个对象")
        return errors

    name_val = str(fm.get("name", ""))
    if not re.match(r"^[a-z0-9-]+$", name_val):
        errors.append("name 字段仅允许小写字母、数字、连字符")
    if name_val != skill_name:
        errors.append(f"name（{name_val}）与目录名（{skill_name}）不一致")

    version = str(fm.get("version", "")).strip()
    # version 为可选字段（官方 Agent Skills 规范未强制，如阿里云技能无 version）：
    # 缺失时默认 1.0.0 不报错；有则要求语义化版本号（x.y.z，兼容 x.y 与 v 前缀）
    if version and not re.match(r"^v?\d+\.\d+(\.\d+)?$", version):
        errors.append("version 字段必须符合语义化版本号格式（x.y.z）")

    desc = str(fm.get("description", "")).strip()
    if not desc:
        errors.append("description 字段不能为空")
    elif len(desc) > 500:
        errors.append("description 字段不超过 500 字")

    if name_val in PROJECT_SKILLS:
        errors.append(f"技能名「{name_val}」与项目级技能冲突，请换一个名称")

    # 3. 检查禁止文件（仅禁二进制可执行；文本脚本 .py/.sh 等放行——官方规范 scripts/ 支持）
    for root, dirs, files in os.walk(upload_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in FORBIDDEN_EXTS:
                errors.append(f"不允许上传可执行文件: {f}")
                return errors
            if ext not in ALLOWED_REF_EXTS and ext not in SCRIPT_EXTS and ext not in ("", ".md", ".yaml", ".yml", ".json", ".txt"):
                errors.append(f"不支持的文件类型: {f}")

    return errors
