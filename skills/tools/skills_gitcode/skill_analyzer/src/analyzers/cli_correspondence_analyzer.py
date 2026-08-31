import re
import subprocess
from typing import Dict, List, Optional

from src.infra.logger import get_step_logger
from src.models.api_interface import ApiBusinessRole, OpenAPIUsage
from src.models.cli_command import KooCliCommand, KooCliCorrespondence, KooCliParameter
from src.models.enums import Acceptability, CliInfoSource, CorrespondenceStatus
from src.models.report import CliCorrespondenceResult, ReportStageResult

_GLOBAL_SERVICE_OPS_CACHE: Dict[str, List[str]] = {}


class CliCorrespondenceAnalyzer:
    def __init__(self, cli_tool_available: bool = False, cli_tool_path: str = ""):
        self.cli_tool_available = cli_tool_available
        self.cli_tool_path = cli_tool_path
        self.log = get_step_logger("CliCorrespondenceAnalyzer")
        self._verify_cache: Dict[str, bool] = {}
        self._service_ops_cache = _GLOBAL_SERVICE_OPS_CACHE

    def analyze(
        self,
        openapi_usages: List[OpenAPIUsage],
        api_business_roles: List[ApiBusinessRole],
    ) -> CliCorrespondenceResult:
        commands = self._get_koocli_commands(openapi_usages)
        correspondences = []
        incomplete_apis = []

        for usage in openapi_usages:
            candidates = self._find_candidates(usage, commands)
            verified = self._verify_candidates(candidates)
            selected = verified[0] if verified else None
            has_match = selected is not None
            status = CorrespondenceStatus.EXACT if has_match else CorrespondenceStatus.NONE
            acceptable = Acceptability.ACCEPTABLE if has_match else Acceptability.NOT_ACCEPTABLE
            corr = KooCliCorrespondence(
                usage_id=usage.usage_id,
                api_name=usage.api_name,
                service_name=usage.service_name,
                candidate_commands=candidates,
                selected_command=selected,
                status=status,
                equivalence_result=None,
                difference_description="" if has_match else "无对应KooCLI命令",
                impact_description="" if has_match else "该接口无法通过KooCLI实现",
                acceptable=acceptable,
                info_source=selected.info_source if selected else CliInfoSource.NONE,
                manual_verification_items=[],
                api_explorer_url=self._build_api_explorer_url(usage.service_name, usage.api_name) if not has_match else "",
            )
            correspondences.append(corr)
            if not has_match:
                incomplete_apis.append(usage.api_name)

        summary = self._build_summary(correspondences)
        stage_2 = ReportStageResult(
            stage_name="第二阶段：KooCLI一一对应关系",
            summary=summary,
            details={
                "total": len(correspondences),
                "exact": sum(1 for c in correspondences if c.status == CorrespondenceStatus.EXACT),
                "partial": sum(1 for c in correspondences if c.status == CorrespondenceStatus.PARTIAL),
                "none": sum(1 for c in correspondences if c.status == CorrespondenceStatus.NONE),
                "unknown": sum(1 for c in correspondences if c.status == CorrespondenceStatus.UNKNOWN),
            },
        )
        return CliCorrespondenceResult(
            correspondences=correspondences,
            equivalence_results=[],
            incomplete_apis=incomplete_apis,
            info_source="local_cli" if commands else "none",
            analysis_warnings=[] if commands else ["未获取到KooCLI命令清单"],
            stage_2_result=stage_2,
        )

    def _get_koocli_commands(self, openapi_usages: List[OpenAPIUsage] = None) -> List[KooCliCommand]:
        commands = []
        if not self.cli_tool_available:
            return commands
        try:
            from src.resources.cli_tool_manager import CliToolManager
            mgr = CliToolManager(self.cli_tool_path)
            needed_services = set()
            if openapi_usages:
                for usage in openapi_usages:
                    if usage.service_name:
                        needed_services.add(usage.service_name.upper())
                        needed_services.add(usage.service_name)
            for service in needed_services:
                if service.upper() == "OBS":
                    obs_cmds = self._get_obs_commands(mgr)
                    commands.extend(obs_cmds)
                else:
                    if service not in self._service_ops_cache:
                        self._service_ops_cache[service] = mgr.get_service_operations(service)
                    for op in self._service_ops_cache[service]:
                        commands.append(self._to_command(service, op))
            if not commands:
                raw = mgr.get_supported_commands()
                for service, cmd_list in raw.items():
                    for cmd in cmd_list:
                        commands.append(self._to_command(service, cmd))
        except Exception as e:
            self.log.info(f"Failed to read local KooCLI commands: {e}")
        return commands

    def _get_obs_commands(self, mgr) -> List[KooCliCommand]:
        obs_commands = [
            ("mb", "创建桶", "bucket"),
            ("ls", "列举桶/对象", "bucket/object"),
            ("stat", "查询桶/对象属性", "bucket/object"),
            ("chattri", "设置桶/对象属性", "bucket/object"),
            ("rm", "删除桶/对象", "bucket/object"),
            ("cp", "上传/下载/复制对象", "object"),
            ("mv", "移动对象", "object"),
            ("sync", "增量同步", "object"),
            ("mkdir", "创建文件夹", "folder"),
            ("restore", "恢复归档存储对象", "object"),
            ("sign", "生成下载链接", "object"),
            ("abort", "删除分段上传任务", "multipart"),
            ("bucketpolicy", "桶策略管理", "bucket"),
            ("create-share", "创建分享授权码", "object"),
            ("share-ls", "授权码列举对象", "object"),
            ("share-cp", "授权码下载对象", "object"),
            ("config", "更新配置", ""),
            ("help", "查看帮助", ""),
            ("version", "查看版本", ""),
        ]
        result = []
        for op, desc, resource in obs_commands:
            cmd = KooCliCommand(
                command=f"hcloud obs {op}",
                service_name="OBS",
                operation_name=op,
                resource_object=resource,
                info_source=CliInfoSource.LOCAL_CLI,
            )
            result.append(cmd)
        return result

    def _to_command(self, service: str, operation: str) -> KooCliCommand:
        return KooCliCommand(
            command=f"hcloud {service} {operation}",
            service_name=service,
            operation_name=operation,
            resource_object="",
            parameters=[],
            info_source=CliInfoSource.LOCAL_CLI,
        )

    def _find_candidates(self, usage: OpenAPIUsage, commands: List[KooCliCommand]) -> List[KooCliCommand]:
        if self._norm(usage.service_name) == "obs":
            return self._find_obs_candidates(usage, commands)
        service = self._norm(usage.service_name)
        api_base = self._strip_version(usage.api_name)
        api_base_norm = self._norm(api_base)
        exact_matches = []
        for cmd in commands:
            if self._norm(cmd.service_name) != service:
                continue
            cmd_base = self._strip_version(cmd.operation_name)
            if self._norm(cmd_base) == api_base_norm:
                exact_matches.append(cmd)
        return exact_matches[:3]

    def _verify_candidates(self, candidates: List[KooCliCommand]) -> List[KooCliCommand]:
        if not candidates:
            return candidates
        obs_cmds = [c for c in candidates if c.service_name.upper() == "OBS"]
        non_obs_cmds = [c for c in candidates if c.service_name.upper() != "OBS"]
        verified = list(non_obs_cmds)
        for cmd in obs_cmds:
            exists = self._check_command_exists(cmd)
            if exists:
                verified.append(cmd)
            else:
                self.log.info(f"KooCLI command verification failed: {cmd.command}")
        return verified

    def _check_command_exists(self, cmd: KooCliCommand) -> bool:
        cache_key = cmd.command
        if cache_key in self._verify_cache:
            return self._verify_cache[cache_key]
        exists = self._do_check_command(cmd)
        self._verify_cache[cache_key] = exists
        return exists

    def _do_check_command(self, cmd: KooCliCommand) -> bool:
        try:
            from src.resources.cli_tool_manager import CliToolManager
            mgr = CliToolManager(self.cli_tool_path)
            cli_path = mgr._get_cli_path()
            parts = cmd.command.split()
            if len(parts) < 3:
                return False
            service = parts[1]
            operation = parts[2]
            if service.lower() == "obs":
                return self._check_obs_command_by_guidance(operation)
            result = subprocess.run(
                [cli_path, service, operation, "--help"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return True
            err = (result.stderr or "").lower()
            if "not found" in err or "invalid" in err or "error" in err or "unsupported" in err:
                return False
            out = (result.stdout or "").lower()
            if "usage" in out or "parameter" in out or "参数" in out:
                return True
            return False
        except Exception as e:
            self.log.info(f"Command verification error for {cmd.command}: {e}")
            return True

    def _find_obs_candidates(self, usage: OpenAPIUsage, commands: List[KooCliCommand]) -> List[KooCliCommand]:
        obs_api_to_cli = {
            "create": ["mb", "mkdir", "cp"],
            "delete": ["rm", "abort"],
            "update": ["chattri", "cp", "bucketpolicy"],
            "show": ["stat", "ls"],
            "get": ["stat", "ls"],
            "list": ["ls"],
            "set": ["chattri", "bucketpolicy", "cp"],
            "put": ["cp"],
            "head": ["stat"],
            "copy": ["cp"],
            "download": ["cp", "share-cp"],
            "upload": ["cp"],
            "restore": ["restore"],
            "check": ["stat"],
            "query": ["stat", "ls"],
            "associate": ["chattri", "cp"],
            "disassociate": ["chattri", "rm"],
            "grant": ["chattri", "bucketpolicy"],
            "revoke": ["chattri", "bucketpolicy"],
            "enable": ["chattri"],
            "disable": ["chattri"],
        }
        api_name_lower = (usage.api_name or "").lower()
        if "bucket" in api_name_lower and "policy" in api_name_lower:
            candidates_ops = ["bucketpolicy"]
        elif "multipart" in api_name_lower or ("upload" in api_name_lower and "part" in api_name_lower):
            if "abort" in api_name_lower:
                candidates_ops = ["abort"]
            elif "list" in api_name_lower:
                candidates_ops = ["ls"]
            else:
                candidates_ops = ["cp"]
        elif "version" in api_name_lower and "delete" not in api_name_lower:
            candidates_ops = ["ls", "stat"]
        elif "tag" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "acl" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "cors" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "logging" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "website" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "notification" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "encryption" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "replication" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "quota" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        elif "payment" in api_name_lower:
            candidates_ops = ["stat", "chattri"]
        else:
            action = self._action(usage.api_name)
            candidates_ops = obs_api_to_cli.get(action, ["ls", "stat", "cp"])
        obs_cmds = [c for c in commands if c.service_name.upper() == "OBS"]
        matched = [c for c in obs_cmds if c.operation_name in candidates_ops]
        return matched[:3] if matched else obs_cmds[:3]

    @staticmethod
    def _strip_version(name: str) -> str:
        return re.sub(r"/v\d+$", "", name or "")

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    @staticmethod
    def _action(value: str) -> str:
        lower = (value or "").lower()
        for action in ["create", "delete", "update", "show", "get", "list", "batch", "set", "put", "check", "count", "associate", "disassociate", "grant", "revoke", "enable", "disable", "download", "upload", "copy", "move", "rename", "search", "query", "validate", "verify", "login", "logout", "register", "unregister"]:
            if action in lower:
                return action
        return ""

    @staticmethod
    def _build_summary(correspondences: List[KooCliCorrespondence]) -> str:
        exact = sum(1 for c in correspondences if c.status == CorrespondenceStatus.EXACT)
        total = len(correspondences)
        return f"共分析{total}个Open API接口，其中{exact}个存在效果完全一致的KooCLI命令。"

    _OBS_KNOWN_COMMANDS = frozenset({
        "mb", "ls", "stat", "chattri", "rm", "cp", "mv", "sync", "mkdir",
        "restore", "sign", "abort", "bucketpolicy", "create-share", "share-ls",
        "share-cp", "config", "help", "version", "archive", "clear",
    })

    def _check_obs_command_by_guidance(self, operation: str) -> bool:
        return operation in self._OBS_KNOWN_COMMANDS

    @staticmethod
    def _build_api_explorer_url(service_name: str, api_name: str) -> str:
        if not service_name or not api_name:
            return ""
        compute_name = service_name.upper()
        return f"https://console.huaweicloud.com/apiexplorer/#/openapi/{compute_name}/cli?api={api_name}"