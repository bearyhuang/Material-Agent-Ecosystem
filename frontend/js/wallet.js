/* MAE 链上层 · wallet.js（无 DOM 依赖，先于 app.js 加载）
 *
 * 需求标准：docs/Agent竞马结果上链.md §5/§6
 * 职责：
 *   · EIP-1193 钱包连接 / Chain 677 校验与切换；
 *   · 合约只读客户端（BrowserProvider 优先，直连 RPC 兜底）；
 *   · 三合约的写操作（用户钱包签名）：submitRanking / submitPrediction / openRace / finalizeRace；
 *   · 链上配置来自 data/chain.json（blockchain/mae.mjs deploy 自动刷新）。
 *
 * 红线：前端不产生任何指标；数值由操作者按链下确定性内核结果填写后签名上链。
 */
window.MAE = (() => {
  'use strict';
  const CHAIN_ID = 677;
  const CHAIN_ID_HEX = '0x' + CHAIN_ID.toString(16);   // 0x2a5

  /* Human-readable ABI（与 blockchain/contracts/*.sol 逐字段对齐） */
  const ABI = {
    MaterialAgentRegistry: [
      'function agentCount() view returns (uint256)',
      'function getAgent(uint256 id) view returns (tuple(uint256 id,string name,string domain,bytes32 modelHash,string version,address owner,uint64 at,bool active))',
      'function getAgents(uint256[] ids) view returns (tuple(uint256 id,string name,string domain,bytes32 modelHash,string version,address owner,uint64 at,bool active)[])',
      'function canRank(uint256 id, address who) view returns (bool)',
      'function registerAgent(string name,string domain,bytes32 modelHash,string version) returns (uint256)',
    ],
    AgentCompetition: [
      'function raceCount() view returns (uint256)',
      'function getRace(uint256 id) view returns (tuple(uint256 id,bytes32 raceHash,string materialTask,string domain,address creator,uint64 at,bool finalized,uint256 winnerAgentId,uint32 participants))',
      'function raceAgents(uint256 raceId) view returns (uint256[])',
      'function getRanking(uint256 raceId,uint256 agentId) view returns (tuple(uint256 agentId,int32 scoreMilli,bytes32 scoreHash,bool redacted,address endorser,uint64 claimedAt,uint64 at,uint32 revision))',
      'function stats() view returns (uint256 races,uint256 finalized,uint32 participantsMax)',
      'function openRace(bytes32 raceHash,string materialTask,string domain) returns (uint256)',
      'function submitRanking(uint256 raceId,uint256 agentId,int32 scoreMilli,bytes32 scoreHash,bool redacted,uint64 claimedAt)',
      'function finalizeRace(uint256 raceId,uint256 winnerAgentId)',
    ],
    PredictionRecord: [
      'function predictionCount() view returns (uint256)',
      'function getPrediction(uint256 id) view returns (tuple(uint256 id,address user,uint256 agentId,bytes32 materialHash,string materialClass,string property,int32 valueMilli,int32 ciLowMilli,int32 ciHighMilli,bool redacted,bytes32 payloadHash,uint64 claimedAt,uint64 at))',
      'function latest(uint256 n) view returns (tuple(uint256 id,address user,uint256 agentId,bytes32 materialHash,string materialClass,string property,int32 valueMilli,int32 ciLowMilli,int32 ciHighMilli,bool redacted,bytes32 payloadHash,uint64 claimedAt,uint64 at)[])',
      'function stats() view returns (uint256 total,uint32 redacted,uint32 users)',
      'function lastPredictAt(address) view returns (uint64)',
      'function submitPrediction(uint256 agentId,bytes32 materialHash,string materialClass,string property,int32 valueMilli,int32 ciLowMilli,int32 ciHighMilli,bool redacted,bytes32 payloadHash,uint64 claimedAt) returns (uint256)',
    ],
  };

  /* ---------------- 链上配置 ---------------- */
  const FALLBACK_CFG = {
    network: 'BOT Chain 主网', chain_id: CHAIN_ID,
    rpc: 'https://rpc.botchain.ai', explorer: 'https://scan.botchain.ai',
    contracts: { MaterialAgentRegistry: null, AgentCompetition: null, PredictionRecord: null, FormulaSwarmAttestor: null },
    attestor_anchors: [],
  };
  let cfg = { ...FALLBACK_CFG };
  let cfgLoaded = false;

  async function loadCfg() {
    try {
      const r = await fetch('data/chain.json', { cache: 'no-store' });
      if (r.ok) {
        const j = await r.json();
        cfg = { ...FALLBACK_CFG, ...j, contracts: { ...FALLBACK_CFG.contracts, ...(j.contracts || {}) } };
      }
    } catch (_) { /* file:// 打开或数据缺失 → 用兜底配置 */ }
    cfgLoaded = true;
    return cfg;
  }

  /* ---------------- 钱包状态 ---------------- */
  const state = {
    status: 'disconnected',        // disconnected | no-wallet | connecting | connected | wrong-network
    account: null, chainId: null, balance: null,
    provider: null, signer: null, err: null,
  };
  const listeners = new Set();
  const onChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
  const emit = () => listeners.forEach((fn) => { try { fn(snapshot()); } catch (_) {} });
  const snapshot = () => ({
    status: state.status, account: state.account, chainId: state.chainId,
    balance: state.balance, err: state.err, hasWallet: Boolean(window.ethereum),
    ready: state.status === 'connected',
  });

  function setStatus(status, err = null) { state.status = status; state.err = err; emit(); }

  async function refreshBalance() {
    if (!state.provider || !state.account) { state.balance = null; return; }
    try {
      const bal = await state.provider.getBalance(state.account);
      state.balance = ethers.formatEther(bal);
    } catch (_) { state.balance = null; }
    emit();
  }

  async function connect() {
    if (!window.ethereum) { setStatus('no-wallet', '未检测到以太坊钱包（MetaMask / OKX Wallet 等）'); return snapshot(); }
    setStatus('connecting');
    try {
      state.provider = new ethers.BrowserProvider(window.ethereum);
      const accs = await window.ethereum.request({ method: 'eth_requestAccounts' });
      state.account = accs && accs[0] ? accs[0] : null;
      const net = await state.provider.getNetwork();
      state.chainId = Number(net.chainId);
      if (state.chainId !== CHAIN_ID) {
        setStatus('wrong-network', `钱包当前网络 chainId=${state.chainId}，请切换到 BOT Chain ${CHAIN_ID}`);
      } else {
        state.signer = await state.provider.getSigner();
        setStatus('connected');
      }
      refreshBalance();
      return snapshot();
    } catch (e) {
      setStatus('disconnected', friendly(e));
      return snapshot();
    }
  }

  async function switchToBot() {
    if (!window.ethereum) return;
    try {
      await window.ethereum.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: CHAIN_ID_HEX }] });
    } catch (e) {
      if (e && (e.code === 4902 || /unrecognized|not added/i.test(e.message || ''))) {
        try {
          await window.ethereum.request({
            method: 'wallet_addEthereumChain',
            params: [{
              chainId: CHAIN_ID_HEX, chainName: 'BOT Chain',
              nativeCurrency: { name: 'BOT', symbol: 'BOT', decimals: 18 },
              rpcUrls: [cfg.rpc || 'https://rpc.botchain.ai'],
              blockExplorerUrls: [cfg.explorer || 'https://scan.botchain.ai'],
            }],
          });
        } catch (e2) { state.err = friendly(e2); emit(); return; }
      } else { state.err = friendly(e); emit(); return; }
    }
    await connect();   // 切换后重新连接取 signer
  }

  function disconnect() {
    state.provider = null; state.signer = null; state.account = null;
    state.chainId = null; state.balance = null;
    setStatus('disconnected');
  }

  if (window.ethereum) {
    window.ethereum.on?.('accountsChanged', (accs) => { if (!accs || !accs.length) disconnect(); else connect(); });
    window.ethereum.on?.('chainChanged', () => connect());
  }

  /* ---------------- 合约客户端 ---------------- */
  const addressOf = (name) => (cfg.contracts && cfg.contracts[name] ? cfg.contracts[name].address : null);

  function readContract(name) {
    const addr = addressOf(name);
    if (!addr) throw new Error(`${name} 尚未部署（先执行 node blockchain/mae.mjs deploy）`);
    const provider = state.provider || new ethers.JsonRpcProvider(cfg.rpc, { chainId: CHAIN_ID, name: 'botchain' });
    return new ethers.Contract(addr, ABI[name], provider);
  }

  async function writeContract(name) {
    if (state.status !== 'connected' || !state.signer) throw new Error('请先连接钱包并切换到 BOT Chain 677');
    const addr = addressOf(name);
    if (!addr) throw new Error(`${name} 尚未部署（先执行 node blockchain/mae.mjs deploy）`);
    return new ethers.Contract(addr, ABI[name], state.signer);
  }

  const friendly = (e) => (e && (e.shortMessage || e.reason)) || (e && e.message) || String(e);
  const keccak = (s) => ethers.keccak256(ethers.toUtf8Bytes(String(s)));
  const todayISO = () => new Date().toISOString().slice(0, 10);

  /* ---------------- 只读 ---------------- */
  async function agents() {
    const c = readContract('MaterialAgentRegistry');
    const n = Number(await c.agentCount());
    if (!n) return [];
    const ids = Array.from({ length: n }, (_, i) => i + 1);
    const rows = await c.getAgents(ids);
    return rows.map((a) => ({
      id: Number(a.id), name: a.name, domain: a.domain, modelHash: a.modelHash,
      version: a.version, owner: a.owner, at: Number(a.at), active: a.active,
    }));
  }
  async function races(limit = 50) {
    const c = readContract('AgentCompetition');
    const n = Number(await c.raceCount());
    const out = [];
    for (let id = n; id >= Math.max(1, n - limit + 1); id--) {
      const r = await c.getRace(id);
      out.push({
        id: Number(r.id), raceHash: r.raceHash, materialTask: r.materialTask, domain: r.domain,
        creator: r.creator, at: Number(r.at), finalized: r.finalized,
        winnerAgentId: Number(r.winnerAgentId), participants: Number(r.participants),
      });
    }
    return out;
  }
  async function rankings(raceId) {
    const c = readContract('AgentCompetition');
    const ids = (await c.raceAgents(raceId)).map(Number);
    const out = [];
    for (const aid of ids) {
      const k = await c.getRanking(raceId, aid);
      out.push({
        agentId: Number(k.agentId), scoreMilli: Number(k.scoreMilli), scoreHash: k.scoreHash,
        redacted: k.redacted, endorser: k.endorser, claimedAt: Number(k.claimedAt),
        at: Number(k.at), revision: Number(k.revision),
      });
    }
    out.sort((a, b) => (a.redacted ? 1 : 0) - (b.redacted ? 1 : 0) || a.scoreMilli - b.scoreMilli);
    return out;
  }
  async function predictions(n = 8) {
    const c = readContract('PredictionRecord');
    const rows = await c.latest(n);
    return rows.map((p) => ({
      id: Number(p.id), user: p.user, agentId: Number(p.agentId), materialHash: p.materialHash,
      materialClass: p.materialClass, property: p.property,
      valueMilli: Number(p.valueMilli), ciLowMilli: Number(p.ciLowMilli), ciHighMilli: Number(p.ciHighMilli),
      redacted: p.redacted, payloadHash: p.payloadHash, claimedAt: Number(p.claimedAt), at: Number(p.at),
    }));
  }
  async function stats() {
    const out = { agents: null, races: null, predictions: null };
    try { out.agents = Number(await readContract('MaterialAgentRegistry').agentCount()); } catch (_) {}
    try { const s = await readContract('AgentCompetition').stats(); out.races = { races: Number(s[0]), finalized: Number(s[1]) }; } catch (_) {}
    try { const s = await readContract('PredictionRecord').stats(); out.predictions = { total: Number(s[0]), redacted: Number(s[1]), users: Number(s[2]) }; } catch (_) {}
    return out;
  }
  async function lastPredictAt() {
    if (!state.account) return 0;
    try { return Number(await readContract('PredictionRecord').lastPredictAt(state.account)); }
    catch (_) { return 0; }
  }

  /* ---------------- 写（用户钱包签名） ---------------- */
  async function sendAndWait(name, fn, args) {
    const c = await writeContract(name);
    const tx = await c[fn](...args);
    info(`交易已广播，等待确认… ${tx.hash}`);
    const rc = await tx.wait();
    return { hash: tx.hash, blockNumber: Number(rc.blockNumber), gasUsed: Number(rc.gasUsed), explorer: `${cfg.explorer}/tx/${tx.hash}` };
  }

  async function submitRanking({ raceId, agentId, scoreMilli, scoreHash, redacted = false, claimedAt }) {
    if (!Number.isInteger(+raceId) || !Number.isInteger(+agentId)) throw new Error('raceId / agentId 必须是整数');
    const m = redacted ? 0 : Math.round(Number(scoreMilli));
    const at = claimedAt || Math.floor(Date.now() / 1000);
    const hash = scoreHash || keccak(`MAE-SCORE-1|race=${raceId}|agent=${agentId}|milli=${m}|at=${at}`);
    return sendAndWait('AgentCompetition', 'submitRanking', [raceId, agentId, m, hash, Boolean(redacted), at]);
  }
  async function submitPrediction({ agentId, materialClass, property, value, ciLow, ciHigh, redacted = false, claimedAt }) {
    const cls = String(materialClass || '').slice(0, 32);
    const prop = String(property || '').slice(0, 64);
    if (!cls || !prop) throw new Error('材料类别与指标名不能为空');
    const v = redacted ? 0 : Math.round(Number(value) * 1000);
    const lo = redacted ? 0 : Math.round(Number(ciLow) * 1000);
    const hi = redacted ? 0 : Math.round(Number(ciHigh) * 1000);
    if (!redacted && !(lo <= v && v <= hi)) throw new Error('区间必须包住预测值（ci_low ≤ value ≤ ci_high）');
    const at = claimedAt || Math.floor(Date.now() / 1000);
    const materialHash = keccak(cls);
    const payloadHash = keccak(`MAE-PRED-1|agent=${agentId}|class=${cls}|property=${prop}|value=${v}|ci=${lo}..${hi}|at=${at}`);
    return sendAndWait('PredictionRecord', 'submitPrediction', [agentId, materialHash, cls, prop, v, lo, hi, Boolean(redacted), payloadHash, at]);
  }
  async function openRace({ task = 'lap_shear_strength', domain = 'adhesive', date } = {}) {
    const d = date || todayISO();
    const raceHash = keccak(`MAE-RACE-1|${d}|${task}|${domain}`);
    // 幂等：已存在同日同题同域的竞马则复用
    const all = await races(200);
    const hit = all.find((r) => String(r.raceHash).toLowerCase() === raceHash.toLowerCase());
    if (hit) return { reused: true, raceId: hit.id, raceHash };
    const r = await sendAndWait('AgentCompetition', 'openRace', [raceHash, task, domain]);
    return { reused: false, ...r, raceHash };
  }
  async function finalizeRace(raceId, winnerAgentId) {
    return sendAndWait('AgentCompetition', 'finalizeRace', [raceId, winnerAgentId]);
  }
  async function registerAgent({ name, domain, version, modelHash }) {
    const h = modelHash || keccak(`MAE-AGENT-1|${name}|${domain}|${version || ''}`);
    return sendAndWait('MaterialAgentRegistry', 'registerAgent', [String(name).slice(0, 64), String(domain).slice(0, 32), h, String(version || 'v1').slice(0, 32)]);
  }

  /* ---------------- 导出 ---------------- */
  const info = (s) => console.log('[MAE]', s);
  return {
    CHAIN_ID, cfg: () => cfg, loadCfg,
    state: snapshot, onChange,
    connect, disconnect, switchToBot,
    read: { agents, races, rankings, predictions, stats, lastPredictAt },
    write: { submitRanking, submitPrediction, openRace, finalizeRace, registerAgent },
    keccak, friendly,
    explorerTx: (h) => `${cfg.explorer}/tx/${h}`,
    explorerAddr: (a) => `${cfg.explorer}/address/${a}`,
    contracts: () => cfg.contracts || {},
    anchors: () => cfg.attestor_anchors || [],
  };
})();
