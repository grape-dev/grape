#!/usr/bin/env node
/**
 * fetch_specs.js — 动态拉取 common-skill-tester 的三份规范 JSON
 *
 *   lint  : 检查点（结构合规/安全基线/资源限制，common_skillslinter.json）
 *   test  : 测试点（运行时功能验证，common_functions.json）
 *   audit : 审计规则（恶意代码/CWE 漏洞，common_auditor.json）
 *
 * 策略：每份先尝试 raw URL，失败(403/网络)则回退到 gitcode API v5（base64 解码）
 *
 * 用法:
 *   node fetch_specs.js                          # 拉取全部三份，输出合并 JSON 到 stdout
 *   node fetch_specs.js --type lint              # 只拉 lint 检查点
 *   node fetch_specs.js --type test              # 只拉功能测试点
 *   node fetch_specs.js --type audit             # 只拉安全审计规则
 *   node fetch_specs.js <url> --type lint        # 指定自定义 URL（不走 API 回退）
 *   node fetch_specs.js --type lint -o /tmp/checks.json   # 拉取并保存到文件
 *   node fetch_specs.js --help                   # 显示用法
 *
 * 退出码: 0=成功 1=失败
 */
const https = require("https");
const http = require("http");
const fs = require("fs");

const SPECS = {
  lint: {
    label: "lint",
    raw: "https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_skillslinter.json",
    api: "https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_skillslinter.json?ref=update-audit-clearlist-20260924",
    validate: (j) => Array.isArray(j.groups),
    summary: (j) => {
      const total = j.groups.reduce((s, g) => s + (g.items ? g.items.length : 0), 0);
      return `${j.groups.length} 组, ${total} 项检查`;
    },
  },
  test: {
    label: "test",
    raw: "https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_functions.json",
    api: "https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_functions.json?ref=update-audit-clearlist-20260924",
    validate: (j) => Array.isArray(j.test_groups),
    summary: (j) => {
      const total = j.test_groups.reduce((s, g) => s + (g.items ? g.items.length : 0), 0);
      return `${j.test_groups.length} 组, ${total} 条测试用例`;
    },
  },
  audit: {
    label: "audit",
    raw: "https://raw.gitcode.com/developer-skill/skills-group-contribution/raw/update-audit-clearlist-20260924/tools/skills_checker/common_auditor.json",
    api: "https://gitcode.com/api/v5/repos/developer-skill/skills-group-contribution/contents/tools/skills_checker/common_auditor.json?ref=update-audit-clearlist-20260924",
    validate: (j) => Array.isArray(j.groups),
    summary: (j) => {
      const total = j.groups.reduce((s, g) => s + (g.items ? g.items.length : 0), 0);
      return `${j.groups.length} 组, ${total} 条检测规则`;
    },
  },
};

function fetch(url) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith("https") ? https : http;
    const req = mod.get(url, { timeout: 20000 }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return fetch(res.headers.location).then(resolve, reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
  });
}

async function tryFetchJSON(spec, rawUrl, apiUrl) {
  // 1) 尝试 raw URL
  try {
    process.stderr.write(`[common-skill-tester:${spec.label}] 尝试 raw URL: ${rawUrl}\n`);
    const raw = await fetch(rawUrl);
    JSON.parse(raw); // validate
    process.stderr.write(`[common-skill-tester:${spec.label}] raw URL 成功\n`);
    return raw;
  } catch (e1) {
    process.stderr.write(
      `[common-skill-tester:${spec.label}] raw URL 失败(${e1.message})，回退到 API v5\n`
    );
  }
  // 2) 回退到 API v5 (base64)
  const apiRaw = await fetch(apiUrl);
  const apiJson = JSON.parse(apiRaw);
  if (apiJson.encoding === "base64" && apiJson.content) {
    const decoded = Buffer.from(apiJson.content, "base64").toString("utf-8");
    JSON.parse(decoded); // validate
    process.stderr.write(`[common-skill-tester:${spec.label}] API v5 成功\n`);
    return decoded;
  }
  throw new Error("API v5 返回缺少 base64 content");
}

function printUsage() {
  process.stdout.write(`用法:
  node fetch_specs.js                          # 拉取全部三份（lint/test/audit），输出合并 JSON
  node fetch_specs.js --type lint|test|audit   # 只拉指定一份
  node fetch_specs.js <url> --type lint        # 指定自定义 URL（不走 API 回退）
  node fetch_specs.js --type lint -o out.json  # 保存到文件（默认输出到 stdout）
  node fetch_specs.js --help                   # 显示本帮助

退出码: 0=成功 1=失败
`);
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    printUsage();
    process.exit(0);
  }

  let types = ["lint", "test", "audit"];
  let outFile = null;
  const customUrl = {};

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--type" && args[i + 1]) {
      const t = args[++i];
      if (!SPECS[t]) {
        process.stderr.write(`[common-skill-tester] 未知类型: ${t}（可选 lint/test/audit）\n`);
        process.exit(1);
      }
      types = [t];
    } else if (args[i] === "-o" && args[i + 1]) {
      outFile = args[++i];
    } else if (!args[i].startsWith("-")) {
      customUrl[types[0]] = args[i]; // 自定义 URL 只作用于当前类型，不走 API 回退
    }
  }

  try {
    const result = {};
    for (const t of types) {
      const spec = SPECS[t];
      const rawUrl = customUrl[t] || spec.raw;
      const apiUrl = customUrl[t] ? null : spec.api;
      const raw = apiUrl ? await tryFetchJSON(spec, rawUrl, apiUrl) : await fetch(rawUrl);
      const json = JSON.parse(raw);
      if (!spec.validate(json)) {
        throw new Error(`JSON 结构不符合 ${t} 预期（缺少 ${t === "test" ? "test_groups" : "groups"} 字段）`);
      }
      result[t] = json;
      process.stderr.write(`[common-skill-tester:${t}] 拉取成功: ${spec.summary(json)}\n`);
    }

    const output = JSON.stringify(result, null, 2);
    if (outFile) {
      fs.writeFileSync(outFile, output, "utf-8");
      process.stderr.write(`[common-skill-tester] 已保存到: ${outFile}\n`);
    } else {
      process.stdout.write(output);
    }
    process.exit(0);
  } catch (err) {
    process.stderr.write(`[common-skill-tester] 拉取失败: ${err.message}\n`);
    process.exit(1);
  }
}

main();