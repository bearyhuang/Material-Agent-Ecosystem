"""FormulaAgents · 统一旅程地图的后端支撑。

对外只暴露**只读**端点（写路径仍在 CLI / 编排层），与既有 API 契约一致。

新增能力：
  · /api/journey/overview  各旅程阶段的实况（是否有数据、能否演示）
  · /api/wm/graph          Agent 的 Scientific World Model 图数据（原料→中间体→配方→实验→性能 + 供应商/客户应用）
  · /api/design/space      配方设计空间：同批候选在**多个算法族**下的预测差异（算法影响）
"""
from typing import Optional

import numpy as np

# 各族取搜索空间里的首套配置作为默认值（build_model 需要完整超参，空字典会 KeyError）
DEFAULT_CFG = {"ridge": {"alpha": 1.0}, "pls": {"k": 3}, "rf": {"n": 200, "d": 5},
               "gbr": {"n": 200, "lr": 0.05, "d": 3}, "gp": {"ls": 1.0}}
from fastapi import APIRouter, Body, HTTPException

from fsdb import connect

router = APIRouter(prefix="/api")

# 世界模型的节点配色（前端复用，避免两处各写一套）
NODE_COLOR = {
    "raw_material": "#f39c12", "intermediate": "#e67e22",
    "formulation": "#3498db", "experiment": "#9b59b6",
    "performance": "#27ae60", "supplier": "#95a5a6",
    "application": "#16a085", "project": "#34495e",
}
# 边的语义标签：与 doc/02 的主链/旁挂维度一致
EDGE_LABEL = {
    "made_of": "由…构成", "derived_from": "由…合成",
    "tested_by": "经…测试", "supplied_by": "供应自",
    "applied_to": "用于", "belongs_to": "归属项目",
}


def _rows(sql, args=None):
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, args or {})
        return cur.fetchall()


# ---------------------------------------------------------------- 旅程概览
@router.get("/journey/overview")
def journey_overview(system: str = "epoxy", target: str = "lap_shear_strength"):
    """化工工程师一天的八个阶段，各自"有没有料"。缺数据在 UI 上是灰的，不能假装。"""
    q = _rows("""
        select
          (select count(*) from mat.raw_material)                              as materials,
          (select count(*) from mat.raw_material where props ? 'smiles')       as with_smiles,
          (select count(*) from mat.formulation where system=%(sys)s)          as formulations,
          (select count(*) from mat.formulation where system='photoresist')    as pr_formulations,
          (select count(*) from mat.performance)                               as performances,
          (select count(*) from mat.purchase_order)                            as pos,
          (select count(*) from evo.answer_audit)                              as answers,
          (select count(*) from evo.capsule)                                   as capsules,
          (select count(*) from evo.capsule where passed)                      as capsules_passed,
          (select coalesce(sum(trials_total),0) from evo.capsule)              as trials_total,
          (select count(*) from evo.gene)                                      as genes,
          (select count(*) from evo.datacard)                                  as datacards,
          (select count(*) from evo.experiment_plan)                           as plans,
          (select count(*) from evo.evolution_event)                           as events
    """, {"sys": system})
    base = q[0]
    pr_forms = int(base.get("pr_formulations") or 0)

    champ = _rows("""
        select g.family, c.holdout_mae, c.coverage, c.round, c.system
        from evo.capsule c join evo.gene g on g.id = c.gene_id
        where c.passed and c.holdout_mae is not null
        order by c.round desc, c.holdout_mae asc limit 1
    """)
    stages = [
        {"id": "s1", "name": "接任务", "desc": "定体系·定目标性能·定约束",
         "ready": True, "metric": f"{base['formulations']} 个配方"},
        {"id": "s2", "name": "找经验", "desc": "跨项目发现问答 + 谱系溯源",
         "ready": base["answers"] > 0, "metric": f"{base['answers']} 条带证据回答"},
        {"id": "s3", "name": "看分子", "desc": "3D 结构与官能团标注",
         "ready": base["with_smiles"] > 0, "metric": f"{base['with_smiles']} 种有结构"},
        {"id": "s4", "name": "选算法", "desc": "蜂群竞技场 · 为什么赢",
         "ready": base["capsules"] > 0, "metric": f"{base['capsules']} 条 Capsule"},
        {"id": "s5", "name": "做设计", "desc": "3D 设计空间 · 算法影响",
         "ready": bool(champ), "metric": champ[0]["family"] if champ else "无冠军"},
        {"id": "s6", "name": "定实验", "desc": "建议清单 · 人批准",
         "ready": base["plans"] > 0, "metric": f"{base['plans']} 条建议"},
        {"id": "s7", "name": "出数据卡", "desc": "卡片式交付 · 可导出",
         "ready": base["datacards"] > 0, "metric": f"{base['datacards']} 张"},
        {"id": "s8", "name": "极限制造", "desc": "工艺窗口 · 数字孪生",
         "ready": pr_forms > 0, "metric": f"{pr_forms} 个光刻胶配方"},
    ]
    return {
        "project": "FormulaAgents", "system": system, "target": target,
        "counts": base,
        "champion": champ[0] if champ else None,
        "stages": stages,
        "disclaimer": "数据为合成/脱敏，展示流程与决策质量，不声称真实精度。",
    }


