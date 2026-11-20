#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
domain 领域工厂自检脚本（domain-qa 验收权威版，domain-coding 交付副本）
====================================
在领域化仓库根目录运行，检查二次开发项目是否满足验收标准（对应
skills/domain-qa/references/qa-checklist.md 与 skills/domain-coding/references/delivery-and-docs-guide.md）。
权威版本维护在 skills/domain-qa/references/scripts/，两技能目录内容保持一致（勿分叉修改）。

历史:
- v2.7: 新增"扩展子类可实例化"检查（finance 实坑）——继承 ApplicationPluginExtension/BaseExtension
  的子类必须实现抽象方法 initialize/shutdown，漏实现则实例化抛
  TypeError: Can't instantiate abstract class ... with abstract methods initialize, shutdown，
  插件未注册、全域 <domain>.* RPC unknown method。
- v2.8: 新增"Agent 领域工具全量注册"检查（24z，对应 SKILL.md v3.4 / coding v4.3，account 实坑）——
  逐个 extensions/*_platform/*_gateway_tool.py 提取 @tool 工具名，验证 interface_deep.py 有
  _register_shared_tool 注册块；缺注册 → 工具不在模型工具链，主对话代办"只聊不做、零落库"
  （account 实坑：发票/凭证页面齐全、WS RPC 全通，但主对话"帮我登记发票"只回话术、SQLite 行数不变）。
- v2.9: 新增"扩展模块绝对导入"检查（24b，对应 SKILL.md v3.5 / coding v4.4，finance 实坑）——
  loader 以 importlib.util.spec_from_file_location("jiuwenswarm.loaded_extension.<name>", entry)
  动态加载 extension.py，磁盘无 jiuwenswarm.loaded_extension 父包；扩展内模块残留相对导入
  from .xxx import → 报 No module named 'jiuwenswarm.loaded_extension' → 扩展整体加载失败、
  全域 <domain>.* RPC 全部 unknown method（finance 实坑：46 个 RPC 全丢）。扫描
  extensions/<domain>_platform/*.py 全部模块，发现相对导入 → FAIL（改绝对导入）。

用法:
    python validate_domain_project.py <repo_path> [--domain <domain>] [--verbose]

    <repo_path>   领域化仓库根目录（默认: 当前目录）
    --domain      领域名（全小写，如 legal）。缺省时自动探测 extensions/*_platform/
    --verbose     输出每条检查结果

退出码:
    0  全部通过
    1  存在失败项（-v 会列出失败原因）
"""
from __future__ import annotations  # Py3.9 兼容：启用后 `str | None` 等注解延迟求值，避免 typing 取 Union

import argparse
import json
import re
import sys
from pathlib import Path

# 兼容 Windows GBK 控制台：确保 ✅ 等非 ASCII 输出不会触发 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESERVED_PORTS = {"5173", "1793", "18092", "19000", "19001"}
# 参考项目（EduSwarm）已占用端口：新领域应避免，但不阻断
REFERENCE_OCCUPIED_PORTS = {"19200"}


class Check:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.failures = []
        self.warnings = []
        self.count = 0

    def ok(self, name: str, detail: str = ""):
        self.count += 1
        if self.verbose:
            print(f"[PASS] {name}" + (f"  --  {detail}" if detail else ""))

    def fail(self, name: str, detail: str):
        self.count += 1
        self.failures.append((name, detail))
        print(f"[FAIL] {name}")
        if detail:
            print(f"        {detail}")

    def warn(self, name: str, detail: str):
        self.count += 1
        self.warnings.append((name, detail))
        print(f"[WARN] {name}  --  {detail}")


def detect_domain(root: Path) -> str | None:
    ext_dir = root / "jiuwenswarm" / "extensions"
    if not ext_dir.is_dir():
        return None
    candidates = [d.name for d in ext_dir.iterdir() if d.is_dir() and d.name.endswith("_platform")]
    if not candidates:
        return None
    shortest = min(candidates, key=len)
    return shortest[: -len("_platform")]


def extract_domain_rpc_names(py_text: str, domain: str) -> set[str]:
    """提取扩展文件注册的 <domain>.<x>.<y> 方法名集合。

    兼容两种注册形态（基线 legal/finance 均用第二种）：
    1) 字面量注册：`"legal.statute.search"`
    2) 元组 + 拼接：`LEGAL_RPC_METHODS = ("statute.search", ...)` +
       `f"legal.{method}"`（方法名无 domain 前缀，补前缀比对）
    """
    names = set()
    # 形态 1：直接出现的字面量 "domain.xxx.yyy"
    for m in re.finditer(rf"['\"]{domain}\.([\w.]+)['\"]", py_text):
        names.add(f"{domain}.{m.group(1)}")
    # 形态 2：`*_RPC_METHODS = ( "health", "statute.search", ... )` 元组拼接注册
    for block in re.finditer(r"[A-Za-z_]+_RPC_METHODS\s*=\s*\((.*?)\)", py_text, re.DOTALL):
        for item in re.finditer(r"['\"]([\w.]+)['\"]", block.group(1)):
            name = item.group(1)
            if not name.startswith(f"{domain}."):
                name = f"{domain}.{name}"
            names.add(name)
    return names


def find_extension_classes(py_text: str) -> list[dict]:
    """提取 extension.py 中全部顶层 class 定义，返回 {name, bases, body, line}。

    用于"扩展子类可实例化"检查（finance 实坑）：继承 ApplicationPluginExtension/BaseExtension
    的子类若漏实现抽象方法 initialize/shutdown，实例化时抛
    ``TypeError: Can't instantiate abstract class ... with abstract methods initialize, shutdown``，
    插件从未注册 → 全域 <domain>.* RPC 全部 unknown method。
    """
    classes = []
    body: list[str] = []
    current: dict | None = None
    line_no = 0
    for raw in py_text.splitlines():
        line_no += 1
        m = re.match(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", raw)
        if m:
            if current is not None:
                current["body"] = "\n".join(body)
                classes.append(current)
            current = {"name": m.group(1), "bases": m.group(2).strip(), "body": "", "line": line_no}
            body = []
            continue
        if current is not None:
            # 顶层新定义（class / def / async def）结束当前类体
            if re.match(r"^(class\s|def\s|async\s+def\s)", raw):
                current["body"] = "\n".join(body)
                classes.append(current)
                current = None
                body = []
            else:
                body.append(raw)
    if current is not None:
        current["body"] = "\n".join(body)
        classes.append(current)
    return classes


def load_yaml_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description="领域 Swarm 工厂生成物自检")
    ap.add_argument("repo_path", nargs="?", default=".", help="领域化仓库根目录")
    ap.add_argument("--domain", default=None, help="领域名（全小写），缺省自动探测")
    ap.add_argument("--old-brand", default="WorkSwarm", help="模板/参考项目默认品牌名（前端残留扫描用，如 WorkSwarm）")
    ap.add_argument("--verbose", action="store_true", help="输出每条检查结果")
    args = ap.parse_args()

    root = Path(args.repo_path).resolve()
    if not (root / "pyproject.toml").is_file():
        print(f"[FAIL] {root} 不是有效项目根目录（缺少 pyproject.toml）")
        return 1

    domain = args.domain or detect_domain(root)
    if not domain:
        print("[FAIL] 未找到领域扩展目录 jiuwenswarm/extensions/*_platform/，且未指定 --domain")
        return 1
    if not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        print(f"[FAIL] domain 命名非法: {domain!r}（应小写字母+数字+下划线）")
        return 1

    check = Check(args.verbose)
    print(f"校验仓库: {root}\n领域(domain): {domain}\n")

    ext_dir = root / "jiuwenswarm" / "extensions" / f"{domain}_platform"
    cfg_file = root / "jiuwenswarm" / "resources" / "config" / "config.example.yaml"
    agent_dir = root / "jiuwenswarm" / "resources" / "agent" / f"{domain}_agent_configs"
    skills_dir = root / "jiuwenswarm" / "resources" / "agent" / "workspace" / "skills"

    # ── 0. 开源合规 ──────────────────────────────────────────────
    check.ok("开源合规目录存在" if (root / "LICENSE").is_file() else "LICENSE 缺失", "")
    if (root / "LICENSE").is_file():
        check.ok("LICENSE", "Apache-2.0 声明保留")
    else:
        check.fail("开源合规", "仓库缺少 LICENSE 文件")
        if (root / "OPEN_SOURCE_SOFTWARE_NOTICE.md").is_file():
            check.ok("OPEN_SOURCE_SOFTWARE_NOTICE.md", "开源声明保留")

    # ── 1. 领域扩展 ──────────────────────────────────────────────
    ext_yaml = ext_dir / "extension.yaml"
    ext_py = ext_dir / "extension.py"
    if ext_yaml.is_file():
        text = load_yaml_text(ext_yaml)
        if f"id: {domain}_platform" in text:
            check.ok("extension.yaml.id", f"{domain}_platform")
        else:
            check.fail("extension.yaml", f"id 应为 {domain}_platform")
        if re.search(r"version:\s*\S+", text):
            check.ok("extension.yaml.version", "存在 version")
    else:
        check.fail("扩展声明", f"缺少 {ext_yaml}")

    if ext_py.is_file():
        py = load_yaml_text(ext_py)
        # 兼容两种扩展模式：registry.register_extensions（edu 先例）与 ApplicationPluginExtension（法务先例）
        if "register_extensions" in py or "ApplicationPluginExtension" in py:
            check.ok("extension.py", "扩展入口存在（register_extensions / ApplicationPluginExtension）")
        else:
            check.fail("extension.py", "缺少 register_extensions / ApplicationPluginExtension 扩展入口")
        # 可用性铁律 1：loader 以 await 方式调用入口，必须为 async def
        if "async def register_extensions" in py:
            check.ok("扩展入口 async", "register_extensions 为 async def（匹配 loader await 调用）")
        elif "register_extensions" in py:
            check.warn("扩展入口 async",
                       "register_extensions 未用 async def（loader 以 await 方式调用，同步 def 会导致扩展注册失败，请改为 async def）")
        if f'"{domain}.' in py or f"'{domain}." in py:
            check.ok("extension.py", f"包含 {domain}.* RPC 注册")
        else:
            check.fail("extension.py", f"缺少 {domain}.* RPC 注册（如 \"{domain}.case.list\"）")
        # 可用性铁律 5：WS 成功回包 payload 必须解出业务数据本体（finance 实坑：
        # 整包下发 payload=result or {} 会让前端 webRequest 拿到 {ok,data} 双层结构，
        # 组件 res.items 取空 → 数据库已落库但页面显示"暂无资产"）
        # 判定依据：文件内有 result.get('data') 解包调用（payload=result.get('data') 或
        # data=result.get('data'); payload=data 均可）；禁止 payload=result / or {} 整包透传。
        bad_payload = re.search(r"payload\s*=\s*result(?:\s+or\s*\{[^}]*\})?\s*[,)]|payload\s*=\s*result\s+or", py)
        unwrap_data = re.search(r"result\s*\.\s*get\s*\(\s*['\"]data['\"]", py)
        if bad_payload:
            check.fail("WS payload 契约",
                       "发现 payload=result（含 or {}）整包下发——dispatch_<domain>_rpc 返回 {ok,data} 双层包裹被整包透传，"
                       "前端 webRequest 的 resolve 值即 payload，组件取 res.items 为空 → 页面'暂无资产'（finance 实坑）。"
                       "成功回包必须写成 payload=result.get('data')（或 data=result.get('data'); payload=data or {}），"
                       "与 REST 网关 result.get('data') 同口径")
        elif unwrap_data:
            check.ok("WS payload 契约",
                     "存在 result.get('data') 解包调用，成功回包 payload 下发业务数据本体（前端 res.items 可直接取到业务字段）")
        elif "send_response" in py:
            check.warn("WS payload 契约",
                       f"{ext_py.name} 含 send_response 但未解析到 result.get('data') 解包写法，"
                       "请人工确认成功回包解出业务数据本体（禁止整包 result / {ok,data}）")
        # 可用性铁律 6：扩展子类可实例化（finance 实坑）——继承 ApplicationPluginExtension/BaseExtension
        # 的子类必须完整实现基类抽象方法 initialize/shutdown，否则实例化抛
        #   TypeError: Can't instantiate abstract class <Domain>PlatformExtension
        #              with abstract methods initialize, shutdown
        # → 插件未注册 → 全域 <domain>.* RPC 全部 unknown method（前端所有功能页"数据加载失败"）。
        # 仅对继承 ApplicationPluginExtension/BaseExtension 的类校验（ManifestApplicationPlugin 已实现，跳过）。
        abstract_subclasses = []
        for cls_def in find_extension_classes(py):
            parent_names = [b.split("(")[0].strip() for b in cls_def["bases"].split(",") if b.strip()]
            if "ManifestApplicationPlugin" in parent_names:
                continue
            if not any(p in parent_names for p in ("ApplicationPluginExtension", "BaseExtension")):
                continue
            missing = []
            if not re.search(r"^\s*(async\s+)?def\s+initialize\s*\(", cls_def["body"], re.MULTILINE):
                missing.append("initialize")
            if not re.search(r"^\s*(async\s+)?def\s+shutdown\s*\(", cls_def["body"], re.MULTILINE):
                missing.append("shutdown")
            abstract_subclasses.append((cls_def, missing))
        if abstract_subclasses:
            bad = [(c, mn) for c, mn in abstract_subclasses if mn]
            if bad:
                detail = "; ".join(
                    f"{c['name']}(第{c['line']}行)缺抽象方法 {'、'.join(mn)}"
                    for c, mn in bad)
                check.fail(
                    "扩展子类可实例化",
                    f"继承 ApplicationPluginExtension/BaseExtension 的子类未实现基类抽象方法 → "
                    f"无法实例化（TypeError: Can't instantiate abstract class ... with abstract methods "
                    f"initialize, shutdown）→ 扩展加载失败、全域 {domain}.* RPC 全部 unknown method、"
                    f"前端所有功能页'数据加载失败'（finance 实坑）。{detail}。"
                    f"修复：给子类补 `async def initialize(self, config)` 与 `async def shutdown(self)`"
                    f"（对照 video_duplex / ManifestApplicationPlugin 先例，可仅打日志）")
            else:
                check.ok("扩展子类可实例化",
                         f"{len(abstract_subclasses)} 个 ApplicationPluginExtension/BaseExtension 子类均完整实现"
                         f" initialize/shutdown 抽象方法（可被 loader 正常实例化、注册 RPC）")
        # 可用性铁律 7：扩展入口模块必须绝对导入（finance 实坑，v4.4/v3.5）——loader 以
        #   importlib.util.spec_from_file_location("jiuwenswarm.loaded_extension.<name>", entry)
        #   动态加载 extension.py，磁盘上不存在 jiuwenswarm.loaded_extension 父包；
        #   入口模块（extension.py）残留相对导入 from .xxx import → 解析父包失败 →
        #   No module named 'jiuwenswarm.loaded_extension' → 整个扩展加载被跳过、
        #   全域 <domain>.* RPC 全部 unknown method（finance 实坑：46 个 RPC 全丢）。
        # 判定分两层：extension.py（loader 直接加载）含相对导入 → FAIL（必改绝对导入）；
        # 其余内部模块（<domain>_backend.py 等，经绝对导入链以真实包上下文加载）含相对导入
        # 通常可正常运行 → 仅 WARN 提示（推荐统一绝对导入，避免被 loader 上下文间接加载时触发同款报错）。
        entry_rel = []
        other_rel = []
        for pymod in sorted(ext_dir.glob("*.py")):
            for lineno, line in enumerate(pymod.read_text(encoding="utf-8").splitlines(), 1):
                code_part = line.split("#", 1)[0]
                if re.search(r"(?:^|[;=,\s])from\s+\.[A-Za-z_]", code_part):
                    (entry_rel if pymod.name == "extension.py" else other_rel).append(
                        f"{pymod.name}:{lineno}: {line.strip()}")
        if entry_rel:
            check.fail("扩展模块绝对导入",
                       f"扩展入口 extension.py 存在相对导入（loader 动态模块名 jiuwenswarm.loaded_extension.<name> "
                       f"父包不存在，相对导入报 No module named 'jiuwenswarm.loaded_extension' → 扩展整体加载失败、"
                       f"全域 {domain}.* RPC unknown method，finance 实坑）："
                       + "；".join(entry_rel[:5])
                       + "。修复：改绝对导入 from jiuwenswarm.extensions.<domain>_platform.<module> import ...")
        elif other_rel:
            check.warn("扩展模块绝对导入",
                       f"入口 extension.py 已绝对导入，但内部模块仍有相对导入（loader 只直接加载 extension.py，"
                       f"内部模块经绝对导入链以真实包上下文加载时相对导入通常可解析；"
                       f"推荐统一改绝对导入 from jiuwenswarm.extensions.<domain>_platform.<module> import ...，"
                       f"避免被间接经 jiuwenswarm.loaded_extension 上下文加载时触发"
                       f" No module named 'jiuwenswarm.loaded_extension'）："
                       + "；".join(other_rel[:5]))
        else:
            check.ok("扩展模块绝对导入",
                     "扩展内模块全部绝对导入（from jiuwenswarm.extensions.<domain>_platform.<module> import，"
                     "loader 动态加载不触发 No module named 'jiuwenswarm.loaded_extension'）")
    else:
        check.fail("扩展入口", f"缺少 {ext_py}")

    # ── 2. 配置段（必须双份：example + 运行时生效 config.yaml） ──
    if cfg_file.is_file():
        text = load_yaml_text(cfg_file)
        if f"{domain}_gateway:" in text:
            check.ok("config.example.yaml", f"包含 {domain}_gateway 段")
        else:
            check.fail("config.example.yaml", f"缺少 {domain}_gateway 段")

        # 可用性铁律 2：运行时生效配置必须同步存在且 enabled: true
        runtime_cfg = root / "jiuwenswarm" / "resources" / "config.yaml"
        if runtime_cfg.is_file():
            rtext = load_yaml_text(runtime_cfg)
            if f"{domain}_gateway:" in rtext:
                check.ok("config.yaml（运行时）", "运行时生效配置包含 <domain>_gateway 段（默认启动时 REST 网关可用）")
                if re.search(rf"{domain}_gateway:\s*\n(?:[ \t]*.*\n)*?[ \t]*enabled:\s*true", rtext):
                    check.ok("config.yaml enabled", "<domain>_gateway.enabled: true（REST 网关默认启动）")
                else:
                    check.warn("config.yaml enabled",
                               "config.yaml 中 <domain>_gateway 未显式 enabled: true（默认启动可能不加载 REST 网关）")
            else:
                check.fail("config.yaml（运行时）",
                           "运行时生效配置 config.yaml 缺少 <domain>_gateway 段（只写 example 会导致默认启动后 REST 网关不可用，必须在 config.yaml 同步添加）")
        else:
            check.warn("config.yaml（运行时）", f"未找到运行时配置 {runtime_cfg}，跳过运行时配置检查")

        ports = set(RESERVED_PORTS)
        m = re.search(rf"{domain}_gateway:\s*\n(?:.*\n)*?.*port:\s*(\d+)", text)
        if m:
            port = m.group(1)
            if port in ports:
                check.fail("端口冲突", f"{domain}_gateway.port={port} 与基础框架保留端口冲突 {sorted(RESERVED_PORTS)}")
            elif port in REFERENCE_OCCUPIED_PORTS and domain != "edu":
                check.warn("端口建议", f"{domain}_gateway.port={port} 与参考项目(EduSwarm)占用端口冲突，建议改用 19210+")
            else:
                ports.add(port)
                check.ok("端口规划", f"{domain}_gateway.port={port}")
        else:
            check.warn("配置端口", f"未在 {domain}_gateway 段中找到 port")
    else:
        check.fail("配置示例", f"缺少 {cfg_file}")

    # ── 3. Agent 配置 ────────────────────────────────────────────
    if agent_dir.is_dir():
        jsons = sorted(agent_dir.glob("*.json"))
        if not jsons:
            check.warn("Agent 配置", f"{agent_dir} 下无 JSON")
        for jf in jsons:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                check.fail("Agent JSON 解析", f"{jf.name}: {e}")
                continue
            for field in ("id", "agent_id", "name", "persona", "skills"):
                if field not in data:
                    check.fail("Agent 字段", f"{jf.name} 缺少字段 {field}")
                    break
            else:
                if str(data.get("id", "")) != f"{domain}_{jf.stem}" and jf.stem != str(data.get("id", "")):
                    check.warn("Agent 命名", f"{jf.name} id={data.get('id')} 与文件名不一致")
                else:
                    check.ok("Agent 配置", jf.name)
    else:
        check.fail("Agent 配置目录", f"缺少 {agent_dir}")

    # ── 4. 技能加载 ← Agent 引用与目录一致性 ──────────────────────
    if skills_dir.is_dir():
        all_skills = {d.name for d in skills_dir.iterdir() if d.is_dir()}
    else:
        check.fail("技能目录", f"缺少 {skills_dir}")
        all_skills = set()

    if agent_dir.is_dir():
        for jf in agent_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for skill_name in data.get("skills", []):
                if skill_name in all_skills:
                    check.ok("技能挂载", f"{jf.name} -> {skill_name}")
                else:
                    check.fail("技能挂载", f"{jf.name} 引用技能 {skill_name} 不存在于 {skills_dir}")
            for skill_name in data.get("skills", []):
                skill_md = skills_dir / skill_name / "SKILL.md"
                if skill_md.is_file():
                    md_text = load_yaml_text(skill_md)
                    if md_text.startswith("---\n") and "\n---" in md_text:
                        fm = md_text.split("---\n")[1].split("\n---")[0]
                        if "name:" in fm and "description:" in fm:
                            check.ok("SKILL.md frontmatter", skill_name)
                        else:
                            check.fail("SKILL.md frontmatter", f"{skill_name} 缺少 name/description")
                    else:
                        check.fail("SKILL.md frontmatter", f"{skill_name} 不是合法 frontmatter 结构")
                else:
                    check.fail("SKILL.md", f"{skill_name} 缺少 SKILL.md")

    # ── 4.5 任务对话可识别（每个功能需能在主对话被模型识别并调 RPC 落库） ──
    if domain != "edu" and skills_dir.is_dir() and agent_dir.is_dir():
        coordination_ok = False
        feature_skill_ok = False
        persona_enum_ok = False
        # (a) 协调技能委派映射：*-swarm-coordination/SKILL.md 含「委派映射」表 + 明确函数词 -> Agent
        co_skill = f"{domain}-swarm-coordination"
        if (skills_dir / co_skill / "SKILL.md").is_file():
            co_md = load_yaml_text(skills_dir / co_skill / "SKILL.md")
            if "委派映射" in co_md or "委派" in co_md:
                check.ok("对话可识别-委派映射", f"协调技能 {co_skill} 含委派映射表（LEADER 可识别任务归属并派对专员）")
                coordination_ok = True
            else:
                check.fail("对话可识别-委派映射",
                           f"协调技能 {co_skill}/SKILL.md 缺少「委派映射」表（LEADER 无法按用户话语识别功能归属并派对专员，任务对话识别不了功能）")
        else:
            check.warn("对话可识别-委派映射", f"未发现 {co_skill} 协调技能（LEADER 需此技能识别任务归属并委派）")

        # (b) 功能技能 SKILL.md：含「适用场景」+ 引用 <domain>.<x>.<y> RPC 调用
        #     收集所有引用该领域 RPC 的技能，判定对话可识别链路存在
        domain_rpc_ref_skills = []
        for d in sorted(all_skills):
            md_path = skills_dir / d / "SKILL.md"
            if not md_path.is_file():
                continue
            md = load_yaml_text(md_path)
            has_domain_rpc = bool(re.search(rf"`?{domain}\.\w+\.\w+`?", md)) or bool(re.search(rf"\b{domain}\.\w+", md))
            has_scene = ("适用场景" in md) or ("trigger" in md) or ("当用户" in md)
            if has_domain_rpc:
                domain_rpc_ref_skills.append(d)
            if has_domain_rpc and has_scene:
                feature_skill_ok = True
        if feature_skill_ok:
            check.ok("对话可识别-技能RPC",
                     f"{len(domain_rpc_ref_skills)} 个技能引用 {domain}.* RPC 且含触发场景"
                     f"（模型对话中可据此调用）: {domain_rpc_ref_skills[:5]}")
        else:
            check.fail("对话可识别-技能RPC",
                       "功能技能 SKILL.md 未同时具备「适用场景」+「调用 <domain>.<x>.<y>」"
                       "（模型在对话中无法识别功能归属并调用正确 RPC，功能只在前端页签但对话不可识别）")

        # (b2) v2.5+: 可执行通道段（防"只聊不做"——模型能识别但拿不到真实调用通道，对话代办形同虚设）
        exec_channel_skills = []
        for d in sorted(all_skills):
            md_path = skills_dir / d / "SKILL.md"
            if not md_path.is_file():
                continue
            md = load_yaml_text(md_path)
            if "可执行通道" in md or "X-API-Key" in md or "webRequest" in md:
                exec_channel_skills.append(d)
        if exec_channel_skills:
            check.ok("对话可执行-可执行通道段",
                     f"{len(exec_channel_skills)} 个技能含「可执行通道段」"
                     f"（REST 网关地址/鉴权头/参数契约或 WebChannel，模型对话代办可真实发起调用）: "
                     f"{exec_channel_skills[:5]}")
        else:
            check.fail("对话可执行-可执行通道段",
                       "功能技能 SKILL.md 均无「可执行通道段」（REST 网关地址/方法/X-API-Key 鉴权头/参数契约，"
                       "或 webRequest 通道）——模型对话只说已查询/已录入却无法真实调用 RPC（只聊不做，qa 24t 判 FAIL）")

        # (c) Agent persona 逐条枚举领域功能（「能做啥」= 平台能力 + 领域能力）
        for jf in agent_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            persona = str(data.get("persona", ""))
            if any(k in persona for k in ("能做", "能力")) and domain in persona and domain_rpc_ref_skills:
                persona_enum_ok = True
                break
        if persona_enum_ok:
            check.ok("对话可识别-persona", "Agent persona 含领域能力枚举（模型对话中知道自己能做什么）")
        else:
            check.warn("对话可识别-persona",
                       "agent persona 未见领域能力枚举描述（LEADER/worker「能做啥」需显式列出功能，否则主对话中模型不确定可调用哪些功能）")

        # (d) 技能引用的 RPC 与 extension.py 注册一致（四处一致：extension.py/后端rpc/技能SKILL.md/Api.ts）
        ext_methods = extract_domain_rpc_names(load_yaml_text(ext_py), domain) if ext_py.is_file() else set()
        skill_rpc = set()
        for d in sorted(all_skills):
            md_path = skills_dir / d / "SKILL.md"
            if not md_path.is_file():
                continue
            md = load_yaml_text(md_path)
            for m in re.finditer(rf"`?({domain})\.([\w.]+)`?", md):
                skill_rpc.add(f"{m.group(1)}.{m.group(2)}")
        if skill_rpc and ext_methods:
            missing = sorted(s for s in skill_rpc if s.split(".", 1)[1] not in {x.split(".", 1)[1] for x in ext_methods})
            if missing:
                check.fail("对话可识别-RPC点名一致",
                           f"技能 SKILL.md 引用的 RPC {missing} 未在 extension.py 注册（四处命名不一致，模型按技能调用会失败）")
            else:
                check.ok("对话可识别-RPC点名一致",
                         f"技能引用的 {len(skill_rpc)} 个 RPC 均已注册（技能/扩展/后端/Api 四处命名一致）")
        elif skill_rpc and not ext_methods:
            check.warn("对话可识别-RPC点名一致", "技能引用领域 RPC 但 extension.py 未解析到注册名（请核对注册形态）")

    # ── 4.6 Agent 领域工具全量注册（24z，v3.4 新增，防"页面/WS 全通但主对话代办只聊不做"account 实坑） ──
    gw_tool_files = sorted(root.glob("jiuwenswarm/extensions/*_platform/*_gateway_tool.py"))
    inter_deep = root / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_deep.py"
    inter_text = load_yaml_text(inter_deep) if inter_deep.is_file() else ""
    if gw_tool_files:
        check.ok("Agent 领域工具-文件", f"发现 {len(gw_tool_files)} 个 <domain>_gateway_tool.py 工具文件")
        for gw in gw_tool_files:
            gw_text = load_yaml_text(gw)
            # 提取 @tool 装饰的工具函数名（兼容 `@tool` / `@tool(...)` / async def）
            names = sorted(set(re.findall(r"@tool(?:\([^)]*\))?\s*\n\s*(?:async\s+)?def\s+(\w+)", gw_text)))
            if not names:
                names = sorted(set(re.findall(r"(?:async\s+)?def\s+(\w+_gateway_call)\b", gw_text)))
            if not names:
                check.warn("Agent 领域工具-文件", f"{gw.name} 未解析到 @tool 工具函数（请人工核对装饰器写法）")
                continue
            for name in names:
                if re.search(rf"register_shared_tool\s*\(\s*{re.escape(name)}\b", inter_text):
                    check.ok(f"Agent 领域工具-注册 {name}", f"{gw.name} 已在 interface_deep.py 注册（主对话模型工具链可见）")
                else:
                    check.fail(
                        f"Agent 领域工具-注册 {name}",
                        f"{gw.name} 定义工具 {name} 但 interface_deep.py 无 _register_shared_tool({name}) 注册块"
                        "（模型工具链没有它 → 对话代办只聊不做、零落库 —— account 实坑，qa 24z 判 FAIL；"
                        "进程内直调工具应无条件注册，勿依赖 <domain>_gateway.enabled）",
                    )
    else:
        check.ok("Agent 领域工具-文件", "未发现 <domain>_gateway_tool.py（本领域无 @tool 领域工具，24z 不适用）")

    # ── 5. 框架守约（核心目录未被大改——抽样看扩展是否独立成目录） ──
    core_dirs = ["jiuwenswarm/agents", "jiuwenswarm/gateway", "jiuwenswarm/server"]
    for d in core_dirs:
        if not (root / d).is_dir():
            check.warn("框架目录", f"缺失 {d}（若为全新仓库请忽略）")
    # 残留品牌检查：仅对非参考领域执行
    brand_hits = []
    if domain != "edu":
        for pattern in ["EduSwarm", "edu_platform", "AI智慧课堂", "教案_"]:
            for p in list(root.glob("*.md")) + list(root.glob("*.py"))[:50] + list(root.glob("*.toml")) + list(root.glob("*.yaml")):
                if pattern in load_yaml_text(p):
                    brand_hits.append((p.name, pattern))
                    break
        if brand_hits:
            check.warn("品牌残留", "; ".join(f"{n}[{pat}]" for n, pat in brand_hits))
        else:
            check.ok("品牌一致性", "未发现模板/参考项目品牌残留")
    else:
        check.ok("品牌一致性", "参考项目(EduSwarm)自身，跳过残留检查")

    # ── 5.5 前端领域功能模块（v2：左侧导航可点 + API 封装 + store） ──
    fe_src = root / "jiuwenswarm" / "channels" / "web" / "frontend" / "src"
    fe_public = root / "jiuwenswarm" / "channels" / "web" / "frontend" / "public"
    domain_cap = domain.capitalize()
    if domain != "edu":  # edu 为早期先例，不适用 v2 前端功能模块要求
        # 前端功能模块：每功能一个独立页面组件 <Domain><Feature>Panel（可选聚合首页 WorkbenchPanel 仅作补充入口）
        components_dir = fe_src / "components" / ""
        feat_panels = []
        if components_dir.is_dir():
            for c in components_dir.iterdir():
                if c.is_dir():
                    if c.name.startswith(domain_cap) and "Panel" in c.name and (c / "index.tsx").is_file():
                        feat_panels.append(c.name)
                elif c.suffix.lower() in (".tsx", ".ts") and c.stem.startswith(domain_cap) and "Panel" in c.stem:
                    feat_panels.append(c.name)
        standalone = sorted(p for p in feat_panels if not p.lower().endswith("workbenchpanel"))
        if standalone:
            detail = ", ".join(standalone[:6]) + ("..." if len(standalone) > 6 else "")
            check.ok("前端功能模块",
                     f"独立功能页面组件 {len(standalone)} 个（每 Spec 功能一个入口）: {detail}")
        elif feat_panels:
            check.warn("前端功能模块",
                       f"仅发现聚合首页 {sorted(feat_panels)}，未发现独立功能页面组件"
                       f"（Spec 每功能需独立组件+独立导航入口，WorkbenchPanel 只可作补充入口）")
        else:
            check.fail("前端功能模块",
                       f"{components_dir} 下未发现 <Domain><Feature>Panel 独立页面组件"
                       f"（对照法务独立组件，每 Spec 功能一个）")

        api_file = fe_src / "services" / f"{domain}Api.ts"
        if api_file.is_file():
            api_text = load_yaml_text(api_file)
            rpc_calls = re.findall(rf"(?:webRequest|rpc)\([^)]{{0,120}}?['\"]({domain}\.[\w.]+)['\"]", api_text)
            if rpc_calls:
                uniq = sorted(set(rpc_calls))
                check.ok("领域 API 层", f"{domain}Api.ts 封装 {len(uniq)} 个 <domain>.* RPC: {uniq[:6]}{'...' if len(uniq) > 6 else ''}")
            else:
                check.fail("领域 API 层", f"{api_file} 未发现 webRequest('{domain}.*') 调用")
        else:
            check.fail("领域 API 层", f"缺少 {api_file}")

        store_file = fe_src / "stores" / f"{domain}Store.ts"
        if store_file.is_file():
            check.ok("领域状态层", f"{domain}Store.ts 存在")
        else:
            check.fail("领域状态层", f"缺少 {store_file}（领域状态层）")

        fpl = fe_src / "utils" / "frontendPlatform.ts"
        if fpl.is_file():
            nav_text = load_yaml_text(fpl)
            nav_keys = [k for k in (
                re.findall(rf"\b{domain}[A-Z]\w*", nav_text)          # 驼峰式：legalCase
                + re.findall(rf"'{domain}-[\w-]+'", nav_text)         # 字符串式：'legal-case'
            ) if not k.strip("'\"").lower().endswith("workbench")]    # 可选聚合 key 不计入"每功能入口"（key 可能带引号）
            if nav_keys:
                check.ok("侧栏导航注册", f"frontendPlatform.ts 含领域 SidebarNavKey: {sorted(set(nav_keys))[:6]}{'...' if len(set(nav_keys)) > 6 else ''}")
                uniq_keys = set(nav_keys)
                if len(uniq_keys) > len(standalone):
                    check.warn("导航粒度",
                               f"SidebarNavKey {len(uniq_keys)} 个 > 独立页面组件 {len(standalone)} 个"
                               f"（每个功能应是独立 key + 独立组件；多个 key 共用一个组件疑似 mode 打包，请拆分）")
            else:
                check.fail("侧栏导航注册", f"{fpl} 缺少 {domain}* SidebarNavKey（功能页签入口）")
        else:
            check.fail("侧栏导航注册", f"缺少 {fpl}")

        # 可用性铁律 3：左侧导航页签过多必须可滚动（icon-rail-nav-list 滚动容器 + overflow-y）
        sb_file = fe_src / "components" / "SessionSidebar" / "index.tsx"
        sb_css = fe_src / "components" / "SessionSidebar" / "SessionSidebar.css"
        if sb_file.is_file() and "icon-rail-nav-list" in load_yaml_text(sb_file):
            check.ok("导航滚动容器", "SessionSidebar/index.tsx 已用 icon-rail-nav-list 包裹导航 items")
            if sb_css.is_file() and "overflow-y" in load_yaml_text(sb_css) and "icon-rail-nav-list" in load_yaml_text(sb_css):
                check.ok("导航滚动样式", "SessionSidebar.css 已提供 .icon-rail-nav-list 滚动样式（overflow-y）")
            else:
                check.warn("导航滚动样式",
                           f"SessionSidebar.css 未提供 .icon-rail-nav-list 的 overflow-y 滚动样式（页签过多时底部页签不可达）")
        else:
            check.warn("导航滚动容器",
                       "SessionSidebar/index.tsx 未发现 icon-rail-nav-list 滚动容器（领域页签 + 平台页签过多时需可滚动，请包裹导航 items）")

        if sb_file.is_file():
            sb_text = load_yaml_text(sb_file)
            # 可用性铁律 4：页签排序-双路径（静态数组 domainFeatureNavItems + 应用插件动态页签 applicationPluginItems
            # 都必须插在 chat 之后、skills 之前；禁止 [...mainNavItems, ...applicationPluginItems] append 末尾）
            nb_fail = []
            if "domainFeatureNavItems" not in sb_text:
                nb_fail.append("未发现 domainFeatureNavItems 常量数组（领域页签入口）")
            else:
                # 静态数组路径：domainFeatureNavItems 必须在 mainNavItems 中 chat 之后
                if re.search(r"\[.*['\"]chat['\"]", sb_text) and "domainFeatureNavItems" in sb_text:
                    check.ok("页签排序-静态数组", "domainFeatureNavItems 存在（领域页签入口）")
            # 双路径 → 检查是否出现 append 末尾的写法
            if re.search(r"\[\.\.\.mainNavItems\s*,\s*\.\.\.applicationPluginItems\s*\]", sb_text):
                nb_fail.append("出现 [...mainNavItems, ...applicationPluginItems] append 末尾——"
                               "领域/插件页签会排到'更新'等平台页签之后，必须拆出 chat/skills 插位")
            if re.search(r"visibleMainNavItems\s*=\s*\[\s*chatNavItem\s*,|chat,\s*\.\.\.domainFeatureNavItems", sb_text):
                check.ok("页签排序-插位", "visibleMainNavItems 按 chat→领域→插件→其余 顺序插位")
            elif nb_fail:
                nb_fail.append("visibleMainNavItems 未体现 chat 之后插领域/插件页签的组装顺序")
            if nb_fail:
                check.fail("页签排序-双路径", "; ".join(nb_fail))
            else:
                check.ok("页签排序-双路径",
                         "双路径均满足：静态 domainFeatureNavItems 紧随 chat，无 append 末尾的 applicationPluginItems 组装")
        else:
            check.warn("页签排序-双路径", "缺少 SessionSidebar/index.tsx，无法校验领域页签插位")

    # ── 5.6 品牌定制（v2：左上角 logo + 欢迎语 + 「能做啥」= 平台+领域能力） ──
    if domain != "edu":
        logo_file = fe_public / f"{domain}-ai-logo.svg"
        sb_file = fe_src / "components" / "SessionSidebar" / "index.tsx"
        if logo_file.is_file():
            check.ok("品牌 logo", f"{domain}-ai-logo.svg 存在")
            if sb_file.is_file() and f"/{domain}-ai-logo.svg" in load_yaml_text(sb_file):
                check.ok("品牌 logo 引用", "SessionSidebar 已引用领域 logo（左上角图标已更换）")
            else:
                check.fail("品牌 logo 引用", "SessionSidebar/index.tsx 未引用领域 logo")
        else:
            check.fail("品牌 logo", f"缺少 {logo_file}（左上角图标未按领域实现更换）")

        # 5.6.1 浏览器页签 title（index.html <title> 领域化，模板默认如 WorkSwarm）
        old_brand = args.old_brand or "WorkSwarm"
        fe_index = fe_src.parent / "index.html"
        if fe_index.is_file():
            html_text = load_yaml_text(fe_index)
            m = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
            title_val = m.group(1).strip() if m else ""
            if title_val and old_brand in title_val:
                check.fail("浏览器页签 title",
                           f"index.html <title> 仍为模板默认品牌名 {title_val!r}（应改为领域品牌名，如 {domain.capitalize()}Swarm）")
            elif title_val:
                check.ok("浏览器页签 title", f"index.html <title>={title_val!r}（已领域化）")
            else:
                check.warn("浏览器页签 title", "index.html 未找到 <title>（浏览器标签页无品牌名）")
        else:
            check.warn("浏览器页签 title", "缺少 frontend/index.html")

        # 5.6.2 旧品牌名残留（任务对话窗口硬编码欢迎语/ChatPanel/i18n 散落文案/分享图 producer）
        leftover = []
        if fe_src.is_dir():
            for p in fe_src.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".ts", ".tsx", ".js", ".json", ".html") \
                        and old_brand in load_yaml_text(p):
                    leftover.append(str(p.relative_to(root)))
        if leftover:
            check.fail("品牌名清零",
                       f"前端仍含模板品牌名 {old_brand!r}: "
                       + ", ".join(sorted(set(leftover))[:6])
                       + "（需替换为领域品牌名——任务对话窗口 ChatPanel 硬编码欢迎语/标语、i18n 散落文案、分享图 producer 标签一个都不能漏）")
        else:
            check.ok("品牌名清零", f"前端无模板品牌名 {old_brand!r} 残留（浏览器页签/欢迎语/i18n 均已领域化）")

        for loc in ("zh", "en"):
            loc_file = fe_src / "i18n" / "locales" / f"{loc}.json"
            if loc_file.is_file():
                loc_text = load_yaml_text(loc_file)
                # 兼容两种命名与两种存储形态：聚合首页 nav.<domain>Workbench 或工作台 nav.<domain>Home
                # （基线先例用驼峰 Home；JSON 文本可能为嵌套形态 "legalHome" 或扁平形态 "nav.legalHome"）
                home_key = re.search(rf'"(?:nav\.)?{re.escape(domain)}(Home|Workbench)"', loc_text)
                if home_key:
                    check.ok(f"i18n {loc}", f"含 nav.{domain}{home_key.group(1)} key（领域页签文案）")
                elif re.search(rf'"(?:nav\.)?{re.escape(domain)}\w+"', loc_text):
                    check.ok(f"i18n {loc}", f"含 nav.{domain}* key（领域页签文案）")
                else:
                    check.fail(f"i18n {loc}", f"{loc}.json 缺少 nav.{domain}* key（领域页签文案，zh/en 必须成对）")
                if "welcomeHeading" in loc_text and "welcomeBubbleText" in loc_text:
                    check.ok(f"任务欢迎语 {loc}", "welcomeHeading/welcomeBubbleText 已领域化")
                else:
                    check.warn(f"任务欢迎语 {loc}", "未发现 welcomeHeading/welcomeBubbleText（如未启用欢迎语可忽略）")
            else:
                check.warn("i18n 文件", f"缺少 {loc_file}")

        # 「能做啥」回答 = 平台能力 + 领域能力（软检查）
        howto_checked = False
        if agent_dir.is_dir():
            for jf in sorted(agent_dir.glob("*.json")):
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                persona = str(data.get("persona", ""))
                if any(k in persona for k in ("能做", "能做什么", "能帮你")) and "平台" in persona and domain in persona:
                    check.ok("「能做啥」回答", f"{jf.name} persona 含平台能力+领域能力二段式")
                    howto_checked = True
                    break
        if not howto_checked:
            check.warn("「能做啥」回答", "agent persona 未见完整二段式（平台+领域）回答，请确认领域上下文 hook 或 persona 已注入")

    # ── 5.7 后端功能落库（业务分发 + RPC + SQLite 建表） ──
    backend_file = ext_dir / f"{domain}_backend.py"
    if backend_file.is_file():
        btext = load_yaml_text(backend_file)
        if any(k in btext for k in ("CREATE TABLE", "sqlite3", "INSERT INTO")):
            check.ok("后端落库", f"{domain}_backend.py 含 SQLite 建表/写入（功能可落库）")
        else:
            check.fail("后端落库", f"{domain}_backend.py 未发现 CREATE TABLE / sqlite3 / INSERT INTO")
        if f"dispatch_{domain}_rpc" in btext:
            check.ok("后端分发", f"含 dispatch_{domain}_rpc 统一分发")
        else:
            check.fail("后端分发", f"{domain}_backend.py 缺少 dispatch_{domain}_rpc（前端 RPC 需统一分发）")
    elif ext_py.is_file() and any(k in load_yaml_text(ext_py) for k in ("CREATE TABLE", "sqlite3", "INSERT INTO")):
        check.ok("后端落库", "extension.py 内联 SQLite 建表/写入")
        check.warn("后端分层", f"建议将业务与落库抽到 {domain}_backend.py")
    else:
        check.fail("后端落库", "未发现领域业务层（<domain>_backend.py 或 extension.py 内含 SQLite）")

    # ── 6. 领域文档四件套 ─────────────────────────────────────────
    for docname in [f"{domain_cap}Swarm快速部署指南.md", f"{domain_cap}Swarm架构图.svg",
                    f"{domain_cap}Swarm经验分享.md"]:
        if (root / docname).is_file():
            check.ok("领域文档", docname)
        else:
            check.warn("领域文档（可选）", f"缺少 {docname}（MVP 建议提供）")

    # ── 汇总 ─────────────────────────────────────────────────────
    print(f"\n共 {check.count} 项检查：通过 {check.count - len(check.failures) - len(check.warnings)}，"
          f"失败 {len(check.failures)}，警告 {len(check.warnings)}")
    if check.warnings:
        print("警告项（不阻断验收，建议处理）：")
        for name, detail in check.warnings:
            print(f"  ! {name}: {detail}")
    if check.failures:
        print("失败项（必须修复）：")
        for name, detail in check.failures:
            print(f"  X {name}: {detail}")
        return 1
    print("✅ 验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())