# diff 解析：变更符号提取规则

## 目标

从 PR diff 提取"变更了哪些符号（函数/类/方法/组件）"，作为影响面追溯的输入。

## 数据源

```bash
GET /repos/{owner}/{repo}/pulls/{number}/files
```

返回每个文件的 `filename`、`status`、`additions`、`deletions`、`patch`（unified diff 文本）。

## 通用预处理

1. 跳过非源码文件：`.md`、`.json`、`.yml`、`.yaml`、`.txt`、`.gitignore`、`.lock`、`.svg`、`.png`、`.jpg`、`.pdf`
2. 跳过 `status=removed` 的文件（删除不产生变更符号，但记录到变更摘要）
3. 跳过 `status=renamed` 且内容未变的文件
4. 对每个保留文件的 `patch`，按语言提取符号

## 按语言提取规则

### Python

符号定义模式：
- `def <name>(...)` — 函数
- `async def <name>(...)` — 异步函数
- `class <name>(...)` / `class <name>:` — 类
- 顶层赋值 `NAME = ...`（全大写视为常量，可选）

提取步骤：
1. 用 `git diff --unified=0` 或解析 `patch` 拿到每个 hunk 的 `+` 行范围
2. 对每个 hunk，向上回溯找到最近的 `def`/`class` 行（同缩进或更外层）
3. 判定签名变更：函数名改动 / 参数列表改动 / `async` 增减 / 装饰器增减

正则示例：
- 函数定义：`^(async\s+)?def\s+(\w+)\s*\(([^)]*)\)`
- 类定义：`^class\s+(\w+)\s*(\(([^)]*)\))?\s*:`
- 装饰器：`^@\w+`

### Java

符号定义模式：
- `[修饰符] <returnType> <name>(...)` — 方法（修饰符含 public/private/protected/static/final）
- `class <name>` / `interface <name>` / `enum <name>`
- 字段：`[修饰符] <type> <name> = ...;`

提取步骤：
1. 解析 hunk `+` 行
2. 回溯到最近的方法签名行（含 `(` 且以 `)` 或 `{` 或 `throws` 结尾）
3. 签名变更：方法名/参数列表/返回类型/可见性/`static`/`final` 改动

正则示例：
- 方法：`(?:public|private|protected|static|final|\s)*\s+[\w<>\[\],\s]+\s+(\w+)\s*\(([^)]*)\)`
- 类：`\b(?:class|interface|enum)\s+(\w+)`

### JavaScript / TypeScript / Vue

符号定义模式：
- `function <name>(...)` — 命名函数
- `const <name> = (...) =>` / `const <name> = function` — 箭头/函数表达式
- `export function <name>` / `export const <name>` — 导出
- Vue `<script setup>` 内的 `const <name> = ...`、`function <name>`
- Vue 组件：`defineComponent({ name: '<name>' })` 或文件名推断
- React 组件：`function <Name>(props)` 或 `const <Name> = (props) =>`（首字母大写）

提取步骤：
1. 解析 hunk `+` 行
2. 匹配上述模式，回溯到定义行
3. 签名变更：函数名/参数/导出修饰/组件 props 改动

正则示例：
- 命名函数：`(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)`
- 箭头：`(?:export\s+)?const\s+(\w+)\s*=\s*\(([^)]*)\)\s*=>`
- Vue setup：在 `<script setup>` 块内匹配 `const|function`

## 签名变更判定

| 改动类型 | 签名变更 |
|---------|---------|
| 函数体内部改动（逻辑） | 否 |
| 函数名改动 | 是 |
| 参数增删/类型/默认值 | 是 |
| 返回类型改动 | 是 |
| 可见性（public/private/protected）改动 | 是 |
| `static`/`final`/`async` 增减 | 是 |
| 装饰器/注解增删 | 是 |
| 类继承关系改动 | 是 |
| 字段增删 | 是（影响序列化/反射） |

## 输出结构

```json
{
  "changed_symbols": [
    {
      "file": "src/api/event_pipeline.py",
      "symbol": "run_pipeline",
      "kind": "function",
      "language": "python",
      "lines": "239-507",
      "added_lines": 12,
      "deleted_lines": 3,
      "signature_changed": true,
      "signature_change_detail": "参数新增 strict_mode"
    }
  ]
}
```

## 边界情况

- **大文件**：单文件 >2000 行时，仅提取 hunk 涉及的符号，不全文解析
- **生成代码**：跳过 `*.generated.*`、`*_pb2.py`、`dist/`、`build/`、`node_modules/`
- **测试文件本身变更**：测试文件（`test_*.py`/`*Test.java`/`*.spec.js`）的变更仍提取符号，但标记 `is_test=true`，影响面追溯时跳过
- **无法解析的语言**：记录文件变更但不提取符号，标注 `symbol=unknown`