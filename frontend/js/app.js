/* FormulaAgents · 统一旅程地图前端（Apple HIG 风格）
 *
 * 原则（与后端一致）：
 *   · 数值全部来自 API，前端不产生任何指标；
 *   · 任一阶段取数失败 → 显示"数据不可用"，不编造、不静默留白；
 *   · 所有 3D 库来自 vendor/，断网也能演示。
 */
const API = location.origin;
const STAGES = [
  { id: 's1', n: '1', name: '接任务', metric: '' }, { id: 's2', n: '2', name: '找经验', metric: '' },
  { id: 's3', n: '3', name: '材料计算中心', metric: '' }, { id: 's4', n: '4', name: '选算法', metric: '' },
  { id: 's5', n: '5', name: '做设计', metric: '' }, { id: 's6', n: '6', name: '定实验', metric: '' },
  { id: 's7', n: '7', name: '出数据卡', metric: '' }, { id: 's8', n: '8', name: '极限制造', metric: '' },
];
const TARGETS = {
  epoxy: ['lap_shear_strength'], pu: ['lap_shear_strength'], acrylic: ['lap_shear_strength'],
  photoresist: ['resolution_nm', 'ler_nm'],
};
const TARGET_LABEL = { lap_shear_strength: '剪切强度 (MPa)', resolution_nm: '分辨率 (nm)', ler_nm: '线宽粗糙度 (nm)' };
// 后端 /api/journey/overview 可能只回部分阶段；这里保证页面 8 段始终可导航
const STAGE_FALLBACK = { id: 's8', n: '8', name: '极限制造', metric: '' };

