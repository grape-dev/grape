from typing import Dict, Type

from src.infra.logger import get_step_logger
from src.llm.base_provider import BaseLlmProvider
from src.llm.openai_provider import OpenAIProvider
from src.llm.pangu_provider import PanguProvider
from src.models.llm_config import LlmConfig, LlmProvider


class LlmFactory:
    _registry: Dict[str, Type[BaseLlmProvider]] = {
        LlmProvider.OPENAI.value: OpenAIProvider,
        LlmProvider.PANGU.value: PanguProvider,
    }

    def __init__(self):
        self.log = get_step_logger("LlmFactory")

    @classmethod
    def register_provider(cls, name: str, provider_cls: Type[BaseLlmProvider]) -> None:
        cls._registry[name.lower()] = provider_cls

    def create_provider(self, config: LlmConfig) -> BaseLlmProvider:
        provider_name = config.provider.value if isinstance(config.provider, LlmProvider) else config.provider
        provider_name = provider_name.lower()

        if provider_name not in self._registry:
            raise ValueError(
                f"Unknown LLM provider: {provider_name}. "
                f"Available: {list(self._registry.keys())}"
            )

        provider_cls = self._registry[provider_name]
        self.log.info(f"Creating LLM provider: {provider_name}")

        return provider_cls(
            model_name=config.model_name,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            **config.extra_params,
        )