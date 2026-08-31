from typing import Any, Dict, Optional

from src.infra.logger import get_step_logger
from src.llm.base_provider import BaseLlmProvider


class OpenAIProvider(BaseLlmProvider):
    def __init__(
        self,
        model_name: str = "gpt-4o",
        api_key: str = "",
        base_url: str = "",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        **kwargs,
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_kwargs = kwargs
        self.log = get_step_logger("OpenAIProvider")
        self._llm = None

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        try:
            from langchain_openai import ChatOpenAI

            params = {
                "model": self.model_name,
                "api_key": self.api_key,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.base_url:
                params["base_url"] = self.base_url
            params.update(self.extra_kwargs)
            self._llm = ChatOpenAI(**params)
        except Exception as e:
            self.log.info(f"Failed to initialize ChatOpenAI: {e}")
            raise
        return self._llm

    def invoke(self, prompt: str, **kwargs) -> str:
        llm = self._get_llm()
        from langchain_core.messages import HumanMessage

        response = llm.invoke([HumanMessage(content=prompt)], **kwargs)
        return response.content

    def invoke_with_structured_output(self, prompt: str, schema: type, **kwargs) -> dict:
        llm = self._get_llm()
        structured_llm = llm.with_structured_output(schema)
        result = structured_llm.invoke(prompt, **kwargs)
        if isinstance(result, dict):
            return result
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    def is_available(self) -> bool:
        try:
            self._get_llm()
            return True
        except Exception:
            return False

    def get_model_info(self) -> dict:
        return {
            "provider": "openai",
            "model": self.model_name,
            "base_url": self.base_url or "https://api.openai.com/v1",
        }