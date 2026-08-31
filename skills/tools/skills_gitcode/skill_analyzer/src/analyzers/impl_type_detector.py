import re
from pathlib import Path
from typing import List

from src.infra.file_utils import find_files, read_file_content
from src.infra.logger import get_step_logger
from src.models.enums import ImplType
from src.models.skill import SkillImplResult
from src.resources.skills_repo_manager import SkillsRepoManager


CLI_PATTERNS = [
    re.compile(r"hcloud\s+\w+", re.IGNORECASE),
    re.compile(r"subprocess\.\w+.*hcloud", re.IGNORECASE),
    re.compile(r"os\.system.*hcloud", re.IGNORECASE),
    re.compile(r"os\.popen.*hcloud", re.IGNORECASE),
]

SDK_PATTERNS = [
    re.compile(r"from\s+huaweicloudsdk\w+", re.IGNORECASE),
    re.compile(r"import\s+huaweicloudsdk\w+", re.IGNORECASE),
    re.compile(r"\w+Client\(", re.IGNORECASE),
    re.compile(r"ClientBuilder\(", re.IGNORECASE),
]

SHELL_CLI_PATTERN = re.compile(r"hcloud\s+\w+", re.IGNORECASE)


class ImplTypeDetector:
    def __init__(self):
        self.log = get_step_logger("ImplTypeDetector")

    def detect(self, skill_path: str) -> SkillImplResult:
        path = Path(skill_path)
        skill_name = path.name

        cli_evidence: List[str] = []
        sdk_evidence: List[str] = []

        for file_path in find_files(path):
            content = read_file_content(file_path)
            if not content:
                continue

            rel = str(file_path.relative_to(path))

            if file_path.suffix == ".py":
                for pattern in CLI_PATTERNS:
                    for match in pattern.finditer(content):
                        cli_evidence.append(f"{rel}: {match.group()}")

                for pattern in SDK_PATTERNS:
                    for match in pattern.finditer(content):
                        sdk_evidence.append(f"{rel}: {match.group()}")

            elif file_path.suffix == ".sh":
                for match in SHELL_CLI_PATTERN.finditer(content):
                    cli_evidence.append(f"{rel}: {match.group()}")

        cli_evidence = list(dict.fromkeys(cli_evidence))
        sdk_evidence = list(dict.fromkeys(sdk_evidence))

        has_cli = len(cli_evidence) > 0
        has_sdk = len(sdk_evidence) > 0

        if has_cli and has_sdk:
            impl_type = ImplType.HYBRID
        elif has_cli:
            impl_type = ImplType.KOOCLI
        elif has_sdk:
            impl_type = ImplType.SDK
        else:
            impl_type = ImplType.UNKNOWN

        business_goal = self._extract_business_goal(path)

        self.log.info(
            f"Detected: {skill_name} -> {impl_type.value} "
            f"(CLI evidence: {len(cli_evidence)}, SDK evidence: {len(sdk_evidence)})"
        )

        return SkillImplResult(
            skill_name=skill_name,
            implementation_type=impl_type,
            source_path=str(path),
            business_goal=business_goal,
            cli_evidence=cli_evidence,
            sdk_evidence=sdk_evidence,
        )

    def _extract_business_goal(self, skill_path: Path) -> str:
        skill_md = skill_path / "SKILL.md"
        if skill_md.exists():
            content = read_file_content(skill_md)
            if content:
                desc = SkillsRepoManager._parse_front_matter_description(content)
                if desc:
                    return desc
        repo_manager = SkillsRepoManager(str(skill_path.parent))
        config = repo_manager.read_skill_config(skill_path.name)
        return config.get("description", "")