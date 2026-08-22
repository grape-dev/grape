# REST API Skill 脚本生成模板

> rest-api-skill-creator 生成 REST API 管理 skill 脚本时的模板。
> 子命令数量、端点路径、参数定义由 API 文档解析结果动态填充。

## 模板结构

生成的 skill 包含两个文件模板：Python 脚本模板（下方代码块）和 SKILL.md 段落模板（末尾）。

### Python 脚本模板

生成的脚本遵循以下结构（以 2 个端点为例，实际数量由 API 文档决定）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""{skill_name} — {description}

能力（由 API 文档解析生成）:
  A. {subcommand_1} — {description_1}
  B. {subcommand_2} — {description_2}
  ...

认证: {AUTH_TYPE}（{CRED_PREFIX}_* 环境变量，前缀由 skill name 推导）
退出码: 0=成功; 2=参数错误; 3=缺少配置（地址或认证）; 4=API 调用失败
"""

import argparse
import json
import os
import sys


# ---------------------------------------------------------------------------
# Credentials（前缀由 skill name 推导，认证方式由需求澄清决定）
# ---------------------------------------------------------------------------

_CRED_PREFIX = "{CRED_PREFIX}"  # 生成时替换：skill name 转大写下划线
_AUTH_TYPE = "{AUTH_TYPE}"      # 生成时替换：basic / token / api_key / none
_API_KEY_HEADER = "{API_KEY_HEADER}"  # 生成时替换：API Key 的 Header/Query 名（如 X-API-Key / api_key）
_API_KEY_LOCATION = "{API_KEY_LOCATION}"  # 生成时替换：header / query

def _load_credentials():
    """获取认证凭据——4 级优先级（与 base_url 一致）。

    1. 项目知识：递归扫描 .project-info/ 下所有 JSON 文件（secrets.{PREFIX}_*）
    2. 环境变量：扫描 {PREFIX}_* 开头变量（项目知识缺失时回退）
    3. 当前上下文：A2A context 已注入的环境变量（已包含在步骤 2）
    4. 交互输入：以上都无时提示用户输入
    """
    creds = {}
    # 1. 项目知识优先
    creds.update(_load_from_project_knowledge())
    # 2. 环境变量回退：项目知识缺失的字段从环境变量补充
    for k, v in os.environ.items():
        u = k.upper()
        if u.startswith(_CRED_PREFIX):
            if 'USERNAME' in u or u.endswith('_USER'):
                creds.setdefault('username', v)
            if 'PASSWORD' in u or u.endswith('_PASS'):
                creds.setdefault('password', v)
            if 'TOKEN' in u:
                creds.setdefault('token', v)
            if 'API_KEY' in u:
                creds.setdefault('api_key', v)
            if 'BASE_URL' in u:
                creds.setdefault('base_url', v)
    return creds


def _load_from_project_knowledge():
    """从 .project-info/ 目录递归查找 JSON 配置文件，读取 secrets.{PREFIX}_* 字段。

    不固定文件名——扫描所有 .json 文件，检查是否含 secrets/config 字段。
    """
    import glob
    creds = {}
    # 搜索当前目录及上级目录的 .project-info/ 下所有 JSON 文件
    for pattern in ['.project-info/**/*.json',
                    '../.project-info/**/*.json',
                    '../../.project-info/**/*.json']:
        for filepath in glob.glob(pattern, recursive=True):
            try:
                with open(filepath) as f:
                    data = __import__('json').load(f)
                secrets = data.get('secrets', {})
                config = data.get('config', {})
                prefix = _CRED_PREFIX + '_'
                for key, val in secrets.items():
                    if key.upper().startswith(prefix):
                        if 'USERNAME' in key.upper():
                            creds.setdefault('username', val)
                        if 'PASSWORD' in key.upper():
                            creds.setdefault('password', val)
                        if 'TOKEN' in key.upper():
                            creds.setdefault('token', val)
                        if 'API_KEY' in key.upper():
                            creds.setdefault('api_key', val)
                for key, val in config.items():
                    if key.upper().startswith(prefix) and 'BASE_URL' in key.upper():
                        creds.setdefault('base_url', val)
            except Exception:
                continue
    return creds


def _get_base_url():
    """获取 API base URL——4 级优先级（与凭据读取一致）。"""
    creds = _load_credentials()
    base_url = creds.get('base_url', '').rstrip('/')
    if not base_url:
        print("未检测到 %s_BASE_URL 环境变量，也未在 .project-info/ 找到配置。" % _CRED_PREFIX)
        print("请输入目标系统 API 地址（如 http://host:port）：")
        base_url = input("> ").strip().rstrip('/')
        if not base_url:
            print("错误：API 地址不能为空", file=sys.stderr)
            sys.exit(3)
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            print("错误：地址必须以 http:// 或 https:// 开头", file=sys.stderr)
            sys.exit(3)
        # 提示用户如何永久设置
        print("提示：可通过 export %s_BASE_URL=\"%s\" 永久设置，避免每次输入。" % (_CRED_PREFIX, base_url))
    return base_url


# ---------------------------------------------------------------------------
# API client（按认证方式生成不同请求代码）
# ---------------------------------------------------------------------------

def _build_auth(creds):
    """根据认证方式构建请求认证参数。
    
    返回值：dict，可能包含 auth/headers（不含 params——api_key query 模式的
    api_key 由 _api_request 合并到 params 里，避免 params 冲突）。
    """
    if _AUTH_TYPE == "basic":
        from requests.auth import HTTPBasicAuth
        if not creds.get('username') or not creds.get('password'):
            print("错误：缺少认证，请设置环境变量（%s_USERNAME / %s_PASSWORD）" % (_CRED_PREFIX, _CRED_PREFIX), file=sys.stderr)
            sys.exit(3)
        return {"auth": HTTPBasicAuth(creds['username'], creds['password'])}
    elif _AUTH_TYPE == "token":
        if not creds.get('token'):
            print("错误：缺少认证，请设置环境变量（%s_TOKEN）" % _CRED_PREFIX, file=sys.stderr)
            sys.exit(3)
        return {"headers": {"Authorization": "Bearer " + creds['token']}}
    elif _AUTH_TYPE == "api_key":
        if not creds.get('api_key'):
            print("错误：缺少认证，请设置环境变量（%s_API_KEY）" % _CRED_PREFIX, file=sys.stderr)
            sys.exit(3)
        if _API_KEY_LOCATION == "header":
            return {"headers": {_API_KEY_HEADER: creds['api_key']}}
        else:
            # query 模式：返回 api_key 信息，由 _api_request 合并到 params（避免 params 冲突）
            return {"_api_key_param": _API_KEY_HEADER, "_api_key_value": creds['api_key']}
    elif _AUTH_TYPE == "none":
        return {}  # 无认证（公开 API）
    else:
        print("错误：未知认证方式 %s" % _AUTH_TYPE, file=sys.stderr)
        sys.exit(2)


def _api_request(method, path, payload=None, params=None, files=None):
    """调用目标系统 REST API。"""
    import requests

    base_url = _get_base_url()
    creds = _load_credentials()
    auth_result = _build_auth(creds)
    
    # 提取 api_key query 信息（如果有），合并到 params
    api_key_param = auth_result.pop("_api_key_param", None)
    api_key_value = auth_result.pop("_api_key_value", None)
    if api_key_param and api_key_value:
        params = dict(params or {})
        params[api_key_param] = api_key_value
    
    auth_kwargs = auth_result
    url = f"{base_url}{path}"

    try:
        if method == 'GET':
            resp = requests.get(url, params=params, timeout=30, **auth_kwargs)
        elif method == 'POST':
            resp = requests.post(url, json=payload, files=files, timeout=30, **auth_kwargs)
        elif method == 'PUT':
            resp = requests.put(url, json=payload, files=files, timeout=30, **auth_kwargs)
        elif method == 'PATCH':
            resp = requests.patch(url, json=payload, timeout=30, **auth_kwargs)
        elif method == 'DELETE':
            resp = requests.delete(url, params=params, timeout=30, **auth_kwargs)
        else:
            print(f"错误：不支持的方法 {method}", file=sys.stderr)
            sys.exit(2)

        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}
    except requests.exceptions.HTTPError as e:
        print(f"错误：API 调用失败 {resp.status_code}: {e}", file=sys.stderr)
        sys.exit(4)
    except requests.exceptions.RequestException as e:
        print(f"错误：网络请求失败: {e}", file=sys.stderr)
        sys.exit(4)
    except ValueError:
        print(f"错误：API 返回非 JSON 格式（可能返回 HTML 错误页）", file=sys.stderr)
        sys.exit(4)


# ---------------------------------------------------------------------------
# Subcommand implementations（由 API 文档解析动态生成）
# ---------------------------------------------------------------------------

def cmd_{subcommand_1}(args):
    """能力 A：{description_1}"""
    # {method} {path}
    result = _api_request('{METHOD}', '{PATH}', payload={PAYLOAD}, params={PARAMS})
    return result


def cmd_{subcommand_2}(args):
    """能力 B：{description_2}"""
    # {method} {path}
    result = _api_request('{METHOD}', '{PATH}', payload={PAYLOAD}, params={PARAMS})
    return result


# --- 以下场景生成时参考对应代码片段（不替换上方占位符示例）---

# GET/DELETE 路径参数：路径中的变量用 f-string 拼接（路径和变量名由 API 文档决定）
result = _api_request('GET', f'{base_path}/{args.{id_param}}')
result = _api_request('DELETE', f'{base_path}/{args.{id_param}}', params=params)

# 可选参数：用 is not None 判断，不传 None 到 API
payload = {'{required}': args.{required}}
if args.{optional} is not None:
    payload['{optional}'] = args.{optional}

# 文件上传单文件：files=dict（value 是单个 file object）
with open(args.filepath, 'rb') as f:
    result = _api_request('POST', '{path}', files={'{field}': f})

# 文件上传批量：files=元组列表（不能用 dict value 为列表——requests 会报 unpack 错误）
files = [open(fp, 'rb') for fp in args.filepaths]
try:
    result = _api_request('POST', '{path}', files=[('{field}', f) for f in files])
finally:
    for f in files:
        f.close()


# ---------------------------------------------------------------------------
# Capability list
# ---------------------------------------------------------------------------

def cmd_capability_list(args):
    """列出本 skill 所有能力项。"""
    return {
        'capability': 'capability-list',
        'skill': '{skill_name}',
        'version': '{version}',
        'capabilities': [
            {'name': '{subcommand_1}', 'description': '{description_1}',
             'command': '{subcommand_1} {params_hint_1}'},
            {'name': '{subcommand_2}', 'description': '{description_2}',
             'command': '{subcommand_2} {params_hint_2}'},
            {'name': 'capability-list', 'description': '列出本 skill 所有能力项',
             'command': 'capability-list'},
        ],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_md(payload):
    """将 JSON 输出转为 Markdown 表格（可选）。"""
    cap = payload.get('capability', '')
    if cap == 'capability-list':
        lines = [f"## 能力清单（{payload.get('skill', '')}）", "",
                 "| 能力 | 说明 | 命令 |", "|---|---|---|"]
        for c in payload.get('capabilities', []):
            lines.append(f"| {c['name']} | {c['description']} | `{c['command']}` |")
        return "\n".join(lines)
    # 其他子命令的 Markdown 渲染按实际响应结构生成
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='{skill_name}',
        description='{description}')

    def add_common_args(p):
        p.add_argument('--format', choices=['json', 'md'], default='json',
                       help='输出格式，默认 json')

    sub = parser.add_subparsers(dest='command', help='能力命令')

    # 子命令由 API 文档解析动态生成——以下为模板示例
    p_1 = sub.add_parser('{subcommand_1}', help='{description_1}')
    # 参数由 API 文档的 parameters 定义决定
    p_1.add_argument('--param1', required=True, help='参数说明')
    add_common_args(p_1)

    p_2 = sub.add_parser('{subcommand_2}', help='{description_2}')
    p_2.add_argument('--param2', default=None, help='参数说明')
    add_common_args(p_2)

    # capability-list
    p_cl = sub.add_parser('capability-list', help='列出本 skill 所有能力项')
    add_common_args(p_cl)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    dispatch = {
        '{subcommand_1}': cmd_{subcommand_1},
        '{subcommand_2}': cmd_{subcommand_2},
        'capability-list': cmd_capability_list,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        print(f"错误：未知命令 {args.command}", file=sys.stderr)
        sys.exit(2)

    try:
        payload = handler(args)
        if args.format == 'md':
            print(render_md(payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(4)


if __name__ == '__main__':
    main()
```

## 占位符说明

| 占位符 | 来源 | 说明 |
|--------|------|------|
| {skill_name} | 用户需求 | skill 名称（小写连字符） |
| {entry_file} | 固定 | Python 入口文件路径（scripts/{name}.py，不生成根目录 main.py） |
| {description} | 用户需求 | skill 描述 |
| {version} | SKILL.md frontmatter | 版本号 |
| {CRED_PREFIX} | skill name 推导 | 凭据变量前缀（skill name 转大写下划线，如 blog-manager → BLOG_MANAGER） |
| {AUTH_TYPE} | 需求澄清确认 | 认证方式：basic / token / api_key / none |
| {API_KEY_HEADER} | 需求澄清确认 | API Key 的 Header/Query 参数名（如 X-API-Key / api_key / key） |
| {API_KEY_LOCATION} | 需求澄清确认 | API Key 传递位置：header / query |
| {subcommand_N} | API 文档解析 | 子命令名（由端点生成规则决定） |
| {description_N} | API 文档解析 | 子命令描述 |
| {METHOD} | API 文档解析 | HTTP 方法（GET/POST/PUT/PATCH/DELETE） |
| {PATH} | API 文档解析 | API 路径（如 /api/articles） |
| {PAYLOAD} | API 文档解析 | 请求体参数（POST/PUT/PATCH 时） |
| {PARAMS} | API 文档解析 | 查询参数（GET 时） |
| {params_hint_N} | API 文档解析 | 参数提示文本 |

## 生成规则

1. API 文档解析得到 N 个端点 → 生成 N 个子命令 + 1 个 capability-list
2. 每个子命令的参数由 API 文档的 parameters / requestBody 定义决定
3. GET 端点 → 参数转 query params
4. POST/PUT/PATCH 端点 → 参数转 JSON body
5. DELETE 端点 → 可能有 query 参数（如 soft=true），_api_request 传 params
6. 所有子命令支持 --format json/md 切换输出格式
7. 退出码统一：0=成功；2=参数错误；3=缺少配置（地址或认证）；4=API 调用失败

### SKILL.md 段落模板

生成的 SKILL.md 必须符合 `references/skill-spec.md` 规范，包含以下段落（spec_check.py 会检查必填段落）。

rest-api-skill-creator 通过 grape A2A 链路触发，生成的 skill 默认为系统级——包含安全硬约束/评论规范/任务结束等系统级段落。

````markdown
---
name: {skill_name}
version: {version}
description: {description}
triggers:
  - {trigger_1}
  - {trigger_2}
tags:
  - {tag_1}
  - {tag_2}
tools:
  - curl
---

# {skill_name}

## 安全硬约束（必须遵守）

**通用约束（所有 skill 必含）**：

1. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
2. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
3. 所有 curl / API 调用设 30s 超时——超时判定失败，不 hang。
4. 严禁 mock 模式/假数据：所有 API 调用必须真实访问目标系统。
5. 调用目标 API 一律以 `references/api-reference.md` 为准：严禁根据经验/记忆/猜测自行拼写 API 路径。

**动态约束（按认证方式生成，无认证时省略）**：

- 有认证（Basic Auth / Bearer Token / API Key）：凭据按 4 级优先级读取（与 base_url 一致）——项目知识（.project-info/ 下 JSON 配置文件的 secrets.{CRED_PREFIX}_*）> 环境变量（`{CRED_PREFIX}_*`）> 交互输入。凭据不得写入任何文件/日志/评论。
- 无认证：省略此条。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[$AGENT_NAME]` 开头（如果环境变量未注入，使用 skill name 作为前缀，如 `[blog-use]`）
- 引用用户原文：`> {用户评论}\n\n[$AGENT_NAME] {回复}`

## Configuration

目标系统 API 地址必须在使用前确定。所有命令中的 `{base_url}` 替换为实际地址（格式 `http://<host>:<port>`）。

`{base_url}` 解析优先级：

1. **项目知识** — 检查 `.project-info/` 目录下 JSON 配置文件（config.{CRED_PREFIX}_BASE_URL）
2. **环境变量** — `{CRED_PREFIX}_BASE_URL`
3. **当前上下文** — 用户直接提供或 A2A context 中已包含
4. **交互输入** — 以上都无时提示用户输入

### 配置指导

**方式一：环境变量（临时，推荐快速测试）**

```bash
export {CRED_PREFIX}_BASE_URL="http://<host>:<port>"
# 有认证时还需要（按认证方式选）：
export {CRED_PREFIX}_TOKEN="your-token"
# 或
export {CRED_PREFIX}_USERNAME="your-username"
export {CRED_PREFIX}_PASSWORD="your-password"
```

**方式二：项目知识 JSON 文件（持久化，推荐生产使用）**

在项目根目录下创建 `.project-info/` 目录，放入任意名称的 `.json` 文件（脚本会递归扫描所有 JSON 文件）：

无认证：
```json
{
  "config": {
    "{CRED_PREFIX}_BASE_URL": "http://<host>:<port>"
  }
}
```

有认证（Bearer Token 示例）：
```json
{
  "version": 1,
  "secrets": {
    "{CRED_PREFIX}_TOKEN": "your-token-here"
  },
  "config": {
    "{CRED_PREFIX}_BASE_URL": "http://<host>:<port>"
  }
}
```

> key 必须按 `{CRED_PREFIX}_` 前缀命名（由 skill name 推导），避免与其他系统凭据冲突。
> ⚠️ `.project-info/` 含敏感凭据，不提交到 git 仓库（加入 .gitignore）。

## 场景 / When to Use

- 用户要求{从需求提取的操作场景}
- 用户要求{从需求提取的操作场景}
- ...

Don't use for: {非本 API 的操作}

## 知识 / Knowledge

### API Base URL

```
{base_url}
```

All endpoints below are relative to this base. {AUTH_TYPE} 认证{需要/不需要} header。

### Endpoints

按资源分组列出端点表（从 API 文档解析）：

| Method | Path | Body | Description |
|--------|------|------|-------------|
| {METHOD} | {PATH} | {body} | {description} |
| ... | ... | ... | ... |

### 认证方式
- Basic Auth：{CRED_PREFIX}_USERNAME / {CRED_PREFIX}_PASSWORD
- Bearer Token：{CRED_PREFIX}_TOKEN
- API Key：{CRED_PREFIX}_API_KEY
- 无认证：不需要凭据
- API 地址：{CRED_PREFIX}_BASE_URL

**凭据前缀**：由 skill name 推导（`{skill_name}` → `{CRED_PREFIX}`），skill name 转大写下划线。

**凭据读取优先级（4 级，与 base_url 一致）**：

1. **项目知识** — 递归扫描 `.project-info/` 下所有 JSON 文件，读取 `secrets.{CRED_PREFIX}_*` 字段（key 必须按前缀命名，避免与其他系统凭据冲突）
2. **环境变量** — 项目知识缺失时扫描 `{CRED_PREFIX}_*` 开头变量（如 `{CRED_PREFIX}_TOKEN`）
3. **当前上下文** — A2A context 已注入的环境变量（已包含在步骤 2）
4. **交互输入** — 以上都无时提示用户输入

**项目知识 JSON 配置示例**（有认证时）：

```json
{
  "secrets": {
    "{CRED_PREFIX}_TOKEN": "your-token-here"
  },
  "config": {
    "{CRED_PREFIX}_BASE_URL": "http://<host>:<port>"
  }
}
```

### Common Pitfalls
1. **API 不可达。** 先检查 base_url 是否正确，curl /health 确认可达。
2. **{从 API 文档提取的陷阱}。** {解决方案}。
3. **{从 API 文档提取的陷阱}。** {解决方案}。
4. **{从 API 文档提取的陷阱}。** {解决方案}。
5. **{从 API 文档提取的陷阱}。** {解决方案}。

### 字段说明
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| {field} | {type} | {yes/no} | {default} | {description} |
| ... | ... | ... | ... | ... |

## 步骤 / Steps

主流程按序号排列，每个操作是步骤里的子步骤。

### 1. 检查 API 可达性

```bash
python3 scripts/{skill_name}.py health-check
```
Expected: `{"status":"ok"}`

### 2. 查询可用资源

```bash
python3 scripts/{skill_name}.py {list-subcommand}
```
Expected: `{"code":200,"data":[...]}`

### 3. 执行操作

⚠️ 每个子命令必须生成一个子步骤（含命令 + Expected + 参数说明），不得省略。

#### {subcommand_1}
```bash
python3 scripts/{skill_name}.py {subcommand_1} {params_hint_1}
```
Expected: `{expected_return}`

参数说明：
- `--{param_1}`：{param_1_desc}
- `--format`：输出格式 json/md，默认 json

#### capability-list
```bash
python3 scripts/{skill_name}.py capability-list
```
Expected: `{"capability":"capability-list","subcommand_count":N,...}`

## 判断标准 / Verification

- [ ] API 可达（GET /health 返回 200）
- [ ] 只读操作返回有效数据（非空/字段完整）
- [ ] 脚本 --help 无语法错误
- [ ] 退出码正确（0=成功/2=参数错误/3=缺少配置/4=API失败）

## 输出规范 / Output Format

### 脚本输出

- 默认输出：JSON
- 可选输出：Markdown 表格（--format md）
- 使用示例：`cd skills/{skill_name}/ && python3 scripts/{skill_name}.py {subcommand_1} {params_hint_1}`

### Issue 评论格式

执行完操作后，Issue 评论 body 格式：

```
[$AGENT_NAME] ✅ {操作名称}完成

## 操作结果

| 操作 | 状态 | 资源 ID | 详情 |
|------|------|---------|------|
| {操作名} | ✅ 成功 / 🔴 失败 | {id} | {简要说明} |

**API 地址**：{base_url}
**执行时间**：{UTC ISO}
```

失败时：

```
[$AGENT_NAME] 🔴 {操作名称}失败

## 错误分析

- **操作**：{操作名}
- **错误**：{API 返回的错误信息}
- **HTTP 状态码**：{code}
- **原因**：{分析原因}

### 修复建议
{修复步骤}
```

## 参考文档 / References

- [API 端点文档](references/api-reference.md)
- [测试用例](templates/test-vars.json)

## 分析结论示例

成功时：

```
分析结论：
- 场景：{API 操作}
- Skill：{skill_name} v{version}
- 操作：{subcommand} {params}
- 结果：✅ 成功（{API 返回的关键数据摘要}）
- 必须执行：Issue 评论汇总结果 → 任务结束
```

失败时：

```
分析结论：
- 场景：{API 操作}
- Skill：{skill_name} v{version}
- 操作：{subcommand} {params}
- 结果：🔴 失败（{错误原因}）
- 必须执行：Issue 评论报告错误 → 任务结束
```

## 任务结束

完成所有业务动作后，在最终答复末尾输出以下 JSON 块并结束任务：

```json
{
  "actions": ["{具体执行的操作，如 list-articles: 返回 5 篇文章}"],
  "conclusion": "{处理结论，如 查询成功，返回 5 篇文章}",
  "artifacts": ["{产生的产物：写操作返回资源 ID 或操作结果 / 文件操作返回 URL / 只读查询无产物填空数组}"],
  "next_step": "{下一步建议，如 无 / 等待用户下一步指令}",
  "issue_summary": "{Issue 最新聚合结论，紧凑文本，用 | 分隔}"
}
```

注意：只输出一个 JSON 块，放在答复末尾。
````
