import time
from datetime import datetime, timezone
from typing import Optional

from src.infra.logger import get_step_logger
from src.llm.base_provider import BaseLlmProvider
from src.llm.llm_factory import LlmFactory
from src.models.llm_config import LlmAnalysisResult, LlmConfig


class LlmClient:
    def __init__(self, config: LlmConfig, max_retries: int = 3, timeout: int = 60):
        self.config = config
        self.max_retries = max_retries
        self.timeout = timeout
        self.log = get_step_logger("LlmClient")
        self._provider: Optional[BaseLlmProvider] = None
        self._total_tokens = 0

        if config.is_enabled:
            try:
                factory = LlmFactory()
                self._provider = factory.create_provider(config)
                self.log.info(f"LLM client initialized: {config.provider.value}/{config.model_name}")
            except Exception as e:
                self.log.info(f"LLM client initialization failed: {e}, will degrade to rule-based analysis")

    @property
    def is_available(self) -> bool:
        if self._provider is None:
            return False
        try:
            return self._provider.is_available()
        except Exception:
            return False

    def analyze(self, prompt: str, analysis_type: str) -> Optional[LlmAnalysisResult]:
        if not self.is_available:
            self.log.info(f"LLM not available, skipping {analysis_type} analysis")
            return None

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self._provider.invoke(prompt)
                model_info = self._provider.get_model_info()

                result = LlmAnalysisResult(
                    analysis_type=analysis_type,
                    prompt=prompt[:500],
                    response=response,
                    model_used=model_info.get("model", self.config.model_name),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

                self.log.info(f"LLM {analysis_type} analysis completed (attempt {attempt + 1})")
                return result

            except Exception as e:
                last_error = e
                wait_time = 2 ** attempt
                self.log.info(f"LLM call failed (attempt {attempt + 1}/{self.max_retries}): {e}, retrying in {wait_time}s")
                if attempt < self.max_retries - 1:
                    time.sleep(wait_time)

        self.log.info(f"LLM {analysis_type} analysis failed after {self.max_retries} retries: {last_error}")
        return None