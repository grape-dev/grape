from dataclasses import dataclass, field
from typing import List

from src.models.enums import ImplType


@dataclass
class SkillImplResult:
    skill_name: str
    implementation_type: ImplType
    source_path: str
    business_goal: str = ""
    cli_evidence: List[str] = field(default_factory=list)
    sdk_evidence: List[str] = field(default_factory=list)