#!/usr/bin/env node
/**
 * MAE 链上 CLI（BOT Chain 主网 · Chain ID 677）
 * 需求标准：docs/Agent竞马结果上链.md §8
 *
 *   node mae.mjs deploy  [--dry-run] [--cooldown 86400]   部署三合约（Registry→Competition→PredictionRecord）
 *   node mae.mjs register [--name N --domain D --model-hash 0x.. --version V] [--from file.json]
 *   node mae.mjs race    [--task lap_shear_strength] [--domain adhesive] [--date 2026-10-13]
 *   node mae.mjs rank    --race 1 --agent 1 --milli 412 [--score-hash 0x..] [--redacted]
 *   node mae.mjs predict --agent 1 --class epoxy_adhesive --property lap_shear_strength \
 *                          --value 32.5 --ci-low 30.1 --ci-high 34.9 [--redacted]
 *   node mae.mjs status  [--wallet 0x..]
 *
 * 约定（与 backend/scripts/chain_*.py 一致）：
 *   · 私钥只从 blockchain/.env 的 BOT_PRIVATE_KEY 读，绝不入库、绝不进日志；
 *   · deployments.json 为数组追加；每次 deploy 同步刷新 frontend/data/chain.json；
 *   · redacted=true 时数值强制归零（合约侧会二次校验，夹带即 revert）。
 */
import fs from 'node:fs';
import path from 'node:path';
import { Wallet, Interface, keccak256, toUtf8Bytes } from 'ethers';
import {
  CHAIN_DIR, ROOT, CFG, rpc, chainId, blockNumber, balanceOf, nonceOf,
  call, estimateGas, gasPrice, waitReceipt, txUrl, addrUrl, fromHex,
} from './lib/rpc.mjs';

const ABI_DIR = path.join(CHAIN_DIR, 'abi');
const DEPLOY_FILE = path.join(CHAIN_DIR, 'deployments.json');
const CHAIN_JSON = path.join(ROOT, 'frontend', 'data', 'chain.json');

/* ---------------------------------------------------------------- 基础工具 */
const say = (s) => console.log(s);
const info = (s) => say(`[i] ${s}`);
const ok = (s) => say(`[+] ${s}`);
const done = (s) => say(`[✓] ${s}`);
const dry = (s) => say(`[DRY-RUN] ${s}`);
const bad = (s) => { say(`[!] ${s}`); process.exitCode = 1; };

const keccak = (text) => keccak256(toUtf8Bytes(String(text)));
const INT32_MIN = -2147483648, INT32_MAX = 2147483647;
const asInt32 = (v, what) => {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n) || n < INT32_MIN || n > INT32_MAX) throw new Error(`${what} 超出 int32 范围：${v}`);
  return n;
};
const milli = (v) => Math.round(Number(v) * 1000);

function parseArgs(argv) {
  const a = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const s = argv[i];
    if (!s.startsWith('--')) { a._.push(s); continue; }
    const body = s.slice(2);
    const eq = body.indexOf('=');
    if (eq >= 0) { a[body.slice(0, eq)] = body.slice(eq + 1); continue; }
    if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) { a[body] = argv[++i]; }
    else a[body] = true;
  }
  return a;
}

function loadAbi(name) {
  const f = path.join(ABI_DIR, `${name}.json`);
  if (!fs.existsSync(f)) throw new Error(`缺少 ABI 产物 ${f}（先 node compile.js）`);
  const j = JSON.parse(fs.readFileSync(f, 'utf8'));
  return { iface: new Interface(j.abi), json: j };
}
const loadDeployments = () => (fs.existsSync(DEPLOY_FILE) ? JSON.parse(fs.readFileSync(DEPLOY_FILE, 'utf8')) : []);
const findDeployment = (contract) => [...loadDeployments()].reverse().find((d) => d.contract === contract);
const short = (h) => (typeof h === 'string' && h.length > 16 ? `${h.slice(0, 10)}…${h.slice(-8)}` : h);

function requireKey() {
  if (!CFG.pk) throw new Error('未配置 BOT_PRIVATE_KEY：cp blockchain/.env.example blockchain/.env 后填入');
  return new Wallet(CFG.pk);
}

