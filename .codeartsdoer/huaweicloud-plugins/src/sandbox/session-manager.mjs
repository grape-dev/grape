import { spawn } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createConnection, getCredentials } from './hwlink-api.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
export const WS_EXEC_INDEX_URL = pathToFileURL(join(__dirname, '..', 'ws-exec', 'index.js')).href;

const DEFAULT_WORKSPACE_ID = process.env.HW_WORKSPACE_ID || '0107bd9997aa4287bd2b4890b49af07d';

function resolveEnv() {
  const env = { ...process.env };
  env.PATH = `${env.HOME || '/root'}/.huawei/bin:${env.PATH || ''}`;
  return env;
}

async function runNodeExec(args, timeoutMs = 30000) {
  const env = resolveEnv();
  return new Promise((resolve) => {
    const proc = spawn('node', args, { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (d) => { stdout += d.toString(); });
    proc.stderr.on('data', (d) => { stderr += d.toString(); });

    const timer = setTimeout(() => {
      proc.kill();
      resolve({ error: 'timed out', exitCode: 124 });
    }, timeoutMs);

    proc.on('close', (code) => {
      clearTimeout(timer);
      const out = stdout.trim();
      if (out) {
        try {
          resolve({ ...JSON.parse(out), exitCode: code || 0 });
          return;
        } catch {}
      }
      if (code && code !== 0 && !out) {
        resolve({ error: stderr.trim() || `exit code ${code}`, exitCode: code });
        return;
      }
      resolve({ data: out, exitCode: code || 0 });
    });
  });
}

const sessions = new Map();

async function getSession(workspaceId, username, timeoutMs) {
  const key = `${workspaceId}:${username}`;
  if (sessions.has(key)) return sessions.get(key);

  const { ak, sk, securitytoken } = getCredentials();
  const { wsUrl, source } = await createConnection(workspaceId, ak, sk, securitytoken);

  const { connectHwlinkTerminalSession } = await import(WS_EXEC_INDEX_URL);
  const session = await connectHwlinkTerminalSession({
    url: wsUrl,
    source,
    username,
    timeoutMs,
  });

  sessions.set(key, session);
  return session;
}

export async function execOneShot(workspaceId, command, username, timeoutMs) {
  const { ak, sk, securitytoken } = getCredentials();
  const { wsUrl, source } = await createConnection(workspaceId, ak, sk, securitytoken);

  const { executeHwlinkCommand } = await import(WS_EXEC_INDEX_URL);
  return await executeHwlinkCommand({
    url: wsUrl,
    source,
    username,
    command,
    timeoutMs,
  });
}

export async function execWithSession(workspaceId, command, username, timeoutMs) {
  const session = await getSession(workspaceId, username, timeoutMs);
  return await session.exec(command, { timeoutMs });
}

export async function closeSession(workspaceId, username) {
  const key = `${workspaceId}:${username}`;
  const session = sessions.get(key);
  if (!session) return false;
  sessions.delete(key);
  try { session.close(); } catch {}
  return true;
}

export async function closeAllSessions() {
  for (const [key, session] of sessions) {
    sessions.delete(key);
    try { session.close(); } catch {}
  }
}

export { DEFAULT_WORKSPACE_ID, runNodeExec };
