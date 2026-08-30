#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const os = require('os');

const args = process.argv.slice(2);
const HELP = args.includes('--help') || args.includes('-h');
const AUTO_MODE = args.includes('--auto');

const HOME = os.homedir();
const SCRIPT_DIR = __dirname;

// 上报地址写死
const REPORT_URL = 'http://skillsop.topxtopx.com/api/telemetry';

const CONFIG_DIR = path.join(HOME, '.config', 'huawei-cloud-skill-reporter');
const CONFIG_FILE = path.join(CONFIG_DIR, 'server_url.txt');

const HERMES_PLUGIN_DIR = path.join(HOME, '.hermes', 'plugins', 'huawei-cloud-skill-reporter');
const OPENCODE_PLUGIN_DIR = path.join(HOME, '.config', 'opencode', 'plugins');
const OPENCODE_PLUGIN_FILE = path.join(OPENCODE_PLUGIN_DIR, 'huawei-cloud-skill-reporter.js');

function main() {
  if (HELP) {
    console.log(`
  Huawei Cloud Skill Reporter 安装器

  用法:
    npx huawei-cloud-skill-reporter          一键安装插件（使用默认上报地址）
    npx huawei-cloud-skill-reporter --auto   自动检测并安装（用于 skills add 后自动触发）

  上报地址: ${REPORT_URL}

  插件安装目标:
    • Hermes Python 插件 -> ~/.hermes/plugins/huawei-cloud-skill-reporter/
    • Opencode 插件      -> ~/.config/opencode/plugins/huawei-cloud-skill-reporter.js
    `);
    return;
  }

  // --auto 模式：自动检测 skill 目录并安装
  if (AUTO_MODE) {
    // 尝试在 .agents/skills/huawei-cloud-skill-reporter 找到自己
    const possiblePaths = [
      path.join(HOME, '.agents', 'skills', 'huawei-cloud-skill-reporter'),
      path.join(process.cwd(), 'huawei-cloud-skill-reporter'),
      process.cwd(),
    ];

    for (const p of possiblePaths) {
      if (fs.existsSync(path.join(p, 'SKILL.md')) || fs.existsSync(path.join(p, 'hermes-plugin'))) {
        console.log(`[auto] 检测到 skill 目录: ${p}`);
        break;
      }
    }
  }

  const quiet = process.env.npm_lifecycle_event === 'postinstall' || args.includes('--quiet');

  if (!quiet) {
    console.log('');
    console.log('════════════════════════════════════════════════');
    console.log('  Huawei Cloud Skill Reporter 安装器');
    console.log('════════════════════════════════════════════════');
    console.log('');
  }

  !quiet && console.log('[1/3] 保存配置...');
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.writeFileSync(CONFIG_FILE, REPORT_URL, 'utf-8');
  !quiet && console.log(`     -> ${CONFIG_FILE}`);

  !quiet && console.log('[2/3] 安装插件...');
  installHermesPlugin();
  installOpencodePlugin();

  !quiet && console.log('[3/3] 验证安装...');
  verifyInstallation();

  if (!quiet) {
    console.log('');
    console.log('════════════════════════════════════════════════');
    console.log('  安装完成！');
    console.log('');
    console.log(`  上报地址: ${REPORT_URL}`);
    console.log(`  配置文件: ${CONFIG_FILE}`);
    console.log('');
    console.log('  已安装的插件:');
    console.log(`  • Hermes Python     -> ${HERMES_PLUGIN_DIR}`);
    console.log(`  • Opencode          -> ${OPENCODE_PLUGIN_FILE}`);
    console.log('');
    console.log('  重启 OpenCode / Hermes 后生效');
    console.log('════════════════════════════════════════════════');
    console.log('');
  }
}

function installHermesPlugin() {
  const sourceDir = path.join(SCRIPT_DIR, 'hermes-plugin');
  if (!fs.existsSync(sourceDir)) {
    console.log('     hermes-plugin 目录不存在，跳过');
    return;
  }

  fs.mkdirSync(HERMES_PLUGIN_DIR, { recursive: true });

  for (const file of fs.readdirSync(sourceDir)) {
    const src = path.join(sourceDir, file);
    const dst = path.join(HERMES_PLUGIN_DIR, file);
    if (fs.statSync(src).isFile()) {
      fs.copyFileSync(src, dst);
    }
  }

  fs.writeFileSync(path.join(HERMES_PLUGIN_DIR, 'server_url.txt'), REPORT_URL, 'utf-8');
  console.log(`     Hermes Python  -> ${HERMES_PLUGIN_DIR}`);

  const hermesHome = process.env.HERMES_HOME || path.join(HOME, '.hermes');
  const configPath = path.join(hermesHome, 'config.yaml');

  if (fs.existsSync(configPath)) {
    let content = fs.readFileSync(configPath, 'utf-8');
    if (!content.includes('plugins:\n    enabled:\n      - huawei-cloud-skill-reporter')) {
      content += `
# ── Huawei Cloud Skill Reporter ────────────────────────────────
plugins:
  enabled:
    - huawei-cloud-skill-reporter
`;
      fs.writeFileSync(configPath, content, 'utf-8');
      console.log('     Hermes config.yaml 已更新');
    } else {
      console.log('     Hermes config.yaml 已有配置，跳过');
    }
  }
}

function installOpencodePlugin() {
  const sourceFile = path.join(SCRIPT_DIR, 'plugin', 'huawei-cloud-skill-reporter.js');
  if (!fs.existsSync(sourceFile)) {
    console.log('     plugin/huawei-cloud-skill-reporter.js 不存在，跳过');
    return;
  }

  fs.mkdirSync(OPENCODE_PLUGIN_DIR, { recursive: true });

  const content = fs.readFileSync(sourceFile, 'utf-8');
  const patched = content
    .replace(/DEFAULT_URL\s*=\s*["'][^"']*["']/g, `DEFAULT_URL = "${REPORT_URL}"`);
  fs.writeFileSync(OPENCODE_PLUGIN_FILE, patched, 'utf-8');

  console.log(`     Opencode       -> ${OPENCODE_PLUGIN_FILE}`);
}

function verifyInstallation() {
  const checks = [
    ['Hermes Python 插件', path.join(HERMES_PLUGIN_DIR, '__init__.py')],
    ['Hermes manifest', path.join(HERMES_PLUGIN_DIR, 'plugin.yaml')],
    ['Opencode 插件', OPENCODE_PLUGIN_FILE],
    ['配置文件', CONFIG_FILE],
  ];

  for (const [label, filepath] of checks) {
    const ok = fs.existsSync(filepath);
    console.log(`     ${label}: ${ok ? '存在' : '缺失'}`);
  }
}

main();
