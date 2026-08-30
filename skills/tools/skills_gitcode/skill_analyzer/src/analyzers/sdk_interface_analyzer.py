import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.infra.file_utils import find_files, read_file_content
from src.infra.logger import get_step_logger
from src.models.api_interface import (
    ApiBusinessRole,
    ApiInterface,
    ApiParameter,
    OpenAPIUsage,
    SdkCallInfo,
    SourceSdkCall,
)
from src.models.enums import HttpMethod, IsMandatory
from src.models.report import SdkAnalysisResult
from src.resources.sdk_source_manager import SdkSourceManager


class SdkInterfaceAnalyzer:
    def __init__(self):
        self.log = get_step_logger("SdkInterfaceAnalyzer")

    def analyze(self, skill_path: str, sdk_source_path: str) -> SdkAnalysisResult:
        sdk_manager = SdkSourceManager(sdk_source_path)
        service_index = sdk_manager.build_service_index()

        sdk_calls = self._extract_sdk_calls(skill_path)
        if not sdk_calls:
            self.log.info("No SDK calls found in skill source")
            return SdkAnalysisResult()

        api_interfaces: List[ApiInterface] = []
        openapi_usages: List[OpenAPIUsage] = []
        api_business_roles: List[ApiBusinessRole] = []
        mapping_failures: List[str] = []

        for idx, call in enumerate(sdk_calls, start=1):
            api = self._map_to_api(call, sdk_manager, service_index)
            if api:
                api_interfaces.append(api)
                usage_id = f"{call.service_name}.{api.api_name}.{idx}"
                role = self._infer_business_role(usage_id, call, api)
                usage = OpenAPIUsage(
                    usage_id=usage_id,
                    service_name=api.service_name,
                    api_name=api.api_name,
                    api_path=api.api_path,
                    http_method=api.http_method,
                    required_params=api.required_params,
                    optional_params=api.optional_params,
                    actual_params=call.call_arguments,
                    source_sdk_call=SourceSdkCall(
                        service_name=call.service_name,
                        client_class=call.client_class,
                        method_name=call.method_name,
                        source_file=call.source_file,
                        line_number=call.line_number,
                        call_arguments=call.call_arguments,
                    ),
                    business_role=role,
                )
                openapi_usages.append(usage)
                api_business_roles.append(role)
            else:
                mapping_failures.append(f"{call.service_name}.{call.method_name}")

        self.log.info(
            f"SDK analysis: {len(openapi_usages)} Open APIs mapped, "
            f"{len(mapping_failures)} mappings failed"
        )

        from src.models.report import ReportStageResult
        stage_1 = ReportStageResult(
            stage_name="第一阶段：Open API接口清单",
            summary=f"共识别{len(openapi_usages)}个Open API接口。",
            details={
                "total": len(openapi_usages),
                "mandatory": sum(1 for r in api_business_roles if r.is_mandatory == IsMandatory.MANDATORY),
                "mapping_failures": mapping_failures,
            },
        )

        return SdkAnalysisResult(
            api_interfaces=api_interfaces,
            openapi_usages=openapi_usages,
            api_business_roles=api_business_roles,
            sdk_calls=sdk_calls,
            mapping_failures=mapping_failures,
            stage_1_result=stage_1,
        )

    # ============================================================
    # Unified SDK call extraction
    # ============================================================

    def _extract_sdk_calls(self, skill_path: str) -> List[SdkCallInfo]:
        path = Path(skill_path)
        py_files = list(find_files(path, extensions={".py"}))

        file_trees: Dict[str, ast.AST] = {}
        for file_path in py_files:
            content = read_file_content(file_path)
            if not content:
                continue
            try:
                tree = ast.parse(content)
                rel = str(file_path.relative_to(path))
                file_trees[rel] = tree
            except SyntaxError:
                continue

        symtab = self._build_symbol_table(file_trees)

        calls: List[SdkCallInfo] = []
        for rel, tree in file_trees.items():
            file_calls = self._find_api_calls_in_file(tree, rel, symtab)
            calls.extend(file_calls)

        seen: Set[Tuple[str, str]] = set()
        unique_calls: List[SdkCallInfo] = []
        unresolved: List[SdkCallInfo] = []
        for call in calls:
            key = (call.service_name, call.method_name)
            if key in seen:
                continue
            seen.add(key)
            if call.service_name:
                unique_calls.append(call)
            else:
                unresolved.append(call)

        if unresolved:
            self.log.info(
                f"{len(unresolved)} calls with unresolved service_name: "
                + ", ".join(f"{c.client_class}.{c.method_name}" for c in unresolved[:5])
            )

        return unique_calls

    # ============================================================
    # Symbol table: unified representation of all names
    # ============================================================

    def _build_symbol_table(self, file_trees: Dict[str, ast.AST]) -> dict:
        symtab = {
            "client_classes": {},
            "var_types": {},
            "func_returns": {},
            "local_str_vars": {},
        }

        for _round in range(3):
            for rel, tree in file_trees.items():
                self._scan_symbols(tree, symtab)

        return symtab

    def _scan_symbols(self, tree: ast.AST, symtab: dict):
        client_classes = symtab["client_classes"]
        var_types = symtab["var_types"]
        func_returns = symtab["func_returns"]
        local_str_vars = symtab["local_str_vars"]

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "huaweicloudsdk" in node.module:
                    for alias in node.names:
                        real_name = alias.name
                        local_name = alias.asname or alias.name
                        if real_name == "*":
                            cls = self._infer_client_from_module(node.module)
                            if cls:
                                client_classes[cls] = cls
                        elif real_name.endswith("Client"):
                            client_classes[local_name] = real_name

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    var_key = self._assign_target_key(target)
                    if not var_key:
                        continue
                    cls = self._resolve_assign_to_client(node.value, client_classes, func_returns)
                    if cls:
                        var_types[var_key] = cls
                    str_val = self._extract_string_value(node.value)
                    if str_val:
                        local_str_vars[f"__str__{var_key}"] = str_val
                    svc = self._extract_service_from_host(node.value)
                    if svc:
                        local_str_vars[f"__svc__{var_key}"] = svc

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                ret_cls = self._resolve_return_annotation(node.returns, client_classes)
                if ret_cls:
                    func_returns[node.name] = ret_cls
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            var_key = self._assign_target_key(target)
                            if not var_key:
                                continue
                            if var_key not in var_types:
                                cls = self._resolve_assign_to_client(child.value, client_classes, func_returns)
                                if cls:
                                    var_types[var_key] = cls

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.Return) and child.value:
                        cls = self._resolve_expr_to_client(child.value, client_classes, func_returns, var_types)
                        if cls:
                            func_returns[node.name] = cls

    def _resolve_return_annotation(self, returns: Optional[ast.expr], client_classes: dict) -> Optional[str]:
        if isinstance(returns, ast.Name):
            if returns.id in client_classes:
                return client_classes[returns.id]
            if returns.id.endswith("Client"):
                return returns.id
        if isinstance(returns, ast.Attribute):
            if returns.attr in client_classes:
                return client_classes[returns.attr]
            if returns.attr.endswith("Client"):
                return returns.attr
        return None

    @staticmethod
    def _assign_target_key(target: ast.expr) -> Optional[str]:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name):
                if target.value.id == "self":
                    return f"self.{target.attr}"
                return f"{target.value.id}.{target.attr}"
        return None

    def _resolve_assign_to_client(self, value: ast.expr, client_classes: dict, func_returns: dict) -> Optional[str]:
        if not isinstance(value, ast.Call):
            return None

        func = value.func
        if isinstance(func, ast.Name):
            name = func.id
            if name in client_classes:
                return client_classes[name]
            if name.endswith("Client"):
                return name
            if name in func_returns:
                return func_returns[name]
            if name.startswith("create_") and name.endswith("_client"):
                return self._factory_name_to_client(name)
            if name.startswith("get_") and name.endswith("_client"):
                return self._factory_name_to_client(name)
            if name.startswith("_init_") and name in func_returns:
                return func_returns[name]

        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr == "new_builder" and isinstance(func.value, ast.Name):
                return client_classes.get(func.value.id, func.value.id)
            if attr == "build":
                cls = self._trace_builder_class(func.value)
                if cls:
                    return client_classes.get(cls, cls)
            if attr.endswith("Client"):
                return attr
            if attr in func_returns:
                return func_returns[attr]
            if attr.startswith("create_") and attr.endswith("_client"):
                return self._factory_name_to_client(attr)
            if attr.startswith("get_") and attr.endswith("_client"):
                return self._factory_name_to_client(attr)
            if isinstance(func.value, ast.Name) and func.value.id == "self" and attr in func_returns:
                return func_returns[attr]

        return None

    def _resolve_expr_to_client(self, value: ast.expr, client_classes: dict, func_returns: dict, var_types: dict) -> Optional[str]:
        cls = self._resolve_assign_to_client(value, client_classes, func_returns)
        if cls:
            return cls
        if isinstance(value, ast.Name):
            return var_types.get(value.id)
        return None

    @staticmethod
    def _factory_name_to_client(name: str) -> str:
        if name.startswith("create_"):
            svc = name[len("create_"):]
        elif name.startswith("get_"):
            svc = name[len("get_"):]
        else:
            return ""
        if svc.endswith("_client"):
            svc = svc[:-len("_client")]
        if not svc or svc == "client":
            return ""
        return f"{svc.capitalize()}Client"

    def _trace_builder_class(self, node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "new_builder" and isinstance(node.func.value, ast.Name):
                return node.func.value.id
            return self._trace_builder_class(node.func.value)
        return None

    @staticmethod
    def _infer_client_from_module(module: str) -> Optional[str]:
        match = re.match(r"huaweicloudsdk([a-z][a-z0-9]*)", module)
        if match:
            svc = match.group(1)
            return f"{svc[0].upper()}{svc[1:]}Client"
        return None

    # ============================================================
    # Unified API call finder: one question per Call node
    # "Does this call result in an HTTP request to a Huawei Cloud API?"
    # ============================================================

    def _find_api_calls_in_file(self, tree: ast.AST, rel: str, symtab: dict) -> List[SdkCallInfo]:
        calls: List[SdkCallInfo] = []
        client_classes = symtab["client_classes"]
        var_types = symtab["var_types"]
        func_returns = symtab["func_returns"]
        local_str_vars = symtab["local_str_vars"]

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                local_vt: Dict[str, str] = {}
                local_sv: Dict[str, str] = {}
                for child in ast.walk(node):
                    setattr(child, '_parent_func', node.name)
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            key = self._assign_target_key(target)
                            if not key:
                                continue
                            cls = self._resolve_assign_to_client(child.value, client_classes, func_returns)
                            if cls:
                                local_vt[key] = cls
                            combined_for_resolve = dict(local_str_vars)
                            combined_for_resolve.update(local_sv)
                            str_val = self._resolve_string_expr(child.value, combined_for_resolve)
                            if str_val:
                                local_sv[f"__str__{key}"] = str_val
                                svc = self._parse_url_to_service(str_val) or self._parse_host_to_service(str_val)
                                if svc:
                                    local_sv[f"__svc__{key}"] = svc
                            if isinstance(child.value, ast.Call) and self._is_urlparse_call(child.value):
                                source = self._resolve_string_expr(child.value.args[0], combined_for_resolve) if child.value.args else None
                                if source:
                                    local_sv[f"__urlparse__{key}"] = source

                combined_vt = dict(var_types)
                combined_vt.update(local_vt)
                combined_sv = dict(local_str_vars)
                combined_sv.update(local_sv)

                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        call_info = self._classify_call(child, client_classes, combined_vt, func_returns, combined_sv)
                        if call_info:
                            service_name, client_class, method_name, call_args = call_info
                            calls.append(SdkCallInfo(
                                service_name=service_name,
                                client_class=client_class,
                                method_name=method_name,
                                call_arguments=call_args,
                                source_file=rel,
                                line_number=child.lineno if hasattr(child, "lineno") else 0,
                            ))

        module_level_calls = self._find_module_level_calls(tree, rel, client_classes, var_types, func_returns, local_str_vars)
        calls.extend(module_level_calls)

        return calls

    def _find_module_level_calls(self, tree: ast.AST, rel: str, client_classes: dict, var_types: dict, func_returns: dict, local_str_vars: dict) -> List[SdkCallInfo]:
        calls: List[SdkCallInfo] = []
        func_def_ranges: Set[Tuple[int, int]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                end_lineno = getattr(node, 'end_lineno', node.lineno + 100)
                func_def_ranges.add((node.lineno, end_lineno))

        def _in_function(node: ast.AST) -> bool:
            lineno = getattr(node, 'lineno', 0)
            for start, end in func_def_ranges:
                if start <= lineno <= end:
                    return True
            return False

        module_vt = dict(var_types)
        module_sv = dict(local_str_vars)

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and not _in_function(node):
                for target in node.targets:
                    key = self._assign_target_key(target)
                    if not key:
                        continue
                    cls = self._resolve_assign_to_client(node.value, client_classes, func_returns)
                    if cls:
                        module_vt[key] = cls
                    str_val = self._resolve_string_expr(node.value, local_str_vars)
                    if str_val:
                        module_sv[f"__str__{key}"] = str_val
                        svc = self._parse_url_to_service(str_val) or self._parse_host_to_service(str_val)
                        if svc:
                            module_sv[f"__svc__{key}"] = svc

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and not _in_function(node):
                call_info = self._classify_call(node, client_classes, module_vt, func_returns, module_sv)
                if call_info:
                    service_name, client_class, method_name, call_args = call_info
                    calls.append(SdkCallInfo(
                        service_name=service_name,
                        client_class=client_class,
                        method_name=method_name,
                        call_arguments=call_args,
                        source_file=rel,
                        line_number=node.lineno if hasattr(node, "lineno") else 0,
                    ))

        return calls

    def _classify_call(self, node: ast.Call, client_classes: dict, var_types: dict, func_returns: dict, local_str_vars: dict) -> Optional[Tuple[str, str, str, dict]]:
        if isinstance(node.func, ast.Name):
            callee_name = node.func.id
            result = self._classify_helper_call(node, callee_name, local_str_vars)
            if result:
                return result
            return None

        if not isinstance(node.func, ast.Attribute):
            return None

        method_name = node.func.attr
        if method_name == "new_builder" or method_name == "build" or method_name.startswith("with_"):
            return None
        receiver = node.func.value

        if isinstance(receiver, ast.Name):
            var_name = receiver.id
            client_class = self._resolve_var_to_client(var_name, client_classes, var_types, func_returns)
            if client_class:
                service_name = self._client_class_to_service(client_class)
                if service_name:
                    return (service_name, client_class, method_name, self._extract_call_arguments(node))

        if isinstance(receiver, ast.Attribute):
            if isinstance(receiver.value, ast.Name) and receiver.value.id == "self":
                var_key = f"self.{receiver.attr}"
                client_class = var_types.get(var_key)
                if client_class:
                    service_name = self._client_class_to_service(client_class)
                    if service_name:
                        return (service_name, client_class, method_name, self._extract_call_arguments(node))

        if isinstance(receiver, ast.Name) and receiver.id == "requests":
            if method_name in ("post", "get", "delete", "put", "patch", "request"):
                effective_method = method_name
                if method_name == "request" and node.args:
                    method_val = self._resolve_string_expr(node.args[0], local_str_vars)
                    if method_val and method_val.upper() in ("POST", "GET", "DELETE", "PUT", "PATCH"):
                        effective_method = method_val.lower()
                    else:
                        effective_method = "get"
                return self._classify_requests_call(node, effective_method, local_str_vars)


        return None

    def _classify_helper_call(self, node: ast.Call, callee_name: str, local_str_vars: dict) -> Optional[Tuple[str, str, str, dict]]:
        service_name = None
        resource_path = ""
        http_method = "GET"

        for kw in node.keywords:
            if kw.arg == "host":
                service_name = self._extract_service_from_host(kw.value)
                if not service_name:
                    host_value = self._resolve_string_expr(kw.value, local_str_vars)
                    if host_value:
                        service_name = self._parse_host_to_service(host_value) or self._parse_url_to_service(host_value)
            if kw.arg == "resource_path":
                resource_path = self._resolve_string_expr(kw.value, local_str_vars) or ""
            if kw.arg == "method":
                mv = self._extract_string_value(kw.value)
                if mv:
                    http_method = mv.upper()

        if node.args and len(node.args) >= 2:
            method_val = self._resolve_string_expr(node.args[0], local_str_vars)
            if method_val and method_val.upper() in ("POST", "GET", "DELETE", "PUT", "PATCH"):
                http_method = method_val.upper()
            if not service_name:
                service_name = self._extract_service_from_host(node.args[1])
                if not service_name:
                    host_value = self._resolve_string_expr(node.args[1], local_str_vars)
                    if host_value:
                        service_name = self._parse_host_to_service(host_value) or self._parse_url_to_service(host_value)
            if not resource_path and len(node.args) >= 3:
                resource_path = self._resolve_string_expr(node.args[2], local_str_vars) or ""

        if not service_name and resource_path:
            service_name = self._infer_service_from_resource_path(resource_path)

        if not service_name:
            return None

        api_name, method_name = self._infer_api_from_context(node, http_method.lower(), resource_path)
        return (service_name, "SignerBypass", method_name, {"resource_path": resource_path, "http_method": http_method})

    def _collect_string_context(self, root: ast.AST, context: dict, exclude_func: bool = False):
        for node in ast.walk(root):
            if exclude_func and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                key = self._assign_target_key(target)
                if not key:
                    continue
                value = self._resolve_string_expr(node.value, context)
                if value:
                    context[f"__str__{key}"] = value
                    svc = self._parse_url_to_service(value) or self._parse_host_to_service(value)
                    if svc:
                        context[f"__svc__{key}"] = svc
                if isinstance(node.value, ast.Call) and self._is_urlparse_call(node.value):
                    source = self._resolve_string_expr(node.value.args[0], context) if node.value.args else None
                    if source:
                        context[f"__urlparse__{key}"] = source

    def _resolve_string_expr(self, node: ast.expr, context: dict) -> Optional[str]:
        direct = self._extract_string_value(node)
        if direct:
            return direct
        if isinstance(node, ast.Name):
            return context.get(f"__str__{node.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._resolve_string_expr(node.left, context)
            right = self._resolve_string_expr(node.right, context)
            if left is not None and right is not None:
                return left + right
            if left is not None:
                return left
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                source = context.get(f"__urlparse__{node.value.id}")
                if source:
                    if node.attr == "path":
                        return self._parse_url_to_path(source)
                    if node.attr == "hostname":
                        return self._parse_url_to_host(source)
        return None

    @staticmethod
    def _is_urlparse_call(node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name) and node.func.id == "urlparse":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "urlparse":
            return True
        return False

    def _resolve_var_to_client(self, var_name: str, client_classes: dict, var_types: dict, func_returns: dict) -> Optional[str]:
        if var_name in var_types:
            return var_types[var_name]
        if var_name in client_classes:
            return client_classes[var_name]
        if var_name in func_returns:
            return func_returns[var_name]

        return None

    def _classify_requests_call(self, node: ast.Call, http_method: str, local_str_vars: dict) -> Optional[Tuple[str, str, str, dict]]:
        url_str = self._extract_url_from_requests_call(node, http_method, local_str_vars)
        service_name = None
        resource_path = ""

        if url_str:
            service_name = self._parse_url_to_service(url_str)
            resource_path = self._parse_url_to_path(url_str)

        if not service_name:
            host_svc = local_str_vars.get("__svc__host") or local_str_vars.get("__svc__request.host")
            if host_svc:
                service_name = host_svc

        if not resource_path:
            rp = local_str_vars.get("__str__resource_path") or local_str_vars.get("__str__request.resource_path")
            if rp:
                resource_path = rp

        method_from_sdk_req = local_str_vars.get("__str__request.method")
        if method_from_sdk_req and http_method.lower() == "get":
            http_method = method_from_sdk_req.lower()

        if not service_name:
            service_name, resource_path = self._find_api_info_from_call_context(node, local_str_vars)

        if not service_name:
            return None

        api_name, method_name = self._infer_api_from_context(node, http_method, resource_path)

        return (service_name, "SignerBypass", method_name, {"resource_path": resource_path, "http_method": http_method.upper()})

    def _extract_url_from_requests_call(self, node: ast.Call, http_method: str, local_str_vars: dict) -> Optional[str]:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "request":
            if len(node.args) >= 2:
                return self._resolve_string_expr(node.args[1], local_str_vars)
        elif node.args:
            return self._resolve_string_expr(node.args[0], local_str_vars)
        for kw in node.keywords:
            if kw.arg == "url":
                val = self._resolve_string_expr(kw.value, local_str_vars)
                if val:
                    return val
        return None

    def _find_api_info_from_call_context(self, node: ast.Call, local_str_vars: dict) -> Tuple[Optional[str], str]:
        service_name = None
        resource_path = ""

        for kw in node.keywords:
            if kw.arg == "host":
                service_name = self._extract_service_from_host(kw.value)
                if not service_name:
                    host_value = self._resolve_string_expr(kw.value, local_str_vars)
                    if host_value:
                        service_name = self._parse_host_to_service(host_value) or self._parse_url_to_service(host_value)
            if kw.arg == "resource_path":
                resource_path = self._resolve_string_expr(kw.value, local_str_vars) or ""

        if node.args and len(node.args) >= 2:
            if not service_name:
                service_name = self._extract_service_from_host(node.args[1])
                if not service_name:
                    host_value = self._resolve_string_expr(node.args[1], local_str_vars)
                    if host_value:
                        service_name = self._parse_host_to_service(host_value) or self._parse_url_to_service(host_value)
            if not resource_path and len(node.args) >= 3:
                resource_path = self._resolve_string_expr(node.args[2], local_str_vars) or ""

        if not service_name and resource_path:
            service_name = self._infer_service_from_resource_path(resource_path)

        return service_name, resource_path

    def _infer_api_from_context(self, node: ast.Call, http_method: str, resource_path: str) -> Tuple[str, str]:
        path_api = self._infer_api_from_path(http_method, resource_path)
        if path_api:
            return path_api

        func_name = self._find_enclosing_function_name(node)
        if func_name:
            api_name = self._func_name_to_api_name(func_name, http_method)
            method_name = self._api_name_to_method(api_name, http_method)
            return api_name, method_name

        return "UnknownApi", f"{http_method.lower()}_unknown"

    def _infer_api_from_path(self, http_method: str, resource_path: str) -> Optional[Tuple[str, str]]:
        if not resource_path:
            return None
        normalized = resource_path.split("?", 1)[0]
        special = {
            ("GET", "/v3/projects"): ("KeystoneListProjects", "keystone_list_projects"),
            ("POST", "/v1/light-instances"): ("CreateLightInstances", "create_light_instances"),
            ("GET", "/v1/resources"): ("ListResources", "list_resources"),
        }
        key = (http_method.upper(), normalized)
        if key in special:
            return special[key]
        parts = [p for p in normalized.split("/") if p and not p.startswith("{") and not re.fullmatch(r"v\d+", p)]
        if not parts:
            return None
        resource = parts[-1].replace("-", "_")
        singular = resource[:-1] if resource.endswith("s") else resource
        pascal = "".join(p.capitalize() for p in resource.split("_"))
        known_actions = ["show", "list", "create", "delete", "update", "get", "set", "put", "batch", "check", "query", "count", "register", "unregister", "enable", "disable", "start", "stop", "restart", "reboot", "cancel", "confirm", "validate", "authorize", "revoke", "grant", "associate", "disassociate", "download", "upload", "export", "import", "reset", "renew", "release", "lock", "unlock", "freeze", "unfreeze"]
        resource_lower = resource.lower()
        for act in known_actions:
            if resource_lower.startswith(act + "_") or resource_lower == act:
                api_name = pascal
                method_name = resource
                return api_name, method_name
        if http_method.upper() == "GET":
            action = "Show" if normalized.rstrip("/").endswith("}") else "List"
        elif http_method.upper() == "POST":
            action = "Create"
        elif http_method.upper() == "DELETE":
            action = "Delete"
        else:
            action = "Update"
        api_name = f"{action}{pascal}"
        method_name = f"{action.lower()}_{resource}"
        return api_name, method_name

    @staticmethod
    def _find_enclosing_function_name(node: ast.AST) -> Optional[str]:
        if hasattr(node, '_parent_func'):
            return node._parent_func
        return None

    # ============================================================
    # String / URL / Host extraction utilities
    # ============================================================

    def _extract_string_value(self, node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    parts.append("{var}")
            return "".join(parts)
        return None

    def _extract_service_from_host(self, node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self._parse_host_to_service(node.value)
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    service = self._parse_host_to_service(part.value)
                    if service:
                        return service
        return None

    @staticmethod
    def _parse_host_to_service(host_str: str) -> Optional[str]:
        match = re.search(r"([a-z][a-z0-9]*)\.[a-z]+[0-9]*\.myhuaweicloud\.com", host_str)
        if match:
            return match.group(1)
        match = re.search(r"([a-z][a-z0-9]*)\.", host_str)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_url_to_service(url_str: str) -> Optional[str]:
        match = re.search(r"([a-z][a-z0-9]*)\.[a-z]+[0-9]*\.myhuaweicloud\.(?:com|eu)", url_str)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_url_to_path(url_str: str) -> str:
        match = re.search(r"myhuaweicloud\.(?:com|eu)(/[^?]*)", url_str)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _parse_url_to_host(url_str: str) -> str:
        match = re.search(r"https?://([^/]+)", url_str)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _infer_service_from_resource_path(resource_path: str) -> Optional[str]:
        path_service_map = {
            "/maas/": "modelarts",
            "/v1/{project_id}/maas/": "modelarts",
            "/v2/{project_id}/fgs/": "functiongraph",
        }
        for prefix, service in path_service_map.items():
            if prefix in resource_path:
                return service
        return None

    # ============================================================
    # Name conversion utilities
    # ============================================================

    @staticmethod
    def _func_name_to_api_name(func_name: str, http_method: str) -> str:
        name = func_name.lower()
        if name.startswith("create_") or name.startswith("add_"):
            resource = func_name.split("_", 1)[1] if "_" in func_name else func_name
            return f"Create{resource.capitalize()}"
        if name.startswith("delete_") or name.startswith("remove_"):
            resource = func_name.split("_", 1)[1] if "_" in func_name else func_name
            return f"Delete{resource.capitalize()}"
        if name.startswith("update_") or name.startswith("modify_"):
            resource = func_name.split("_", 1)[1] if "_" in func_name else func_name
            return f"Update{resource.capitalize()}"
        if name.startswith("list_") or name.startswith("get_all_"):
            resource = func_name.split("_", 1)[1] if "_" in func_name else func_name
            return f"List{resource.capitalize()}"
        if name.startswith("check_") or name.startswith("show_") or name.startswith("get_"):
            resource = func_name.split("_", 1)[1] if "_" in func_name else func_name
            return f"Show{resource.capitalize()}"
        if http_method.lower() == "post":
            return f"Create{func_name.capitalize()}"
        if http_method.lower() == "delete":
            return f"Delete{func_name.capitalize()}"
        if http_method.lower() == "get":
            return f"Show{func_name.capitalize()}"
        return func_name.capitalize()

    @staticmethod
    def _api_name_to_method(api_name: str, http_method: str) -> str:
        for prefix, action in [("Create", "create"), ("Delete", "delete"), ("Update", "update"), ("List", "list"), ("Show", "show"), ("Get", "get")]:
            if api_name.startswith(prefix):
                resource = api_name[len(prefix):]
                return f"{action}_{resource.lower()}"
        return api_name[0].lower() + api_name[1:]

    @staticmethod
    def _client_class_to_service(client_class: str) -> str:
        if client_class.endswith("Client"):
            return client_class[:-6]
        return ""

    # ============================================================
    # AST call argument extraction
    # ============================================================

    def _extract_call_arguments(self, node: ast.Call) -> Dict[str, str]:
        args: Dict[str, str] = {}
        for kw in node.keywords:
            if kw.arg:
                args[kw.arg] = self._ast_to_str(kw.value)
        return args

    def _ast_to_str(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return str(node.value)
        elif isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._ast_to_str(node.value)}.{node.attr}"
        return "..."

    # ============================================================
    # API mapping (SDK source + signer bypass fallback)
    # ============================================================

    def _map_to_api(self, call: SdkCallInfo, sdk_manager: SdkSourceManager, service_index: Dict[str, Path]) -> Optional[ApiInterface]:
        if not call.service_name:
            return None

        if call.client_class == "SignerBypass":
            return self._map_signer_bypass_to_api(call)

        service_lower = call.service_name.lower()
        if service_lower not in service_index:
            self.log.info(f"Service not found in SDK source: {call.service_name}")
            return None

        http_info = sdk_manager.get_http_info(call.service_name, call.method_name)
        if not http_info or not http_info.get("resource_path"):
            self.log.info(f"HTTP info not found: {call.service_name}.{call.method_name}")
            return None

        try:
            http_method = HttpMethod(http_info.get("method", "GET"))
        except ValueError:
            http_method = HttpMethod.POST

        required_params: List[ApiParameter] = []
        optional_params: List[ApiParameter] = []

        request_type = http_info.get("request_type", "")
        if request_type:
            model_info = sdk_manager.get_request_model(call.service_name, request_type)
            if model_info:
                attr_map = model_info.get("attribute_map", {})
                openapi_types = model_info.get("openapi_types", {})
                for param in model_info.get("required_params", []):
                    api_name = attr_map.get(param, param)
                    p_type = openapi_types.get(param, "str")
                    required_params.append(ApiParameter(name=api_name, param_type=p_type, is_required=True))
                for param in model_info.get("optional_params", []):
                    api_name = attr_map.get(param, param)
                    p_type = openapi_types.get(param, "str")
                    optional_params.append(ApiParameter(name=api_name, param_type=p_type, is_required=False))

        api_name = self._method_to_api_name(call.method_name)

        return ApiInterface(
            service_name=call.service_name,
            api_name=api_name,
            api_path=http_info.get("resource_path", ""),
            http_method=http_method,
            required_params=required_params,
            optional_params=optional_params,
        )

    def _map_signer_bypass_to_api(self, call: SdkCallInfo) -> Optional[ApiInterface]:
        api_name = self._method_to_api_name(call.method_name)
        resource_path = call.call_arguments.get("resource_path", "")
        http_method_str = call.call_arguments.get("http_method", "GET")
        try:
            http_method = HttpMethod(http_method_str)
        except ValueError:
            http_method = HttpMethod.GET

        return ApiInterface(
            service_name=call.service_name,
            api_name=api_name,
            api_path=resource_path,
            http_method=http_method,
            required_params=[],
            optional_params=[],
        )

    # ============================================================
    # Business role inference
    # ============================================================

    def _infer_business_role(self, usage_id: str, call: SdkCallInfo, api: ApiInterface) -> ApiBusinessRole:
        action = self._action_from_method(call.method_name)
        resource = self._resource_from_path(api.api_path)
        role_text = f"通过{api.service_name}服务执行{action or api.api_name}操作"
        if resource:
            role_text += f"，作用对象为{resource}"
        stage = "资源查询" if action in ("list", "show", "get") else "资源变更"
        state_impact = "读取云资源状态" if action in ("list", "show", "get") else "变更云资源状态或配置"
        return ApiBusinessRole(
            usage_id=usage_id,
            business_role=role_text,
            business_stage=stage,
            is_mandatory=IsMandatory.MANDATORY,
            state_impact=state_impact,
            rule_basis=f"根据SDK方法名{call.method_name}、HTTP方法{api.http_method.value}和API路径{api.api_path}推断",
        )

    @staticmethod
    def _action_from_method(method_name: str) -> str:
        lower = method_name.lower()
        for action in ["create", "delete", "update", "show", "get", "list", "batch", "set", "put", "check", "hibernate", "awake", "scale"]:
            if action in lower:
                return action
        return ""

    @staticmethod
    def _resource_from_path(path: str) -> str:
        parts = [p for p in (path or "").split("/") if p and not p.startswith("{") and not p.startswith("v")]
        return parts[-1] if parts else ""

    @staticmethod
    def _method_to_api_name(method_name: str) -> str:
        parts = re.split(r"_", method_name)
        return "".join(p.capitalize() for p in parts if p)