/** 发送一笔已签名交易（EIP-1559 优先，失败回退 legacy）。等待回执并校验 status。 */
async function sendTx(wallet, { to = null, data, gas }) {
  const from = await wallet.getAddress();
  const nonce = await nonceOf(from);
  let tx;
  try {
    const prio = fromHex(await rpc('eth_maxPriorityFeePerGas'));
    const blk = await rpc('eth_getBlockByNumber', ['latest', false]);
    const base = fromHex(blk.baseFeePerGas) || 0n;
    const p = prio > 0n ? prio : 1000000000n;
    tx = { type: 2, to, data, nonce, gasLimit: gas, maxPriorityFeePerGas: p, maxFeePerGas: base * 2n + p, chainId: CFG.chainId };
  } catch {
    tx = { type: 0, to, data, nonce, gasLimit: gas, gasPrice: await gasPrice(), chainId: CFG.chainId };
  }
  const signed = await wallet.signTransaction(tx);
  const hash = await rpc('eth_sendRawTransaction', [signed]);
  info(`交易已广播 ${short(hash)} → ${txUrl(hash)}`);
  const rc = await waitReceipt(hash);
  if (rc.status !== '0x1') throw new Error(`交易执行失败（status=0）：${hash}`);
  return { hash, receipt: rc };
}

/** 估算 gas（+20% buffer）。 */
async function gasFor(wallet, { to = null, data }) {
  const from = await wallet.getAddress();
  const g = await estimateGas({ from, to, data });
  return g * 120n / 100n;
}

async function view(contract, fn, args = []) {
  const { iface } = loadAbi(contract);
  const d = findDeployment(contract);
  if (!d) throw new Error(`${contract} 未部署（先 node mae.mjs deploy）`);
  const data = iface.encodeFunctionData(fn, args);
  const raw = await call(d.address, data);
  return iface.decodeFunctionResult(fn, raw);
}

function recordDeployment(entry) {
  const all = loadDeployments();
  all.push(entry);
  fs.writeFileSync(DEPLOY_FILE, JSON.stringify(all, null, 2) + '\n');
  ok(`deployments.json 已记录 ${entry.contract} @ ${entry.address}`);
}

/** 同步 frontend/data/chain.json（前端唯一链上配置来源）。 */
function syncChainJson() {
  const all = loadDeployments();
  const byName = {};
  for (const d of all) byName[d.contract] = d;
  const pick = (name) => (byName[name] ? {
    address: byName[name].address, deploy_tx: byName[name].tx_hash,
    block: byName[name].block, explorer: addrUrl(byName[name].address),
    tx_explorer: byName[name].tx_explorer, deployed_at: byName[name].deployed_at,
  } : null);
  let prev = {};
  if (fs.existsSync(CHAIN_JSON)) { try { prev = JSON.parse(fs.readFileSync(CHAIN_JSON, 'utf8')); } catch {} }
  const out = {
    ...prev,
    network: 'BOT Chain 主网', chain_id: CFG.chainId,
    rpc: CFG.url, explorer: CFG.explorer,
    contracts: {
      MaterialAgentRegistry: pick('MaterialAgentRegistry'),
      AgentCompetition: pick('AgentCompetition'),
      PredictionRecord: pick('PredictionRecord'),
      FormulaSwarmAttestor: pick('FormulaSwarmAttestor'),
    },
  };
  fs.mkdirSync(path.dirname(CHAIN_JSON), { recursive: true });
  fs.writeFileSync(CHAIN_JSON, JSON.stringify(out, null, 2) + '\n');
  ok(`frontend/data/chain.json 已刷新`);
}

