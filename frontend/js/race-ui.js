/* MAE DApp 面板 · race-ui.js（依赖 app.js 的 $/esc/get 与 wallet.js 的 MAE）
 *
 * 需求标准：docs/Agent竞马结果上链.md §6
 * 渲染「⛓ 链上」modal：钱包条 / Agent 竞马上链 / 预测调用上链 / 链上证明。
 * 原则：数值只来自链上与后端 API，前端不产生指标；写操作一律用户钱包签名。
 */
window.RaceUI = (() => {
  'use strict';
  let booted = false;
  let cache = { agents: [], races: [], predictions: [], stats: null, lastPredictAt: 0 };
  let busy = false;

  const $id = (id) => document.getElementById(id);
  const short = (h) => (typeof h === 'string' && h.length > 16 ? `${h.slice(0, 10)}…${h.slice(-8)}` : h);
  const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '—');
  const milliStr = (m) => (m / 1000).toFixed(3);

  /* ---------------- 外壳 ---------------- */
  function shell() {
    return `
    <div class="chain-net">
      <span class="cb">网络 <b>${esc(MAE.cfg().network || 'BOT Chain 主网')}</b></span>
      <span class="cb">chain_id <b>${MAE.CHAIN_ID}</b></span>
      <span class="cb">RPC <b class="mono">${esc(MAE.cfg().rpc || '')}</b></span>
      <span class="cb">浏览器 <b class="mono"><a class="chain-link" href="${esc(MAE.cfg().explorer || '')}" target="_blank" rel="noopener">${esc(MAE.cfg().explorer || '')}</a></b></span>
    </div>
    <div id="dx-wallet" class="dx-wallet"></div>

    <div class="chain-sec">🏁 Agent 竞马上链</div>
    <div class="dx-form">
      <label>竞马
        <span class="dx-inline"><select id="dx-race"></select>
        <button id="dx-race-new" class="chip" type="button">＋ 开今日竞马</button></span>
      </label>
      <label>Agent <select id="dx-agent"></select></label>
      <label>成绩 scoreMilli（越小越好）
        <span class="dx-inline"><input id="dx-milli" type="number" step="1" placeholder="如 412">
        <button id="dx-prefill" class="chip" type="button">用冠军指标预填</button></span>
      </label>
      <label class="chk"><input type="checkbox" id="dx-rank-redacted"> 脱敏提交（链上数值强制归零）</label>
      <div class="dx-actions">
        <button id="dx-rank-submit" class="primary" type="button">提交排名</button>
        <button id="dx-finalize" class="chip" type="button">定冠军</button>
      </div>
    </div>
    <div id="dx-rank-status" class="dx-status" role="status" aria-live="polite"></div>
    <div id="dx-rankings" class="dx-rankings"></div>

    <div class="chain-sec">🔮 预测调用上链</div>
    <div class="dx-form">
      <label>Agent <select id="dx-p-agent"></select></label>
      <label>材料类别 <input id="dx-p-class" value="epoxy_adhesive" maxlength="32" placeholder="如 epoxy_adhesive"></label>
      <label>指标 <input id="dx-p-prop" value="lap_shear_strength" maxlength="64" placeholder="如 lap_shear_strength"></label>
      <label>预测值 <input id="dx-p-value" type="number" step="0.001" placeholder="如 32.5"></label>
      <label>区间下限 <input id="dx-p-cilo" type="number" step="0.001" placeholder="如 30.1"></label>
      <label>区间上限 <input id="dx-p-cihi" type="number" step="0.001" placeholder="如 34.9"></label>
      <label class="chk"><input type="checkbox" id="dx-p-redacted"> 脱敏提交（链上数值强制归零）</label>
      <div class="dx-actions">
        <button id="dx-p-submit" class="primary" type="button">提交预测</button>
        <span class="hint" id="dx-cooldown"></span>
      </div>
    </div>
    <div id="dx-p-status" class="dx-status" role="status" aria-live="polite"></div>

    <div class="chain-sec">⛓ 链上证明（合约地址 · 交易记录 · 浏览器）</div>
    <div class="chain-grid" id="dx-contracts"></div>
    <div class="chain-sec">最近预测调用（链上 latest）</div>
    <div class="boardwrap"><table class="chain-table"><thead><tr>
      <th>#</th><th>Agent</th><th>材料类别 / 指标</th><th>预测值 [区间]</th><th>调用钱包</th><th>链上时间</th>
    </tr></thead><tbody id="dx-preds"></tbody></table></div>
    <div class="chain-sec">V1 留痕锚定（FormulaSwarmAttestor · 前序工作佐证）</div>
    <div class="boardwrap"><table class="chain-table"><thead><tr>
      <th>锚点</th><th>对象</th><th>交易（区块 / gas）</th><th>说明</th>
    </tr></thead><tbody id="dx-anchors"></tbody></table></div>
    <div class="chain-note">
      <b>诚实声明：</b>链上不可篡改 ≠ 模型更准；公证的是「结论如何被产生、谁调用过」，不是「结论更准」。
      脱敏对象的链上数值一律归零，⛓ 徽标不代表数值被公证。数值唯一来源为链下确定性评测内核，LLM 不产出任何指标。
      三合约未部署时，写操作不可用 —— 配置 <b class="mono">blockchain/.env</b> 私钥后执行
      <b class="mono">node mae.mjs deploy</b>，再刷新本页。
    </div>`;
  }

  function bind() {
    $id('dx-rank-submit').onclick = onRankSubmit;
    $id('dx-p-submit').onclick = onPredictSubmit;
    $id('dx-race-new').onclick = onOpenRace;
    $id('dx-finalize').onclick = onFinalize;
    $id('dx-prefill').onclick = onPrefill;
    $id('dx-race').onchange = onRaceChange;
    $id('dx-rank-redacted').onchange = syncRedacted;
    $id('dx-p-redacted').onchange = syncRedacted;
  }

  /* ---------------- 钱包行 ---------------- */
  function renderWallet() {
    const box = $id('dx-wallet'); if (!box) return;
    const s = MAE.state();
    const chips = { connected: ['ok', '已连接'], 'wrong-network': ['warn', '网络不符'], connecting: ['warn', '连接中…'], 'no-wallet': ['bad', '无钱包'], disconnected: ['', '未连接'] };
    const [cls, label] = chips[s.status] || chips.disconnected;
    let html = `<span class="dx-badge ${cls}">${label}</span>`;
    if (s.status === 'connected') {
      html += `<span class="mono">${esc(s.account)}</span>
        <span class="hint">余额 ${s.balance == null ? '—' : esc(s.balance)} BOT</span>
        <button class="chip" type="button" id="dx-disconnect">断开</button>`;
    } else if (s.status === 'wrong-network') {
      html += `<span class="mono">${esc(s.account || '')}</span>
        <span class="hint">${esc(s.err || '')}</span>
        <button class="primary" type="button" id="dx-switch">切换到 BOT Chain ${MAE.CHAIN_ID}</button>`;
    } else if (s.status === 'no-wallet') {
      html += `<span class="hint">${esc(s.err || '')}</span>
        <button class="primary" type="button" id="dx-connect">连接钱包</button>`;
    } else if (s.status === 'connecting') {
      html += `<span class="hint">请在钱包中批准连接…</span>`;
    } else {
      html += `<button class="primary" type="button" id="dx-connect">🦊 连接钱包</button>
        <span class="hint">支持 MetaMask / OKX / 任意 EIP-1193 钱包；写操作需用户签名</span>`;
    }
    box.innerHTML = html;
    $id('dx-connect') && ($id('dx-connect').onclick = () => MAE.connect());
    $id('dx-disconnect') && ($id('dx-disconnect').onclick = () => MAE.disconnect());
    $id('dx-switch') && ($id('dx-switch').onclick = () => MAE.switchToBot());
    syncGating();
  }

  function syncGating() {
    const s = MAE.state();
    const c = MAE.contracts();
    const deployed = Boolean(c.MaterialAgentRegistry && c.AgentCompetition && c.PredictionRecord);
    const ok = s.status === 'connected' && deployed && !busy;
    ['dx-rank-submit', 'dx-p-submit', 'dx-race-new', 'dx-finalize'].forEach((id) => {
      const b = $id(id); if (b) b.disabled = !ok;
    });
    // 不覆盖已有状态信息（如更具体的"链上读取失败"）；仅在空时给引导提示
    const hint = (id, msg) => { const box = $id(id); if (box && !box.innerHTML.trim()) setStatus(id, 'warn', msg); };
    if (!deployed) {
      hint('dx-rank-status', '三合约尚未部署：写操作暂不可用（见底部说明）。只读数据如下。');
      hint('dx-p-status', '三合约尚未部署：写操作暂不可用（见底部说明）。');
    } else if (s.status !== 'connected' && s.status !== 'connecting') {
      hint('dx-rank-status', '请先连接钱包（BOT Chain 677）后提交。');
      hint('dx-p-status', '请先连接钱包（BOT Chain 677）后提交。');
    }
  }

  function setStatus(id, kind, msg) {
    const box = $id(id); if (!box) return;
    box.innerHTML = `<div class="dx-${kind}">${esc(msg)}</div>`;
  }
  function receipt(id, kind, title, r) {
    const box = $id(id); if (!box) return;
    box.innerHTML = `<div class="dx-receipt ${kind}">
      <b>${esc(title)}</b><br>
      交易 <a class="chain-link" href="${r.explorer}" target="_blank" rel="noopener">${esc(short(r.hash))}</a>
      · 区块 ${r.blockNumber.toLocaleString()} · gas ${r.gasUsed.toLocaleString()}</div>`;
  }

  /* ---------------- 数据加载 ---------------- */
  async function refreshAll() {
    renderContracts();
    renderAnchors();
    fillSelects(); renderRaceOptions(); renderPreds();   // 先按空/缓存渲染，保证占位符出现
    try {
      const [agents, races, preds, st] = await Promise.all([
        MAE.read.agents(), MAE.read.races(50), MAE.read.predictions(8), MAE.read.stats(),
      ]);
      cache = { ...cache, agents, races, predictions: preds, stats: st };
      fillSelects(); renderRaceOptions(); renderRankings(); renderPreds();
      if (MAE.state().account) { cache.lastPredictAt = await MAE.read.lastPredictAt(); renderCooldown(); }
      setStatus('dx-rank-status', 'ok', `链上数据已加载：${agents.length} 个 Agent · ${races.length} 场竞马 · ${st.predictions ? st.predictions.total : 0} 条预测`);
      setStatus('dx-p-status', 'ok', '');
    } catch (e) {
      setStatus('dx-rank-status', 'bad', `链上读取失败：${MAE.friendly(e)}。若 RPC 不可达（DNS 投毒），请连接钱包后用钱包网络重试。`);
    }
    syncGating();
  }

  function renderContracts() {
    const c = MAE.contracts(), ex = MAE.cfg().explorer || '';
    const rows = [
      ['MaterialAgentRegistry', 'Agent 链上身份证', c.MaterialAgentRegistry, (st) => `${st.agents ?? '—'} 个 Agent`],
      ['AgentCompetition', '竞马 / 排名 / 定冠军', c.AgentCompetition, (st) => st.races ? `${st.races.races} 场（已定冠军 ${st.races.finalized}）` : '—'],
      ['PredictionRecord', 'C 端预测调用记录', c.PredictionRecord, (st) => st.predictions ? `${st.predictions.total} 条 · ${st.predictions.users} 个钱包` : '—'],
      ['FormulaSwarmAttestor', 'V1 留痕（前序）', c.FormulaSwarmAttestor, () => '已部署'],
    ];
    $id('dx-contracts').innerHTML = rows.map(([name, desc, d, stat]) => {
      const st = cache.stats || {};
      const body = d
        ? `<div class="v mono"><a class="chain-link" href="${ex}/address/${d.address}" target="_blank" rel="noopener">${esc(d.address)}</a></div>
           <div class="d">${esc(desc)} · ${esc(stat(st))}</div>
           <div class="d">部署 tx <a class="chain-link" href="${ex}/tx/${d.deploy_tx}" target="_blank" rel="noopener">${esc(short(d.deploy_tx))}</a> · 区块 ${(d.block || 0).toLocaleString()}</div>`
        : `<div class="v" style="color:var(--warn)">待部署</div>
           <div class="d">${esc(desc)}</div>
           <div class="d hint">node mae.mjs deploy 后自动出现</div>`;
      return `<div class="card"><div class="t">${esc(name)}</div>${body}</div>`;
    }).join('');
  }

  function renderAnchors() {
    const ex = MAE.cfg().explorer || '';
    const rows = MAE.anchors();
    $id('dx-anchors').innerHTML = rows.length ? rows.map((a) => `<tr>
      <td><b>${esc(a.k)}</b></td><td>${esc(a.obj)}</td>
      <td><a class="chain-link" href="${ex}/tx/${a.tx}" target="_blank" rel="noopener">${esc(short(a.tx))}</a><br>
        <span class="hint">区块 ${a.block.toLocaleString()} · gas ${a.gas.toLocaleString()}</span></td>
      <td class="hint">${esc(a.extra || '')}</td></tr>`).join('')
      : `<tr><td colspan="4" class="hint">暂无锚定记录</td></tr>`;
  }

  function agentName(id) {
    const a = cache.agents.find((x) => x.id === Number(id));
    return a ? `${a.name}${a.active ? '' : '（已退役）'}` : `#${id}`;
  }
  function fillSelects() {
    const opts = cache.agents.map((a) => `<option value="${a.id}">#${a.id} ${esc(a.name)} [${esc(a.domain)}]</option>`).join('');
    ['dx-agent', 'dx-p-agent'].forEach((id) => {
      const sel = $id(id); if (!sel) return;
      const keep = sel.value;
      sel.innerHTML = opts || `<option value="">（链上暂无 Agent）</option>`;
      if (keep) sel.value = keep;
    });
  }
  function renderRaceOptions() {
    const sel = $id('dx-race'); if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = cache.races.map((r) =>
      `<option value="${r.id}">#${r.id} ${esc(r.materialTask)} / ${esc(r.domain)} · ${r.participants} 方${r.finalized ? ' · 已定冠军 #' + r.winnerAgentId : ''}</option>`).join('')
      || `<option value="">（链上暂无竞马，点「开今日竞马」）</option>`;
    if (keep && cache.races.some((r) => String(r.id) === keep)) sel.value = keep;
  }
  async function renderRankings() {
    const raceId = Number($id('dx-race')?.value);
    const box = $id('dx-rankings'); if (!box) return;
    if (!raceId) { box.innerHTML = ''; return; }
    try {
      const rows = await MAE.read.rankings(raceId);
      box.innerHTML = rows.length
        ? `<div class="boardwrap"><table class="chain-table"><thead><tr><th>名次</th><th>Agent</th><th>scoreMilli</th><th>提交钱包</th><th>修订</th><th>时间</th></tr></thead><tbody>
           ${rows.map((k, i) => `<tr><td>${i + 1}</td><td>${esc(agentName(k.agentId))}</td>
             <td class="${k.redacted ? 'hint' : 'pass'}">${k.redacted ? '脱敏(0)' : k.scoreMilli}</td>
             <td class="mono">${esc(short(k.endorser))}</td><td>r${k.revision}</td><td>${fmtTime(k.at)}</td></tr>`).join('')}
           </tbody></table></div>`
        : `<div class="hint">本场竞马还没有排名提交。</div>`;
    } catch (e) { box.innerHTML = `<div class="dx-bad">榜单读取失败：${esc(MAE.friendly(e))}</div>`; }
  }
  function renderPreds() {
    $id('dx-preds').innerHTML = cache.predictions.length ? cache.predictions.map((p) => `<tr>
      <td>#${p.id}</td><td>${esc(agentName(p.agentId))}</td>
      <td>${esc(p.materialClass)} / ${esc(p.property)}</td>
      <td class="${p.redacted ? 'hint' : 'pass'}">${p.redacted ? '脱敏(0,0,0)' : `${milliStr(p.valueMilli)} [${milliStr(p.ciLowMilli)}, ${milliStr(p.ciHighMilli)}]`}</td>
      <td class="mono">${esc(short(p.user))}</td><td>${fmtTime(p.at)}</td></tr>`).join('')
      : `<tr><td colspan="6" class="hint">链上暂无预测调用记录</td></tr>`;
  }
  async function renderCooldown() {
    const box = $id('dx-cooldown'); if (!box) return;
    const last = cache.lastPredictAt || 0;
    if (!last) { box.textContent = '同一钱包 1 天/次（cooldown 86400s）'; return; }
    const left = last + 86400 - Math.floor(Date.now() / 1000);
    box.textContent = left > 0 ? `冷却中，${Math.ceil(left / 3600)} 小时后可再次提交` : '可提交';
    box.style.color = left > 0 ? 'var(--warn)' : '';
  }

  /* ---------------- 操作 ---------------- */
  async function guard() {
    if (busy) return false;
    const s = MAE.state();
    if (s.status !== 'connected') { setStatus('dx-rank-status', 'warn', '请先连接钱包并切换到 BOT Chain 677'); return false; }
    return true;
  }
  async function onRankSubmit() {
    if (!(await guard())) return;
    const raceId = Number($id('dx-race').value), agentId = Number($id('dx-agent').value);
    const milli = Number($id('dx-milli').value), redacted = $id('dx-rank-redacted').checked;
    if (!raceId || !agentId) { setStatus('dx-rank-status', 'bad', '请选择竞马与 Agent'); return; }
    if (!redacted && !Number.isFinite(milli)) { setStatus('dx-rank-status', 'bad', '请填写 scoreMilli（越小越好）'); return; }
    busy = true; syncGating();
    setStatus('dx-rank-status', 'warn', '请在钱包中确认交易…');
    try {
      const r = await MAE.write.submitRanking({ raceId, agentId, scoreMilli: redacted ? 0 : milli, redacted });
      receipt('dx-rank-status', 'ok', `排名已上链：race #${raceId} · agent #${agentId} · scoreMilli ${redacted ? 0 : milli}`, r);
      await refreshAll();
    } catch (e) { setStatus('dx-rank-status', 'bad', `提交失败：${MAE.friendly(e)}`); }
    busy = false; syncGating();
  }
  async function onPredictSubmit() {
    if (!(await guard())) return;
    const agentId = Number($id('dx-p-agent').value);
    const cls = $id('dx-p-class').value.trim(), prop = $id('dx-p-prop').value.trim();
    const v = Number($id('dx-p-value').value), lo = Number($id('dx-p-cilo').value), hi = Number($id('dx-p-cihi').value);
    const redacted = $id('dx-p-redacted').checked;
    if (!agentId || !cls || !prop) { setStatus('dx-p-status', 'bad', '请填写 Agent、材料类别与指标'); return; }
    if (!redacted && !(Number.isFinite(v) && Number.isFinite(lo) && Number.isFinite(hi))) { setStatus('dx-p-status', 'bad', '请填写预测值与区间'); return; }
    busy = true; syncGating();
    setStatus('dx-p-status', 'warn', '请在钱包中确认交易…');
    try {
      const r = await MAE.write.submitPrediction({ agentId, materialClass: cls, property: prop, value: v, ciLow: lo, ciHigh: hi, redacted });
      receipt('dx-p-status', 'ok', `预测调用已上链：${cls} / ${prop} = ${redacted ? '脱敏' : v}`, r);
      await refreshAll();
    } catch (e) { setStatus('dx-p-status', 'bad', `提交失败：${MAE.friendly(e)}`); }
    busy = false; syncGating();
  }
  async function onOpenRace() {
    if (!(await guard())) return;
    const sel = $id('dx-race');
    const cur = cache.races.find((r) => String(r.id) === sel.value);
    const task = cur ? cur.materialTask : 'lap_shear_strength';
    const domain = cur ? cur.domain : 'adhesive';
    busy = true; syncGating();
    setStatus('dx-rank-status', 'warn', `正在开设今日竞马（${task} / ${domain}）…`);
    try {
      const r = await MAE.write.openRace({ task, domain });
      if (r.reused) setStatus('dx-rank-status', 'ok', `今日竞马已存在，复用 race #${r.raceId}`);
      else receipt('dx-rank-status', 'ok', `已开赛 race #${r.raceId || ''}（${task} / ${domain}）`, r);
      await refreshAll();
      if (r.raceId) { $id('dx-race').value = String(r.raceId); renderRankings(); }
    } catch (e) { setStatus('dx-rank-status', 'bad', `开赛失败：${MAE.friendly(e)}`); }
    busy = false; syncGating();
  }
  async function onFinalize() {
    if (!(await guard())) return;
    const raceId = Number($id('dx-race').value), agentId = Number($id('dx-agent').value);
    if (!raceId || !agentId) { setStatus('dx-rank-status', 'bad', '请选择竞马与 Agent（冠军）'); return; }
    busy = true; syncGating();
    setStatus('dx-rank-status', 'warn', '请在钱包中确认定冠军交易…');
    try {
      const r = await MAE.write.finalizeRace(raceId, agentId);
      receipt('dx-rank-status', 'ok', `已定冠军：race #${raceId} → agent #${agentId}`, r);
      await refreshAll();
    } catch (e) { setStatus('dx-rank-status', 'bad', `定冠军失败：${MAE.friendly(e)}（仅开赛人或 scribe 可定冠军）`); }
    busy = false; syncGating();
  }
  async function onPrefill() {
    try {
      const lb = await get('/api/leaderboard', { passed_only: true });
      const rows = (lb.rows || []).filter((r) => r.holdout_mae != null).sort((a, b) => a.holdout_mae - b.holdout_mae);
      if (!rows.length) { setStatus('dx-rank-status', 'warn', '暂无已通过闸门的冠军指标可预填'); return; }
      const best = rows[0];
      $id('dx-milli').value = String(Math.round(best.holdout_mae * 1000));
      setStatus('dx-rank-status', 'ok', `已预填冠军 ${best.family}（R${best.round}）golden MAE ${best.holdout_mae} → scoreMilli ${Math.round(best.holdout_mae * 1000)}`);
    } catch (e) { setStatus('dx-rank-status', 'bad', `预填失败：${e.message}`); }
  }
  function onRaceChange() { renderRankings(); }
  function syncRedacted() {
    const rankR = $id('dx-rank-redacted').checked, pR = $id('dx-p-redacted').checked;
    ['dx-milli'].forEach((id) => { const el = $id(id); if (el) el.disabled = rankR; });
    ['dx-p-value', 'dx-p-cilo', 'dx-p-cihi'].forEach((id) => { const el = $id(id); if (el) el.disabled = pR; });
  }

  /* ---------------- 入口 ---------------- */
  async function onOpen() {
    await MAE.loadCfg();
    if (!booted) {
      $('#chain-body').innerHTML = shell();
      bind(); booted = true;
      MAE.onChange(renderWallet);
    }
    renderWallet(); renderContracts(); renderAnchors();
    refreshAll();
  }
  return { onOpen, refresh: refreshAll, renderWallet };
})();
