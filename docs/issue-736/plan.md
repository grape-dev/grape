# Plan: Issue #736 — contents --raw 明文直出实现

## 实现路径（TDD）
1. **grape-cli**（主改动，分支 `feat/issue-736`，基于 main）：
   - RED：`test/test_registry.py` 新增
     - `test_contents_raw_binary_outputs_raw_bytes`（二进制按原始字节输出）
     - `test_contents_base64_flag_outputs_encoded_string`（--base64 旧能力）
     - `test_contents_raw_invalid_base64_falls_back_to_json`（非法 base64 行为有定义）
   - GREEN：`src/registry.py`
     - `--raw`：`base64.b64decode` → UTF-8 解码输出；`UnicodeDecodeError` → `click.get_binary_stream("stdout").write(原始字节)`；解码异常回退 JSON。
     - 新增 `--base64` flag：原样输出 `content` 字段。
     - `--raw`/`--base64` help 文案改为明文语义描述。
   - README.md / README.zh.md 示例补充 `--base64` 行与语义注释。
2. **grape-skills**（文档兜底，分支 `feat/issue-736`）：issue-cli 技能 `grape-cli-reference.md`、`issue-runbook.md`、`issue-developability-analysis.md` 标注 `--raw` 直出明文、无需 `base64 -d`。
3. **grape 主仓**：`docs/issue-736/{spec,plan,task}.md` 流程文档，PR base = `weekly-08-mys`（场景注入基线）。

## 验证
- `python3 -m pytest test/ -q`：新增 3 测试通过；既有失败项（config/cli/d_scope/client-token-env）在 main 基线同样失败，属环境/历史遗留，与本次改动无关。
- 实机冒烟：本地源码运行 `contents --path README.md --base64` 输出 base64 原文；`--raw` 输出明文。

## 分支与 PR
- grape-cli / grape-skills：`feat/issue-736` → PR base `main`（两仓无 weekly 分支，说明见 PR body）。
- grape 主仓：`feat/issue-736` → PR base `weekly-08-mys`（权威注入值）。