/* ---------------------------------------------------------------- deploy */
async function cmdDeploy(a) {
  const cooldown = a.cooldown !== undefined ? Number(a.cooldown) : 86400;   // 默认 1 天/次
  const wallet = CFG.pk ? new Wallet(CFG.pk) : null;
  const deployer = wallet ? await wallet.getAddress() : '(未配置 BOT_PRIVATE_KEY)';
  info(`网络 chain_id=${CFG.chainId} · RPC=${CFG.url} · 浏览器=${CFG.explorer}`);
  info(`部署者 ${deployer}`);

  const plan = [
    { contract: 'MaterialAgentRegistry', args: [], note: 'Agent 链上身份证（无参）' },
    { contract: 'AgentCompetition', args: ['<registry>'], note: '竞马/排名（依赖 Registry 地址）' },
    { contract: 'PredictionRecord', args: ['<registry>', cooldown], note: `预测调用记录（cooldown=${cooldown}s = ${cooldown / 86400} 天/次）` },
  ];
  for (const p of plan) say(`  · ${p.contract}(${p.args.join(', ')})  ${p.note}`);

  if (!wallet) {
    if (a['dry-run']) { dry('未配置私钥，仅展示部署计划；配置 BOT_PRIVATE_KEY 后可预估 gas 与余额'); return; }
    bad('未配置 BOT_PRIVATE_KEY'); return;
  }
  const bal = await balanceOf(deployer);
  info(`部署者余额 ${bal.toFixed(6)} BOT`);
  if (bal <= 0) { bad('余额为 0，无法支付 gas'); return; }

  const cid = await chainId();
  if (cid !== CFG.chainId) { bad(`链 chainId=${cid} 与期望 ${CFG.chainId} 不符，检查 RPC 配置`); return; }

  if (a['dry-run']) {
    for (const p of plan) {
      try {
        const { iface, json } = loadAbi(p.contract);
        const data = iface.encodeDeploy(p.args.map((x) => (x === '<registry>' ? '0x' + '11'.repeat(20) : x)));
        const g = await gasFor(wallet, { data });
        dry(`${p.contract} 预估 gas ≈ ${g.toString()}`);
      } catch (e) { dry(`${p.contract} gas 预估失败：${e.message}`); }
    }
    dry('未发送任何交易。去掉 --dry-run 正式部署。');
    return;
  }

  // 1) Registry（无参）
  let reg = findDeployment('MaterialAgentRegistry');
  if (reg) info(`MaterialAgentRegistry 已存在，跳过部署：${reg.address}`);
  else {
    const { iface, json } = loadAbi('MaterialAgentRegistry');
    const data = iface.encodeDeploy([]);
    const { hash, receipt } = await sendTx(wallet, { data, gas: await gasFor(wallet, { data }) });
    reg = {
      network: 'mainnet', chain_id: CFG.chainId, contract: 'MaterialAgentRegistry',
      address: receipt.contractAddress, deployer, tx_hash: hash,
      block: Number(fromHex(receipt.blockNumber)), gas_used: Number(fromHex(receipt.gasUsed)),
      explorer: addrUrl(receipt.contractAddress), tx_explorer: txUrl(hash),
      deployed_at: new Date().toISOString(), abi: 'abi/MaterialAgentRegistry.json',
    };
    recordDeployment(reg);
    done(`MaterialAgentRegistry @ ${reg.address}（区块 ${reg.block}，gas ${reg.gas_used}）`);
  }

  // 2) Competition(registry)
  let comp = findDeployment('AgentCompetition');
  if (comp) info(`AgentCompetition 已存在，跳过部署：${comp.address}`);
  else {
    const { iface, json } = loadAbi('AgentCompetition');
    const data = iface.encodeDeploy([reg.address]);
    const { hash, receipt } = await sendTx(wallet, { data, gas: await gasFor(wallet, { data }) });
    comp = {
      network: 'mainnet', chain_id: CFG.chainId, contract: 'AgentCompetition',
      address: receipt.contractAddress, deployer, tx_hash: hash,
      block: Number(fromHex(receipt.blockNumber)), gas_used: Number(fromHex(receipt.gasUsed)),
      explorer: addrUrl(receipt.contractAddress), tx_explorer: txUrl(hash),
      deployed_at: new Date().toISOString(), abi: 'abi/AgentCompetition.json',
    };
    recordDeployment(comp);
    done(`AgentCompetition @ ${comp.address}（区块 ${comp.block}，gas ${comp.gas_used}）`);
  }

  // 3) PredictionRecord(registry, cooldown)
  let pred = findDeployment('PredictionRecord');
  if (pred) info(`PredictionRecord 已存在，跳过部署：${pred.address}`);
  else {
    const { iface, json } = loadAbi('PredictionRecord');
    const data = iface.encodeDeploy([reg.address, cooldown]);
    const { hash, receipt } = await sendTx(wallet, { data, gas: await gasFor(wallet, { data }) });
    pred = {
      network: 'mainnet', chain_id: CFG.chainId, contract: 'PredictionRecord',
      address: receipt.contractAddress, deployer, tx_hash: hash,
      block: Number(fromHex(receipt.blockNumber)), gas_used: Number(fromHex(receipt.gasUsed)),
      explorer: addrUrl(receipt.contractAddress), tx_explorer: txUrl(hash),
      deployed_at: new Date().toISOString(), abi: 'abi/PredictionRecord.json',
    };
    recordDeployment(pred);
    done(`PredictionRecord @ ${pred.address}（区块 ${pred.block}，gas ${pred.gas_used}）`);
  }

  syncChainJson();
  say('');
  say('回填 blockchain/.env（可选）：');
  say(`  BOT_REGISTRY=${reg.address}`);
  say(`  BOT_COMPETITION=${comp.address}`);
  say(`  BOT_PREDICTION=${pred.address}`);
  say(`  BOT_CONTRACT=${pred.address}`);
}

