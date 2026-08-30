import os
import platform
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from src.infra.file_utils import ensure_dir
from src.infra.logger import get_step_logger


class CliToolManager:
    WINDOWS_CLI_URL = "https://hwcloudcli.obs.cn-north-1.myhuaweicloud.com/cli/latest/hcloud.zip"
    LINUX_CLI_URL = "https://hwcloudcli.obs.cn-north-1.myhuaweicloud.com/cli/latest/hcloud_linux.zip"

    def __init__(self, install_dir: str = "./cache/koo-cli"):
        self.install_dir = Path(install_dir)
        self.log = get_step_logger("CliToolManager")
        self._is_windows = platform.system() == "Windows"

    def ensure_available(self, skip_install: bool = False) -> str:
        if self.is_installed():
            self.log.info("KooCLI is already installed")
            return str(self.install_dir)

        if skip_install:
            raise RuntimeError("KooCLI not installed and skip_install=True")

        return self._install()

    def is_installed(self) -> bool:
        exe_name = "hcloud.exe" if self._is_windows else "hcloud"
        return (self.install_dir / exe_name).exists()

    def get_version(self) -> Optional[str]:
        try:
            result = subprocess.run(
                [self._get_cli_path(), "version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def verify_available(self) -> bool:
        try:
            result = subprocess.run(
                [self._get_cli_path(), "--help"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_supported_commands(self) -> Dict[str, List[str]]:
        commands = {}

        try:
            result = subprocess.run(
                [self._get_cli_path(), "--help"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return commands

            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("Usage") or line.startswith("Options") or line.startswith("示例") or line.startswith("参数"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    service = parts[0]
                    cmd = parts[1] if len(parts) > 1 else ""
                    if service.startswith("--") or service.startswith("hcloud"):
                        continue
                    if service not in commands:
                        commands[service] = []
                    if cmd and cmd not in commands[service]:
                        commands[service].append(cmd)
        except Exception as e:
            self.log.info(f"Failed to get CLI commands: {e}")

        return commands

    def get_service_operations(self, service_name: str) -> List[str]:
        operations = []
        try:
            result = subprocess.run(
                [self._get_cli_path(), service_name, "--help"],
                capture_output=True, text=True, timeout=30,
            )
            skip_words = {"KooCLI", "Service:", "Available", "Operations:", "Usage", "Options", "service", "operation", "Version", "Copyright(C)", "Copyright"}
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("Usage") or line.startswith("Options") or line.startswith("--"):
                    continue
                parts = line.split()
                for part in parts:
                    if part.startswith("--") or part.startswith("hcloud") or part in skip_words:
                        continue
                    if part[0].isupper() and len(part) > 3 and part not in operations:
                        operations.append(part)
        except Exception as e:
            self.log.info(f"Failed to get operations for {service_name}: {e}")
        return operations

    def _get_cli_path(self) -> str:
        exe_name = "hcloud.exe" if self._is_windows else "hcloud"
        return str(self.install_dir / exe_name)

    def _install(self) -> str:
        ensure_dir(self.install_dir)
        url = self.WINDOWS_CLI_URL if self._is_windows else self.LINUX_CLI_URL

        self.log.info(f"Downloading KooCLI from: {url}")
        import requests

        zip_path = self.install_dir / "hcloud.zip"
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()

        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        self.log.info("Extracting KooCLI...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(self.install_dir)

        zip_path.unlink(missing_ok=True)

        if not self._is_windows:
            hcloud_path = self.install_dir / "hcloud"
            if hcloud_path.exists():
                hcloud_path.chmod(0o755)

        if self.verify_available():
            self.log.info("KooCLI installed and verified successfully")
        else:
            self.log.info("KooCLI installed but verification failed")

        return str(self.install_dir)