# ---------------------------------------------------------------- 世界模型
@router.get("/wm/graph")
def world_model(system: Optional[str] = None, limit: int = 6):
    """Agent 的 Scientific World Model（原四级关联库升级）。

    主链：原材料 → 中间体 → 配方 → 实验 → 性能
    旁挂：供应商（挂在原料）、客户应用（挂在性能侧）
    """
    lim = max(1, min(int(limit), 30))
    args = {"lim": lim}
    sysf = "and f.system = %(sys)s" if system else ""
    if system:
        args["sys"] = system

    nodes, edges = [], {}
    def node(nid, ntype, label, extra=None):
        nodes.append({"id": nid, "type": ntype, "label": label, **(extra or {})})
    def edge(a, b, kind):
        edges[(a, b, kind)] = True

    # 配方（主链中心）
    fs = _rows(f"""
        select f.id, f.code, f.system, f.family, f.stage,
               pj.code as project_code, ap.code as app_code, ap.industry, ap.substrate
        from mat.formulation f
        left join mat.project pj on pj.id = f.project_id
        left join mat.customer_application ap on ap.id = f.application_id
        where true {sysf}
        order by f.created_at limit %(lim)s
    """, args)

    for f in fs:
        fid = f"F{f['id']}"
        node(fid, "formulation", f["code"],
             {"system": f["system"], "family": f["family"], "stage": f["stage"]})
        if f["project_code"]:
            node(f"P{f['project_code']}", "project", f["project_code"])
            edge(fid, f"P{f['project_code']}", "belongs_to")
        if f["app_code"]:
            nid = f"A{f['app_code']}"
            node(nid, "application", f["app_code"],
                 {"industry": f["industry"], "substrate": f["substrate"]})
            edge(fid, nid, "applied_to")

        # 组成：原料（含供应商）与中间体
        for it in _rows("""
            select r.code, r.name, r.category,
                   coalesce(r.props->>'smiles', '') as smiles,
                   sp.code as supplier_code,
                   i.code  as im_code, i.name as im_name
            from mat.formulation_item fi
            left join mat.raw_material r on r.id = fi.raw_material_id
            left join mat.supplier  sp on sp.id = r.supplier_id
            left join mat.intermediate i on i.id = fi.intermediate_id
            where fi.formulation_id = %(fid)s
        """, {"fid": f["id"]}):
            if it["im_code"]:
                nid = f"I{it['im_code']}"
                node(nid, "intermediate", it["im_name"] or it["im_code"])
                edge(nid, fid, "made_of")
            if it["code"]:
                nid = f"R{it['code']}"
                node(nid, "raw_material", it["name"] or it["code"],
                     {"category": it["category"], "smiles": it["smiles"]})
                edge(nid, fid, "made_of")
                if it["supplier_code"]:
                    sid = f"S{it['supplier_code']}"
                    node(sid, "supplier", it["supplier_code"])
                    edge(sid, nid, "supplied_by")

        # 实验 → 性能
        for pf in _rows("""
            select id, test_item, value, unit, verdict from mat.performance
            where formulation_id = %(fid)s order by id limit 4
        """, {"fid": f["id"]}):
            eid = f"E{pf['id']}"
            node(eid, "experiment", f"实验 #{pf['id']}")
            edge(eid, fid, "tested_by")
            pid = f"V{pf['id']}"
            node(pid, "performance", f"{pf['value']} {pf['unit'] or ''}",
                 {"test_item": pf["test_item"], "verdict": pf["verdict"]})
            edge(pid, eid, "derived_from")

    # 中间体 → 原料（BOM 展开，让"中间体由什么合成"可见）
    for r in _rows("""
        select i.code as im_code, r.code as rm_code, r.name as rm_name
        from mat.intermediate_input ii
        join mat.intermediate i on i.id = ii.intermediate_id
        left join mat.raw_material r on r.id = ii.raw_material_id
        where r.code is not null limit 40
    """):
        node(f"I{r['im_code']}", "intermediate", r["im_code"])
        node(f"R{r['rm_code']}", "raw_material", r["rm_name"] or r["rm_code"])
        edge(f"R{r['rm_code']}", f"I{r['im_code']}", "made_of")

    # 去重（节点按 id 合并）
    seen, uniq = set(), []
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        uniq.append(n)
    return {
        "nodes": uniq,
        "links": [{"source": a, "target": b, "kind": k,
                   "label": EDGE_LABEL.get(k, k)} for (a, b, k) in edges],
        "colors": NODE_COLOR,
        "chain": ["raw_material", "intermediate", "formulation", "experiment", "performance"],
    }


