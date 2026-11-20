"""[插件市场-A1] plugin.json 清单标准 + L0 校验器测试（Issue #1079）。

Seam 约定（只测公开接口，不测内部实现）：
- check_schema(manifest) -> list[ValidationError]          ① schema 结构校验
- check_binaries(plugin_dir, manifest) -> list[...]        ② 禁二进制扫描
- check_commands(manifest, prober=...) -> list[...]        ③ 免凭据命令探测（401 分层）
- validate(plugin_dir, prober=None) -> ValidationReport / validate_for_publish(plugin_dir)
  发布门禁 + POST /api/plugin-market/validate
"""
import json
import os
import re

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.mgmt.plugin_market.validator import (
    check_binaries,
    check_commands,
    check_schema,
    classify_probe_status,
    probe_cli,
    probe_http,
    probe_mcp_stdio,
    validate,
    validate_for_publish,
    validate_manifest,
    ProbeResult,
)

# D7（总纲 #1078）：平台 6 引擎单一来源（与 src/agent/executor.py _BACKENDS 一致）
from src.domains.mgmt.plugin_market.schema import PLATFORM_ENGINES as SIX_ENGINES

pytestmark = pytest.mark.usefixtures("plugin_market_stubs")


def _write_plugin_tree(tmp_path, files: dict, manifest: dict) -> str:
    """共享落盘助手：写 plugin.json（manifest）+ 附加文件树，返回插件目录路径。"""
    for rel, content in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    (tmp_path / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(tmp_path)


class TestSchemaCheck:
    def test_minimal_valid_manifest_passes(self):
        assert check_schema(self._manifest_factory()) == []

    def test_missing_required_top_fields_reports_paths(self):
        errors = check_schema({})
        paths = " ".join(e.path for e in errors)
        for field in ("name", "version", "adapters", "commands"):
            assert field in paths, f"缺少 {field} 的错误定位（含字段路径），got: {paths}"

    def test_invalid_transport_rejected(self):
        m = self._manifest_factory()
        m["adapters"][0]["transport"] = "ftp"
        errors = check_schema(m)
        assert errors and any("adapters[0].transport" in e.path for e in errors)

    def test_bad_version_pattern_rejected(self):
        m = self._manifest_factory()
        m["version"] = "v1"
        errors = check_schema(m)
        assert errors and any("version" in e.path for e in errors)

    def test_unknown_top_level_property_rejected(self):
        m = self._manifest_factory()
        m["hacker_extra"] = True
        errors = check_schema(m)
        assert errors and any("hacker_extra" in e.path for e in errors)

    def test_cli_adapter_requires_all_six_engines(self):
        # D7 强声明：cli/http 适配器必须声明全部 6 引擎
        m = self._manifest_factory()
        m["adapters"][0]["compat"]["engines"] = ["opencode"]
        errors = check_schema(m)
        assert errors and any("adapters[0].compat.engines" in e.path for e in errors)

    def test_mcp_adapter_non_codex_engine_rejected(self):
        # D7：mcp 适配器初期限 codex
        m = self._manifest_factory()
        m["adapters"] = [
            {
                "id": "mcp",
                "transport": "mcp-stdio",
                "command": "demo-mcp",
                "compat": {"engines": ["opencode"]},
            }
        ]
        m["commands"][0]["adapters"] = ["mcp"]
        errors = check_schema(m)
        assert errors and any("compat.engines" in e.path for e in errors)

    def test_mcp_adapter_missing_url_rejected(self):
        m = self._manifest_factory()
        m["adapters"] = [
            {
                "id": "remote",
                "transport": "mcp-remote",
                "compat": {"engines": ["codex"]},
            }
        ]
        m["commands"][0]["adapters"] = ["remote"]
        errors = check_schema(m)
        assert errors and any("adapters[0]" in e.path for e in errors)

    def test_credential_reference_integrity(self):
        # 语义检查：adapter.auth.credential 必须指向 credentials 声明的键
        m = self._manifest_factory()
        m["adapters"][0]["auth"] = {"credential": "GHOST_KEY", "inject": "env", "env": "GHOST_KEY"}
        errors = check_schema(m)
        assert errors and any("GHOST_KEY" in e.reason for e in errors)

    def test_command_references_unknown_adapter_rejected(self):
        m = self._manifest_factory()
        m["commands"][0]["adapters"] = ["nonexistent"]
        errors = check_schema(m)
        assert errors and any("nonexistent" in e.reason for e in errors)

    def test_duplicate_adapter_id_rejected(self):
        m = self._manifest_factory()
        m["adapters"].append(dict(m["adapters"][0]))
        errors = check_schema(m)
        assert errors and any("default" in e.reason for e in errors)

    def test_error_structure_has_check_path_reason_hint(self):
        m = self._manifest_factory()
        m["version"] = "bad"
        errors = check_schema(m)
        e = errors[0]
        assert e.check == "schema"
        assert e.path
        assert e.reason
        assert e.hint
        assert e.severity == "error"
        d = e.as_dict()
        assert {"check", "code", "path", "reason", "hint", "severity"} <= set(d)


class TestBinaryScan:
    """② 禁二进制扫描：magic bytes / 文件后缀 / 命令路径白名单核验。"""

    def test_text_only_package_passes(self, tmp_path):
        d = _write_plugin_tree(tmp_path, {
            "bin/demo-weather.sh": "#!/bin/sh\necho ok\n",
            "skills/demo/SKILL.md": "# demo\n",
        }, self._manifest_factory())
        assert check_binaries(d, self._manifest_factory()) == []

    def test_elf_magic_rejected(self, tmp_path):
        d = _write_plugin_tree(tmp_path, {"bin/tool": b"\x7fELF\x02\x01\x01\x00payload"},
                               self._manifest_factory())
        errors = check_binaries(d, self._manifest_factory())
        assert errors and any("bin/tool" in e.path for e in errors)

    def test_pe_magic_rejected(self, tmp_path):
        d = _write_plugin_tree(tmp_path, {"bin/tool.exe": b"MZ\x90\x00payload"},
                               self._manifest_factory())
        errors = check_binaries(d, self._manifest_factory())
        assert errors and any("bin/tool.exe" in e.path for e in errors)

    def test_binary_suffix_rejected_even_if_content_text(self, tmp_path):
        d = _write_plugin_tree(tmp_path, {"lib/helper.pyc": "fake pyc content"},
                               self._manifest_factory())
        errors = check_binaries(d, self._manifest_factory())
        assert errors and any("lib/helper.pyc" in e.path for e in errors)

    def test_error_carries_reason_and_hint(self, tmp_path):
        d = _write_plugin_tree(tmp_path, {"bin/tool": b"\x7fELFpayload"}, self._manifest_factory())
        e = check_binaries(d, self._manifest_factory())[0]
        assert e.check == "binary" and e.reason and e.hint and e.severity == "error"

    def test_command_path_traversal_rejected(self, tmp_path):
        m = self._manifest_factory()
        m["adapters"][0]["command"] = "../../evil"
        errors = check_binaries(_write_plugin_tree(tmp_path, {}, m), m)
        assert errors and any("adapters[0].command" in e.path for e in errors)

    def test_exec_whitelist_must_cover_adapter_commands(self, tmp_path):
        m = self._manifest_factory()
        m["permissions"] = {"exec": ["other-tool"]}
        d = _write_plugin_tree(tmp_path, {}, self._manifest_factory())
        errors = check_binaries(d, m)
        assert errors and any("demo-weather" in e.reason and "exec" in (e.path + e.reason) for e in errors)

    def test_exec_whitelist_cover_passes(self, tmp_path):
        m = self._manifest_factory()
        m["permissions"] = {"exec": ["demo-weather"]}
        d = _write_plugin_tree(tmp_path, {}, m)
        assert check_binaries(d, m) == []

    def test_exec_whitelist_required_when_cli_command_declared(self, tmp_path):
        # 安全门禁：permissions.exec 未声明时白名单核验不得整体跳过
        # （否则 /validate probe=true 会以子进程真实执行未核验 command）
        m = self._manifest_factory()
        m.pop("permissions")  # 未声明 permissions.exec
        d = _write_plugin_tree(tmp_path, {}, self._manifest_factory())
        errors = check_binaries(d, m)
        assert errors and any("exec" in (e.path + e.reason) for e in errors)

    def test_exec_whitelist_required_for_mcp_stdio_command(self, tmp_path):
        m = self._manifest_factory()
        m["adapters"] = [{
            "id": "mcp", "transport": "mcp-stdio", "command": "demo-mcp",
            "compat": {"engines": ["codex"]},
        }]
        m["commands"][0]["adapters"] = ["mcp"]
        d = _write_plugin_tree(tmp_path, {}, self._manifest_factory())
        errors = check_binaries(d, m)
        assert errors and any("exec" in (e.path + e.reason) for e in errors)


class TestBundledSkillsPathSafety:
    """D1 路径安全：bundled_skills.path 禁止穿越/绝对路径（schema + 校验器双层）。"""

    def test_schema_rejects_traversal_segment(self):
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "evil", "path": "skills/../../etc"}]
        errors = check_schema(m)
        assert errors and any("bundled_skills[0].path" in e.path for e in errors)

    def test_schema_rejects_absolute_path(self):
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "evil", "path": "/etc/passwd"}]
        errors = check_schema(m)
        assert errors and any("bundled_skills[0].path" in e.path for e in errors)

    def test_schema_rejects_windows_drive_and_backslash(self):
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "evil", "path": "C:\\\\windows\\\\evil"}]
        errors = check_schema(m)
        assert errors and any("bundled_skills[0].path" in e.path for e in errors)

    def test_schema_accepts_relative_skill_path(self):
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "demo", "path": "skills/release-notes-helper"}]
        assert check_schema(m) == []

    def test_binary_scan_rejects_skill_path_traversal(self):
        # 纵深防御：校验器对 bundled_skills 路径穿越独立拒绝（不依赖 schema）
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "evil", "path": "../escape"}]
        errors = check_binaries("plugin-dir-unused-for-this-check", m)
        assert errors and any(e.code == "binary.skill_path" for e in errors)

    def test_binary_scan_rejects_skill_absolute_path(self):
        m = self._manifest_factory()
        m["bundled_skills"] = [{"name": "evil", "path": "/etc/passwd"}]
        errors = check_binaries("plugin-dir-unused-for-this-check", m)
        assert errors and any("bundled_skills[0].path" in e.path for e in errors)


