from dataclasses import dataclass, field
from typing import List

from src.models.enums import IsMandatory, ToolCategory


@dataclass
class SkillToolDependency:
    name: str
    category: ToolCategory
    version: str = ""
    description: str = ""
    is_mandatory: IsMandatory = IsMandatory.MANDATORY
    evidence_sources: List[str] = field(default_factory=list)
    is_confirmed: bool = False