# ---------------------------------------------------------------- 材料性能影响
@router.get("/material/{code}/impact")
def material_impact(code: str):
    """某原料在库中的"性能影响"：被哪些配方用到、对应性能指标汇总。

    这是 3D 分子"材料计算中心"的**性能**一层 —— 把孤立的分子结构连回真实配方与性能，
    体现 Agent 的 Scientific World Model（分子 → 配方 → 性能 的链路）。
    """
    code = (code or "").upper()
    rows = _rows("""
        select f.code            as fcode, f.system, f.stage,
               p.test_item, p.value, p.unit, p.verdict
        from mat.formulation_item fi
        join mat.raw_material rm on rm.id = fi.raw_material_id
        join mat.formulation  f on f.id = fi.formulation_id
        left join mat.performance p on p.formulation_id = f.id
        where rm.code = %(code)s
        order by f.created_at
    """, {"code": code})
    if not rows:
        raise HTTPException(404, f"未找到原料 {code} 的配方关联")
    # 按 (test_item) 聚合
    agg = {}
    for r in rows:
        ti = r["test_item"] or "—"
        a = agg.setdefault(ti, {"n": 0, "sum": 0.0, "pass": 0, "unit": r["unit"] or ""})
        a["n"] += 1
        if r["value"] is not None:
            a["sum"] += float(r["value"])
        if r["verdict"] == "pass":
            a["pass"] += 1
    per_target = [{"test_item": k, "n": v["n"], "avg": round(v["sum"] / v["n"], 3),
                   "pass_rate": round(v["pass"] / v["n"], 3), "unit": v["unit"]}
                  for k, v in agg.items()]
    formulas = [{"code": r["fcode"], "system": r["system"], "stage": r["stage"]}
                for r in rows]
    # 去重（按配方）
    seen, uniq = set(), []
    for f in formulas:
        if f["code"] in seen:
            continue
        seen.add(f["code"]); uniq.append(f)
    return {"code": code, "formulations_used": len(uniq),
            "formulation_codes": [f["code"] for f in uniq][:40],
            "per_target": per_target}


# ---------------------------------------------------------------- 设计空间
_design_space_cache: dict = {}


