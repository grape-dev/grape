from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LlmProvider(str, Enum):
    OPENAI = "openai"
    PANGU = "pangu"
    CUSTOM = "custom"


@dataclass
class LlmConfig:
    provider: LlmProvider = LlmProvider.OPENAI
    model_name: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.1
    max_tokens: int = 4096
    extra_params: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class LlmAnalysisResult:
    analysis_type: str
    prompt: str
    response: str
    model_used: str
    token_usage: Dict[str, int] = field(default_factory=dict)
    timestamp: str = ""