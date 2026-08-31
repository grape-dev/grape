from src.infra.logger import get_step_logger
from src.llm.openai_provider import OpenAIProvider


PANGU_BASE_URL = "https://pangu-api.cn-east-3.huaweicloud.com/v1"


class PanguProvider(OpenAIProvider):
    def __init__(
        self,
        model_name: str = "pangu-v2",
        api_key: str = "",
        base_url: str = "",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        **kwargs,
    ):
        effective_base_url = base_url or PANGU_BASE_URL
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=effective_base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        self.log = get_step_logger("PanguProvider")

    def get_model_info(self) -> dict:
        return {
            "provider": "pangu",
            "model": self.model_name,
            "base_url": self.base_url,
        }