class TestCommandProbe:
    """③ 命令有效性免凭据探测：方法名探测 + 401 分层。"""

    def test_probe_ok_passes(self):
        assert check_commands(self._manifest_factory(), prober=self._probe_ok) == []

    def test_probe_401_passes_with_credential_hint(self):
        # 401 分层：命令存在仅缺凭据 → 视为通过，但给出补凭据提示
        errors = check_commands(self._manifest_factory(), prober=self._probe_auth_required)
        assert not [e for e in errors if e.severity == "error"]
        assert errors and any("凭据" in e.hint for e in errors)

    def test_probe_404_rejected(self):
        errors = check_commands(self._manifest_factory(), prober=self._probe_invalid)
        assert errors and all(e.severity == "error" for e in errors)
        assert any("commands[0]" in e.path for e in errors)

    def test_probe_refused_without_exec_whitelist(self):
        # 安全门禁：permissions.exec 未声明 → 拒绝探测（不得以子进程执行未核验命令）
        calls = []

        def prober(command, adapter):
            calls.append(1)
            return ProbeResult(status="ok", evidence="stub")

        m = self._manifest_factory()
        m.pop("permissions")  # 未声明 permissions.exec
        errors = check_commands(m, prober=prober)
        assert calls == [], "未声明 exec 白名单时不得调用探测器（不得执行命令）"
        assert errors and all(e.severity == "error" for e in errors)
        assert all(e.code == "probe.exec_whitelist" for e in errors)

    def test_probe_refused_when_command_not_in_whitelist(self):
        calls = []

        def prober(command, adapter):
            calls.append(1)
            return ProbeResult(status="ok", evidence="stub")

        m = self._manifest_factory()
        m["permissions"] = {"exec": ["other-tool"]}
        errors = check_commands(m, prober=prober)
        assert calls == [], "command 不在白名单时不得调用探测器（不得执行命令）"
        assert errors and all(e.severity == "error" for e in errors)
        assert any("demo-weather" in e.reason for e in errors)

    def test_every_command_adapter_pair_probed(self):
        calls = []

        def prober(command, adapter):
            calls.append((command["name"], adapter["transport"]))
            return ProbeResult(status="ok", evidence="stub")

        m = self._manifest_factory()
        m["adapters"].append({
            "id": "http-impl", "transport": "http",
            "url": "https://api.example-weather.dev",
            "compat": {"engines": list(SIX_ENGINES)},
        })
        m["commands"][0]["adapters"] = ["default", "http-impl"]
        check_commands(m, prober=prober)
        assert ("weather.query", "cli") in calls
        assert ("weather.query", "http") in calls

    def test_classify_probe_status(self):
        assert classify_probe_status(200) == "ok"
        assert classify_probe_status(204) == "ok"
        assert classify_probe_status(405) == "ok"     # OPTIONS 不被支持但端点存在
        assert classify_probe_status(401) == "auth_required"
        assert classify_probe_status(403) == "auth_required"
        assert classify_probe_status(404) == "invalid"
        assert classify_probe_status(400) == "invalid"
        assert classify_probe_status(301) == "invalid"  # 3xx 重定向不跟随，不能证明命令存在
        assert classify_probe_status(302) == "invalid"
        assert classify_probe_status(500) == "invalid"  # 5xx 不能证明命令存在
        assert classify_probe_status(503) == "invalid"

    def test_probe_cli_reports_missing_binary(self, tmp_path):
        r = probe_cli("definitely-not-a-grape-demo-cli-9x7", [], cwd=str(tmp_path))
        assert r.status == "invalid"

    def test_probe_cli_help_ok(self, tmp_path):
        import stat
        script = tmp_path / "fake-cli"
        script.write_text("#!/bin/sh\necho 'usage: fake-cli'\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        r = probe_cli(str(script), [], cwd=str(tmp_path))
        assert r.status == "ok"

    def test_probe_http_401_layered(self):
        # 本地 401 端点：allow_private 供测试；分层判定 401 → auth_required
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_OPTIONS(self):
                self.send_response(401)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = probe_http(f"http://127.0.0.1:{srv.server_port}/mcp", allow_private=True)
            assert r.status == "auth_required"
        finally:
            srv.shutdown()

    def test_probe_http_unreachable_invalid(self):
        r = probe_http("http://127.0.0.1:1/mcp", allow_private=True, timeout=2)
        assert r.status == "invalid"

    def test_probe_http_private_host_blocked_by_default(self):
        r = probe_http("http://127.0.0.1:9/mcp")
        assert r.status == "invalid"
        assert "内网" in r.reason or "private" in r.reason.lower()

    def test_probe_http_redirect_not_followed(self):
        # SSRF 防护：3xx 重定向不跟随（目标不复核私网即隐式可达），重定向本身判无效
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hit = {"target": 0}

        class Redirector(BaseHTTPRequestHandler):
            def _redir(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/mcp")
                self.end_headers()

            do_OPTIONS = _redir
            do_HEAD = _redir

            def log_message(self, *a):
                pass

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hit["target"] += 1
                self.send_response(200)
                self.end_headers()

            do_OPTIONS = do_GET
            do_HEAD = do_GET

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Redirector)
        target = HTTPServer(("127.0.0.1", 0), Target)
        target_port = target.server_port
        for s in (srv, target):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            r = probe_http(f"http://127.0.0.1:{srv.server_port}/mcp", allow_private=True)
            assert r.status == "invalid"
            assert "重定向" in r.reason
            assert hit["target"] == 0, "重定向目标不得被访问"
        finally:
            srv.shutdown()
            target.shutdown()

    def test_probe_http_head_fallback(self):
        # Spec「http OPTIONS·HEAD」：OPTIONS 404 但 HEAD 可达 → 端点存在
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_OPTIONS(self):
                self.send_response(404)
                self.end_headers()

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = probe_http(f"http://127.0.0.1:{srv.server_port}/mcp", allow_private=True)
            assert r.status == "ok", f"{r.status}: {r.reason}"
        finally:
            srv.shutdown()

    def test_probe_http_5xx_rejected(self):
        # 5xx 不能证明命令存在（偏宽误判）
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def _boom(self):
                self.send_response(500)
                self.end_headers()

            do_OPTIONS = _boom
            do_HEAD = _boom

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = probe_http(f"http://127.0.0.1:{srv.server_port}/mcp", allow_private=True)
            assert r.status == "invalid"
        finally:
            srv.shutdown()

    def test_probe_mcp_stdio_exit0_without_jsonrpc_invalid(self, tmp_path):
        # mcp-stdio 方法名探测：exit=0 但无 JSON-RPC 响应不能证明 tools/list 可用
        import stat
        script = tmp_path / "fake-mcp"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        r = probe_mcp_stdio(str(script), [], cwd=str(tmp_path))
        assert r.status == "invalid"

    def test_probe_cli_timeout_invalid(self, tmp_path):
        # 超时不能证明命令存在：挂死进程不算有效命令（与 http 白名单式严格判定一致）
        import stat
        script = tmp_path / "hanging-cli"
        script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        r = probe_cli(str(script), [], cwd=str(tmp_path), timeout=0.5)
        assert r.status == "invalid"
        assert "超时" in r.reason

    def test_probe_mcp_stdio_timeout_invalid(self, tmp_path):
        # mcp-stdio 超时无 JSON-RPC 响应 → invalid（常驻但无响应不能证明 tools/list 可用）
        import stat
        script = tmp_path / "hanging-mcp"
        script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        r = probe_mcp_stdio(str(script), [], cwd=str(tmp_path), timeout=0.5)
        assert r.status == "invalid"
        assert "超时" in r.reason


class TestValidateEntry:
    """L0 validate 入口：三子检查合流 + 发布门禁。"""

    def _plugin_dir(self, tmp_path, files=None, manifest=None):
        # 与 TestBinaryScan 共用 _write_plugin_tree；默认文件树保持原样
        return _write_plugin_tree(
            tmp_path,
            files if files is not None else {"bin/demo-weather.sh": "#!/bin/sh\necho ok\n"},
            manifest or self._manifest_factory())

    def test_validate_ok_package(self, tmp_path):
        report = validate(self._plugin_dir(tmp_path), prober=self._probe_ok)
        assert report.valid is True
        assert report.errors == []
        assert report.to_dict()["valid"] is True

    def test_validate_combines_all_three_checks(self, tmp_path):
        m = self._manifest_factory()
        m["version"] = "bad"
        d = self._plugin_dir(tmp_path, files={"bin/evil": b"\x7fELFjunk"}, manifest=m)
        report = validate(d, prober=self._probe_invalid)
        assert report.valid is False
        checks = {e.check for e in report.errors}
        assert checks == {"schema", "binary", "probe"}

    def test_validate_missing_manifest_rejected(self, tmp_path):
        report = validate(str(tmp_path), prober=self._probe_ok)
        assert report.valid is False
        assert any("plugin.json" in e.reason for e in report.errors)

    def test_validate_manifest_not_json_rejected(self, tmp_path):
        (tmp_path / "plugin.json").write_text("not-json{", encoding="utf-8")
        report = validate(str(tmp_path), prober=self._probe_ok)
        assert report.valid is False
        assert any("JSON" in e.reason for e in report.errors)

    def test_validate_for_publish_returns_structured_dict(self, tmp_path):
        result = validate_for_publish(self._plugin_dir(tmp_path), probe=False)
        assert result["valid"] is True
        assert result["errors"] == []
        assert isinstance(result["warnings"], list)

    def test_validate_manifest_public_entry_structured(self):
        # 公开 manifest 级门禁入口（/api/plugin-market/validate 背后不再自行拼装报告）
        result = validate_manifest(self._manifest_factory())
        assert result["valid"] is True
        assert result["errors"] == []
        assert isinstance(result["warnings"], list)

    def test_validate_manifest_probe_refuses_without_exec_whitelist(self):
        m = self._manifest_factory()
        m.pop("permissions")
        result = validate_manifest(m, probe=True)
        assert result["valid"] is False
        assert any(e["code"] == "probe.exec_whitelist" for e in result["errors"])


class TestSchemaLoader:
    """schema 加载器：缺失时错误信息必须可排障（不含死变量 None 污染）。"""

    def test_missing_schema_error_not_polluted_by_dead_none(self, monkeypatch, tmp_path):
        from src.domains.mgmt.plugin_market import schema as schema_mod

        monkeypatch.setattr(schema_mod, "_CACHE", None)
        monkeypatch.setattr(schema_mod, "_repo_root_candidates", lambda: iter([tmp_path]))
        with pytest.raises(FileNotFoundError) as ei:
            schema_mod.load_plugin_schema()
        assert "None" not in str(ei.value)


class TestEngineListCrossCheck:
    """D7：6 引擎清单三处重复知识互验（plugin.schema.json 枚举 / schema.py PLATFORM_ENGINES / executor._BACKENDS）。"""

    def test_public_entries_declared_in_package_all(self):
        # 公开入口必须列入 __all__（/validate 背后的 validate_manifest 曾漏挂）
        import src.domains.mgmt.plugin_market as pm_pkg

        for name in ("check_schema", "check_binaries", "check_commands",
                     "validate", "validate_for_publish", "validate_manifest"):
            assert name in pm_pkg.__all__, f"{name} 未列入 src/plugin_market __all__"
            assert hasattr(pm_pkg, name), f"plugin_market.{name} 未导出"

    def test_schema_enum_matches_platform_engines(self):
        # schema 枚举与 schema.py PLATFORM_ENGINES 必须一致（单一来源互验）
        from src.domains.mgmt.plugin_market.schema import PLATFORM_ENGINES, load_plugin_schema

        engines_enum = load_plugin_schema()["$defs"]["compat"]["properties"]["engines"]["items"]["enum"]
        assert sorted(engines_enum) == sorted(PLATFORM_ENGINES)

    def test_executor_backends_match_platform_engines(self):
        # executor 后端注册表 ⊇ schema.py PLATFORM_ENGINES（单一来源互验）。
        # arch11：executor 已扩展 aishell-dsh/atomcode 新后端，schema 六引擎必须是真实后端的子集。
        from src.domains.runtime.engine.executor import _BACKENDS

        assert set(SIX_ENGINES) <= set(_BACKENDS), (
            f"schema 引擎超出 executor 后端注册表: {sorted(set(SIX_ENGINES) - set(_BACKENDS))}")


class TestChannelsAndScriptSchema:
    """[插件市场-A3/A5 补录]（Issue #1311）：commands[].channels 优先级声明 + 第五型 script adapter 声明。

    - channels：同命令多通道按优先级降序（向后兼容：旧单 adapter 写法等价单通道，零告警）；
    - script：签名脚本通道（script 相对路径白名单 + 产物交付端点 url 必填）。
    """

    def _script_manifest(self):
        m = self._manifest_factory()
        m["adapters"] = [
            {
                "id": "sig",
                "transport": "script",
                "script": "wrapper/sign_request.py",
                "url": "https://gw.api.taobao.com/router/rest",
                "compat": {"engines": list(SIX_ENGINES)},
            }
        ]
        m["commands"][0]["adapters"] = ["sig"]
        return m

    def test_script_transport_manifest_passes_schema(self):
        # script 型合法声明零 error（url 远端端点按既有口径给 network 白名单 warning）
        errors = check_schema(self._script_manifest())
        assert [e for e in errors if e.severity == "error"] == []

    def test_script_adapter_requires_script_field(self):
        m = self._script_manifest()
        del m["adapters"][0]["script"]
        errors = check_schema(m)
        assert errors and any("adapters[0].script" in e.path for e in errors)

    def test_script_adapter_requires_url(self):
        # script 产物交传输通道：url（交付端点）必填
        m = self._script_manifest()
        del m["adapters"][0]["url"]
        errors = check_schema(m)
        assert errors and any("adapters[0].url" in e.path for e in errors)

    def test_script_requires_all_six_engines(self):
        # script 为子进程执行型：与 cli/http 同口径（全 6 引擎强声明）
        m = self._script_manifest()
        m["adapters"][0]["compat"]["engines"] = ["codex"]
        errors = check_schema(m)
        assert errors and any("adapters[0].compat.engines" in e.path for e in errors)

    def test_script_path_pattern_rejects_traversal(self):
        m = self._script_manifest()
        m["adapters"][0]["script"] = "../outside/sign.py"
        errors = check_schema(m)
        assert errors and any("adapters[0].script" in e.path for e in errors)

    def test_script_path_pattern_rejects_absolute_backslash_drive(self):
        for bad in ("/abs/evil.py", "wrapper\\sign.py", "C:tools/sign.py", ""):
            m = self._script_manifest()
            m["adapters"][0]["script"] = bad
            errors = check_schema(m)
            assert errors and any(
                "adapters[0].script" in e.path for e in errors
            ), f"应拒绝非法 script 路径: {bad!r}"

    def test_binary_scan_rejects_script_path_traversal(self, tmp_path):
        # 独立于 schema 的纵深复核（schema 放松后校验器不失守）
        m = self._script_manifest()
        m["adapters"][0]["script"] = "../outside/sign.py"
        d = _write_plugin_tree(tmp_path, {}, m)
        errors = check_binaries(d, m)
        assert errors and any("adapters[0].script" in e.path for e in errors)

    def test_command_channels_declaration_passes_schema(self):
        # channels 优先级声明（降序）合法
        m = self._manifest_factory()
        m["adapters"].append({
            "id": "backup",
            "transport": "cli",
            "command": "demo-weather-backup",
            "compat": {"engines": list(SIX_ENGINES)},
        })
        m["permissions"]["exec"].append("demo-weather-backup")
        m["commands"][0]["adapters"] = ["default", "backup"]
        m["commands"][0]["channels"] = ["default", "backup"]
        assert check_schema(m) == []

    def test_command_channels_unknown_reference_rejected(self):
        # channels 引用完整性（与 adapters 引用同语义）
        m = self._manifest_factory()
        m["commands"][0]["channels"] = ["ghost"]
        errors = check_schema(m)
        assert errors and any(
            "commands[0].channels[0]" in e.path and "ghost" in e.reason for e in errors
        )

    def test_command_channels_must_be_nonempty_unique(self):
        m = self._manifest_factory()
        m["commands"][0]["channels"] = []
        assert any("commands[0].channels" in e.path for e in check_schema(m))
        m["commands"][0]["channels"] = ["default", "default"]
        errors = check_schema(m)
        assert errors and any("commands[0].channels" in e.path for e in errors)

    def test_probe_covers_script_transport_via_url(self):
        # script 交付端点参与免凭据探测（url 同 http 口径），不再报「缺少 command/url」
        captured = {}

        def prober(command, adapter):
            captured["adapter"] = adapter
            return ProbeResult(status="ok")

        m = self._script_manifest()
        errors = check_commands(m, prober=prober)
        assert errors == []
        assert captured["adapter"]["transport"] == "script"


class TestScriptPathRuleParity:
    """script 路径白名单规则三处实现对拍（Issue #1311 QA 防漂移钉扎）。

    同一份白名单知识在三处独立落地：
    - plugin.schema.json adapter.script pattern（发布门禁，加载期）
    - validator._check_script_paths（L0 纵深复核，经公开 seam check_binaries）
    - plugin_runner/adapters.script_path_allowed（Runner 加载期 + 运行时）
    本测试以同一用例矩阵对拍三处判定，任一侧单独放松/收紧即红。
    """

    CASES_ACCEPT = ["sign.py", "wrapper/sign_request.py", "a1/B2.c3-d4.e5"]
    CASES_REJECT = [
        "",                                # 空路径
        "/abs/evil.py",                    # 绝对路径
        "../outside/sign.py",              # .. 穿越
        "wrapper/../sign.py",              # 中段 .. 穿越
        "wrapper//sign.py",                # 空段
        "./sign.py",                       # . 段
        "a b.py",                          # 段内非法字符（空格）
        "wrapper\\sign.py",                # 反斜杠
        "C:tools/sign.py", "C:/abs.py",    # 盘符
    ]

    def _schema_pattern_ok(self, script) -> bool:
        from src.domains.mgmt.plugin_market.schema import load_plugin_schema

        pattern = load_plugin_schema()["$defs"]["adapter"]["properties"]["script"]["pattern"]
        return re.fullmatch(pattern, script) is not None

    def _validator_ok(self, script) -> bool:
        m = {"adapters": [{"id": "sig", "transport": "script", "script": script}]}
        return check_binaries("plugin-dir-unused-for-this-check", m) == []

    def _runner_ok(self, script) -> bool:
        from src.domains.mgmt.plugin_runner.adapters import script_path_allowed

        return script_path_allowed(script)

    def test_three_implementations_agree(self):
        for s in self.CASES_ACCEPT:
            assert self._schema_pattern_ok(s), f"schema 应接受: {s!r}"
            assert self._validator_ok(s), f"validator 应接受: {s!r}"
            assert self._runner_ok(s), f"runner 白名单应接受: {s!r}"
        for s in self.CASES_REJECT:
            assert not self._schema_pattern_ok(s), f"schema 应拒绝: {s!r}"
            assert not self._validator_ok(s), f"validator 应拒绝: {s!r}"
            assert not self._runner_ok(s), f"runner 白名单应拒绝: {s!r}"
