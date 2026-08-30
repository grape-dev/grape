import ast
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.infra.file_utils import find_files, read_file_content
from src.infra.logger import get_step_logger
from src.models.enums import IsMandatory, ToolCategory
from src.models.tool_dependency import SkillToolDependency


class SkillToolAnalyzer:
    def __init__(self, cli_tool_path: str = ""):
        self.cli_tool_path = cli_tool_path
        self.log = get_step_logger("SkillToolAnalyzer")

    def analyze(self, skill_path: str) -> List[SkillToolDependency]:
        path = Path(skill_path)
        deps: Dict[str, SkillToolDependency] = {}

        self._analyze_skill_md(path, deps)
        self._analyze_python_sources(path, deps)
        self._analyze_shell_sources(path, deps)
        self._analyze_requirements(path, deps)
        self._resolve_versions(deps)
        self._enrich_descriptions(path, deps)
        self._cross_validate(path, deps)

        return sorted(deps.values(), key=lambda d: (d.category.value, d.name))

    def _analyze_skill_md(self, path: Path, deps: Dict[str, SkillToolDependency]):
        skill_md = path / "SKILL.md"
        if not skill_md.exists():
            return
        content = read_file_content(skill_md)
        if not content:
            return

        self._extract_from_front_matter(content, deps)
        self._extract_from_body(content, deps)

    def _extract_from_front_matter(self, content: str, deps: Dict[str, SkillToolDependency]):
        if not content.startswith("---"):
            return
        end = content.find("---", 3)
        if end == -1:
            return
        fm = content[3:end]

        for line in fm.splitlines():
            stripped = line.strip().lower()
            if "huaweicloudsdk" in stripped or "python sdk" in stripped:
                self._add_sdk_deps_from_text(fm, deps, "SKILL.md(front_matter)")
                break

    def _extract_from_body(self, content: str, deps: Dict[str, SkillToolDependency]):
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()

            sdk_matches = re.findall(r"huaweicloudsdk(\w+)", stripped)
            for svc in sdk_matches:
                pkg = f"huaweicloudsdk{svc}"
                key = f"sdk:{pkg}"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name=pkg, category=ToolCategory.SDK,
                        evidence_sources=["SKILL.md"],
                    )
                elif "SKILL.md" not in deps[key].evidence_sources:
                    deps[key].evidence_sources.append("SKILL.md")

            if "pip install" in stripped:
                pkgs = re.findall(r"pip install\s+([\w\-\.\[\]]+)", stripped)
                for pkg in pkgs:
                    pkg = re.sub(r"\[.*\]", "", pkg)
                    if pkg.startswith("huaweicloudsdk"):
                        key = f"sdk:{pkg}"
                        if key not in deps:
                            deps[key] = SkillToolDependency(
                                name=pkg, category=ToolCategory.SDK,
                                evidence_sources=["SKILL.md"],
                            )
                    elif pkg not in ("pip",):
                        key = f"lib:{pkg}"
                        if key not in deps:
                            deps[key] = SkillToolDependency(
                                name=pkg, category=ToolCategory.LIBRARY,
                                evidence_sources=["SKILL.md"],
                            )

            if re.search(r"\bhcloud\b", stripped):
                key = "tool:koocli"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name="KooCLI (hcloud)", category=ToolCategory.KOOCLI,
                        evidence_sources=["SKILL.md"],
                    )

            py_ver = re.search(r"Python\s*(\d+\.\d+)", stripped)
            if py_ver:
                key = "lang:python"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name="Python", category=ToolCategory.LANGUAGE,
                        version=py_ver.group(1),
                        evidence_sources=["SKILL.md"],
                    )

            jq_match = re.search(r"\bjq\b", stripped)
            if jq_match and "jq" in stripped.lower():
                key = "tool:jq"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name="jq", category=ToolCategory.TOOL,
                        evidence_sources=["SKILL.md"],
                    )

    def _add_sdk_deps_from_text(self, text: str, deps: Dict[str, SkillToolDependency], source: str):
        for pkg in re.findall(r"huaweicloudsdk(\w+)", text):
            full = f"huaweicloudsdk{pkg}"
            key = f"sdk:{full}"
            if key not in deps:
                deps[key] = SkillToolDependency(
                    name=full, category=ToolCategory.SDK,
                    evidence_sources=[source],
                )

    def _analyze_python_sources(self, path: Path, deps: Dict[str, SkillToolDependency]):
        for py_file in find_files(path, extensions={".py"}):
            content = read_file_content(py_file)
            if not content:
                continue
            rel = str(py_file.relative_to(path))
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self._process_import(alias.name, deps, rel)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        self._process_import(node.module, deps, rel)

    def _process_import(self, module_name: str, deps: Dict[str, SkillToolDependency], source: str):
        if module_name.startswith("huaweicloudsdk"):
            svc = module_name.split(".")[0]
            key = f"sdk:{svc}"
            if key not in deps:
                deps[key] = SkillToolDependency(
                    name=svc, category=ToolCategory.SDK,
                    evidence_sources=[source],
                )
            elif source not in deps[key].evidence_sources:
                deps[key].evidence_sources.append(source)
        elif module_name.startswith("subprocess") or module_name == "os":
            pass
        else:
            stdlib = {"sys", "os", "re", "json", "datetime", "pathlib", "typing", "collections", "argparse", "logging", "time", "math", "csv", "io", "hashlib", "base64", "urllib", "http", "socket", "threading", "queue", "shutil", "tempfile", "configparser", "enum", "dataclasses", "abc", "functools", "itertools", "copy", "struct", "xml", "html", "email", "ssl", "signal"}
            top = module_name.split(".")[0]
            if top in stdlib:
                return
            key = f"lib:{top}"
            if key not in deps:
                deps[key] = SkillToolDependency(
                    name=top, category=ToolCategory.LIBRARY,
                    evidence_sources=[source],
                )

    def _analyze_shell_sources(self, path: Path, deps: Dict[str, SkillToolDependency]):
        for sh_file in find_files(path, extensions={".sh"}):
            content = read_file_content(sh_file)
            if not content:
                continue
            rel = str(sh_file.relative_to(path))
            if re.search(r"\bhcloud\b", content):
                key = "tool:koocli"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name="KooCLI (hcloud)", category=ToolCategory.KOOCLI,
                        evidence_sources=[rel],
                    )
                elif rel not in deps[key].evidence_sources:
                    deps[key].evidence_sources.append(rel)
            if re.search(r"\bjq\b", content):
                key = "tool:jq"
                if key not in deps:
                    deps[key] = SkillToolDependency(
                        name="jq", category=ToolCategory.TOOL,
                        evidence_sources=[rel],
                    )

    def _analyze_requirements(self, path: Path, deps: Dict[str, SkillToolDependency]):
        for req_file in path.glob("requirements*.txt"):
            content = read_file_content(req_file)
            if not content:
                continue
            rel = str(req_file.relative_to(path))
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([\w\-\.\[\]]+)([><=!~].*)?", line)
                if m:
                    pkg = re.sub(r"\[.*\]", "", m.group(1))
                    ver_spec = (m.group(2) or "").strip()
                    if pkg.startswith("huaweicloudsdk"):
                        key = f"sdk:{pkg}"
                        if key not in deps:
                            deps[key] = SkillToolDependency(
                                name=pkg, category=ToolCategory.SDK,
                                version=ver_spec, evidence_sources=[rel],
                            )
                        else:
                            if ver_spec and not deps[key].version:
                                deps[key].version = ver_spec
                            if rel not in deps[key].evidence_sources:
                                deps[key].evidence_sources.append(rel)
                    else:
                        key = f"lib:{pkg}"
                        if key not in deps:
                            deps[key] = SkillToolDependency(
                                name=pkg, category=ToolCategory.LIBRARY,
                                version=ver_spec, evidence_sources=[rel],
                            )

    def _resolve_versions(self, deps: Dict[str, SkillToolDependency]):
        key = "lang:python"
        if key not in deps:
            deps[key] = SkillToolDependency(
                name="Python", category=ToolCategory.LANGUAGE,
                evidence_sources=["系统检测"],
            )
        if not deps[key].version or deps[key].version == "":
            import sys
            deps[key].version = f"{sys.version_info.major}.{sys.version_info.minor}"

        key = "tool:koocli"
        if key in deps and not deps[key].version:
            ver = self._get_koocli_version()
            if ver:
                deps[key].version = ver

    def _get_koocli_version(self) -> str:
        try:
            from src.resources.cli_tool_manager import CliToolManager
            mgr = CliToolManager(self.cli_tool_path)
            cli_path = mgr._get_cli_path()
            result = subprocess.run(
                [cli_path, "--version"], capture_output=True, text=True, timeout=10,
            )
            m = re.search(r"(\d+\.\d+[\.\d]*)", result.stdout + result.stderr)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    def _enrich_descriptions(self, path: Path, deps: Dict[str, SkillToolDependency]):
        skill_md = path / "SKILL.md"
        md_content = read_file_content(skill_md) or ""

        for key, dep in deps.items():
            if dep.category == ToolCategory.SDK:
                svc = dep.name.replace("huaweicloudsdk", "")
                dep.description = self._infer_sdk_description(svc, md_content)
                dep.is_mandatory = IsMandatory.MANDATORY
            elif dep.category == ToolCategory.KOOCLI:
                dep.description = "华为云命令行工具，用于通过CLI调用云服务API"
                dep.is_mandatory = IsMandatory.MANDATORY
            elif dep.category == ToolCategory.LANGUAGE:
                dep.description = "Skill实现语言及运行时环境"
                dep.is_mandatory = IsMandatory.MANDATORY
            elif dep.category == ToolCategory.TOOL:
                if dep.name == "jq":
                    dep.description = "JSON命令行处理工具，用于解析和过滤API返回的JSON数据"
                    dep.is_mandatory = IsMandatory.OPTIONAL
                else:
                    dep.description = f"辅助工具: {dep.name}"
                    dep.is_mandatory = IsMandatory.OPTIONAL
            elif dep.category == ToolCategory.LIBRARY:
                desc = self._infer_lib_description(dep.name, md_content)
                dep.description = desc
                dep.is_mandatory = IsMandatory.MANDATORY

    @staticmethod
    def _infer_sdk_description(service: str, md_content: str) -> str:
        svc_lower = service.lower()
        desc_map = {
            "eip": "弹性公网IP(EIP)服务SDK，用于查询和管理弹性公网IP",
            "ecs": "云服务器(ECS)服务SDK，用于创建、查询和管理云服务器",
            "evs": "云硬盘(EVS)服务SDK，用于管理云硬盘和卷",
            "vpc": "虚拟私有云(VPC)服务SDK，用于管理网络和子网",
            "iam": "身份与访问管理(IAM)服务SDK，用于用户和权限管理",
            "obs": "对象存储服务(OBS) SDK，用于对象存储操作",
            "ces": "云监控(CES)服务SDK，用于监控指标和告警",
            "elb": "弹性负载均衡(ELB)服务SDK，用于管理负载均衡器",
            "cce": "云容器引擎(CCE)服务SDK，用于管理Kubernetes集群",
            "bms": "裸金属服务器(BMS)服务SDK",
            "as": "弹性伸缩(AS)服务SDK",
            "nat": "NAT网关服务SDK",
            "functiongraph": "函数工作流(FunctionGraph)服务SDK",
            "swr": "容器镜像服务(SWR) SDK",
            "rms": "配置审计(RMS)服务SDK",
            "bss": "运营能力(BSS)服务SDK",
            "billing": "计费服务SDK",
            "enterprise": "企业管理服务SDK",
            "core": "华为云SDK核心库，提供认证、HTTP请求等基础能力",
        }
        if svc_lower in desc_map:
            return desc_map[svc_lower]
        for line in md_content.splitlines():
            if svc_lower in line.lower() and ("sdk" in line.lower() or "api" in line.lower()):
                clean = line.strip().lstrip("#*- ").strip()
                if len(clean) > 10 and len(clean) < 200:
                    return f"{service}服务SDK: {clean}"
        return f"{service}服务SDK，用于调用华为云{service}相关API"

    @staticmethod
    def _infer_lib_description(lib_name: str, md_content: str) -> str:
        desc_map = {
            "requests": "HTTP请求库，用于调用REST API",
            "jinja2": "模板引擎，用于生成HTML/JSON报告",
            "smtplib": "邮件发送库(标准库)，用于告警通知",
            "email": "邮件构建库(标准库)，用于告警通知",
            "argparse": "命令行参数解析(标准库)",
            "json": "JSON处理(标准库)",
            "csv": "CSV处理(标准库)",
            "datetime": "日期时间处理(标准库)",
            "logging": "日志记录(标准库)",
            "pathlib": "路径处理(标准库)",
            "re": "正则表达式(标准库)",
            "os": "操作系统接口(标准库)",
            "sys": "系统相关(标准库)",
            "subprocess": "子进程管理(标准库)，用于调用CLI命令",
            "yaml": "YAML解析库",
            "pyyaml": "YAML解析库",
            "openpyxl": "Excel文件处理库",
            "pandas": "数据分析库",
            "numpy": "数值计算库",
            "matplotlib": "图表绘制库",
            "flask": "Web框架",
            "django": "Web框架",
            "click": "命令行框架",
        }
        lower = lib_name.lower()
        if lower in desc_map:
            return desc_map[lower]
        return f"第三方库: {lib_name}"

    def _cross_validate(self, path: Path, deps: Dict[str, SkillToolDependency]):
        py_files = list(find_files(path, extensions={".py"}))
        sh_files = list(find_files(path, extensions={".sh"}))
        all_sources = [str(f.relative_to(path)) for f in py_files + sh_files]

        for key, dep in list(deps.items()):
            if dep.category == ToolCategory.SDK:
                confirmed = any(s.endswith(".py") for s in dep.evidence_sources)
                dep.is_confirmed = confirmed
            elif dep.category == ToolCategory.KOOCLI:
                confirmed = any(
                    "hcloud" in (read_file_content(path / s) or "")
                    for s in dep.evidence_sources
                    if s in all_sources
                )
                dep.is_confirmed = confirmed or bool(py_files)
            elif dep.category == ToolCategory.LIBRARY:
                dep.is_confirmed = any(s.endswith(".py") for s in dep.evidence_sources)
            else:
                dep.is_confirmed = True