@router.post("/design/space")
def design_space(payload: dict = Body(...)):
    """配方设计空间：同一批候选在**多个算法族**下的预测差异 —— 即"算法对设计的影响"。

    返回每个候选点的 {x,y,z, 各族预测值, 分歧度}，供前端做 3D 散点/曲面。
    数值全部来自确定性模型，LLM 不参与。
    """
    system = payload.get("system", "epoxy")
    target = payload.get("target", "lap_shear_strength")
    cache_key = (system, target, int(payload.get("grid", 6)),
                 payload.get("mode", "both"),
                 tuple(payload.get("families") or []),
                 tuple(payload.get("axes") or []))
    cached = _design_space_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import fsdata
        from fsmodels import build_model
    except Exception as e:                                    # pragma: no cover
        raise HTTPException(500, f"建模依赖不可用: {e}")

    try:
        X, y, meta, feats = fsdata.load_dataset(target, payload.get("mode", "both"),
                                                system=system)
    except SystemExit as e:
        raise HTTPException(404, str(e))

    n = len(y)
    split = int(n * 0.8)
    Xtr, ytr = X[:split], y[:split]

    families = payload.get("families") or ["ridge", "pls", "rf", "gbr"]
    default_cfg = DEFAULT_CFG
    # 在三个主要组成维度上做网格（用训练集的取值范围，不越界外推）
    import fsrecipe
    axes = payload.get("axes") or ["resin_pct", "toughener_pct", "filler_pct"]
    idxs = []
    for a in axes:
        if a in feats:
            idxs.append(feats.index(a))
    if len(idxs) < 3:                       # 退化：取方差最大的三个特征维
        var = Xtr.var(axis=0)
        idxs = list(np.argsort(-var)[:3])

    models = {}
    for fam in families:
        try:
            m = build_model(fam, default_cfg.get(fam, {}), len(feats)).fit(Xtr, ytr)
            models[fam] = m
        except Exception:
            continue
    if not models:
        raise HTTPException(500, "没有任何算法族可用")

    g = int(payload.get("grid", 6))
    # 一次性造出整个网格矩阵，再按族**批量**预测：逐行 predict 的 Python 开销是主要瓶颈
    # （512 点 × 4 族 = 2048 次单样本调用 ≈ 数十秒，批量后降到一次调用）
    steps = np.linspace(0, 1, g)
    combos = np.array(np.meshgrid(steps, steps, steps, indexing="ij")).reshape(3, -1).T
    ranges = [(float(Xtr[:, i].min()), float(Xtr[:, i].max())) for i in idxs]
    M = np.repeat(Xtr.mean(axis=0)[None, :], len(combos), axis=0)
    for k, i in enumerate(idxs):
        lo, hi = ranges[k]
        M[:, i] = lo + combos[:, k] * (hi - lo)

    preds_by_fam = {}
    for fam, m in models.items():
        try:
            preds_by_fam[fam] = np.asarray(m.predict(M)).ravel()
        except Exception:
            continue

    pts = []
    for k in range(len(combos)):
        preds = {}
        for fam, arr in preds_by_fam.items():
            v = arr[k]
            if np.isfinite(v):
                preds[fam] = round(float(v), 4)
        if not preds:
            continue
        vals = list(preds.values())
        pts.append({
            "x": round(float(M[k, idxs[0]]), 4),
            "y": round(float(M[k, idxs[1]]), 4),
            "z": round(float(M[k, idxs[2]]), 4),
            "pred": preds,
            "mean": round(float(np.mean(vals)), 4),
            "spread": round(float(max(vals) - min(vals)), 4),   # 算法分歧度
        })

    result = {
        "system": system, "target": target,
        "axes": [feats[i] for i in idxs],
        "families": list(models.keys()),
        "n_train": int(split), "points": pts,
        "note": "spread = 同一候选在不同算法族下的预测极差；分歧大的区域正是"
                "竞技场必须介入、且不应盲信单点预测的地方。",
    }
    _design_space_cache[cache_key] = result
    return result


# ---------------------------------------------------------------- 极限制造工艺窗口
_process_window_cache: dict = {}


