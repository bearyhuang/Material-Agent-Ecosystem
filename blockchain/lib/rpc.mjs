#!/usr/bin/env node
/**
 * BOT Chain JSON-RPC 客户端（Node）。
 *
 * 为什么需要它：`rpc.botchain.ai` 的 DNS 被投毒（返回 Facebook 段 IP），
 * 且带明文 SNI=rpc.botchain.ai 的 ClientHello 会被 RST 注入（实测 read ECONNRESET）。
 * 2026-10-13 实测唯一可用组合（src/fschain.py 里"不发 SNI"的旧方案已失效）：
 *   · 直连真实 IP（AWS ELB，东京；会变，失效时用 CNAME 重新解析）
 *   · SNI 用 ELB 默认证书名 `fluxlayer.xyz`（服务端返回的就是这张证书）
 *   · 用 HTTP Host 头 `rpc.botchain.ai` 让 ELB 路由
 *   · 证书链照常校验（servername 与返回证书一致，无需关闭主机名校验）
 *
 * 环境变量（chain/.env）：
 *   BOT_RPC_URL   常规 URL（DNS 正常时可用，如海外）
 *   BOT_RPC_IP    直连 IP（有则优先，忽略 BOT_RPC_URL）
 *   BOT_RPC_HOST  直连时的 Host 头，默认 rpc.botchain.ai
 *   BOT_RPC_SNI   直连时的 SNI，默认 fluxlayer.xyz
 *   BOT_CHAIN_ID  默认 677
 */
import https from 'node:https';
import tls from 'node:tls';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const CHAIN_DIR = path.resolve(HERE, '..');      // blockchain/
export const ROOT = path.resolve(CHAIN_DIR, '..');      // 仓库根

/** 极简 .env 加载（与 backend/src/fschain.py:load_dotenv 同语义：不覆盖已有环境变量）。 */
export function loadDotenv(file = path.join(CHAIN_DIR, '.env')) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const s = line.trim();
    if (!s || s.startsWith('#') || !s.includes('=')) continue;
    const i = s.indexOf('=');
    const k = s.slice(0, i).trim();
    if (process.env[k] === undefined) process.env[k] = s.slice(i + 1).trim().replace(/^["']|["']$/g, '');
  }
}
loadDotenv();

export const CFG = {
  url: process.env.BOT_RPC_URL || 'https://rpc.botchain.ai',
  ip: (process.env.BOT_RPC_IP || '').trim(),
  host: (process.env.BOT_RPC_HOST || 'rpc.botchain.ai').trim(),
  sni: (process.env.BOT_RPC_SNI || 'fluxlayer.xyz').trim(),
  chainId: Number(process.env.BOT_CHAIN_ID || 677),
  pk: (process.env.BOT_PRIVATE_KEY || '').trim(),
  contract: (process.env.BOT_CONTRACT || '').trim(),
  explorer: (process.env.BOT_EXPLORER || 'https://scan.botchain.ai').replace(/\/+$/, ''),
};

let _rid = 1;
let _mode = null; // 'direct' | 'url' | null

/** 发一次 JSON-RPC 请求。返回 result 字段（错误时抛带 message 的 Error）。 */
export function rpc(method, params = []) {
  const body = JSON.stringify({ jsonrpc: '2.0', method, params, id: _rid++ });
  const useDirect = Boolean(CFG.ip) && _mode !== 'url';
  const opts = useDirect
    ? {
        host: CFG.ip, port: 443, path: '/', method: 'POST',
        servername: CFG.sni,                       // ELB 默认证书名，避免明文 SNI 被 RST
        headers: { Host: CFG.host, 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
        agent: false,
        rejectUnauthorized: true,                  // 证书链照常校验
      }
    : { method: 'POST', headers: { 'content-type': 'application/json' } };

  const url = useDirect ? null : CFG.url;
  const req = https.request(url || opts, (res) => {
    let raw = '';
    res.setEncoding('utf8');
    res.on('data', (c) => (raw += c));
    res.on('end', () => {
      try {
        const j = JSON.parse(raw);
        if (j.error) return _reject(new Error(`${j.error.message} (code ${j.error.code})`));
        _resolve(j.result);
      } catch (e) {
        _reject(new Error(`bad JSON-RPC response: ${raw.slice(0, 200)}`));
      }
    });
  });

  let _resolve, _reject;
  const p = new Promise((res, rej) => { _resolve = res; _reject = rej; });
  req.on('error', (e) => {
    if (useDirect && _mode === null) {
      _mode = 'url'; // 直连失败 → 下次回退常规 URL
      return rpc(method, params).then(_resolve, _reject);
    }
    _reject(e);
  });
  req.write(body);
  req.end();
  return p;
}

export const toHex = (n) => '0x' + BigInt(n).toString(16);
export const fromHex = (h) => (h == null ? 0n : BigInt(h));

export async function chainId() { return Number(fromHex(await rpc('eth_chainId'))); }
export async function blockNumber() { return Number(fromHex(await rpc('eth_blockNumber'))); }

export async function balanceOf(addr) {
  const wei = fromHex(await rpc('eth_getBalance', [addr, 'latest']));
  // BOT 链原生代币 18 位小数
  return Number(wei) / 1e18;
}

export async function nonceOf(addr) { return Number(fromHex(await rpc('eth_getTransactionCount', [addr, 'latest']))); }

/** eth_call：返回 hex 字符串。 */
export async function call(to, data, blockTag = 'latest') {
  return rpc('eth_call', [{ to, data }, blockTag]);
}

/** eth_estimateGas。 */
export async function estimateGas(tx) { return fromHex(await rpc('eth_estimateGas', [tx])); }

/** eth_getLogs：fromBlock/toBlock 用十进制数字或 'latest'（内部转 hex）。 */
export async function getLogs(filter) {
  const f = { ...filter };
  const blk = (v) => (typeof v === 'string' ? v : toHex(v));
  if (f.fromBlock != null) f.fromBlock = blk(f.fromBlock);
  if (f.toBlock != null) f.toBlock = blk(f.toBlock);
  return rpc('eth_getLogs', [f]);
}

export async function gasPrice() { return fromHex(await rpc('eth_gasPrice')); }

export function txUrl(hash) { return `${CFG.explorer}/tx/${hash}`; }
export function addrUrl(a) { return `${CFG.explorer}/address/${a}`; }

/** 等待回执（轮询）。超时抛错。 */
export async function waitReceipt(hash, { timeoutMs = 180000, intervalMs = 3000 } = {}) {
  const t0 = Date.now();
  for (;;) {
    const rc = await rpc('eth_getTransactionReceipt', [hash]);
    if (rc) return rc;
    if (Date.now() - t0 > timeoutMs) throw new Error(`等待回执超时：${hash}`);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export { tls };
