import os
from pathlib import Path
from typing import Optional

from src.infra.logger import get_step_logger
from src.models.llm_config import LlmConfig, LlmProvider


class LlmConfigManager:
    def __init__(self):
        self.log = get_step_logger("LlmConfigManager")

    def get_config(
        self,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        no_llm: bool = False,
    ) -> LlmConfig:
        if no_llm:
            return LlmConfig(api_key="")

        cli_provider = provider
        cli_model = model_name
        cli_api_key = api_key
        cli_base_url = base_url
        cli_temperature = temperature
        cli_max_tokens = max_tokens

        env_provider = os.environ.get("LLM_PROVIDER", "")
        env_model = os.environ.get("LLM_MODEL", "")
        env_api_key = os.environ.get("LLM_API_KEY", "")
        env_base_url = os.environ.get("LLM_BASE_URL", "")

        final_provider = cli_provider or env_provider or "openai"
        final_model = cli_model or env_model or ("gpt-4o" if final_provider == "openai" else "pangu-v2")
        final_api_key = cli_api_key or env_api_key or ""
        final_base_url = cli_base_url or env_base_url or ""
        final_temperature = cli_temperature if cli_temperature is not None else 0.1
        final_max_tokens = cli_max_tokens if cli_max_tokens is not None else 4096

        try:
            provider_enum = LlmProvider(final_provider.lower())
        except ValueError:
            provider_enum = LlmProvider.OPENAI

        config = LlmConfig(
            provider=provider_enum,
            model_name=final_model,
            api_key=final_api_key,
            base_url=final_base_url,
            temperature=final_temperature,
            max_tokens=final_max_tokens,
        )

        self.log.info(f"LLM config: provider={config.provider.value}, model={config.model_name}, enabled={config.is_enabled}")
        return config

    def is_llm_enabled(self, config: LlmConfig) -> bool:
        return config.is_enabled