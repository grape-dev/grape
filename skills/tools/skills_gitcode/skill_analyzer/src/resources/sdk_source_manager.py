import ast
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from src.infra.file_utils import ensure_dir, find_files, read_file_content
from src.infra.logger import get_step_logger


class SdkSourceManager:
    DEFAULT_REPO_URL = "https://github.com/huaweicloud/huaweicloud-sdk-python-v3"

    def __init__(self, cache_dir: str = "./cache/sdk-source", repo_url: Optional[str] = None):
        self.cache_dir = Path(cache_dir)
        self.repo_url = repo_url or self.DEFAULT_REPO_URL
        self.log = get_step_logger("SdkSourceManager")

    def ensure_available(self) -> str:
        if self.is_cache_valid():
            self.log.info("SDK source cache is valid, using cached version")
            return str(self.cache_dir)

        try:
            return self._clone_repo()
        except Exception as e:
            self.log.info(f"Git clone failed: {e}, trying ZIP download")
            try:
                return self._download_zip()
            except Exception as e2:
                if self.cache_dir.exists():
                    self.log.info("Using existing incomplete cache as fallback")
                    return str(self.cache_dir)
                raise RuntimeError(f"Failed to obtain SDK source: {e}, {e2}")

    def is_cache_valid(self) -> bool:
        if not self.cache_dir.exists():
            return False
        core_dir = self.cache_dir / "huaweicloud-sdk-core"
        if not core_dir.exists():
            return False
        return any(self.cache_dir.glob("huaweicloud-sdk-*"))

    def _clone_repo(self) -> str:
        self.log.info(f"Cloning SDK repo: {self.repo_url}")
        ensure_dir(self.cache_dir.parent)

        if self.cache_dir.exists() and (self.cache_dir / ".git").exists():
            try:
                from git import Repo
                repo = Repo(str(self.cache_dir))
                repo.remotes.origin.pull(rebase=True)
                self.log.info("Updated existing SDK repo cache")
            except Exception as e:
                self.log.info(f"Git pull failed: {e}, re-cloning")
                shutil.rmtree(self.cache_dir, ignore_errors=True)
                from git import Repo
                Repo.clone_from(
                    self.repo_url, str(self.cache_dir), depth=1
                )
        else:
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir, ignore_errors=True)
            from git import Repo
            Repo.clone_from(
                self.repo_url, str(self.cache_dir), depth=1
            )

        self.log.info("SDK source ready")
        return str(self.cache_dir)

    def _download_zip(self) -> str:
        import requests
        zip_url = f"{self.repo_url}/archive/refs/heads/master.zip"
        self.log.info(f"Downloading SDK source ZIP: {zip_url}")

        ensure_dir(self.cache_dir.parent)
        zip_path = self.cache_dir.parent / "sdk-source.zip"

        response = requests.get(zip_url, timeout=300, stream=True)
        response.raise_for_status()

        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir, ignore_errors=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(self.cache_dir.parent)

        extracted_dir = self.cache_dir.parent / "huaweicloud-sdk-python-v3-master"
        if extracted_dir.exists():
            extracted_dir.rename(self.cache_dir)

        zip_path.unlink(missing_ok=True)
        self.log.info("SDK source ZIP downloaded and extracted")
        return str(self.cache_dir)

    def build_service_index(self) -> Dict[str, Path]:
        index = {}
        if not self.cache_dir.exists():
            return index

        for sdk_dir in self.cache_dir.glob("huaweicloud-sdk-*"):
            if not sdk_dir.is_dir() or sdk_dir.name == "huaweicloud-sdk-core":
                continue
            service_name = sdk_dir.name.replace("huaweicloud-sdk-", "")
            pkg_dirs = list(sdk_dir.glob(f"huaweicloudsdk{service_name}/v*"))
            if pkg_dirs:
                index[service_name] = pkg_dirs[0]
        return index

    def get_service_client_path(self, service_name: str) -> Optional[Path]:
        index = self.build_service_index()
        service_path = index.get(service_name.lower())
        if service_path is None:
            return None

        client_file = service_path / f"{service_name.lower()}_client.py"
        if client_file.exists():
            return client_file
        return None

    def get_http_info(self, service_name: str, method_name: str) -> Optional[Dict]:
        client_path = self.get_service_client_path(service_name)
        if client_path is None:
            return None

        content = read_file_content(client_path)
        if not content:
            return None

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None

        http_info_method = f"_{method_name}_http_info"
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == http_info_method:
                info = self._extract_http_info_from_func(node, content)
                if not info.get("request_type") or info["request_type"].startswith("__"):
                    info["request_type"] = self._method_to_request_type(method_name)
                return info
        return None

    @staticmethod
    def _method_to_request_type(method_name: str) -> str:
        parts = method_name.split("_")
        return "".join(p.capitalize() for p in parts if p) + "Request"

    def _extract_http_info_from_func(self, func_node, content: str) -> Dict:
        info = {"method": "", "resource_path": "", "request_type": ""}
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and node.value:
                if isinstance(node.value, ast.Call):
                    for kw in node.value.keywords:
                        if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                            info["method"] = kw.value.value
                        elif kw.arg == "resource_path" and isinstance(kw.value, ast.Constant):
                            info["resource_path"] = kw.value.value
                        elif kw.arg == "request_type" and isinstance(kw.value, ast.Constant):
                            info["request_type"] = kw.value.value
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "http_info":
                        if isinstance(node.value, ast.Dict):
                            for key, val in zip(node.value.keys, node.value.values):
                                if isinstance(key, ast.Constant):
                                    if key.value == "method" and isinstance(val, ast.Constant):
                                        info["method"] = val.value
                                    elif key.value == "resource_path" and isinstance(val, ast.Constant):
                                        info["resource_path"] = val.value
                                    elif key.value == "request_type" and isinstance(val, ast.Constant):
                                        info["request_type"] = val.value
                                    elif key.value == "request_type" and isinstance(val, ast.Attribute):
                                        if isinstance(val.value, ast.Attribute) and val.attr == "__name__":
                                            info["request_type"] = val.value.attr
                                        else:
                                            info["request_type"] = val.attr
        return info

    def get_request_model(self, service_name: str, request_type: str) -> Optional[Dict]:
        index = self.build_service_index()
        service_path = index.get(service_name.lower())
        if service_path is None:
            return None

        model_dir = service_path / "model"
        if not model_dir.exists():
            return None

        snake_name = self._camel_to_snake(request_type.replace("Request", ""))
        request_file = model_dir / f"{snake_name}_request.py"
        if not request_file.exists():
            for f in model_dir.glob("*_request.py"):
                if request_type.lower().replace("request", "") in f.name.lower():
                    request_file = f
                    break
            else:
                return None

        content = read_file_content(request_file)
        if not content:
            return None

        return self._parse_request_model(content, request_type)

    def _parse_request_model(self, content: str, request_type: str) -> Dict:
        import ast
        result = {"openapi_types": {}, "attribute_map": {}, "required_params": [], "optional_params": []}

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == request_type:
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                if target.id == "openapi_types" and isinstance(item.value, ast.Dict):
                                    result["openapi_types"] = self._ast_dict_to_python(item.value)
                                elif target.id == "attribute_map" and isinstance(item.value, ast.Dict):
                                    result["attribute_map"] = self._ast_dict_to_python(item.value)

                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        for arg in item.args.args[1:]:
                            param_name = arg.arg
                            if param_name in result.get("attribute_map", {}):
                                result["required_params"].append(param_name)
                        for default_val in item.args.defaults:
                            if result["required_params"]:
                                moved = result["required_params"].pop()
                                result["optional_params"].append(moved)

                break

        return result

    @staticmethod
    def _ast_dict_to_python(dict_node) -> Dict[str, str]:
        result = {}
        for key, value in zip(dict_node.keys, dict_node.values):
            k = key.value if isinstance(key, ast.Constant) else str(key)
            v = value.value if isinstance(value, ast.Constant) else str(value)
            result[k] = v
        return result

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        import re
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()