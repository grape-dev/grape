"""端到端验证华为云技能包 MCP retrieveSkill 修复。

验证修改的文件：/opt/grape/.codeartsdoer/huaweicloud-plugins/src/tools.mjs
修改内容：retrieveSkill 函数支持子技能包结构（references/<name>/SKILL.md）。

测试通过 subprocess 调用 MCP server 的 JSON-RPC stdio 接口，
不依赖任何 JS 测试框架。
"""
import json
import subprocess
import os

MCP_SERVER = "/opt/grape/.codeartsdoer/huaweicloud-plugins/src/mcp-server.mjs"

INIT_MSG = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
})


def _mcp_call(skill_name: str) -> dict:
    """Call huaweicloud_retrieve_skill via raw MCP stdio JSON-RPC."""
    call_msg = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "huaweicloud_retrieve_skill",
            "arguments": {"name": skill_name},
        },
    })
    proc = subprocess.run(
        ["node", MCP_SERVER],
        input=INIT_MSG + "\n" + call_msg + "\n",
        capture_output=True, text=True, timeout=10,
    )
    for line in proc.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("id") == 2:
            text = d.get("result", {}).get("content", [{}])[0].get("text", "{}")
            return json.loads(text)
    return {"ok": False, "error": f"no response for {skill_name}"}


class TestRetrieveSkillPack:
    """父技能包 huawei-cloud 必须可检索。"""

    def test_parent_pack_retrievable(self):
        result = _mcp_call("huawei-cloud")
        assert result["ok"] is True, f"huawei-cloud failed: {result.get('error')}"
        assert "华为云" in result["content"]
        assert "22" in result["content"]


class TestRetrieveSubSkill:
    """子技能（在 references/<name>/SKILL.md 下）必须可检索。"""

    def test_huawei_ecs(self):
        result = _mcp_call("huawei-ecs")
        assert result["ok"] is True, f"huawei-ecs: {result.get('error')}"
        assert "ECS" in result["content"] or "ecs" in result["content"].lower()
        assert len(result.get("references", [])) == 4

    def test_huawei_obs(self):
        result = _mcp_call("huawei-obs")
        assert result["ok"] is True, f"huawei-obs: {result.get('error')}"
        assert "OBS" in result["content"] or "obs" in result["content"].lower()
        assert len(result.get("references", [])) == 5

    def test_huawei_vpc(self):
        result = _mcp_call("huawei-vpc")
        assert result["ok"] is True, f"huawei-vpc: {result.get('error')}"
        assert "VPC" in result["content"] or "vpc" in result["content"].lower()
        assert len(result.get("references", [])) == 3

    def test_huawei_iam(self):
        result = _mcp_call("huawei-iam")
        assert result["ok"] is True, f"huawei-iam: {result.get('error')}"
        assert "IAM" in result["content"] or "iam" in result["content"].lower()

    def test_huawei_cce(self):
        result = _mcp_call("huawei-cce")
        assert result["ok"] is True, f"huawei-cce: {result.get('error')}"
        assert "CCE" in result["content"] or "cce" in result["content"].lower()


class TestRetrieveNonexistent:
    """不存在的技能应该报错并列出可用技能。"""

    def test_nonexistent_returns_error_with_available(self):
        result = _mcp_call("nonexistent-skill")
        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()
        assert "huawei-cloud" in result.get("error", "")


class TestNoEISDIR:
    """父技能包的 references/ 含子目录时不能报 EISDIR（回归保护）。"""

    def test_parent_pack_no_eisdir(self):
        """修复前 retrieveSkill('huawei-cloud') 会 EISDIR 报错，因为
        references/ 下全是子目录，readFileSync 读不了目录。"""
        result = _mcp_call("huawei-cloud")
        # 不应该有 error 字段（ok=True 即可证明没有 EISDIR）
        assert result["ok"] is True
        assert "EISDIR" not in result.get("error", "")