let SYS = 'epoxy', TARGET = 'lap_shear_strength';
let WM = null, DESIGN = null, MOL = null, MOLECULES = null, PW = null;

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h !== undefined) e.innerHTML = h; return e; };
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const _cache = new Map();
async function get(path, params, ttl = 15000) {
  const u = new URL(API + path);
  Object.entries(params || {}).forEach(([k, v]) => v != null && u.searchParams.set(k, v));
  const key = u.toString();
  const hit = _cache.get(key);
  if (hit && Date.now() - hit.t < ttl) return hit.d;
  const r = await fetch(u, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  const d = await r.json();
  _cache.set(key, { t: Date.now(), d });
  return d;
}
async function post(path, body) {
  const r = await fetch(API + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
const num = (v, d = 3) => (v == null || Number.isNaN(+v)) ? '—' : (+v).toFixed(d);

/* ---------------- 旅程导航 ---------------- */
function renderJourney() {
  const nav = $('#journey'); nav.innerHTML = '';
  const curId = $('.stage:not(.hidden)')?.id;      // 重绘导航后要保住当前所在阶段的高亮
  STAGES.forEach((s, i) => {
    if (i) nav.appendChild(el('span', 'arrow', '→'));
    const b = el('div', 'step' + (s.ready ? ' ready' : ' off'), '');
    b.dataset.stage = s.id;
    b.innerHTML = `<span class="n">${s.n ?? (i + 1)}</span><span>${s.name}</span><span class="m">${s.metric || ''}</span>`;
    b.onclick = () => { stopTour(); go(s.id); };
    nav.appendChild(b);
  });
  if (curId) $$('.step').forEach(s => s.classList.toggle('active', s.dataset.stage === curId));
}
function go(id) {
  $$('.stage').forEach(s => s.classList.add('hidden'));
  const sec = $('#' + id); if (sec) sec.classList.remove('hidden');
  $$('.step').forEach(s => s.classList.toggle('active', s.dataset.stage === id));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (id === 's2') loadWorldModel(), loadMolecules();
  if (id === 's3') loadMolecules();
  if (id === 's4') loadArena();
  if (id === 's5') loadDesign();
  if (id === 's6') loadPlans();
  if (id === 's7') loadCards();
  if (id === 's8') {}  // 由按钮触发
}

/* ---------------- S1 接任务 ---------------- */
async function loadOverview() {
  try {
    const d = await get('/api/journey/overview', { system: SYS, target: TARGET });
    // 后端可能只回部分阶段（如缺 s8），这里补齐，保证 8 段始终可导航
    const stages = (d.stages || []).map((s, i) => ({ ...s, n: s.n ?? String(i + 1) }));
    if (!stages.some(s => s.id === 's8')) stages.push({ ...STAGE_FALLBACK });
    STAGES.splice(0, STAGES.length, ...stages); renderJourney();
    const c = d.counts, ch = d.champion;
    $('#s1-cards').innerHTML = [
      ['原料主数据', c.materials, `${c.with_smiles} 种有 3D 结构`],
      ['配方', c.formulations, `${SYS} 体系`],
      ['性能实测', c.performances, '含失败样本（一等公民）'],
      ['Capsule 评测', c.capsules, `通过 ${c.capsules_passed}`],
      ['累计试验次数', c.trials_total, '试得越多，胜者越被高估'],
      ['Gene 策略资产', c.genes, '可继承'],
      ['材料数据卡', c.datacards, '可导出 PLM/CAE'],
      ['建议实验', c.plans, '待人批准'],
    ].map(([t, v, d2]) => `<div class="card"><div class="t">${t}</div><div class="v">${v}</div><div class="d">${d2}</div></div>`).join('')
      + (ch ? `<div class="card accent"><div class="t">可信冠军</div><div class="v" style="color:var(--ok)">${ch.family}</div><div class="d">golden MAE ${num(ch.holdout_mae)} · 覆盖 ${num(ch.coverage, 2)}</div></div>` : '');
  } catch (e) { $('#s1-cards').innerHTML = `<div class="card">概览取数失败：${e.message}</div>`; }
}

/* ---------------- S2 世界模型 ---------------- */
const WM_NAME = { raw_material: '原材料', intermediate: '中间体', formulation: '配方', experiment: '实验', performance: '性能', supplier: '供应商', application: '客户应用', project: '项目' };
async function loadWorldModel() {
  const box = $('#wm-graph');
  try {
    const d = await get('/api/wm/graph', { system: SYS === 'photoresist' ? 'photoresist' : null, limit: 8 });
    if (WM) { try { WM._destructor?.(); } catch (_) {} box.innerHTML = ''; WM = null; }
    const colors = d.colors || {};
    const w = Math.max(280, Math.min(box.clientWidth || 600, 1200));   // 钳制宽度，防网格撑爆正反馈
    WM = ForceGraph3D()(box).width(w).height(440).backgroundColor('#0b0d12')
      .graphData(d).nodeColor(n => colors[n.type] || '#8b949e')
      .nodeLabel(n => `${WM_NAME[n.type] || n.type}：${n.label}`)
      .linkColor(() => '#2a2f3a').linkWidth(0.7).nodeRelSize(6.5);
    try { WM.d3Force('charge').strength(-140); } catch (e) {}
    try { WM.onEngineStop(() => { try { WM.zoomToFit(600, 40); } catch (e) {} }); } catch (e) {}
    $('#wm-legend').innerHTML = Object.keys(colors).map(k => `<span><i style="background:${colors[k]}"></i>${WM_NAME[k] || k}</span>`).join('') + `<span>· ${d.nodes.length} 节点 / ${d.links.length} 关系</span>`;
  } catch (e) { box.innerHTML = `<div style="padding:16px;color:var(--bad)">世界模型取数失败：${e.message}</div>`; }
}
async function ask(q) {
  const out = $('#ask-out');
  const input = $('#ask-input'), button = $('#ask-btn');
  const cleanQ = String(q || '').trim();
  if (!cleanQ) return;
  input.disabled = true; button.disabled = true;
  out.innerHTML =
    `<div class="bubble user"><span class="who">你</span>${esc(cleanQ)}</div>` +
    `<div class="bubble agent"><span class="who">🔎 智能体</span><span class="loading-dots">检索中</span></div>`;
  try {
    const r = await post('/api/ask', { question: cleanQ });
    const ev = r.evidence || [];
    const evHtml = ev.length
      ? `<details open><summary>证据链（${ev.length} 条，可点开核对）</summary>` +
        ev.map(e => `<div style="margin:6px 0"><b>${esc(e.kind)}</b> ← ${esc(e.source)}<br><code>${(esc(e.sql) || '').slice(0, 140)}</code></div>`).join('') +
        '</details>'
      : `未命中 —— 库里没有，不做推测。`;
    out.innerHTML =
      `<div class="bubble user"><span class="who">你</span>${esc(cleanQ)}</div>` +
      `<div class="bubble agent"><span class="who">🔎 智能体</span>${esc(r.answer || '（无答案）')}</div>` +
      `<div class="bubble evidence">${evHtml}</div>`;
  } catch (e) {
    out.innerHTML =
      `<div class="bubble user"><span class="who">你</span>${esc(cleanQ)}</div>` +
      `<div class="bubble err"><span class="who">⚠️</span>问答失败：${esc(e.message)}</div>`;
  } finally {
    input.disabled = false;
    button.disabled = false;
    input.focus();
  }
}

/* ---------------- S3 材料计算中心 ---------------- */
let molViewer = null, curMol = null, molTab = 'prop', bondSel = new Set(), curGroups = [];
async function loadMolecules() {
  if (MOLECULES) return renderMolList();
  try { MOLECULES = await get('/data/molecules.json'); renderMolList(); }
  catch (e) { $('#mol-list').innerHTML = `<div style="padding:8px;color:var(--bad)">分子加载失败：${e.message}</div>`; }
}
function renderMolList() {
  const list = $('#mol-list'); list.innerHTML = '';
  (MOLECULES?.molecules || []).forEach(m => {
    const d = el('div', '', `${m.code}<br><span style="color:var(--dim);font-size:11.5px">${m.formula} · MW ${m.mw}</span>`);
    d.onclick = () => { $$('#mol-list div').forEach(x => x.classList.remove('sel')); d.classList.add('sel'); showMol(m); };
    list.appendChild(d);
  });
  if (!curMol && MOLECULES?.molecules?.length) { list.firstChild?.classList.add('sel'); showMol(MOLECULES.molecules[0]); }
}
function showMol(m) {
  curMol = m; molTab = 'prop'; bondSel.clear();
  $$('#mol-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === 'prop'));
  const box = $('#mol-3d');
  // 复用同一个 viewer：每次 createViewer 都会新建一个 WebGL 上下文，切换分子会耗尽上下文
  if (!molViewer) { box.innerHTML = ''; molViewer = $3Dmol.createViewer(box, { backgroundColor: 'white' }); }
  else { molViewer.removeAllModels(); molViewer.removeAllShapes(); molViewer.removeAllLabels(); }
  molViewer.addModel(m.molblock, 'mol');
  molViewer.setStyle({}, { stick: { radius: 0.14 }, sphere: { radius: 0.32 } });
  molViewer.zoomTo(); molViewer.render(); molViewer.resize();
  renderMolInfo();
}
function renderMolInfo() {
  const box = $('#mol-info'); const m = curMol; if (!m) return;
  if (molTab === 'prop') {
    const p = m.properties || {};
    const kv = Object.entries(p).map(([k, v]) => `<div><b>${k}</b>${v}</div>`).join('')
      || '<div><b>—</b>库内暂未登记数值属性</div>';
    box.innerHTML = `<div><b>${m.name}</b> · <span style="color:var(--dim)">${m.category || '—'}</span></div>
      <div style="margin-top:6px">${m.role || '（角色说明缺失）'}</div>
      <div class="kv2"><div><b>分子式</b>${m.formula}</div><div><b>精确分子量</b>${m.mw}</div>${kv}</div>`;
  } else if (molTab === 'bond') {
    const cnt = {}; (m.bonds || []).forEach(b => { const k = b.order + (b.aromatic ? 'a' : ''); cnt[k] = (cnt[k] || 0) + 1; });
    const lbl = { '1': '单键', '2': '双键', '3': '三键', '4': '芳香键', '1a': '芳香单键' };
    box.innerHTML = `<div>点击键型可在此分子上高亮对应化学键：</div><div style="margin-top:8px">` +
      Object.entries(cnt).map(([k, v]) => `<span class="tag ${bondSel.has(k) ? 'on' : ''}" data-k="${k}">${lbl[k] || k} ×${v}</span>`).join('') + `</div>`;
    $$('#mol-info .tag').forEach(t => t.onclick = () => {
      const k = t.dataset.k; if (bondSel.has(k)) bondSel.delete(k); else bondSel.add(k);
      t.classList.toggle('on'); drawBonds();
    });
    drawBonds();
  } else if (molTab === 'group') {
    // 同名同色的基团（如两个环氧基）合并展示，避免重复卡片
    const merged = [];
    (m.groups || []).forEach(g => {
      const hit = merged.find(x => x.name === g.name && x.color === g.color);
      if (hit) hit.atoms = [...new Set([...hit.atoms, ...(g.atoms || [])])];
      else merged.push({ ...g, atoms: [...(g.atoms || [])] });
    });
    curGroups = merged;
    box.innerHTML = `<div>悬停基团名可在分子上高亮对应原子（回答"哪个原子在起作用"）：</div><div style="margin-top:8px">` +
      (merged.length ? merged.map((g, i) => `<span class="tag" data-i="${i}"><i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${g.color};margin-right:5px"></i>${g.name} ×${g.atoms.length}</span>`).join('')
        : '<span style="color:var(--dim)">该分子未标注官能团</span>') + `</div>`;
    $$('#mol-info .tag').forEach(s => {
      const g = merged[parseInt(s.dataset.i)];
      s.onmouseenter = () => highlightGroup(g, true);
      s.onmouseleave = () => highlightGroup(g, false);
    });
  } else if (molTab === 'perf') {
    box.innerHTML = '<div style="color:var(--dim)">拉取该原料在库中的性能影响…</div>';
    get(`/api/material/${m.code}/impact`).then(r => {
      const pt = r.per_target || [];
      box.innerHTML = `<div><b>${m.code}</b> 被 <b>${r.formulations_used}</b> 个配方使用。</div>` +
        (pt.length ? `<div class="kv2">` + pt.map(x => `<div><b>${x.test_item}（${x.unit}）</b>均值 ${num(x.avg)} · 通过率 ${(x.pass_rate * 100).toFixed(0)}%</div>`).join('') + `</div>`
          : '<div style="margin-top:8px;color:var(--warn)">该原料暂未关联到带性能记录的配方。</div>');
    }).catch(e => box.innerHTML = `<div style="color:var(--bad)">性能关联失败：${e.message}</div>`);
  }
}
function drawBonds() {
  if (!molViewer || !curMol) return;
  molViewer.removeAllShapes();
  const coord = i => { const a = curMol.atoms[i]; return { x: a.x, y: a.y, z: a.z }; };
  const color = { '1': '#9aa0a6', '2': '#ff9f0a', '3': '#ff3b30', '4': '#5e5ce6', '1a': '#5e5ce6' };
  (curMol.bonds || []).forEach(b => {
    const k = b.order + (b.aromatic ? 'a' : '');
    if (!bondSel.has(k)) return;
    molViewer.addCylinder({ start: coord(b.i), end: coord(b.j), color: color[k] || '#9aa0a6', radius: 0.13 });
  });
  molViewer.render();
}
function highlightGroup(g, on) {
  if (!molViewer || !g?.atoms?.length) return;
  molViewer.setStyle({ serial: g.atoms }, on
    ? { sphere: { color: g.color, radius: .6 }, stick: { color: g.color, radius: .16 } }
    : { stick: { radius: .14 }, sphere: { radius: .32 } });
  molViewer.render();
}

/* ---------------- S4 竞技场 ---------------- */
const AGENT_COLOR = { ridge: '#0071e3', pls: '#34c759', rf: '#ff9f0a', gbr: '#af52de', gp: '#ff3b30' };
let arenaToken = 0;
async function loadArena() {
  const box = $('#arena-agents'), tb = $('#arena-board').querySelector('tbody');
  const my = ++arenaToken;                    // 快速切阶段时丢弃上一轮的逐条动画
  try {
    const [lb] = await Promise.all([get('/api/leaderboard', { passed_only: false }), get('/api/agents')]);
    if (my !== arenaToken) return;
    const rows = (lb.rows || []).slice(0, 24); tb.innerHTML = ''; box.innerHTML = '';
    const fams = [...new Set(rows.map(r => r.family))];
    fams.forEach(f => box.appendChild(el('div', 'agent', `<span class="dot" style="background:${AGENT_COLOR[f] || '#888'}"></span><span><span class="who">🧬 ${f} Agent</span><br><span class="say">提案中…</span></span>`)));
    rows.forEach((r, i) => setTimeout(() => {
      if (my !== arenaToken) return;
      const best = rows.filter(x => x.family === r.family).sort((a, b) => a.dev_mae - b.dev_mae)[0];
      const node = box.children[fams.indexOf(r.family)];
      if (node) { node.classList.add('on'); node.querySelector('.say').textContent = `n 匹配 → dev MAE ${num(r.dev_mae)}${best === r ? '（本族最优）' : ''}`; }
      tb.appendChild(el('tr', '', `<td>${r.family}</td><td>${num(r.dev_mae)}</td><td>${num(r.holdout_mae)}</td>
        <td>R${r.round ?? '—'} · ${r.split || '—'}</td><td>${num(r.coverage, 2)}</td><td>${r.trials_total ?? '—'}</td>
        <td class="${r.passed ? 'pass' : 'fail'}">${r.passed ? '🏆 通过' : '✕ ' + (r.fail_reason || '未过')}</td>`));
    }, i * 90));
    try {
      const ct = await get('/api/contrast'); if (my !== arenaToken) return;
      const sw = ct.swarm, bl = ct.blind;
      $('#arena-why').innerHTML = [
        ['蜂群可信冠军', `${num(sw.best_mae)}（Integrator 冻结择优）`],
        ['单 Agent 盲选', `${num(bl.best_mae)} · 被闸门否决`],
        ['拒掉的谎报幅度', num(ct.emergent_gap)],
        ['闸门否决率', (sw.gate_reject_rate * 100).toFixed(1) + '%'],
        ['负例入库', `${sw.failed} / ${sw.total}（失败是一等公民）`],
        ['累计试验次数', sw.trials_total],
      ].map(([k, v]) => `<div><b>${k}</b><br>${v}</div>`).join('');
    } catch (e) { $('#arena-why').innerHTML = `<div>对比数据不可用：${e.message}</div>`; }
  } catch (e) { tb.innerHTML = `<tr><td colspan="7" style="color:var(--bad)">竞技场取数失败：${e.message}</td></tr>`; }
}

/* ---------------- S5 3D 设计空间 ---------------- */
let designScene = null, designFamily = null, designLoading = false, designRaf = 0;
async function loadDesign() {
  const box = $('#design-3d');
  if (designLoading) return;
  designLoading = true;
  const t0 = Date.now();
  box.innerHTML = `<div class="loading-box"><div class="spinner"></div>` +
    `<div class="lt">确定性建模中…（不调用 LLM，首次约 10 秒）</div>` +
    `<div class="le" id="design-elapsed">已用时 0.0s</div></div>`;
  const timer = setInterval(() => {
    const e = $('#design-elapsed'); if (e) e.textContent = `已用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`;
  }, 200);
  try {
    const d = await post('/api/design/space', { system: SYS, target: TARGET, grid: 8 });
    clearInterval(timer);
    DESIGN = d; $('#design-note').textContent = d.note || '';
    $('#design-family-chips').innerHTML = d.families.map(f => `<button class="chip" data-f="${f}" style="border-color:${AGENT_COLOR[f] || '#888'}">${f}</button>`).join('');
    $$('#design-family-chips button').forEach(b => b.onclick = () => { designFamily = designFamily === b.dataset.f ? null : b.dataset.f; $('#design-spread').checked = !designFamily; paintDesign(); });
    $('#design-spread').onchange = () => paintDesign();
    buildDesign(box, d);
  } catch (e) {
    clearInterval(timer);
    box.innerHTML = `<div style="padding:16px;color:var(--bad)">设计空间取数失败：${e.message}</div>`;
  } finally { designLoading = false; }
}
function disposeDesign() {
  if (!designScene) return;
  cancelAnimationFrame(designRaf); designRaf = 0;
  try {
    designScene.pts.geometry.dispose(); designScene.pts.material.dispose();
    designScene.renderer.dispose(); designScene.renderer.forceContextLoss?.();
  } catch (_) {}
  designScene = null;
}
function buildDesign(box, d) {
  disposeDesign();
  box.innerHTML = ''; const w = box.clientWidth || 900, h = 440;
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(55, w / h, .1, 5000); cam.position.set(180, 150, 220);
  cam.lookAt(0, 0, 0);
  const renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setSize(w, h); box.appendChild(renderer.domElement);
  const xs = d.points.map(p => p.x), ys = d.points.map(p => p.y), zs = d.points.map(p => p.z), vs = d.points.flatMap(p => Object.values(p.pred));
  const mm = a => [Math.min(...a), Math.max(...a)];
  const [x0, x1] = mm(xs), [y0, y1] = mm(ys), [z0, z1] = mm(zs), [v0, v1] = mm(vs); const S = 120;
  const geo = new THREE.BufferGeometry(); const pos = [], col = [];
  d.points.forEach(p => { pos.push((p.x - x0) / (x1 - x0 || 1) * S - S / 2, (p.y - y0) / (y1 - y0 || 1) * S - S / 2, (p.z - z0) / (z1 - z0 || 1) * S - S / 2); col.push(1, 1, 1); });
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  const pts = new THREE.Points(geo, new THREE.PointsMaterial({ size: 4.2, vertexColors: true, sizeAttenuation: true }));
  scene.add(pts); scene.add(new THREE.AxesHelper(80));
  const grid = new THREE.GridHelper(S, 10, 0xd0d0d5, 0xe8e8ed); grid.position.y = -S / 2; scene.add(grid);
  designScene = { scene, cam, renderer, pts, d, v0, v1 }; paintDesign();
  (function loop() {
    designRaf = requestAnimationFrame(loop);
    if (!designScene) return;                       // 已被销毁则不再自持
    pts.rotation.y += 0.0025; renderer.render(scene, cam);
  })();
}
function paintDesign() {
  if (!designScene) return;
  const { pts, d, v0, v1 } = designScene; const arr = pts.geometry.attributes.color.array;
  const ramp = t => { t = Math.max(0, Math.min(1, t)); const r = t < .5 ? t * 2 : 1, g = t < .5 ? .62 : 1 - (t - .5) * 2, b = t < .5 ? 1 - t * 2 : 0; return [r, g, b]; };
  // 归一化区间只算一次（原实现在循环内重复 map，512 点即 26 万次冗余遍历）
  let lo = v0, hi = v1;
  if (!designFamily) {
    lo = Infinity; hi = -Infinity;
    for (const p of d.points) { if (p.spread < lo) lo = p.spread; if (p.spread > hi) hi = p.spread; }
  }
  const span = (hi - lo) || 1;
  d.points.forEach((p, i) => {
    const raw = designFamily ? p.pred[designFamily] : p.spread;
    const [r, g, b] = ramp((raw - lo) / span);
    arr[i * 3] = r; arr[i * 3 + 1] = g; arr[i * 3 + 2] = b;
  });
  pts.geometry.attributes.color.needsUpdate = true;
  $('#design-note').innerHTML = designFamily
    ? `当前按 <b>${designFamily}</b> 的预测着色（蓝=低 / 红=高）。切换其它族即可看到<b>同一批候选被不同算法排成不同样子</b>。`
    : `当前按 <b>算法分歧度</b> 着色：红=各族预测差得最多的区域 —— 那里最需要竞技场介入，也最不该盲信单点预测。`;
}

/* ---------------- S6 / S7 ---------------- */
async function loadPlans() {
  const tb = $('#plan-table').querySelector('tbody');
  try {
    const d = await get('/api/foundry/plans', { limit: 30 });
    const rows = d.rows || d.plans || [];
    tb.innerHTML = rows.map(p => `<tr><td>PLAN-${p.id}</td><td>${p.code || '—'}</td><td>${num(p.predicted)}</td>
      <td>${num(p.ci_low)} ~ ${num(p.ci_high)}</td><td class="${p.status === 'approved' ? 'pass' : ''}">${p.status}</td>
      <td>${p.approved_by || '—（待批准）'}</td></tr>`).join('') || '<tr><td colspan="6">（无建议实验）</td></tr>';
  } catch (e) { tb.innerHTML = `<tr><td colspan="6" style="color:var(--bad)">取数失败：${e.message}</td></tr>`; }
}
async function loadCards() {
  const g = $('#dc-grid');
  try {
    const d = await get('/api/foundry/datacards', { limit: 24 });
    const cards = d.rows || d.datacards || [];
    g.innerHTML = cards.map(c => `<div class="card"><div class="t">${c.code || ''}</div>
      <div class="v">${num(c.predicted)} <span style="font-size:13px;color:var(--dim)">${c.target || ''}</span></div>
      <div class="d">区间 ${num(c.ci_low)} ~ ${num(c.ci_high)}</div>
      <div class="d">适用域：${(c.applicability || {}).decision || ((c.applicability || {}).in_domain === false ? '域外' : '域内')} · ${c.interval_method || '—'}</div>
      <div class="d" style="margin-top:6px;color:var(--warn)">${c.simulated ? '⚠️ 模拟结果，非实测' : '实测'}</div></div>`).join('')
      || '<div class="card">（暂无数据卡）</div>';
  } catch (e) { g.innerHTML = `<div class="card">取数失败：${e.message}</div>`; }
}

/* ---------------- S8 极限制造 工艺窗口 ---------------- */
function syncPWNote() {
  const n = $('#pw-note'); if (!n) return;
  n.innerHTML = SYS === 'photoresist'
    ? `目标 ${TARGET_LABEL[TARGET] || TARGET} · 扫描 PEB 温度 × PAG 负载，看工艺窗口有多窄`
    : `工艺窗口需要 PEB 温度 × PAG 负载两个工艺特征，当前体系不支持 · <button class="chip" data-switch="photoresist">切到光刻胶体系</button>`;
}
async function loadProcessWindow() {
  const box = $('#pw-heat');
  if (SYS !== 'photoresist') {
    box.innerHTML = '<div style="color:var(--warn)">仅光刻胶体系支持工艺窗口（需 PEB 温度 × PAG 负载工艺特征）。请先在顶部切到「光刻胶」。</div>';
    $('#pw-desc').textContent = '';
    return;
  }
  const btn = $('#pw-btn');
  const t0 = Date.now();
  btn.disabled = true;
  box.innerHTML = `<div class="loading-box"><div class="spinner"></div>` +
    `<div class="lt">扫描 PEB × PAG 网格中…（确定性建模，首次约 10 秒）</div>` +
    `<div class="le" id="pw-elapsed">已用时 0.0s</div></div>`;
  const timer = setInterval(() => {
    const e = $('#pw-elapsed'); if (e) e.textContent = `已用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`;
  }, 200);
  try {
    const d = await post('/api/process/window', { system: SYS, target: TARGET, grid: 22 });
    clearInterval(timer); btn.disabled = false;
    PW = d; const vals = d.heat.map(h => h.val); const mn = Math.min(...vals), mx = Math.max(...vals);
    // 亮度 = 越优越亮（已按方向：resolution 越小越好 → sign=-1，val 已取负，越大越好）
    const norm = v => (v - mn) / (mx - mn || 1);
    const G = d.grid;
    let html = '<table><tbody>';
    for (let i = 0; i < G; i++) {
      html += '<tr>';
      for (let j = 0; j < G; j++) {
        const h = d.heat[i * G + j]; const t = norm(h.val);
        // 冷→暖：暗蓝(差) → 亮黄绿(优)
        const r = Math.round(40 + t * 200), g = Math.round(80 + t * 160), b = Math.round(160 - t * 140);
        html += `<td style="background:rgb(${r},${g},${b})" title="PEB ${h.peb}℃ · PAG ${h.pag} · ${num(h.val)}"></td>`;
      }
      html += '</tr>';
    }
    html += '</tbody></table>';
    box.innerHTML = html;
    // 实测点叠加提示
    const realTxt = d.real.length ? `另有 ${d.real.length} 个 golden 段实测点可用于核对。` : '';
    $('#pw-desc').innerHTML = `${d.note} <br>最优工艺点：PEB ${d.best.peb}℃ · PAG ${d.best.pag}%（${TARGET_LABEL[TARGET] || TARGET} 方向取优）。 ${realTxt}`;
  } catch (e) {
    clearInterval(timer); btn.disabled = false;
    box.innerHTML = `<span style="color:var(--bad)">窗口生成失败：${e.message}</span>`;
  }
}

/* ---------------- 导览 ---------------- */
let tourTimer = null;
function stopTour() { clearInterval(tourTimer); tourTimer = null; $('#btn-tour').textContent = '▶ 导览'; }
function tour() {
  let i = 0;
  const tick = () => { if (i >= STAGES.length) { stopTour(); return; } go(STAGES[i].id); i++; };
  $('#btn-tour').textContent = '⏹ 停止'; tick(); tourTimer = setInterval(tick, 6000);
}
function toggleTour() { tourTimer ? stopTour() : tour(); }

/* ---------------- 链上信息 ---------------- */
const CHAIN = {
  network: 'BOT Chain 主网', chainId: 677,
  rpc: 'https://rpc.botchain.ai', explorer: 'https://scan.botchain.ai',
  contract: '0x3A0d7c52D1EFa6183a8AfB5c95B9Ab737b9fac45',
  deployer: '0x4Bd9a6ab15329F592f885f7d3891406178c4070f',
  deploy: { tx: '0xaf80ccfe72cc498e9acf14183b2195cbcb021b55e48e11bb8bd6397b55125a6c',
    block: 25906737, gas: 1577125, cost: 0.031542, balance: 0.46267886 },
  anchors: [
    { k: '① 批准留痕', obj: 'plan#37 · run=fdep-5139a2 R1',
      tx: '0x5ea8d59b6ee661eb40cdd6ba3ec2a3918210db156d80f40a5b7e894dbed15db9',
      block: 25907726, gas: 142376,
      extra: 'planHash 0x1e4b…8bf0 · approverHash 与链下 keccak256(批准人) 一致' },
    { k: '② 预测登记锁', obj: 'PLAN-363',
      tx: '0x87829d6c5dbe209bafc329455841b9433d98cfcf6476d180a86d1123a2910c22',
      block: 25907733, gas: 146556,
      extra: 'redacted=True，链上数值 (0,0,0) · 真实预测值未上链' },
  ],
};
const shortHash = h => (h.length > 14 ? h.slice(0, 8) + '…' + h.slice(-6) : h);
function renderChain() {
  // DApp 面板（wallet.js + race-ui.js）：钱包连接 / Agent 竞马上链 / 预测调用上链 / 链上证明。
  // 静态兜底（脚本未加载时）仍展示 V1 锚定记录。
  if (window.RaceUI && window.MAE) return;
  const c = CHAIN, ex = c.explorer;
  $('#chain-body').innerHTML =
    `<div class="chain-net">
      <span class="cb">网络 <b>${esc(c.network)}</b></span>
      <span class="cb">chain_id <b>${c.chainId}</b></span>
      <span class="cb">RPC <b class="mono">${esc(c.rpc)}</b></span>
      <span class="cb">浏览器 <b class="mono"><a class="chain-link" href="${ex}" target="_blank" rel="noopener">${esc(ex)}</a></b></span>
    </div>
    <div class="chain-grid">
      <div class="card"><div class="t">已部署合约</div>
        <div class="v mono"><a class="chain-link" href="${ex}/address/${c.contract}" target="_blank" rel="noopener">${esc(c.contract)}</a></div>
        <div class="d">FormulaSwarmAttestor · VERSION=fs-attestor/1.0</div></div>
      <div class="card"><div class="t">部署交易</div>
        <div class="v mono"><a class="chain-link" href="${ex}/tx/${c.deploy.tx}" target="_blank" rel="noopener">${shortHash(c.deploy.tx)}</a></div>
        <div class="d">区块 ${c.deploy.block.toLocaleString()} · gas ${c.deploy.gas.toLocaleString()} · 花费 ${c.deploy.cost} BOT</div></div>
      <div class="card"><div class="t">部署者钱包</div>
        <div class="v mono"><a class="chain-link" href="${ex}/address/${c.deployer}" target="_blank" rel="noopener">${esc(c.deployer)}</a></div>
        <div class="d">当前余额 ${c.deploy.balance} BOT</div></div>
    </div>
    <div class="chain-sec">锚定记录（各 1 条 · 取最新一轮）</div>
    <div class="boardwrap">
    <table class="chain-table"><thead><tr>
      <th>锚点</th><th>对象</th><th>交易（区块 / gas）</th><th>说明</th>
    </tr></thead><tbody>
    ${c.anchors.map(a => `<tr>
      <td><b>${esc(a.k)}</b></td>
      <td>${esc(a.obj)}</td>
      <td><a class="chain-link" href="${ex}/tx/${a.tx}" target="_blank" rel="noopener">${shortHash(a.tx)}</a><br>
        <span class="hint">区块 ${a.block.toLocaleString()} · gas ${a.gas.toLocaleString()}</span></td>
      <td class="hint">${esc(a.extra)}</td>
    </tr>`).join('')}
    </tbody></table></div>
    <div class="chain-note">
      <b>诚实声明：</b>链上不可篡改 ≠ 模型更准；公证的是「结论如何被产生」，不是「结论更准」。
      ② 为脱敏对象，链上数值一律归零，⛓ 徽标不代表数值被公证。
      其余 258 条批准 + 294 条预测尚未锚定（余额不足，约需 1.07 BOT）。
      ③ Agent 链上身份（ERC-8004）因接口不存在 / 规范地址被无关合约占用，本轮未做。
    </div>`;
}
function openChain() {
  if (window.RaceUI && window.MAE) RaceUI.onOpen();     // DApp 面板（含钱包与写链）
  else renderChain();                                   // 降级：静态锚定展示
  $('#chain-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function closeChain() { $('#chain-modal').classList.add('hidden'); document.body.style.overflow = ''; }

/* ---------------- 启动 ---------------- */
function buildTargetSeg() {
  const seg = $('#seg-target'); seg.innerHTML = '';
  (TARGETS[SYS] || []).forEach(t => {
    const b = el('button', t === TARGET ? 'on' : '', TARGET_LABEL[t] || t);
    b.onclick = () => { TARGET = t; $$('#seg-target button').forEach(x => x.classList.remove('on')); b.classList.add('on'); reload(); };
    seg.appendChild(b);
  });
}
function setSystem(sys) {
  if (!sys || sys === SYS) return;
  SYS = sys; TARGET = (TARGETS[SYS] || [TARGET])[0];
  $$('button[data-sys]').forEach(x => x.classList.toggle('on', x.dataset.sys === sys));
  buildTargetSeg(); syncPWNote(); reload();
}
function bind() {
  $$('button[data-sys]').forEach(b => b.onclick = () => setSystem(b.dataset.sys));
  document.addEventListener('click', e => {
    const t = e.target?.closest?.('[data-switch]');
    if (t) { setSystem(t.dataset.switch); go('s8'); }
  });
  $('#btn-tour').onclick = toggleTour;
  $('#btn-chain').onclick = openChain;
  $$('#chain-modal [data-close]').forEach(x => x.onclick = closeChain);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#chain-modal').classList.contains('hidden')) closeChain(); });
  $('#ask-btn').onclick = () => $('#ask-input').value && ask($('#ask-input').value);
  $('#ask-input').onkeydown = e => e.key === 'Enter' && $('#ask-input').value && ask($('#ask-input').value);
  $$('.chip[data-q]').forEach(c => c.onclick = () => { $('#ask-input').value = c.dataset.q; ask(c.dataset.q); });
  $$('#mol-tabs button').forEach(b => b.onclick = () => { molTab = b.dataset.t; $$('#mol-tabs button').forEach(x => x.classList.remove('on')); b.classList.add('on'); renderMolInfo(); });
  $('#pw-btn').onclick = loadProcessWindow;
  let rt = 0;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      const w = $('#wm-graph').clientWidth;
      if (WM && w > 100) WM.width(Math.min(w, 1200));
      if (designScene) {
        const b = $('#design-3d'), ww = b.clientWidth || 900;
        designScene.cam.aspect = ww / 440; designScene.cam.updateProjectionMatrix();
        designScene.renderer.setSize(ww, 440);
      }
      if (molViewer) { try { molViewer.resize(); } catch (_) {} }
    }, 150);
  });
}
function reload() { syncPWNote(); loadOverview(); const cur = $('.step.active')?.dataset.stage; if (cur) go(cur); }
buildTargetSeg(); bind(); renderJourney(); syncPWNote(); loadOverview(); go('s1');
