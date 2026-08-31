from dataclasses import dataclass, field
from typing import List

from src.models.enums import AlternativeFeasibility, ImpactLevel, IsMandatory


@dataclass
class CliGap:
    unsupported_api: str
    service_name: str
    business_role: str
    impact_level: ImpactLevel
    is_mandatory: IsMandatory


@dataclass
class AlternativeScheme:
    unsupported_api: str
    alternative_cli: str
    feasibility: AlternativeFeasibility
    alternative_difference: str = ""