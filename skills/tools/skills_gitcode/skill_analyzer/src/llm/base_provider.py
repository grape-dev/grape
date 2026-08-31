from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseLlmProvider(ABC):
    @abstractmethod
    def invoke(self, prompt: str, **kwargs) -> str:
        ...

    @abstractmethod
    def invoke_with_structured_output(self, prompt: str, schema: type, **kwargs) -> dict:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def get_model_info(self) -> dict:
        ...