/* ---------------------------------------------------------------- register */
async function cmdRegister(a) {
  const wallet = requireKey();
  const deployer = await wallet.getAddress();
  let items = [];
  if (a.from) {
    const raw = JSON.parse(fs.readFileSync(path.resolve(a.from), 'utf8'));
    const arr = Array.isArray(raw) ? raw : raw.agents;
    if (!Array.isArray(arr) || !arr.length) throw new Error(`${a.from} 中没有 Agent 数组`);
    items = arr.map((x) => ({
      name: String(x.name ?? x.id ?? '').slice(0, 64),
      domain: String(x.domain ?? x.family ?? x.role ?? 'adhesive').slice(0, 32),
      modelHash: x.modelHash ?? keccak(`MAE-AGENT-1|${x.id ?? x.name}|${x.family ?? x.role}|${(x.capabilities || []).join(',')}`),
      version: String(x.version ?? 'fs-agents/1.0').slice(0, 32),
    })).filter((x) => x.name);
    info(`从 ${a.from} 读取 ${items.length} 个 Agent`);
  } else {
    if (!a.name) { bad('缺少 --name（或 --from file.json）'); return; }
    items = [{
      name: a.name, domain: a.domain ?? 'adhesive',
      modelHash: a['model-hash'] ?? keccak(`MAE-AGENT-1|${a.name}|${a.domain ?? 'adhesive'}|${a.version ?? ''}`),
      version: a.version ?? 'v1',
    }];
  }
  if (!items.length) { bad('没有可注册的 Agent'); return; }

  const reg = findDeployment('MaterialAgentRegistry');
  if (!reg) { bad('MaterialAgentRegistry 未部署'); return; }
  const { iface } = loadAbi('MaterialAgentRegistry');
  const before = Number((await view('MaterialAgentRegistry', 'agentCount'))[0]);

  let data;
  if (items.length === 1) {
    const it = items[0];
    data = iface.encodeFunctionData('registerAgent', [it.name, it.domain, it.modelHash, it.version]);
  } else {
    data = iface.encodeFunctionData('registerAgentBatch', [items]);
  }
  info(`注册 ${items.length} 个 Agent → ${reg.address}`);
  items.forEach((it) => say(`  · ${it.name} [${it.domain}] modelHash=${short(it.modelHash)} v=${it.version}`));
  const { hash, receipt } = await sendTx(wallet, { to: reg.address, data, gas: await gasFor(wallet, { to: reg.address, data }) });
  const after = Number((await view('MaterialAgentRegistry', 'agentCount'))[0]);
  done(`注册成功：agentCount ${before} → ${after}（tx ${short(hash)}，区块 ${Number(fromHex(receipt.blockNumber))}）`);
  say(`  ${txUrl(hash)}`);
}