@router.post("/process/window")
def process_window(payload: dict = Body(default={})):
    """极限制造工艺窗口（数字孪生）：在冠军模型上扫 PEB 温度 × PAG 负载，得到分辨率预测面。

    真实光刻中，PEB 温度漂移 ±1℃ 或 PAG 负载 ±0.5% 就可能让线宽越界 —— 这就是"极限制造"。
    这里用可信冠军把这条工艺窗口**数字化**出来，前端画 2D 热力图，直观看到"安全区"有多窄。
    数值来自确定性模型；LLM 不参与。
    """
    system = payload.get("system", "photoresist")
    target = payload.get("target", "resolution_nm")
    fam = (payload.get("family") or "rf")
    grid = int(payload.get("grid", 24))
    cache_key = (system, target, fam, grid, payload.get("mode", "both"))
    cached = _process_window_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        import fsdata
        from fsmodels import build_model
    except Exception as e:                                    # pragma: no cover
        raise HTTPException(500, f"建模依赖不可用: {e}")
    try:
        X, y, meta, feats = fsdata.load_dataset(target, payload.get("mode", "both"), system=system)
    except SystemExit as e:
        raise HTTPException(404, str(e))

    # 先校验工艺特征，缺了就直接失败，不浪费一次建模
    for key in ("cure_temp_c", "pag_loading_pct"):
        if key not in feats:
            raise HTTPException(400, f"缺少特征 {key}（该体系不支持工艺窗口）")
    i_peb = feats.index("cure_temp_c")
    i_pag = feats.index("pag_loading_pct")

    n = len(y)
    split = int(n * 0.8)
    Xtr, ytr = X[:split], y[:split]
    # 只跑一个稳健族作为"窗口模型"（窗口是相对趋势，不需多族）
    try:
        model = build_model(fam, DEFAULT_CFG.get(fam, {}), len(feats)).fit(Xtr, ytr)
    except Exception as e:
        raise HTTPException(500, f"窗口模型训练失败: {e}")

    peb_lo, peb_hi = float(Xtr[:, i_peb].min()), float(Xtr[:, i_peb].max())
    pag_lo, pag_hi = float(Xtr[:, i_pag].min()), float(Xtr[:, i_pag].max())
    sign = -1.0 if target in ("resolution_nm", "ler_nm") else 1.0

    # 整块网格一次性批量预测（逐行 predict 的单次调用开销会让 22×22 网格跑到数十秒）
    peb_vals = np.linspace(peb_lo, peb_hi, grid)
    pag_vals = np.linspace(pag_lo, pag_hi, grid)
    M = np.repeat(Xtr.mean(axis=0)[None, :], grid * grid, axis=0)
    M[:, i_peb] = np.repeat(peb_vals, grid)
    M[:, i_pag] = np.tile(pag_vals, grid)
    vals = np.asarray(model.predict(M)).ravel() * sign

    heat = [{"peb": round(float(a), 2), "pag": round(float(b), 3), "val": round(float(v), 3)}
            for (a, b), v in zip([(peb_vals[i], pag_vals[j])
                                  for i in range(grid) for j in range(grid)], vals)]
    # 「最优」= 带方向后的最大值（分辨率/线宽越小越好 → sign=-1，取 val 最大即原始预测最小）
    k = int(np.argmax(vals))
    best = (round(float(peb_vals[k // grid]), 2), round(float(pag_vals[k % grid]), 3),
            round(float(vals[k]), 3))

    # 真实测得点（golden 段），作为"实测落在窗口何处"的对照
    real = []
    for i in range(split, n):
        real.append({"peb": round(float(X[i, i_peb]), 2),
                     "pag": round(float(X[i, i_pag]), 3),
                     "val": round(float(y[i]) * sign, 3)})

    result = {
        "system": system, "target": target, "family": fam, "sign": sign,
        "peb_range": [peb_lo, peb_hi], "pag_range": [pag_lo, pag_hi],
        "grid": grid, "heat": heat, "real": real, "best": {"peb": best[0], "pag": best[1], "val": best[2]},
        "note": "热力图：越亮=越优（分辨率越低越好，已按方向归一）。窄亮带即极限制造工艺窗口——"
                "PEB 温度 / PAG 负载稍有漂移就会越界。",
    }
    _process_window_cache[cache_key] = result
    return result

