from pathlib import Path
from typing import Dict, List, Optional

from src.infra.file_utils import find_files, list_subdirectories, read_file_content


class SkillsRepoManager:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def list_skill_names(self) -> List[str]:
        if not self.repo_path.exists():
            return []
        subdirs = list_subdirectories(self.repo_path)
        return [d.name for d in subdirs]

    def discover_all_skills(self) -> Dict[str, Path]:
        result = {}
        if not self.repo_path.exists():
            return result
        for d in self.repo_path.rglob("*"):
            if d.is_dir() and (d / "SKILL.md").exists():
                result[d.name] = d
        if not result:
            for d in list_subdirectories(self.repo_path):
                result[d.name] = d
        return result

    def list_skills(self) -> List[Path]:
        if not self.repo_path.exists():
            return []
        return list_subdirectories(self.repo_path)

    def get_skill_path(self, skill_name: str) -> Optional[Path]:
        skill_path = self.repo_path / skill_name
        if skill_path.exists() and skill_path.is_dir():
            return skill_path
        return None

    def read_skill_files(self, skill_name: str) -> Dict[str, str]:
        skill_path = self.get_skill_path(skill_name)
        if skill_path is None:
            return {}
        result = {}
        for file_path in find_files(skill_path):
            content = read_file_content(file_path)
            if content:
                rel_path = str(file_path.relative_to(skill_path))
                result[rel_path] = content
        return result

    def read_skill_config(self, skill_name: str) -> Dict[str, str]:
        skill_path = self.get_skill_path(skill_name)
        if skill_path is None:
            return {}

        configs = {}

        skill_md_path = skill_path / "SKILL.md"
        if skill_md_path.exists():
            content = read_file_content(skill_md_path)
            if content:
                desc = self._parse_front_matter_description(content)
                if desc:
                    configs["description"] = desc
                    configs["source"] = "SKILL.md"
                    return configs

        readme_path = skill_path / "README.md"
        if readme_path.exists():
            content = read_file_content(readme_path)
            if content:
                lines = content.strip().splitlines()
                desc_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if stripped:
                        desc_lines.append(stripped)
                    elif desc_lines:
                        break
                if desc_lines:
                    configs["description"] = " ".join(desc_lines)
                    configs["source"] = "README.md"
                    return configs

        for config_file in ["metadata.yaml", "metadata.yml", "metadata.json"]:
            config_path = skill_path / config_file
            if config_path.exists():
                content = read_file_content(config_path)
                if content:
                    try:
                        if config_file.endswith(".json"):
                            import json
                            data = json.loads(content)
                        else:
                            import yaml
                            data = yaml.safe_load(content)
                        if isinstance(data, dict) and "description" in data:
                            configs["description"] = str(data["description"])
                            configs["source"] = config_file
                            return configs
                    except Exception:
                        pass

        for config_file in ["skill.yaml", "skill.yml", "skill.json"]:
            config_path = skill_path / config_file
            if config_path.exists():
                content = read_file_content(config_path)
                if content:
                    try:
                        if config_file.endswith(".json"):
                            import json
                            data = json.loads(content)
                        else:
                            import yaml
                            data = yaml.safe_load(content)
                        if isinstance(data, dict) and "description" in data:
                            configs["description"] = str(data["description"])
                            configs["source"] = config_file
                            return configs
                    except Exception:
                        pass

        return configs

    @staticmethod
    def _parse_front_matter_description(content: str) -> str:
        if not content.startswith("---"):
            return ""
        end = content.find("---", 3)
        if end == -1:
            return ""
        front_matter = content[3:end].strip()
        for line in front_matter.splitlines():
            stripped = line.strip()
            if stripped.startswith("description:"):
                desc_value = stripped[len("description:"):].strip()
                if desc_value.startswith("|"):
                    desc_lines = []
                    in_block = False
                    for fm_line in front_matter.splitlines():
                        if fm_line.strip().startswith("description:|") or fm_line.strip() == "description: |":
                            in_block = True
                            continue
                        if in_block:
                            if fm_line and not fm_line[0].isspace():
                                break
                            desc_lines.append(fm_line.strip())
                    return " ".join(desc_lines).strip() if desc_lines else ""
                if desc_value.startswith('"') and desc_value.endswith('"'):
                    return desc_value[1:-1]
                return desc_value
        return ""