/* ---------------------------------------------------------------- race */
async function cmdRace(a) {
  const wallet = requireKey();
  const task = a.task ?? 'lap_shear_strength';
  const domain = a.domain ?? 'adhesive';
  const date = a.date ?? new Date().toISOString().slice(0, 10);
  const raceHash = a['race-hash'] ?? keccak(`MAE-RACE-1|${date}|${task}|${domain}`);
  const comp = findDeployment('AgentCompetition');
  if (!comp) { bad('AgentCompetition 未部署'); return; }

  // 幂等：扫描最近的竞马，同日同题同域则复用
  const total = Number((await view('AgentCompetition', 'raceCount'))[0]);
  for (let id = total; id >= Math.max(1, total - 200); id--) {
    const r = await view('AgentCompetition', 'getRace', [id]);
    if (String(r[1]).toLowerCase() === raceHash.toLowerCase()) {
      ok(`今日竞马已存在，复用 race #${id}（${task} / ${domain} / ${date}）`);
      return;
    }
  }
  const { iface } = loadAbi('AgentCompetition');
  const data = iface.encodeFunctionData('openRace', [raceHash, task, domain]);
  const { hash, receipt } = await sendTx(wallet, { to: comp.address, data, gas: await gasFor(wallet, { to: comp.address, data }) });
  const id = Number((await view('AgentCompetition', 'raceCount'))[0]);
  done(`已开赛 race #${id}：${task} / ${domain} / ${date}（tx ${short(hash)}）`);
  say(`  ${txUrl(hash)}`);
}

/* ---------------------------------------------------------------- rank */
async function cmdRank(a) {
  const wallet = requireKey();
  const raceId = Number(a.race), agentId = Number(a.agent);
  if (!Number.isFinite(raceId) || !Number.isFinite(agentId)) { bad('缺少 --race / --agent'); return; }
  const redacted = Boolean(a['redacted']);
  const scoreMilli = redacted ? 0 : asInt32(a.milli, '--milli');
  const claimedAt = a['claimed-at'] !== undefined ? Number(a['claimed-at']) : Math.floor(Date.now() / 1000);
  const scoreHash = a['score-hash'] ?? keccak(`MAE-SCORE-1|race=${raceId}|agent=${agentId}|milli=${scoreMilli}|at=${claimedAt}`);
  const comp = findDeployment('AgentCompetition');
  if (!comp) { bad('AgentCompetition 未部署'); return; }

  const { iface } = loadAbi('AgentCompetition');
  const data = iface.encodeFunctionData('submitRanking', [raceId, agentId, scoreMilli, scoreHash, redacted, claimedAt]);
  info(`提交排名 race=${raceId} agent=${agentId} scoreMilli=${scoreMilli}${redacted ? '（脱敏归零）' : ''}`);
  const { hash, receipt } = await sendTx(wallet, { to: comp.address, data, gas: await gasFor(wallet, { to: comp.address, data }) });
  done(`排名已上链（tx ${short(hash)}，区块 ${Number(fromHex(receipt.blockNumber))}）`);
  say(`  ${txUrl(hash)}`);
}

/* ---------------------------------------------------------------- predict */
async function cmdPredict(a) {
  const wallet = requireKey();
  const agentId = Number(a.agent);
  if (!Number.isFinite(agentId)) { bad('缺少 --agent'); return; }
  const materialClass = String(a.class ?? '').slice(0, 32);
  const property = String(a.property ?? '').slice(0, 64);
  if (!materialClass || !property) { bad('缺少 --class / --property'); return; }
  const redacted = Boolean(a['redacted']);
  const valueMilli = redacted ? 0 : asInt32(a.value !== undefined ? milli(a.value) : 0, '--value');
  const ciLowMilli = redacted ? 0 : asInt32(a['ci-low'] !== undefined ? milli(a['ci-low']) : 0, '--ci-low');
  const ciHighMilli = redacted ? 0 : asInt32(a['ci-high'] !== undefined ? milli(a['ci-high']) : 0, '--ci-high');
  if (!redacted && !(ciLowMilli <= valueMilli && valueMilli <= ciHighMilli)) {
    bad('区间必须包住预测值（ci_low ≤ value ≤ ci_high），否则合约 revert'); return;
  }
  const claimedAt = a['claimed-at'] !== undefined ? Number(a['claimed-at']) : Math.floor(Date.now() / 1000);
  const materialHash = a['material-hash'] ?? keccak(materialClass);
  const payloadHash = keccak(`MAE-PRED-1|agent=${agentId}|class=${materialClass}|property=${property}|value=${valueMilli}|ci=${ciLowMilli}..${ciHighMilli}|at=${claimedAt}`);
  const pred = findDeployment('PredictionRecord');
  if (!pred) { bad('PredictionRecord 未部署'); return; }

  const { iface } = loadAbi('PredictionRecord');
  const data = iface.encodeFunctionData('submitPrediction',
    [agentId, materialHash, materialClass, property, valueMilli, ciLowMilli, ciHighMilli, redacted, payloadHash, claimedAt]);
  info(`提交预测 agent=${agentId} ${materialClass}/${property} = ${(valueMilli / 1000).toFixed(3)} [${(ciLowMilli / 1000).toFixed(3)}, ${(ciHighMilli / 1000).toFixed(3)}]${redacted ? '（脱敏归零）' : ''}`);
  const { hash, receipt } = await sendTx(wallet, { to: pred.address, data, gas: await gasFor(wallet, { to: pred.address, data }) });
  const id = Number((await view('PredictionRecord', 'predictionCount'))[0]);
  done(`预测调用已上链 prediction #${id}（tx ${short(hash)}，区块 ${Number(fromHex(receipt.blockNumber))}）`);
  say(`  ${txUrl(hash)}`);
}

