#!/usr/bin/env node
// huawei-cloud-skill-reporter 独立上报 CLI
// 供外部脚本（如 skill-auto-test-pr/run-pipeline.sh）复用同一份上报逻辑。
//
// 用法:
//   node report.js <skill> <status> [request] [result]
//     <skill>   skill 名称（必须 huawei-cloud-* 前缀，否则忽略）
//     <status>  success | failure
//     [request] 动作描述（可选）
//     [result]  执行结果/PR 链接（可选）
//
// 环境变量:
//   HUAWEI_CLOUD_SKILL_REPORT_URL  覆盖上报地址（默认读 ~/.config/huawei-cloud-skill-reporter/server_url.txt，再回退 http://skillsop.topxtopx.com/api/telemetry）

import fs from "fs";
import path from "path";
import os from "os";
import https from "https";
import http from "http";

const CONFIG_DIR = path.join(os.homedir(), ".config", "huawei-cloud-skill-reporter");
const URL_FILE = path.join(CONFIG_DIR, "server_url.txt");
const DEFAULT_URL = "http://skillsop.topxtopx.com/api/telemetry";

let cachedPublicIp = null;

function getReportUrl() {
  try {
    if (fs.existsSync(URL_FILE)) {
      return fs.readFileSync(URL_FILE, "utf-8").trim() || DEFAULT_URL;
    }
  } catch {}
  return process.env.HUAWEI_CLOUD_SKILL_REPORT_URL || DEFAULT_URL;
}

function getPublicIp() {
  if (cachedPublicIp) return cachedPublicIp;

  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === "IPv4" && !net.internal) {
        cachedPublicIp = net.address;
        return cachedPublicIp;
      }
    }
  }
  return "";
}

// Async attempt at a public IP; failures are swallowed and return null.
// Kept separate so a fire-and-forget report never crashes on network errors.
async function tryPublicIp() {
  const services = [
    { host: "api.ipify.org", path: "/?format=text" },
    { host: "ifconfig.me", path: "/ip" },
  ];
  for (const svc of services) {
    try {
      const result = await syncFetch(`https://${svc.host}${svc.path}`);
      if (result) {
        cachedPublicIp = result.trim();
        return cachedPublicIp;
      }
    } catch {}
  }
  return null;
}

function syncFetch(url) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith("https") ? https : http;
    const req = mod.get(url, { timeout: 3000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve(data));
    });
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("timeout"));
    });
  });
}

async function report(skill, status, request, result) {
  if (!skill || !skill.startsWith("huawei-cloud-")) {
    process.stderr.write(`[huawei-cloud-skill-reporter] ignored non huawei-cloud-* skill: ${skill}\n`);
    return;
  }
  const url = getReportUrl();
  if (!url) return;

  const ip = (await tryPublicIp()) || getPublicIp();
  const body = JSON.stringify({
    skill,
    status,
    ts: new Date().toISOString(),
    request: request || "",
    result: String(result || "").slice(0, 1000),
    ip,
  });

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(3000),
    });
    process.stderr.write(`[huawei-cloud-skill-reporter] reported ${skill} status=${status} http=${resp.status}\n`);
  } catch (err) {
    process.stderr.write(`[huawei-cloud-skill-reporter] report failed: ${err.message}\n`);
  }
}

const skill = process.argv[2];
const status = process.argv[3] === "failure" ? "failure" : "success";
const request = process.argv[4] || "";
const result = process.argv[5] || "";

report(skill, status, request, result).catch(() => {});
