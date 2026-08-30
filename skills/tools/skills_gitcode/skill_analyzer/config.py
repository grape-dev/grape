import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.models.llm_config import LlmConfig, LlmProvider
from src.models.report import AnalysisConfig


DEFAULT_CONFIG_PATHS = ["config.yaml", "config.yml"]


def load_yaml_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data else {}


def find_default_config() -> Optional[str]:
    for name in DEFAULT_CONFIG_PATHS:
        if Path(name).exists():
            return name
    return None


def merge_config(file_config: Dict, cli_overrides: Dict) -> Dict:
    merged = {}
    for key, value in file_config.items():
        if key == "llm":
            continue
        merged[key] = value

    for key, value in cli_overrides.items():
        if value is not None:
            merged[key] = value

    return merged


class ConfigManager:
    def __init__(self):
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="huaweicloud-skills-analyzer",
            description="Analyze huaweicloud skills - identify implementation patterns, SDK API usage, and CLI coverage",
        )

        parser.add_argument(
            "--config",
            type=str,
            default=None,
            help="Path to YAML config file (default: auto-detect config.yaml in current directory)",
        )

        parser.add_argument(
            "--skills-repo",
            type=str,
            default=None,
            help="Path to the huaweicloud skills repository",
        )
        parser.add_argument(
            "--skill-names",
            nargs="+",
            default=None,
            help="Specific skill names to analyze (default: all skills)",
        )
        parser.add_argument(
            "--sdk-cache-dir",
            type=str,
            default=None,
            help="SDK source cache directory",
        )
        parser.add_argument(
            "--cli-install-dir",
            type=str,
            default=None,
            help="CLI tool installation directory",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default=None,
            help="Report output directory",
        )
        parser.add_argument(
            "--checkpoint-dir",
            type=str,
            default=None,
            help="Checkpoint storage directory",
        )
        parser.add_argument(
            "--skip-cli-install",
            action="store_true",
            default=None,
            help="Skip automatic CLI tool installation",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            default=None,
            help="Resume analysis from checkpoint",
        )
        parser.add_argument(
            "--log-level",
            type=str,
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            default=None,
            help="Log level",
        )
        parser.add_argument(
            "--sdk-repo-url",
            type=str,
            default=None,
            help="SDK repository URL",
        )
        parser.add_argument(
            "--max-retries",
            type=int,
            default=None,
            help="Maximum retry count for failed steps",
        )

        llm_group = parser.add_argument_group("LLM options", "AI model configuration (overrides config file)")
        llm_group.add_argument("--llm-provider", type=str, default=None, help="LLM provider (openai/pangu/custom)")
        llm_group.add_argument("--llm-model", type=str, default=None, help="LLM model name")
        llm_group.add_argument("--llm-api-key", type=str, default=None, help="LLM API key")
        llm_group.add_argument("--llm-base-url", type=str, default=None, help="LLM API base URL")
        llm_group.add_argument("--llm-temperature", type=float, default=None, help="LLM temperature")
        llm_group.add_argument("--llm-max-tokens", type=int, default=None, help="LLM max tokens")
        llm_group.add_argument("--no-llm", action="store_true", default=None, help="Disable LLM enhanced analysis")

        return parser

    def parse_args(self, args: Optional[List[str]] = None) -> Tuple[AnalysisConfig, LlmConfig]:
        parsed = self.parser.parse_args(args)

        config_path = parsed.config or find_default_config()
        file_config: Dict[str, Any] = {}
        if config_path:
            file_config = load_yaml_config(config_path)

        skills_repo = parsed.skills_repo or file_config.get("skills_repo", "")
        if not skills_repo:
            raise ValueError("skills_repo is required: set it in config file or via --skills-repo")

        skill_names = parsed.skill_names if parsed.skill_names is not None else file_config.get("skill_names", [])

        config = AnalysisConfig(
            skills_repo_path=skills_repo,
            skill_names=skill_names,
            sdk_cache_dir=parsed.sdk_cache_dir or file_config.get("sdk_cache_dir", "./cache/sdk-source"),
            cli_install_dir=parsed.cli_install_dir or file_config.get("cli_install_dir", "./cache/koo-cli"),
            output_dir=parsed.output_dir or file_config.get("output_dir", "./reports"),
            checkpoint_dir=parsed.checkpoint_dir or file_config.get("checkpoint_dir", "./cache/checkpoints"),
            skip_cli_install=parsed.skip_cli_install if parsed.skip_cli_install is not None else file_config.get("skip_cli_install", False),
            resume=parsed.resume if parsed.resume is not None else file_config.get("resume", False),
            log_level=parsed.log_level or file_config.get("log_level", "INFO"),
            sdk_repo_url=parsed.sdk_repo_url or file_config.get("sdk_repo_url", "https://github.com/huaweicloud/huaweicloud-sdk-python-v3"),
            max_retries=parsed.max_retries if parsed.max_retries is not None else file_config.get("max_retries", 3),
        )

        llm_config = self._build_llm_config(parsed, file_config)
        return config, llm_config

    def _build_llm_config(self, parsed, file_config: Dict) -> LlmConfig:
        file_llm = file_config.get("llm", {})

        no_llm = parsed.no_llm if parsed.no_llm is not None else file_llm.get("disabled", False)
        if no_llm:
            return LlmConfig(api_key="")

        provider = parsed.llm_provider or file_llm.get("provider", "openai")
        model_name = parsed.llm_model or file_llm.get("model", "")
        api_key = parsed.llm_api_key or file_llm.get("api_key", "")
        base_url = parsed.llm_base_url or file_llm.get("base_url", "")
        temperature = parsed.llm_temperature if parsed.llm_temperature is not None else file_llm.get("temperature", 0.1)
        max_tokens = parsed.llm_max_tokens if parsed.llm_max_tokens is not None else file_llm.get("max_tokens", 4096)

        if not model_name:
            model_name = "gpt-4o" if provider == "openai" else "pangu-v2"

        try:
            provider_enum = LlmProvider(provider.lower())
        except ValueError:
            provider_enum = LlmProvider.OPENAI

        return LlmConfig(
            provider=provider_enum,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