/* ---------------------------------------------------------------- status */
async function cmdStatus(a) {
  info(`网络 chain_id=${await chainId()} · 区块 ${await blockNumber()}`);
  const deps = loadDeployments();
  if (!deps.length) { say('deployments.json 为空：尚未部署任何合约'); }
  for (const d of deps) {
    say(`  · ${d.contract} @ ${d.address}`);
    say(`    部署 tx ${short(d.tx_hash)} · 区块 ${d.block} · ${addrUrl(d.address)}`);
    try {
      if (d.contract === 'MaterialAgentRegistry') {
        const n = Number((await view(d.contract, 'agentCount'))[0]);
        say(`    agentCount = ${n}`);
        for (let id = 1; id <= Math.min(n, 20); id++) {
          const g = await view(d.contract, 'getAgent', [id]);
          say(`      #${g[0]} ${g[1]} [${g[2]}] v=${g[4]} owner=${short(String(g[5]))} active=${g[7]}`);
        }
      } else if (d.contract === 'AgentCompetition') {
        const [races, fin] = await view(d.contract, 'stats');
        say(`    raceCount = ${races} · finalized = ${fin}`);
        for (let id = 1; id <= Math.min(Number(races), 10); id++) {
          const r = await view(d.contract, 'getRace', [id]);
          say(`      race #${r[0]} ${r[2]} / ${r[3]} participants=${r[8]} finalized=${r[5]} winner=${r[6]}`);
        }
      } else if (d.contract === 'PredictionRecord') {
        const [total, red, users] = await view(d.contract, 'stats');
        say(`    predictionCount = ${total} · redacted = ${red} · users = ${users}`);
      } else if (d.contract === 'FormulaSwarmAttestor') {
        const [ap, pr] = await view(d.contract, 'stats');
        say(`    approvals = ${ap} · predictions = ${pr}`);
      }
    } catch (e) { say(`    读取失败：${e.message}`); }
  }
  const wallet = a.wallet ?? (CFG.pk ? new Wallet(CFG.pk).address : process.env.BOT_WALLET);
  if (wallet) info(`钱包 ${wallet} 余额 ${(await balanceOf(wallet)).toFixed(6)} BOT`);
}

/* ---------------------------------------------------------------- main */
const USAGE = `用法：
  node mae.mjs deploy  [--dry-run] [--cooldown 86400]
  node mae.mjs register [--name N --domain D --model-hash 0x.. --version V] [--from file.json]
  node mae.mjs race    [--task T] [--domain D] [--date YYYY-MM-DD] [--race-hash 0x..]
  node mae.mjs rank    --race 1 --agent 1 --milli 412 [--score-hash 0x..] [--redacted] [--claimed-at 0]
  node mae.mjs predict --agent 1 --class epoxy_adhesive --property lap_shear_strength \\
                        --value 32.5 --ci-low 30.1 --ci-high 34.9 [--redacted]
  node mae.mjs status  [--wallet 0x..]`;

const cmd = process.argv[2];
const args = parseArgs(process.argv.slice(3));
const CMDS = { deploy: cmdDeploy, register: cmdRegister, race: cmdRace, rank: cmdRank, predict: cmdPredict, status: cmdStatus };

if (!cmd || args.help || args.h) { say(USAGE); process.exit(cmd ? 0 : 1); }
if (!CMDS[cmd]) { bad(`未知命令 ${cmd}`); say(USAGE); process.exit(1); }

try {
  await CMDS[cmd](args);
} catch (e) {
  bad(`${cmd} 失败：${e.message || e.code || e}`);
  process.exit(1);
}
